"""Explicit startup-only route and presentation changes with immutable history.

This operation cannot change accepted work, limits, policy or grants. It shares
the startup proof with equivalent capability renewal, but records a distinct
configuration change. Preview performs validation without publishing anything;
neither form starts a worker or mutates GitHub or Paradev.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from .adapters.profile import get_profile
from .campaign_execution import CampaignExecution
from .campaign_headless import capability_for, load_capabilities
from .campaign_jobs import TERMINAL, digest
from .campaign_model import CampaignError, CampaignSpec, spec_from_dict, spec_to_dict
from .campaign_preflight import resolve_roles
from .campaign_requalification import (
    _preserve_manifest,
    _startup_only,
    _stored_jobs_quiescent,
    installation,
)
from .campaign_route_readiness import inspect, permissions_for, selected_capability
from .campaign_store import CampaignStore, RevisionConflict, add_event
from .platform_util import atomic_write_text, is_link_like


def _proposed(current: CampaignSpec, proposed: CampaignSpec, policy) -> CampaignSpec:
    """Accept a source specification without re-importing or changing its graph."""
    if proposed.units and proposed.units != current.units:
        raise CampaignError("startup reconfiguration cannot change accepted units")
    proposed = resolve_roles(dataclasses.replace(proposed, units=current.units), policy)
    current = resolve_roles(current, policy)
    left, right = spec_to_dict(current), spec_to_dict(proposed)
    for value in (left, right):
        value.pop("roles")
        value.pop("role_pools")
        value["reasoning"].pop("capabilities")
        value["reasoning"].pop("summary_routes")
        value["paradev"].pop("group_by")
    if left != right:
        raise CampaignError(
            "startup reconfiguration only changes routes, summary pools and grouping; "
            "scope, policy and limits remain frozen"
        )
    route_fields = {"pool", "profile", "model", "effort", "capacity_command"}
    for name, role in proposed.roles:
        previous = dataclasses.asdict(current.role(name))
        changed = dataclasses.asdict(role)
        if any(previous[key] != value for key, value in changed.items() if key not in route_fields):
            raise CampaignError("startup reconfiguration cannot change role limits")
    return spec_from_dict(spec_to_dict(proposed))


def _profiles(current, proposed, template, project, routes, previous_routes):
    """Freeze all choices, but admit only exact qualified and launchable options."""
    profiles = dict(template.profiles)
    previous = resolve_roles(current, template.policy)
    exclusions = {}

    def profile_for(candidate):
        key = "campaign:" + candidate.profile
        if key not in profiles:
            profile = next((p for p in profiles.values() if p.name == candidate.profile), None)
            profiles[key] = profile or get_profile(candidate.profile, project)
        return profiles[key]

    for role, _ in proposed.roles:
        eligible, reasons = [], []
        for candidate in proposed.candidates(role):
            profile = profile_for(candidate)
            changed = candidate not in previous.candidates(role)
            try:
                permission = permissions_for(role)
                cap = selected_capability(profile, candidate, routes, permission)
                try:
                    previous_cap = capability_for(
                        previous_routes,
                        candidate.profile,
                        candidate.model,
                        "review" if permission == "native-review-pair" else permission,
                        effort=candidate.effort,
                    )
                except CampaignError:
                    previous_cap = None
                if (changed or previous_cap is not None) and cap is None:
                    raise CampaignError("new startup route requires exact native qualification")
                if changed or cap is not None:
                    readiness = inspect(profile, candidate, routes, permission)
                    if not readiness["ready"]:
                        raise CampaignError(readiness["reason"])
                eligible.append(candidate)
            except (CampaignError, OSError, ValueError) as exc:
                reasons.append(f"{candidate.profile}/{candidate.model}/{candidate.effort}: {exc}")
        if not eligible:
            raise CampaignError(f"startup route {role}: {'; '.join(reasons)}")
        if reasons:
            exclusions[role] = reasons
    if proposed.reasoning.summary_routes:
        from .campaign_summary_routes import independent_families, qualified_candidates

        options = {}
        for stage, pool in proposed.reasoning.summary_routes:
            for candidate in dict(proposed.role_pools)[pool]:
                profile_for(candidate)
            qualified, reasons = qualified_candidates(proposed, routes, stage)
            eligible = []
            for candidate in qualified:
                readiness = inspect(profile_for(candidate), candidate, routes, "reasoning")
                if readiness["ready"]:
                    eligible.append(candidate)
                else:
                    reasons.append(f"{candidate.profile}/{candidate.model}: {readiness['reason']}")
            if not eligible:
                raise CampaignError(f"startup summary stage {stage}: {'; '.join(reasons)}")
            options[stage] = tuple(eligible)
            if reasons:
                exclusions["summary:" + stage] = reasons
        if not independent_families(options):
            raise CampaignError("startup summary routes require an independent audit family")
    return profiles, exclusions


def _preserve_spec(path: Path, spec: CampaignSpec) -> None:
    content = json.dumps(spec_to_dict(spec), sort_keys=True) + "\n"
    if is_link_like(path) or (path.exists() and path.read_text(encoding="utf-8") != content):
        raise CampaignError("cannot replace preserved configuration history")
    if not path.exists():
        atomic_write_text(path, content, follow_symlinks=False)


def reconfigure(
    store: CampaignStore,
    campaign_id: str,
    proposed: CampaignSpec,
    *,
    expected_revision: int,
    reason: str,
    preview: bool = False,
):
    """Validate, then commit one stopped startup revision under the coordinator lock."""
    if type(expected_revision) is not int or expected_revision < 0 or not reason.strip():
        raise CampaignError("reconfiguration requires an exact revision and explicit reason")
    before = store.load(campaign_id)
    with store.coordinator(before.common_dir, before.spec.target, campaign_id=campaign_id):
        snapshot = store.load(campaign_id)
        if snapshot.revision != expected_revision:
            raise RevisionConflict(
                f"campaign revision {snapshot.revision}, expected {expected_revision}"
            )
        _startup_only(snapshot, store.directory(campaign_id))
        _stored_jobs_quiescent(snapshot)
        binding = snapshot.bindings.get("execution_template") or {}
        template = CampaignExecution.load(Path(binding.get("path", "")), binding.get("digest", ""))
        if (
            template.campaign_id != snapshot.id
            or template.config_revision != snapshot.config_revision
        ):
            raise CampaignError("campaign configuration binding mismatch")
        proposed = _proposed(snapshot.spec, proposed, template.policy)
        if proposed.paradev.reflect:
            from .campaign_titles import group_key

            projection = dataclasses.replace(snapshot, spec=proposed)
            for unit in snapshot.units.values():
                try:
                    group_key(projection, unit)
                except ValueError as exc:
                    raise CampaignError(f"startup grouping for {unit.id}: {exc}") from exc
        old_routes = snapshot.config_snapshot.get("reasoning_capabilities", {})
        routes = old_routes
        if proposed.reasoning.capabilities:
            manifest = Path(proposed.reasoning.capabilities).expanduser()
            if not manifest.is_absolute():
                manifest = Path(snapshot.root) / manifest
            routes = load_capabilities(manifest)
        profiles, exclusions = _profiles(
            snapshot.spec, proposed, template, Path(snapshot.root), routes, old_routes
        )
        provenance = installation()
        if (
            not provenance.get("qualified")
            or not provenance.get("commit")
            or provenance.get("installation_kind") != "git"
        ):
            raise CampaignError("reconfiguration requires an immutable loaded installation")
        revision = snapshot.config_revision + 1
        changes = {
            "previous_config_revision": snapshot.config_revision,
            "config_revision": revision,
            "previous_group_by": snapshot.spec.paradev.group_by,
            "group_by": proposed.paradev.group_by,
            "previous_spec": spec_to_dict(snapshot.spec),
            "spec": spec_to_dict(proposed),
            "previous_capabilities_digest": digest(old_routes),
            "capabilities_digest": digest(routes),
            "route_exclusions": exclusions,
        }
        if preview:
            return {
                "campaign_id": campaign_id,
                "revision": snapshot.revision,
                "preview": True,
                **changes,
            }
        directory = store.directory(campaign_id) / "configuration-revisions" / str(revision)
        if is_link_like(directory) or is_link_like(directory.parent):
            raise CampaignError("configuration revision directory is symlinked")
        updated = dataclasses.replace(
            template, config_revision=revision, directory=directory, profiles=profiles
        )
        path, template_digest = updated.save()
        previous_manifest = directory / "previous-capabilities.json"
        previous_digest = _preserve_manifest(previous_manifest, old_routes)
        manifest = directory / "capabilities.json"
        manifest_digest = _preserve_manifest(manifest, routes)
        _preserve_spec(directory / "previous-spec.json", snapshot.spec)
        _preserve_spec(directory / "spec.json", proposed)
        previous_grouping = snapshot.spec.paradev.group_by
        if previous_grouping != proposed.paradev.group_by:
            snapshot.bindings["paradev_regroup"] = {
                "previous_config_revision": snapshot.config_revision,
                "config_revision": revision,
                "from_group_by": previous_grouping,
                "to_group_by": proposed.paradev.group_by,
                "previous_groups": {
                    key: unit.bindings.get("reflection", {}).get("workspace", {}).get("group")
                    or unit.bindings.get("native", {}).get("group", "")
                    for key, unit in snapshot.units.items()
                },
            }
        elif isinstance(snapshot.bindings.get("paradev_regroup"), dict):
            snapshot.bindings["paradev_regroup"] = {
                **snapshot.bindings["paradev_regroup"],
                "config_revision": revision,
            }
        snapshot.spec = proposed
        snapshot.config_revision = revision
        snapshot.config_snapshot["configuration_revision"] = revision
        snapshot.config_snapshot["reasoning_capabilities"] = routes
        snapshot.bindings["execution_template"] = {"path": str(path), "digest": template_digest}
        stale = []
        for row in snapshot.jobs.values():
            if row["state"] not in TERMINAL and not row.get("attempt"):
                row["state"] = "stale"
                row["reason"] = "explicit startup reconfiguration changed configuration"
                stale.append(row["id"])
        record = {
            **changes,
            "reason": reason,
            "previous_template": binding,
            "template": snapshot.bindings["execution_template"],
            "previous_capabilities_path": str(previous_manifest),
            "previous_capabilities_sha256": previous_digest,
            "capabilities_path": str(manifest),
            "capabilities_sha256": manifest_digest,
            "installation": provenance,
            "stale_jobs": stale,
        }
        snapshot.bindings.setdefault("configuration_revisions", []).append(record)
        add_event(snapshot, "campaign-reconfigured", **record)
        return store.save(snapshot, expected_revision, expected_generation=snapshot.generation)
