"""Explicit capability renewal for a stopped campaign that never began unit work.

This is not a general mid-delivery configuration upgrade. Fresh native proofs
replace only executable/evidence coordinates; policy, profiles, permissions,
limits and source obligations remain frozen. Prior templates and receipts stay
immutable, and a successful renewal never starts or resumes the campaign.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from .campaign_database import EventJournal
from .campaign_execution import CampaignExecution
from .campaign_headless import load_capabilities
from .campaign_jobs import TERMINAL, digest, validate_record
from .campaign_model import CampaignError, CampaignSnapshot, CampaignUnit, UnitState
from .campaign_provenance import inspect as installation
from .campaign_store import CampaignStore, RevisionConflict, add_event
from .platform_util import atomic_write_text, is_link_like


def _surface(value):
    """Keep the prior route's authority while permitting freshly earned pins."""
    if not isinstance(value, dict):
        raise CampaignError("frozen capability route is malformed")
    if "protocol" in value:
        return {
            key: value.get(key)
            for key in (
                "profile",
                "model",
                "efforts",
                "platform",
                "permissions",
                "protocol",
                "confinement",
                "input_mode",
                "tokenizer",
                "native_limits",
                "served_model_aliases",
                "process_containment",
                "runtime_write_paths",
                "context_tokens",
                "input_tokens",
                "output_tokens",
                "reserved_tokens",
                "max_turns",
                "semantic_qualification",
            )
        }
    return {key: _surface(child) for key, child in value.items()}


def _startup_only(snapshot: CampaignSnapshot) -> None:
    from .campaign_acceptance import ensure

    if snapshot.phase != "stopped":
        raise CampaignError("capability requalification requires a stopped campaign")
    for unit in snapshot.units.values():
        expected = CampaignUnit(unit.spec)
        ensure(expected)
        if (
            unit.state != UnitState.PENDING
            or unit.attempt
            or unit.claim is not None
            or unit.bindings
            or unit.evidence != expected.evidence
            or unit.resume_state is not None
        ):
            raise CampaignError("capability requalification requires units with no prior work")
    if any(action.state not in {"confirmed", "rejected"} for action in snapshot.outbox):
        raise CampaignError("capability requalification requires settled outbound effects")
    for row in snapshot.jobs.values():
        claim = row.get("claim")
        if claim and claim.get("quiescent") is not True:
            raise CampaignError("capability requalification requires quiescent reasoning jobs")
        if row["state"] not in TERMINAL and (
            claim is not None
            or row.get("attempt") != 0
            or row["request"]["required"]
            or row["state"] not in {"proposed", "capacity_wait"}
        ):
            raise CampaignError("capability requalification cannot invalidate an attempted job")


def _stored_jobs_quiescent(snapshot: CampaignSnapshot) -> None:
    """Cold terminal records can still contain uncertain ownership; inspect them."""
    if not isinstance(snapshot.events, EventJournal):
        return  # Legacy JSON already exposes every job to _startup_only.
    after = ""
    while rows := snapshot.events.database.jobs(after=after, limit=100):
        for row in rows:
            validate_record(row)
            claim = row.get("claim")
            if claim and claim.get("quiescent") is not True:
                raise CampaignError("requalification requires quiescent stored reasoning jobs")
        last = rows[-1]["id"]
        if not isinstance(last, str) or last <= after:
            raise CampaignError("stored reasoning inventory did not advance")
        after = last


def _preserve_manifest(path: Path, routes) -> str:
    content = json.dumps({"schema_version": 1, "routes": routes}, sort_keys=True) + "\n"
    if is_link_like(path) or (path.exists() and path.read_text(encoding="utf-8") != content):
        raise CampaignError("cannot replace preserved capability renewal evidence")
    if not path.exists():
        atomic_write_text(path, content, follow_symlinks=False)
    return hashlib.sha256(content.encode()).hexdigest()


def requalify(
    store: CampaignStore,
    campaign_id: str,
    capabilities: Path,
    *,
    expected_revision: int,
    reason: str,
) -> CampaignSnapshot:
    """Renew startup-only native evidence under coordinator lock and revision CAS.

    Refuse any existing unit work, unsettled external effect, uncertain reasoning
    owner or attempted nonterminal job. Never-launched optional jobs become stale
    with their original requests retained. A crash before CAS can leave an inert
    new template directory; it cannot replace the prior authoritative binding.
    """
    if (
        type(expected_revision) is not int
        or expected_revision < 0
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        raise CampaignError("requalification requires an exact revision and explicit reason")
    before = store.load(campaign_id)
    with store.coordinator(before.common_dir, before.spec.target, campaign_id=campaign_id):
        snapshot = store.load(campaign_id)
        if snapshot.revision != expected_revision:
            raise RevisionConflict(
                f"campaign revision {snapshot.revision}, expected {expected_revision}"
            )
        _startup_only(snapshot)
        _stored_jobs_quiescent(snapshot)
        provenance = installation()
        if (
            not provenance.get("qualified")
            or not provenance.get("commit")
            or provenance.get("installation_kind") != "git"
        ):
            raise CampaignError("requalification requires an immutable loaded installation")
        routes = load_capabilities(capabilities)
        previous_routes = snapshot.config_snapshot.get("reasoning_capabilities", {})
        if not routes or _surface(routes) != _surface(previous_routes):
            raise CampaignError("requalification cannot change frozen route authority or limits")
        binding = snapshot.bindings.get("execution_template") or {}
        template = CampaignExecution.load(Path(binding.get("path", "")), binding.get("digest", ""))
        if (
            template.campaign_id != snapshot.id
            or template.config_revision != snapshot.config_revision
        ):
            raise CampaignError("campaign configuration binding mismatch")
        revision = snapshot.config_revision + 1
        directory = store.directory(campaign_id) / "configuration-revisions" / str(revision)
        if is_link_like(directory) or is_link_like(directory.parent):
            raise CampaignError("capability renewal directory is symlinked")
        renewed = dataclasses.replace(template, config_revision=revision, directory=directory)
        path, template_digest = renewed.save()
        manifest = directory / "capabilities.json"
        previous_manifest = directory / "previous-capabilities.json"
        previous_manifest_digest = _preserve_manifest(previous_manifest, previous_routes)
        manifest_digest = _preserve_manifest(manifest, routes)
        previous_revision = snapshot.config_revision
        snapshot.config_revision = revision
        snapshot.config_snapshot["configuration_revision"] = revision
        snapshot.config_snapshot["reasoning_capabilities"] = routes
        snapshot.bindings["execution_template"] = {"path": str(path), "digest": template_digest}
        stale_jobs = []
        for row in snapshot.jobs.values():
            if row["state"] not in TERMINAL:
                row["state"] = "stale"
                row["reason"] = "explicit startup capability requalification changed configuration"
                stale_jobs.append(row["id"])
        record = {
            "reason": reason,
            "previous_config_revision": previous_revision,
            "config_revision": revision,
            "previous_template": binding,
            "template": snapshot.bindings["execution_template"],
            "previous_capabilities_digest": digest(previous_routes),
            "previous_capabilities_path": str(previous_manifest),
            "previous_capabilities_sha256": previous_manifest_digest,
            "capabilities_digest": digest(routes),
            "capabilities_path": str(manifest),
            "capabilities_sha256": manifest_digest,
            "supplied_manifest": str(capabilities.absolute()),
            "installation": provenance,
            "stale_jobs": stale_jobs,
        }
        snapshot.bindings.setdefault("capability_requalifications", []).append(record)
        add_event(snapshot, "campaign-capabilities-requalified", **record)
        return store.save(snapshot, expected_revision, expected_generation=snapshot.generation)
