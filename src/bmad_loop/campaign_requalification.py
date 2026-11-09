"""Explicit capability renewal before a stopped campaign's first worker.

This is not a general mid-delivery configuration upgrade. Fresh native proofs
replace only executable/evidence coordinates; policy, profiles, permissions,
limits and source obligations remain frozen. Prior templates and receipts stay
immutable, and a successful renewal never starts or resumes the campaign.
Confirmed, unpublished deterministic workspace preparation can be preserved;
uncertain allocation or any worker/publication history cannot be upgraded.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import NoReturn

from .campaign_database import EventJournal
from .campaign_execution import CampaignExecution
from .campaign_headless import load_capabilities
from .campaign_jobs import TERMINAL, digest, validate_record
from .campaign_model import CampaignError, CampaignSnapshot, CampaignUnit, UnitState
from .campaign_provenance import inspect as installation
from .campaign_store import CampaignStore, RevisionConflict, add_event
from .platform_util import atomic_write_text, is_link_like


def _semantic_surface(capability, *, validate_proofs=True):
    """Keep declared authority independent of mutable proof-closure availability.

    Hash-bound outer proofs identify current contracts for the exact frozen
    route. Old contracts remain history, but corrupt corpus/review files cannot
    silently drop a declared purpose. Renewed routes must deeply validate every
    such purpose through the normal admission validator.
    """
    from .campaign_fidelity import PURPOSES, contract_header, route_binding, validate
    from .campaign_handoff_flow import AUDIT_SCHEMA
    from .campaign_headless import file_digest, read_json

    binding = capability.get("semantic_qualification")
    if not binding:
        return {}
    if not isinstance(binding, dict) or binding.get("schema_version") != 2:
        raise CampaignError("semantic qualification must be a purpose-bound mapping")
    proofs = binding.get("proofs")
    if proofs is None:
        proofs = [{key: binding.get(key) for key in ("path", "sha256")}]
    if not isinstance(proofs, list) or not 1 <= len(proofs) <= 32:
        raise CampaignError("semantic qualification proof list is invalid")
    declarations = []
    for proof in proofs:
        name = proof.get("path") if isinstance(proof, dict) else None
        if not isinstance(name, str):
            raise CampaignError("semantic qualification proof reference is invalid")
        path = Path(name)
        if file_digest(path) != proof.get("sha256"):
            raise CampaignError("semantic qualification declaration changed")
        declarations.append(read_json(path))
    coverage = {}
    for purpose in sorted(PURPOSES):
        contract = (
            "constraints-v1"
            if purpose.startswith("constraint_")
            else "recovery-v1" if purpose.startswith("recovery_") else ""
        )
        semantic, expected = contract_header(purpose, AUDIT_SCHEMA, contract)
        target = route_binding(capability, purpose)
        declared = any(
            isinstance(evidence.get("route_bindings"), list)
            and target in evidence["route_bindings"]
            and all(evidence.get(key) == value for key, value in expected.items())
            for evidence in declarations
        )
        if not declared:
            continue
        if validate_proofs:
            validate(capability, AUDIT_SCHEMA, purpose=purpose, contract=contract)
        coverage[purpose] = semantic
    if not coverage:
        raise CampaignError("semantic qualification has no valid supported purpose declaration")
    return coverage


def _surface(value, *, validate_semantics=True):
    """Keep the prior route's authority while permitting freshly earned pins."""
    if not isinstance(value, dict):
        raise CampaignError("frozen capability route is malformed")
    if "protocol" in value:
        surface = {
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
            )
        }
        surface["semantic_qualification"] = _semantic_surface(
            value, validate_proofs=validate_semantics
        )
        return surface
    return {
        key: _surface(child, validate_semantics=validate_semantics) for key, child in value.items()
    }


def _bootstrap_tracking(snapshot: CampaignSnapshot) -> set[str]:
    """Prove untouched tracking bootstrap from its ordered durable journal.

    Confirmed creates remain owned remote objects, including receipts not yet
    projected into shared bindings. This does not replay or refresh any effect;
    ordinary hierarchy reconciliation still observes remote changes on resume.
    """
    from .campaign_action_history import actions
    from .campaign_github import managed_marker
    from .campaign_tracking import HierarchyCoordinator
    from .campaign_tracking_delta import merge

    def refuse() -> NoReturn:
        raise CampaignError("startup renewal cannot prove bootstrap tracking history")

    def native_binding(value, kind, *, native=False):
        if not isinstance(value, dict):
            refuse()
        number, identity = value.get("number"), value.get("id" if native else "database_id")
        path = "milestone" if kind == "milestone" else "issues"
        if (
            type(number) is not int
            or number < 1
            or type(identity) is not int
            or identity < 1
            or value.get("state") != "open"
            or value.get("html_url" if native else "url")
            != f"https://github.com/{snapshot.repository}/{path}/{number}"
            or (not native and value.get("kind") != kind)
            or (native and "pull_request" in value)
        ):
            refuse()
        return number, identity

    hierarchy: dict[str, dict] = {}
    proposals = {}
    confirmed = {}
    units = set()
    for action in actions(snapshot, kinds=("hierarchy", "github")):
        unit = snapshot.units.get(action.unit)
        if unit is None or action.state != "confirmed" or action.error:
            refuse()
        if action.kind == "hierarchy":
            data = action.receipt
            if (
                action.payload
                or set(data)
                != {
                    "projection_version",
                    "graph_revision",
                    "binding_changes",
                    "unit_changes",
                    "read_fence",
                    "action_key",
                    "intent",
                    "complete",
                    "reason",
                }
                or type(data["projection_version"]) is not int
                or data["projection_version"] != 1
                or data["graph_revision"] != snapshot.graph_revision
                or data["complete"] is not False
                or data["unit_changes"] != {}
                or data["reason"]
                or not isinstance(data["binding_changes"], dict)
                or not isinstance(data["read_fence"], dict)
            ):
                refuse()
            for key, change in data["binding_changes"].items():
                if not isinstance(change, dict) or set(change) != {"before", "after"}:
                    refuse()
                kind = "milestone" if key.startswith("initiative:") else "issue"
                native_binding(change["after"], kind)
            try:
                projected = merge(hierarchy, data["binding_changes"])
            except CampaignError:
                refuse()
            if projected is None or any(
                not isinstance(fence, dict)
                or any(projected.get(key, {}).get(name) != value for name, value in fence.items())
                for key, fence in data["read_fence"].items()
            ):
                refuse()
            hierarchy = projected
            units.add(unit.id)
            intent = data["intent"]
            if intent is None:
                continue
            namespace = unit.spec.source + ":" + (unit.spec.initiative or snapshot.spec.name)
            keys = {
                "initiative:" + namespace: "milestone",
                "epic:" + namespace + ":" + unit.spec.epic: "issue",
            }
            member = unit
            seen = set()
            while member is not None:
                if member.id in seen:
                    refuse()
                seen.add(member.id)
                keys["story:" + member.id] = "issue"
                member = snapshot.units.get(member.spec.parent) if member.spec.parent else None
            matching = [
                key
                for key in keys
                if HierarchyCoordinator._action(snapshot, key, "create") == data["action_key"]
            ]
            if len(matching) != 1 or not isinstance(intent, dict):
                refuse()
            key = matching[0]
            kind = keys[key]
            if (
                intent.get("action_id") != data["action_key"]
                or intent.get("kind") != kind
                or intent.get("method") != "POST"
                or intent.get("path")
                != f"repos/{snapshot.repository}/{'milestones' if kind == 'milestone' else 'issues'}"
                or intent.get("marker") != managed_marker(snapshot.id, key, kind)
                or intent.get("state") != "prepared"
                or intent.get("receipt") is not None
                or intent.get("coordinates") is not None
                or intent.get("error")
                or intent.get("generation") != action.generation
            ):
                refuse()
            prior = proposals.get(data["action_key"])
            value = (action.unit, key, intent)
            if prior is not None and prior != value:
                refuse()
            proposals[data["action_key"]] = value
        else:
            proposed = proposals.get(action.id)
            if proposed is None:
                refuse()
            owner, key, intent = proposed
            if (
                action.unit != owner
                or action.generation != intent["generation"]
                or action.payload
                != {
                    "intent": intent,
                    "purpose": "hierarchy",
                    "tracking_key": action.id,
                    "scope_revision": snapshot.graph_revision,
                }
                or set(action.receipt) != {"intent"}
            ):
                refuse()
            result = action.receipt["intent"]
            if not isinstance(result, dict):
                refuse()
            native = result.get("receipt")
            if result != {**intent, "state": "confirmed", "receipt": native} or native is None:
                refuse()
            kind = intent["kind"]
            coords = native_binding(native, kind, native=True)
            text = native.get("description" if kind == "milestone" else "body", "")
            if not isinstance(text, str) or intent["marker"] not in text:
                refuse()
            confirmed[action.id] = (key, coords)
    if set(proposals) != set(confirmed):
        refuse()
    if hierarchy != snapshot.bindings.get("github_hierarchy", {}):
        refuse()
    for key, coords in confirmed.values():
        if key in hierarchy and coords != native_binding(hierarchy[key], hierarchy[key]["kind"]):
            refuse()
    if any(snapshot.units[key].bindings.get("hierarchy_ready") is not False for key in units):
        refuse()
    return units


def _prepared_unit(
    snapshot: CampaignSnapshot, unit: CampaignUnit, directory: Path, *, tracking=False
) -> None:
    """Recognize only the deterministic, unpublished prepare-unit result.

    Historical action receipts remain authority after SQLite archival. A current
    checkout alone cannot authorize renewal, nor can a lost allocation ACK.
    """
    from . import verify
    from .campaign_action_history import actions
    from .campaign_runtime_intake import control

    rows = actions(snapshot, kinds=("prepare-unit",), unit=unit.id)
    if len(rows) != 1 or rows[0].state != "confirmed" or rows[0].payload:
        raise CampaignError("renewal requires one confirmed ordinary workspace preparation")
    action = rows[0]
    if any(
        row.kind
        not in (
            {
                "prepare-unit",
                "source-observation",
                "reflection",
                "communication",
                "semantic-plan",
                "reasoning-wait",
                "reasoning-stale",
            }
            | ({"hierarchy", "github"} if tracking else set())
        )
        for row in actions(snapshot, unit=unit.id)
    ):
        raise CampaignError(
            "prepared-workspace renewal refuses publication or further unit effects"
        )
    receipt = action.receipt
    fields = {
        "workspace",
        "branch",
        "base",
        "head",
        "native",
        "effective_source",
        "private_paths",
        "canonical_source",
        "canonical_selector",
        "source_authority_root",
        "initial_source",
        "planning_manifest",
        "effective_sources",
        "spec_path",
    }
    retained = {
        key: value
        for key, value in unit.bindings.items()
        if key
        not in ({"intake_control", "reflection"} | ({"hierarchy_ready"} if tracking else set()))
    }
    if set(receipt) != fields or retained != receipt:
        raise CampaignError("prepared workspace bindings differ from the confirmed receipt")
    if "reflection" in unit.bindings:
        reflected = unit.bindings["reflection"]
        confirmations = [
            row
            for row in actions(snapshot, kinds=("reflection",), unit=unit.id)
            if row.state == "confirmed"
        ]
        native = receipt["native"]
        workspace_projection = reflected.get("workspace") if isinstance(reflected, dict) else None
        if (
            not snapshot.spec.paradev.reflect
            or not confirmations
            or reflected != confirmations[-1].receipt
            or not isinstance(reflected, dict)
            or not isinstance(native, dict)
            or not isinstance(workspace_projection, dict)
            or reflected.get("stage") != {"state": "pending", "windows": []}
            or any(workspace_projection.get(key) != value for key, value in native.items())
            or workspace_projection.get("campaign_id") != snapshot.id
            or workspace_projection.get("phase") != "draft"
            or workspace_projection.get("attempt") != 0
            or workspace_projection.get("pr_url")
        ):
            raise CampaignError(
                "prepared reflection differs from confirmed unstarted workspace projection"
            )
    if any(
        not isinstance(receipt[key], str) or not receipt[key]
        for key in ("workspace", "branch", "base", "head")
    ):
        raise CampaignError("prepared workspace receipt lacks exact Git coordinates")
    intake = control(unit)
    if "intake_control" in unit.bindings:
        matching = [
            row
            for row in actions(snapshot, kinds=("intake-control",))
            if row.state == "confirmed"
            and row.payload.get("packet_id") == (intake or {}).get("packet_id")
        ]
        if len(matching) != 1:
            raise CampaignError("prepared intake unit lacks confirmed control authority")
        payload = matching[0].payload
        expected = {
            "schema_version": 1,
            "input_versions": payload.get("input_versions"),
            "state": "triage_pending",
            "optional": True,
            "packet_id": payload.get("packet_id"),
        }
        from .campaign_intake_flow import INBOX

        records = snapshot.bindings.get(INBOX, {}).get("records", {})
        if (
            intake != expected
            or unit.spec.kind != "planning"
            or unit.id
            != "campaign:" + snapshot.id + ":intake:" + str(payload.get("packet_id"))[:16]
            or not expected["input_versions"]
            or not isinstance(expected["input_versions"], list)
            or any(not isinstance(version, str) for version in expected["input_versions"])
            or any(
                records.get(version, {}).get("control_unit") != unit.id
                or records.get(version, {}).get("state") != "reserved"
                or records.get(version, {}).get("attempts") != 0
                for version in expected["input_versions"]
            )
        ):
            raise CampaignError("prepared intake control has begun work or changed identity")
    workspace = Path(receipt["workspace"])
    if (
        not workspace.is_absolute()
        or not verify.worktree_is_registered(Path(snapshot.root), workspace)
        or verify.rev_parse_head(workspace) != receipt["head"]
        or verify.current_branch(workspace) != receipt["branch"]
    ):
        raise CampaignError("prepared workspace Git identity or bootstrap head changed")
    dirty = verify.git_bytes(workspace, "status", "--porcelain", "-z", "--untracked-files=all")
    if dirty.returncode or dirty.stdout:
        raise CampaignError("prepared workspace must retain its clean bootstrap checkout")
    native = receipt["native"]
    if snapshot.spec.paradev.reflect:
        from .adapters.paradev_runtime import read_private_json
        from .campaign_paradev import _git_path

        allocation = (
            directory
            / "reflection"
            / "allocations"
            / (hashlib.sha256(action.id.encode()).hexdigest() + ".json")
        )
        if (
            is_link_like(allocation)
            or not allocation.is_file()
            or allocation.stat().st_size > 1024 * 1024
        ):
            raise CampaignError("prepared native allocation evidence is unavailable")
        observed = read_private_json(allocation)
        if (
            not isinstance(native, dict)
            or observed.get("state") != "confirmed"
            or observed.get("binding") != native
            or native.get("unit_id") != unit.id
            or native.get("action_id") != action.id
            or native.get("project_id") != snapshot.spec.paradev.project_id
            or native.get("path") != str(workspace)
            or native.get("branch") != receipt["branch"]
            or native.get("common_dir") != snapshot.common_dir
            or native.get("common_dir") != str(_git_path(workspace, "--git-common-dir"))
            or native.get("git_dir") != str(_git_path(workspace, "--absolute-git-dir"))
        ):
            raise CampaignError("prepared native allocation differs from its pinned identity")
    elif native:
        raise CampaignError("unexpected native allocation in an unreflected campaign")


def _startup_only(snapshot: CampaignSnapshot, directory: Path) -> bool:
    from .campaign_acceptance import ensure

    if snapshot.phase != "stopped":
        raise CampaignError("capability requalification requires a stopped campaign")
    prepared = False
    if any(action.state not in {"confirmed", "rejected"} for action in snapshot.outbox):
        raise CampaignError("capability requalification requires settled outbound effects")
    tracking = (
        _bootstrap_tracking(snapshot)
        if any(unit.state == UnitState.DRAFT for unit in snapshot.units.values())
        else set()
    )
    for unit in snapshot.units.values():
        expected = CampaignUnit(unit.spec)
        ensure(expected)
        if (
            unit.state not in {UnitState.PENDING, UnitState.DRAFT}
            or unit.attempt
            or unit.claim is not None
            or unit.evidence != expected.evidence
            or unit.resume_state is not None
            or unit.failure_attempts
            or unit.resolution_attempts
            or unit.review_attempts
            or unit.legacy_outcome
        ):
            raise CampaignError("capability requalification requires units with no prior work")
        if unit.state == UnitState.DRAFT:
            _prepared_unit(snapshot, unit, directory, tracking=unit.id in tracking)
            prepared = True
        elif unit.bindings:
            raise CampaignError("capability requalification requires units with no prior work")
    if prepared:
        from .campaign_action_history import actions

        if actions(snapshot, kinds=("launch", "reasoning-launch")):
            raise CampaignError("prepared-workspace renewal refuses any prior worker launch intent")
        workers = directory / "workers"
        if is_link_like(workers) or (
            workers.exists() and (not workers.is_dir() or any(workers.iterdir()))
        ):
            raise CampaignError("prepared-workspace renewal refuses existing worker artifacts")
    for row in snapshot.jobs.values():
        claim = row.get("claim")
        if prepared and (claim is not None or row.get("attempt") != 0):
            raise CampaignError("prepared-workspace renewal refuses prior native job work")
        if claim and claim.get("quiescent") is not True:
            raise CampaignError("capability requalification requires quiescent reasoning jobs")
        if row["state"] not in TERMINAL and (
            claim is not None
            or row.get("attempt") != 0
            or row["request"]["required"]
            or row["state"] not in {"proposed", "capacity_wait"}
        ):
            raise CampaignError("capability requalification cannot invalidate an attempted job")
    return prepared


def _stored_jobs_quiescent(snapshot: CampaignSnapshot, *, never_attempted=False) -> None:
    """Cold terminal records can still contain uncertain ownership; inspect them."""
    if not isinstance(snapshot.events, EventJournal):
        return  # Legacy JSON already exposes every job to _startup_only.
    after = ""
    while rows := snapshot.events.database.jobs(after=after, limit=100):
        for row in rows:
            validate_record(row)
            claim = row.get("claim")
            if never_attempted and (claim is not None or row.get("attempt") != 0):
                raise CampaignError("prepared-workspace renewal refuses stored native job work")
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

    Refuse unit work beyond confirmed preparation, unsettled external effects, uncertain reasoning
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
        prepared = _startup_only(snapshot, store.directory(campaign_id))
        _stored_jobs_quiescent(snapshot, never_attempted=prepared)
        provenance = installation()
        if (
            not provenance.get("qualified")
            or not provenance.get("commit")
            or provenance.get("installation_kind") != "git"
        ):
            raise CampaignError("requalification requires an immutable loaded installation")
        routes = load_capabilities(capabilities)
        previous_routes = snapshot.config_snapshot.get("reasoning_capabilities", {})
        if not routes or _surface(routes) != _surface(previous_routes, validate_semantics=False):
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
