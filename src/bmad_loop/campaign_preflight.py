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

from . import platform_util as fs
from . import policy as policy_mod
from . import runsetup
from .adapters.profile import get_profile
from .campaign_capacity import observe
from .campaign_github import GhTransport, GithubGateway
from .campaign_model import CampaignError, CampaignSpec, RoleSpec
from .campaign_publication import read_rules
from .campaign_scheduler import route_unavailable
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
    required = set(ROLES) | set(roles)
    if "execution_review" in roles:
        required.add("execution")
    if spec.reasoning.enabled:
        from .campaign_reasoning_plan import ROLES as semantic_roles

        required.update(
            semantic_roles[purpose]
            for purpose in spec.reasoning.purposes
            if purpose in semantic_roles
        )
        if any(purpose.startswith("handoff_") for purpose in spec.reasoning.purposes):
            required.update(("execution", "summarization", "handoff_audit"))
    for role in sorted(required):
        selected = roles.get(role, RoleSpec())
        if selected.pool:
            continue
        inherited = policy.adapter.resolved(
            "review"
            if role in {"publication_review", "execution_review"}
            else "dev" if role == "execution" else "triage"
        )
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
    if not fs.HANDLE_ANCHORED_WRITES:
        for area in ("planning", "execution", "publication"):
            problem(
                area,
                "campaign sessions require platform support for handle-anchored candidate/source reads",
            )
    from .campaign_source_freshness import document
    from .campaign_source_freshness import inspect as inspect_source_freshness

    try:
        fresh = inspect_source_freshness(project, spec)
        capabilities["execution"]["observations"]["source_freshness"] = document(fresh)
        if not fresh.ready:
            for area in ("planning", "execution", "publication"):
                problem(area, fresh.reason)
        else:
            graph = import_sources(project, spec)
            capabilities["execution"]["observations"]["unit_count"] = len(graph.units)
    except (ValueError, OSError) as exc:
        for area in ("planning", "execution", "publication"):
            problem(area, "Native source freshness/import failed: " + str(exc))
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
    for role in sorted(set(ROLES) | set(dict(spec.roles))):
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
                        "eligible": installed and not route_unavailable(route),
                    }
                )
            except (ValueError, OSError) as exc:
                choices.append({"profile": profile_name, "eligible": False, "reason": str(exc)})
        area = "execution" if role in {"execution", "execution_review"} else "planning"
        capabilities[area]["observations"][role] = choices
        if not any(choice["eligible"] for choice in choices):
            problem(area, f"{role}: no available authorized model route")
    if spec.reasoning.enabled:
        from .campaign_headless import capability_for, load_capabilities, validate_capability
        from .campaign_reasoning_plan import ROLES as semantic_roles

        try:
            if not spec.reasoning.capabilities:
                raise CampaignError("reasoning requires a qualified capabilities manifest")
            path = Path(spec.reasoning.capabilities).expanduser()
            routes = load_capabilities(path if path.is_absolute() else project / path)
            modes = {
                (semantic_roles[purpose], "no-tools")
                for purpose in spec.reasoning.purposes
                if purpose in semantic_roles
            }
            if any(purpose.startswith("handoff_") for purpose in spec.reasoning.purposes):
                modes.update(
                    {
                        ("summarization", "no-tools"),
                        ("handoff_audit", "no-tools"),
                        ("execution", "workspace-tools"),
                    }
                )
            observations = {}
            for role, mode in sorted(modes):
                qualified = []
                reasons = []
                for candidate in spec.candidates(role):
                    try:
                        cap = capability_for(routes, candidate.profile, candidate.model, mode)
                        validate_capability(cap, candidate, permissions=mode)
                        qualified.append(candidate.profile + ":" + candidate.model)
                    except (CampaignError, OSError, ValueError) as exc:
                        reasons.append(str(exc))
                observations[role] = {
                    "qualified": qualified,
                    "permissions": mode,
                    "problems": reasons,
                }
                if not qualified:
                    problem("planning", f"{role}: no qualified {mode} route for reasoning/handoff")
            capabilities["planning"]["observations"]["reasoning"] = observations
        except (CampaignError, OSError, ValueError) as exc:
            problem("planning", str(exc))
    if spec.planning.enabled or spec.intake.allow_new_units:
        from .campaign_planning_skill import resolve as resolve_planning_skill

        for stage in spec.planning.stages:
            try:
                resolved = resolve_planning_skill(project, spec.planning, stage)
                capabilities["planning"]["observations"].setdefault("native_skills", {})[
                    stage
                ] = resolved
            except (CampaignError, OSError, ValueError) as exc:
                problem("planning", str(exc))
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
                if spec.github.merge_mode == "project-command" and not spec.github.merge_executor:
                    problem("publication", "project-command requires a maintained merge executor")
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
            from . import runs
            from .adapters.paradev_backend import ParadevMultiplexer
            from .campaign_lifecycle import CampaignLifecycle, LifecyclePolicy

            reflection_root = runs.state_root() / "campaign-reflection-preflight"
            if reflection_provider is None:
                from .campaign_paradev import ParadevWorkspaceProvider

                reflection_provider = ParadevWorkspaceProvider(
                    project_id=spec.paradev.project_id,
                    allowed_root=Path(root),
                    repository=project,
                    state_dir=reflection_root,
                )
            capabilities["reflection"]["observations"] = reflection_provider.preflight()
            mux = ParadevMultiplexer(
                host=getattr(reflection_provider, "host", None),
                state_dir=reflection_root / "native-transport",
            )
            transport_available = mux.available()
            capabilities["reflection"]["observations"]["transport_available"] = transport_available
            if not transport_available:
                problem(
                    "reflection",
                    "Bundled Paradev transport or its optional dependency is unavailable",
                )
            lifecycle = CampaignLifecycle(
                reflection_root / "lifecycle", mux=mux, provider=reflection_provider
            ).preflight(LifecyclePolicy(retention=spec.paradev.retention))
            capabilities["reflection"]["observations"]["lifecycle"] = lifecycle
            if not lifecycle["qualified"]:
                problem("reflection", lifecycle["detail"])
        except Exception as exc:
            problem("reflection", f"Paradev capability read failed: {type(exc).__name__}: {exc}")
    if spec.communication.mode == "observe":
        from .campaign_communication import HcomObserver

        try:
            communication = HcomObserver(
                room=Path(spec.communication.room), observer_name="bmad"
            ).preflight()
        except (RuntimeError, ValueError, OSError) as exc:
            communication = {
                "available": False,
                "mode": "observe",
                "sends": False,
                "subscriptions": False,
                "automatic_peer_delivery": False,
                "incoming_hooks_muted": False,
                "detail": "hcom observation unavailable: " + str(exc),
            }
        capabilities["reflection"]["observations"]["communication"] = communication
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
    ready = all(value["ready"] for value in capabilities.values())
    can_start = ready or (
        spec.github.allow_publication_wait
        and spec.github.enabled
        and not capabilities["publication"]["ready"]
        and all(value["ready"] for key, value in capabilities.items() if key != "publication")
    )
    return {
        "schema_version": 1,
        "ok": ready,
        "can_start": can_start,
        "repository": repository,
        "common_dir": str(common),
        "root": str(project),
        "remote_url": remote_url,
        "capabilities": capabilities,
        "config_digest": runsetup.config_digest(pol, project, profiles=base_profiles),
    }
