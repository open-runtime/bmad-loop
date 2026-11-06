"""Read-only campaign capability checks and immutable launch configuration.

Preflight never allocates workspaces, starts agents or changes GitHub rules. Each
capability has a separate verdict so a successful import cannot imply delivery
readiness when required publication or native attribution is unavailable.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
from pathlib import Path
from typing import Any

from . import policy as policy_mod
from . import runsetup
from .adapters.profile import get_profile
from .campaign_capacity import observe
from .campaign_github import GhTransport, GithubGateway
from .campaign_model import CampaignSpec, RoleSpec
from .campaign_publication import read_rules
from .campaign_sources import import_sources
from .campaign_workers import common_directory, git

ROLES = (
    "planning",
    "planning_review",
    "triage",
    "escalation",
    "decision_review",
    "conflict_repair",
    "publication_review",
)


def resolve_roles(spec: CampaignSpec, policy) -> CampaignSpec:
    roles = dict(spec.roles)
    for role in ROLES:
        selected = roles.get(role, RoleSpec())
        if selected.pool:
            continue
        inherited = policy.adapter.resolved("review" if role == "publication_review" else "triage")
        profile = selected.profile or inherited.name
        model = selected.model or (inherited.model if profile == inherited.name else "")
        effort = selected.effort or (inherited.effort if profile == inherited.name else "")
        roles[role] = dataclasses.replace(selected, profile=profile, model=model, effort=effort)
    return dataclasses.replace(spec, roles=tuple(roles.items()))


def repository_name(remote_url: str) -> str:
    match = re.fullmatch(
        r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?",
        remote_url,
    )
    if not match:
        raise ValueError("GitHub repository must be explicit for a non-GitHub remote")
    return match.group(1)


def inspect(
    project: Path,
    spec: CampaignSpec,
    *,
    gateway: GithubGateway | None = None,
    capacity_observer=observe,
    reflection_provider=None,
    native_observer=None,
) -> dict[str, Any]:
    capabilities = {
        key: {"ready": True, "problems": [], "observations": {}}
        for key in ("planning", "execution", "publication", "reflection")
    }

    def problem(area: str, text: str) -> None:
        capabilities[area]["ready"] = False
        capabilities[area]["problems"].append(text)

    common = common_directory(project)
    remote_url = git(project, "remote", "get-url", spec.remote)
    repository = spec.github.repository or repository_name(remote_url)
    pol = policy_mod.load(project / policy_mod.POLICY_FILE)
    spec = resolve_roles(spec, pol)
    base_profiles = runsetup.resolve_profiles(pol, project)
    # CLI usage receipts prove token counts/model identity, not actual billed
    # currency. Account quota remaining is not campaign-spend accounting.
    if spec.resources.budget_usd is not None:
        for area in ("planning", "execution"):
            problem(
                area,
                "USD campaign budget requires qualified actual-cost receipts; current session usage does not supply billed cost",
            )
    for role, configured in spec.roles:
        if configured.budget_usd is not None:
            area = (
                "publication"
                if role == "publication_review"
                else "execution" if role in {"conflict_repair", "escalation"} else "planning"
            )
            problem(
                area,
                f"{role}: USD role budget requires qualified actual-cost receipts; quota telemetry cannot prove campaign spend",
            )
    root = spec.paradev.worktree_root if spec.paradev.reflect else spec.worktree_root
    if not root or not Path(root).expanduser().is_absolute():
        problem("execution", "configure an explicit absolute project worktree root")
    # A project publisher/scheduler must be drained and removed from child
    # ownership. Skipping advisory post_story alone does not neutralize probes.
    conflict_names = ("push-merge", "sync-with-main", "scheduler", "factory-publisher")
    enabled = tuple(pol.plugins.enabled)
    for name in enabled:
        if any(token in name for token in conflict_names):
            problem(
                "execution",
                f"legacy publication plugin must be migrated before campaign activation: {name}",
            )
    for command in (*pol.verify.commands, *pol.environment.probes):
        if any(token in command for token in conflict_names):
            problem(
                "execution",
                "legacy publication/scheduler command conflicts with campaign ownership",
            )
    graph = import_sources(project, spec)
    capabilities["execution"]["observations"]["unit_count"] = len(graph.units)
    capabilities["execution"]["observations"]["limits"] = {
        "execution_workers": spec.max_workers,
        "active_epics": spec.max_active_epics,
        "in_flight_units": spec.max_in_flight_units,
        "review_sessions": spec.max_review_sessions,
        "verification_slots": spec.resources.verify_slots,
        "additional_active_queue_views": (
            spec.resources.verify_slots if spec.github.merge_mode == "queue" else 0
        ),
        "historical_queue_views_retained": True,
    }
    for role in ROLES:
        if role in {"planning", "planning_review"} and not spec.planning.enabled:
            continue
        choices = []
        for candidate in spec.candidates(role):
            profile_name = (
                candidate.profile
                or pol.adapter.resolved("review" if role == "publication_review" else "triage").name
            )
            resolved = dataclasses.replace(
                candidate,
                profile=profile_name,
                model=candidate.model or pol.adapter.resolved("triage").model,
            )
            try:
                profile = get_profile(profile_name, project)
                route = capacity_observer(resolved, project)
                installed = bool(shutil.which(profile.binary) or Path(profile.binary).is_file())
                choices.append(
                    {
                        "profile": profile_name,
                        "model": resolved.model,
                        "effort": resolved.effort,
                        "binary": profile.binary,
                        "installed": installed,
                        "capacity": dataclasses.asdict(route),
                        "eligible": installed
                        and route.available
                        and route.model_access
                        and route.known
                        and route.slots > route.active,
                    }
                )
            except (ValueError, OSError) as exc:
                choices.append({"profile": profile_name, "eligible": False, "reason": str(exc)})
        capabilities["planning"]["observations"][role] = choices
        if not any(choice["eligible"] for choice in choices):
            problem("planning", f"{role}: no available authorized model route")
    if spec.planning.enabled:
        skills = dict(spec.planning.skills)
        for stage in spec.planning.stages:
            if stage == "planning_review":
                continue
            skill = skills.get(stage)
            if not skill or not any(
                (project / folder / skill / "SKILL.md").is_file()
                for folder in (".agents/skills", ".claude/skills")
            ):
                problem("planning", f"{stage}: qualify an installed BMAD skill mapping")
    if spec.github.enabled:
        gateway = gateway or GithubGateway(GhTransport(), repository)
        try:
            rules = read_rules(gateway, spec.target)
            capabilities["publication"]["observations"]["rules"] = dataclasses.asdict(rules)
            if not rules.complete:
                problem("publication", "effective target rules could not be read completely")
            if not rules.merge_commits_allowed:
                problem("publication", "target rules forbid history-preserving merge commits")
            if spec.github.merge_mode in {"direct-strict", "project-command"}:
                if not rules.strict or not rules.checks:
                    problem(
                        "publication",
                        "direct-strict requires server-enforced up-to-date required checks",
                    )
                if rules.queue:
                    problem(
                        "publication",
                        "target requires a merge queue; direct publication is unavailable",
                    )
                if spec.github.merge_mode == "project-command" and not spec.github.merge_command:
                    problem("publication", "project-command requires a maintained publication gate")
            if spec.github.merge_mode == "queue":
                from .campaign_queue import capability

                if rules.queue_merge_method != "MERGE":
                    problem("publication", "merge queue must preserve every commit with MERGE")
                if not rules.queue or not spec.github.candidate_command:
                    problem(
                        "publication",
                        "queue mode requires an active merge queue and qualified merge-group verification command",
                    )
                context = spec.github.candidate_check_context
                if not context or context not in {check.context for check in rules.checks}:
                    problem(
                        "publication",
                        "queue mode requires a configured required combined-candidate evidence check",
                    )
                producer = capability(spec.github, repository, project, rules)
                capabilities["publication"]["observations"]["queue_producer"] = dataclasses.asdict(
                    producer
                )
                if not producer.ready:
                    problem("publication", producer.reason)
            if spec.github.merge_command:
                capabilities["publication"]["observations"]["merge_command"] = list(
                    spec.github.merge_command
                )
        except Exception as exc:
            problem("publication", f"GitHub capability read failed: {type(exc).__name__}: {exc}")
    else:
        problem(
            "publication", "GitHub delivery is disabled; local completion cannot satisfy delivery"
        )
    if spec.paradev.reflect:
        try:
            if reflection_provider is None:
                from . import runs
                from .campaign_paradev import ParadevWorkspaceProvider

                reflection_provider = ParadevWorkspaceProvider(
                    project_id=spec.paradev.project_id,
                    allowed_root=Path(root),
                    repository=project,
                    state_dir=runs.state_root() / "campaign-reflection-preflight",
                )
            capabilities["reflection"]["observations"] = reflection_provider.preflight()
        except Exception as exc:
            problem("reflection", f"Paradev capability read failed: {type(exc).__name__}: {exc}")
    if spec.paradev.project_id:
        try:
            from .campaign_native_inventory import NativeServiceObserver

            observer = native_observer or NativeServiceObserver(
                spec.paradev.project_id, host=getattr(reflection_provider, "host", None)
            )
            native = observer(project, common)
            capabilities["execution"]["observations"]["native_ownership"] = dataclasses.asdict(
                native
            )
            capabilities["execution"]["observations"][
                "native_migration_capabilities"
            ] = NativeServiceObserver.capabilities()
            if not native.complete or native.faults:
                problem("execution", "Native project ownership inventory is incomplete")
        except Exception as exc:
            problem(
                "execution", "Native project ownership inventory unavailable: " + type(exc).__name__
            )
    return {
        "schema_version": 1,
        "ok": all(value["ready"] for value in capabilities.values()),
        "repository": repository,
        "common_dir": str(common),
        "root": str(project),
        "remote_url": remote_url,
        "capabilities": capabilities,
        "config_digest": runsetup.config_digest(pol, project, profiles=base_profiles),
    }
