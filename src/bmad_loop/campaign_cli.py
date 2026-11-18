"""Campaign CLI and read-only projections; runtime imports remain lazy.

Offline validation never opens the target repository or configures a multiplexer.
Commands use the machine envelope contract and established exit codes. Controls
append requests to the inbox; only the coordinator applies state transitions.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import heapq
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import machine
from .campaign_model import CampaignError, CampaignSnapshot, CampaignSpec, parse_spec, spec_to_dict

SCHEMA_VERSION = 1


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "campaign", help="plan, run and deliver parallel work through draft PRs"
    )
    parser.add_argument("--project", default=".", help="target project root (default: cwd)")
    commands = parser.add_subparsers(dest="campaign_command", required=True)
    for command, help_text in (
        ("validate", "validate a campaign specification offline"),
        ("preflight", "read planning, execution, publication and reflection capabilities"),
        ("qualify-headless", "explicitly run a bounded native no-tools canary"),
        ("qualify-writer", "explicitly qualify a scoped native writer in a disposable repository"),
        ("trial-handoff", "explicitly qualify sealed history through native summary jobs"),
        ("migration-inventory", "inspect current work and cache an exact migration review receipt"),
        (
            "migration-qualify",
            "qualify an owner migration manifest against fresh service observations",
        ),
        ("run", "create and supervise a campaign in the foreground"),
        ("resume", "reconcile and supervise an existing campaign"),
        ("list", "list repository campaigns"),
        ("status", "show campaign units, holds and links"),
        ("inbox", "read actionable holds, uncertain effects and route waits"),
        ("history", "read bounded campaign events and retained role history"),
        ("jobs", "read bounded reasoning job state and lifecycle receipts"),
        ("messages", "read durable message delivery and application status"),
        ("endpoints", "read paged managed communication addresses and delivery state"),
        ("steer", "queue advisory instructions for the next safe stage boundary"),
        ("handoff", "queue continuation through approved harness candidates"),
        ("migrate-state", "migrate quiescent campaign storage with a preserved backup"),
        ("reconfigure", "change qualified startup routes and grouping while retaining history"),
        (
            "requalify",
            "renew installed route proofs for a stopped campaign before its first unit worker",
        ),
        ("stop", "close admissions and drain existing bounded stages"),
        ("hold", "hold one unit at a safe stage boundary"),
        ("release", "release an explicit operator hold"),
        ("authorize-scope", "authorize exact acceptance reductions in a delivered revised plan"),
        ("attach", "attach the existing owned unit terminal"),
        ("reconcile", "refresh an existing campaign without starting workers"),
        ("reflect", "refresh existing native display labels in a stopped campaign"),
        ("adoption-proposal", "inspect exact existing PR and service handoff coordinates"),
        ("adopt", "queue an owner-authorized, independently fenced existing PR handoff"),
        (
            "transfer",
            "retire a stopped startup campaign and transfer a preserved PR to a prepared successor",
        ),
        ("provenance", "verify the currently loaded installation and exact fork commit"),
    ):
        child = commands.add_parser(command, help=help_text)
        child.add_argument("--project", default=argparse.SUPPRESS, help="target project root")
        child.set_defaults(func=dispatch)
        if command == "trial-handoff":
            child.add_argument(
                "snapshots", metavar="SNAPSHOTS", help="sealed native history manifest"
            )
            child.add_argument("--capability", required=True, help="earned native route capability")
            child.add_argument(
                "--reducer-capability",
                help="separately earned reducer route; defaults to the map route",
            )
            child.add_argument(
                "--directory", required=True, help="private external evidence directory"
            )
            child.add_argument("--parallel", type=int, default=4)
            child.add_argument("--timeout", type=int, default=600)
            child.add_argument("--prepare-only", action="store_true")
            child.add_argument(
                "--drain",
                action="store_true",
                help="stop new trial admissions while owned jobs finish",
            )
            child.add_argument(
                "--reuse", help="reuse validated map coverage from an explicit prior trial"
            )
        elif command in {"qualify-headless", "qualify-writer"}:
            child.add_argument("proposal", metavar="PROPOSAL", help="unqualified native route JSON")
            child.add_argument(
                "--directory", required=True, help="private external evidence directory"
            )
            child.add_argument("--input-bytes", type=int, default=16384)
            child.add_argument("--output-tokens", type=int, default=4096)
            child.add_argument("--timeout", type=int, default=180)
        elif command in {
            "validate",
            "preflight",
            "run",
            "migration-inventory",
            "migration-qualify",
        }:
            child.add_argument("spec", metavar="SPEC", help="version 1 campaign TOML")
            if command == "migration-qualify":
                child.add_argument(
                    "manifest", metavar="MANIFEST", help="owner-selected migration JSON"
                )
        elif command not in {"list", "provenance"}:
            child.add_argument("campaign_id", metavar="ID")
        if command == "provenance":
            child.add_argument("--expected-commit", default="", metavar="SHA")
        if command in {
            "hold",
            "release",
            "attach",
            "adopt",
            "adoption-proposal",
            "handoff",
            "authorize-scope",
        }:
            child.add_argument("--unit", required=True, metavar="UNIT")
        if command in {"adopt", "adoption-proposal"}:
            child.add_argument(
                "--observer",
                default="native",
                metavar="NAME",
                help="installed trusted handoff observer (default: native)",
            )
        if command == "adopt":
            child.add_argument("--manifest", required=True, metavar="FILE")
        if command == "adoption-proposal":
            child.add_argument("--pr", required=True, type=int, metavar="NUMBER")
        if command == "transfer":
            child.add_argument("--unit", required=True)
            child.add_argument("--successor", required=True)
            child.add_argument("--successor-unit", required=True)
            child.add_argument("--pr", required=True, type=int)
            child.add_argument("--expected-revision", required=True, type=int)
            child.add_argument("--expected-successor-revision", required=True, type=int)
            child.add_argument("--reason", required=True)
            child.add_argument("--preview", action="store_true")
        if command == "hold":
            child.add_argument("--reason", required=True)
        if command == "steer":
            child.add_argument("--unit", default="", metavar="UNIT")
            child.add_argument("--text", required=True)
            child.add_argument("--job", default="")
            child.add_argument("--request-id", default="")
        if command == "reflect":
            child.add_argument("--unit", default="", metavar="UNIT")
            child.add_argument("--preview", action="store_true")
            child.add_argument("--expected-revision", type=int)
        if command == "authorize-scope":
            child.add_argument("--expected-revision", required=True, type=int)
            child.add_argument("--reason", required=True)
        if command == "requalify":
            child.add_argument("--capabilities", required=True, metavar="FILE")
            child.add_argument("--expected-revision", required=True, type=int)
            child.add_argument("--reason", required=True)
        if command == "reconfigure":
            child.add_argument("--spec", required=True, metavar="FILE")
            child.add_argument(
                "--review-policy-source",
                default="",
                metavar="COMMIT:PATH",
                help="explicitly adopt a committed owner publication-family policy before any unit starts",
            )
            child.add_argument("--expected-revision", required=True, type=int)
            child.add_argument("--reason", required=True)
            child.add_argument("--preview", action="store_true")
        if command == "stop":
            child.add_argument(
                "--terminate-workers",
                action="store_true",
                help="request owned worker termination; unresolved ownership remains reserved",
            )
        if command == "attach":
            child.add_argument("--role", default="execution")
        else:
            machine.add_json_flag(child, "versioned campaign result")
        if command == "history":
            child.add_argument("--unit", default="", metavar="UNIT")
            child.add_argument("--role", default="", metavar="ROLE")
            child.add_argument("--after", type=int, default=0)
            child.add_argument("--limit", type=int, default=100)
            history_kind = child.add_mutually_exclusive_group()
            history_kind.add_argument(
                "--observations", action="store_true", help="read archived superseded observations"
            )
            history_kind.add_argument(
                "--actions", action="store_true", help="read archived terminal effect receipts"
            )
        if command == "jobs":
            child.add_argument("--unit", default="", metavar="UNIT")
            child.add_argument("--job", default="", metavar="JOB")
            child.add_argument("--after", default="")
            child.add_argument("--limit", type=int, default=100)
            child.add_argument("--details", action="store_true")
        if command == "inbox":
            child.add_argument("--unit", default="", metavar="UNIT")
            child.add_argument("--after", default="", metavar="ITEM_ID")
            child.add_argument("--limit", type=int, default=100)
        if command == "messages":
            child.add_argument("--unit", default="", metavar="UNIT")
            child.add_argument("--job", default="", metavar="JOB")
            child.add_argument("--after", type=int, default=0)
            child.add_argument("--limit", type=int, default=100)
        if command == "endpoints":
            child.add_argument("--after", type=int, default=0)
            child.add_argument("--limit", type=int, default=100)
            child.add_argument("--state", default="")
        if command == "handoff":
            child.add_argument("--profile", default="")
            child.add_argument("--model", default="")
            child.add_argument("--effort", default="")
            child.add_argument("--reason", default="operator requested continuation")
        if command == "run":
            child.add_argument("--paradev-reflect", action="store_true", default=None)
            child.add_argument("--paradev-project", metavar="PROJECT_ID")
            child.add_argument(
                "--prepare-only",
                action="store_true",
                help="persist a stopped campaign without allocating workspaces or launching sessions",
            )


def _result(command: str, *, ok: bool = True, **fields: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "command": f"campaign.{command}", "ok": ok, **fields}


def _job_document(row, *, details=False, launch_wait=None):
    from .sanitize import scrub_json

    result = {
        key: row.get(key)
        for key in (
            "id",
            "state",
            "attempt",
            "validation",
            "application",
            "reason",
            "retry_at",
            "usage",
            "updated_at",
        )
    }
    request = row.get("request") or {}
    result.update(
        {
            key: request.get(key)
            for key in ("purpose", "role", "unit", "dependencies", "required", "limits")
        }
    )
    selection = row.get("selection") or {}
    result["selection"] = {
        key: selection.get(key)
        for key in ("profile", "model", "effort", "provider_family")
        if key in selection
    }
    claim, receipt = row.get("claim") or {}, row.get("receipt") or {}
    presentation = claim.get("presentation") or {}
    native = receipt.get("native_terminal") or {}
    result["terminal"] = {
        "workspace_id": presentation.get("workspace_id") or native.get("workspace_id"),
        "terminal_id": claim.get("native_id") or native.get("terminal_id"),
        "native_session_id": receipt.get("session_id"),
        "presentation_status": receipt.get("presentation_status")
        or claim.get("presentation_status"),
    }
    result["communication"] = claim.get("managed_communication")
    result["launch_wait"] = launch_wait
    if details:
        receipt = row.get("receipt") or {}
        result["receipt"] = {
            key: receipt[key]
            for key in (
                "job_id",
                "launch_id",
                "generation",
                "outcome",
                "quiescent",
                "input_complete",
                "reason",
                "usage",
                "model",
                "observed_model",
                "observed_models",
                "effort",
                "native_session_id",
                "session_id",
                "native_terminal",
                "presentation_status",
                "output_hash",
            )
            if key in receipt
        }
        result["request_digest"] = row.get("request_digest")
    return scrub_json(result)


def _checkpoint_document(snapshot, unit, now: float) -> dict[str, Any]:
    """Public progress coordinates, never private source paths or worker prose."""
    progress = unit.evidence.get("checkpoint_progress", {})
    value = {
        name: progress[name]
        for name in (
            "state",
            "head",
            "observed_at",
            "dirty",
            "private_source_changed",
            "private_source_unbacked",
            "unbacked_committed",
            "progress_at",
            "dirty_since",
        )
        if name in progress
    }
    value.setdefault("state", "not_observed")
    for field, age in (("progress_at", "progress_age_s"), ("dirty_since", "dirty_age_s")):
        stamp = progress.get(field)
        value[age] = max(0.0, now - stamp) if isinstance(stamp, (int, float)) else None
    if progress.get("reason") or progress.get("error") or value["state"] == "unavailable":
        value["reason"] = "Checkpoint observation unavailable; inspect the private campaign record."
    metadata = progress.get("commit_metadata", "")
    stamp = metadata.rsplit("\n", 1)[-1] if isinstance(metadata, str) else ""
    if stamp.isascii() and stamp.isdigit() and len(stamp) <= 20:
        value["committed_at"] = int(stamp)
    actions = {action.id: action for action in snapshot.outbox}
    debt = []
    for entry in progress.get("backup_debt", []):
        row = {
            name: entry.get(name) for name in ("action_id", "head", "state", "retry_at", "failures")
        }
        if entry.get("reason"):
            row["reason"] = (
                "Backup awaits exact remote reconciliation; inspect the private campaign record."
            )
        action = actions.get(entry.get("action_id"))
        observed = action.payload.get("progress", {}).get("observed_at") if action else None
        row["pending_since"] = observed
        row["pending_age_s"] = (
            max(0.0, now - observed) if isinstance(observed, (int, float)) else None
        )
        debt.append(row)
    value["backup_debt"] = debt
    value["backup_observation_unavailable"] = bool(progress.get("backup_observation_unavailable"))
    if value["backup_observation_unavailable"]:
        value["backup_observation_reason"] = (
            "Backup observations unavailable; inspect the private campaign record."
        )
    value["backup_retry_exhausted"] = bool(progress.get("backup_retry_exhausted"))
    value["last_backed_head"] = unit.bindings.get("last_pushed_head")
    backed_at = unit.bindings.get("last_pushed_at")
    value["last_backed_at"] = backed_at
    value["backup_age_s"] = (
        max(0.0, now - backed_at) if isinstance(backed_at, (int, float)) else None
    )
    return value


def _registration_wait(snapshot, claim):
    """Project the last acknowledged pre-dispatch wait, not a live session."""
    if not claim or claim.get("status") != "prepared":
        return None
    reasons = {
        "before_worker_request": "Managed bridge registration busy before worker dispatch",
        "before_native_request": "Managed bridge registration busy before native reasoning dispatch",
    }
    for action in snapshot.outbox:
        proof = action.receipt.get("registration_wait")
        if (
            action.id == claim.get("launch_id")
            and action.state == "sent_unknown"
            and isinstance(proof, dict)
            and isinstance(proof.get("phase"), str)
            and action.error == reasons.get(proof["phase"])
        ):
            return {
                "launch_id": action.id,
                "phase": proof["phase"],
                "reason": "managed-registration-busy",
                "retry_at": action.next_retry_at,
            }
    return None


def status_document(snapshot: CampaignSnapshot) -> dict[str, Any]:
    from .campaign_action_history import counts as action_counts

    """Project execution identities; omit private configuration and evidence."""
    from .campaign_external_provenance import summary as external_provenance_summary
    from .campaign_github_faults import status_waits
    from .campaign_intake_baseline import admission_waits
    from .campaign_metrics import document as timing_document
    from .campaign_operator import diagnostics
    from .campaign_scheduler import admit
    from .sanitize import scrub_json, scrub_text

    units = []
    admission = admit(snapshot, blocked=admission_waits(snapshot))
    inventory = snapshot.bindings.get("inventory", {})
    discovery_wait = (
        (
            "repository discovery is incomplete"
            if inventory
            else "repository discovery has not completed"
        )
        if not inventory.get("complete")
        else ""
    )
    public_bindings = {
        "workspace",
        "workspace_id",
        "branch",
        "head",
        "base",
        "pr",
        "pr_number",
        "pr_url",
        "issue",
        "issue_number",
        "issue_url",
        "run_id",
        "run_dir",
        "terminal_id",
        "pane_id",
        "native_session_id",
        "session_id",
    }
    for key, unit in snapshot.units.items():
        handoff = unit.evidence.get("handoff", {})
        final_repairs = unit.evidence.get("handoff_final_repairs", {}).get(
            handoff.get("predecessor", {}).get("launch_id", ""), []
        )
        pending_repair = handoff.get("final_repair_pending", {})
        claim = dataclasses.asdict(unit.claim) if unit.claim else None
        hold = (
            {
                "id": unit.hold.id,
                "kind": unit.hold.kind,
                "reason": unit.hold.reason,
                "actor": unit.hold.actor,
            }
            if unit.hold
            else None
        )
        units.append(
            {
                "id": key,
                "title": unit.spec.title,
                "source": unit.spec.source,
                "initiative": unit.spec.initiative,
                "epic": unit.spec.epic,
                "parent": unit.spec.parent,
                "kind": unit.spec.kind,
                "state": str(unit.state),
                "after": list(unit.spec.after),
                "attempt": unit.attempt,
                "resolution_attempts": unit.resolution_attempts,
                "hold": hold,
                "claim": claim,
                "launch_wait": _registration_wait(snapshot, claim),
                "retry_at": unit.retry_at,
                "acceptance": scrub_json(
                    {
                        "revision": unit.evidence.get("acceptance", {}).get("revision", 0),
                        "criteria": [
                            {
                                name: row[name]
                                for name in ("id", "text", "required", "status", "reason")
                                if name in row
                            }
                            for row in unit.evidence.get("acceptance", {}).get("criteria", [])
                        ],
                    }
                ),
                "admission_wait": admission.waits.get(key, "")
                or (discovery_wait if unit.state in {"pending", "ready"} else ""),
                "handoff": scrub_json(
                    {
                        **{
                            name: handoff[name]
                            for name in (
                                "id",
                                "phase",
                                "role",
                                "route_role",
                                "native_role",
                                "reason",
                                "retry_at",
                            )
                            if name in handoff
                        },
                        **(
                            {
                                "final_repair": {
                                    "attempts": len(final_repairs),
                                    "max_attempts": 2,
                                    "pending": bool(pending_repair),
                                    "audit_jobs": [
                                        finding["audit_job"]
                                        for finding in pending_repair.get("findings", [])
                                    ],
                                }
                            }
                            if final_repairs or pending_repair
                            else {}
                        ),
                    }
                ),
                "links": {
                    name: value for name, value in unit.bindings.items() if name in public_bindings
                },
                "legacy_outcome": unit.legacy_outcome,
                "operator": diagnostics(unit),
                "github_sync": (
                    {
                        "phase": "after-stage",
                        "stage": unit.evidence["stage_sync_debt"]["receipt"]["stage"],
                        "launch_id": unit.evidence["stage_sync_debt"]["receipt"]["launch_id"],
                        "worker_quiescent": bool(unit.claim and unit.claim.quiescent),
                    }
                    if unit.evidence.get("stage_sync_debt")
                    else None
                ),
                "usage": unit.evidence.get("usage", {}),
                "checkpoint": _checkpoint_document(snapshot, unit, time.time()),
                "external_contributions": external_provenance_summary(unit),
                "queue": {
                    "group": unit.evidence.get("queue", {}).get("group"),
                    "workspace": unit.evidence.get("queue", {}).get("workspace"),
                    "hosted_check_confirmed": bool(
                        unit.evidence.get("queue", {}).get("hosted_check")
                    ),
                    "external_wait": unit.evidence.get("queue", {}).get("external_wait", ""),
                },
            }
        )
    counts = dict(Counter(str(unit.state) for unit in snapshot.units.values()))
    active_claims = [
        unit.claim for unit in snapshot.units.values() if unit.claim and not unit.claim.quiescent
    ]
    active_jobs = [
        row for row in snapshot.jobs.values() if row.get("claim") and not row["claim"]["quiescent"]
    ]
    active_roles = Counter(claim.role for claim in active_claims)
    active_roles.update(row["request"]["role"] for row in active_jobs)
    for unit in snapshot.units.values():
        if (
            unit.claim
            and not unit.claim.quiescent
            and any(
                action.id == unit.claim.launch_id and action.payload.get("review_candidate")
                for action in snapshot.outbox
            )
        ):
            active_roles["execution_review"] += 1
    report = snapshot.bindings.get("reasoning_decisions", {}).get("report")
    if report:
        from .campaign_reasoning_plan import fingerprint

        report = {
            **report,
            "current": report.get("context_digest") == fingerprint(snapshot, "report"),
        }
    return {
        "id": snapshot.id,
        "name": snapshot.spec.name,
        "repository": snapshot.repository,
        "target": snapshot.spec.target,
        "phase": snapshot.phase,
        **(
            {
                "ownership_retirement": {
                    "state": "retired",
                    "successor": snapshot.bindings["ownership_retirement"]["successor"],
                    "released_units": sorted(snapshot.bindings.get("ownership_transfers", {})),
                }
            }
            if snapshot.bindings.get("ownership_retirement")
            else {}
        ),
        "revision": snapshot.revision,
        "generation": snapshot.generation,
        "graph_revision": snapshot.graph_revision,
        "config_revision": snapshot.config_revision,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
        "timing": timing_document(
            snapshot,
            now=(
                snapshot.updated_at if snapshot.phase in {"delivered", "stopped"} else time.time()
            ),
        ),
        "counts": counts,
        "discovery": {
            "state": (
                "ready" if inventory.get("complete") else "incomplete" if inventory else "pending"
            ),
            "observed_at": inventory.get("observed_at"),
            "refresh_pending": any(
                action.kind == "inventory" and action.state in {"prepared", "sent_unknown"}
                for action in snapshot.outbox
            ),
            "unavailable": [scrub_text(reason) for reason in inventory.get("unavailable", [])],
            "truncated": [scrub_text(reason) for reason in inventory.get("truncated", [])],
        },
        "tracking": {
            "source_completion_scopes": {
                source.id: source.completion_scope for source in snapshot.spec.sources
            },
            "selected_work": (
                (
                    "Selected work delivered; parent scope remains open"
                    if snapshot.phase == "delivered"
                    else "Selected work delivery pending; parent scope remains open"
                )
                if any(
                    source.completion_scope == "selected-units" for source in snapshot.spec.sources
                )
                else ""
            ),
            "hierarchy_complete": snapshot.bindings.get("hierarchy_finalized_graph")
            == snapshot.graph_revision,
            "remaining_external_scope": snapshot.bindings.get("hierarchy_remaining_scope", ""),
        },
        "resources": {
            "max_workers": snapshot.spec.max_workers,
            "max_active_epics": snapshot.spec.max_active_epics,
            "max_in_flight_units": snapshot.spec.max_in_flight_units,
            "max_workers_per_epic": snapshot.spec.max_workers_per_epic,
            "max_sessions": snapshot.spec.resources.max_sessions,
            "max_review_sessions": snapshot.spec.max_review_sessions,
            "active_sessions": sum(claim.resource != "verify" for claim in active_claims)
            + len(active_jobs),
            "active_verifications": sum(claim.resource == "verify" for claim in active_claims),
            "role_limits": {
                role: {
                    "max_sessions": configured.max_sessions,
                    "active_or_reserved": active_roles[role],
                }
                for role, configured in snapshot.spec.roles
            },
            "provider_routes": scrub_json(
                snapshot.bindings.get("capacity", {}).get("observations", [])
            ),
            "provider_faults": scrub_json(
                [
                    {
                        "profile": identity.split("\0", 1)[0],
                        "model": identity.split("\0", 1)[1],
                        **{
                            key: fault[key]
                            for key in ("observed_at", "reason", "quota_retry_at", "quota_source")
                            if key in fault
                        },
                    }
                    for identity, fault in sorted(
                        snapshot.bindings.get("capacity_faults", {}).items()
                    )
                ]
            ),
            "verify_slots": snapshot.spec.resources.verify_slots,
            "active_queue_views": sum(
                bool(unit.claim and not unit.claim.quiescent and unit.claim.role == "queue_verify")
                for unit in snapshot.units.values()
            ),
            "retained_queue_views": len(
                {
                    view["workspace"]
                    for unit in snapshot.units.values()
                    for view in [
                        *unit.evidence.get("queue_history", []),
                        unit.evidence.get("queue", {}),
                    ]
                    if view.get("workspace")
                }
            ),
        },
        "units": units,
        "jobs": [
            _job_document(row, launch_wait=_registration_wait(snapshot, row.get("claim")))
            for row in list(snapshot.jobs.values())[:100]
        ],
        "jobs_truncated": len(snapshot.jobs) > 100,
        "reasoning": {
            "enabled": snapshot.spec.reasoning.enabled,
            "max_sessions": snapshot.spec.reasoning.max_sessions,
            "active_sessions": len(active_jobs),
            "states": dict(Counter(row["state"] for row in snapshot.jobs.values())),
            "errors": scrub_json(snapshot.bindings.get("semantic_jobs", {}).get("errors", {})),
            "report": scrub_json(report),
        },
        "communication": {
            key: snapshot.bindings.get("communication", {}).get(key)
            for key in (
                "mode",
                "available",
                "detail",
                "missing_bindings",
                "observed_at",
                "counts",
                "errors",
                "queued_messages",
                "endpoints",
            )
            if key in snapshot.bindings.get("communication", {})
        },
        "outbox": action_counts(snapshot),
        "waiting_effects": status_waits(snapshot),
    }


def _inbox_text(value, *, limit=2048):
    """Retain useful wait prose while failing closed on credential-shaped text."""
    import re

    from .sanitize import assert_no_leak, scrub_text

    text = scrub_text(str(value))
    # Wait prose can come from remote comments or provider diagnostics. Strip
    # terminal controls before rendering it as an operator-facing line.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = "".join(
        " " if char in "\r\n\t" else char
        for char in text
        if char in "\r\n\t" or (ord(char) >= 32 and not 127 <= ord(char) < 160)
    )
    if assert_no_leak(text):
        text = re.sub(
            r"\S+",
            lambda match: "<redacted:secret>" if assert_no_leak(match[0]) else match[0],
            text,
        )
    if assert_no_leak(text):
        return "Sensitive details withheld; inspect the private receipt"
    return text[:limit]


def _inbox_items(snapshot: CampaignSnapshot, *, now: float):
    """Read recorded waits only; prose never establishes cause or retry authority."""
    from types import SimpleNamespace

    from .campaign_capacity import selected_fault
    from .campaign_child_pause import cause as pause_cause
    from .campaign_github_faults import status_waits
    from .campaign_intake_baseline import admission_waits
    from .campaign_scheduler import CapacityObservation, admit, route_unavailable
    from .model import PauseKind

    def row(
        kind,
        unit,
        reference,
        reason,
        automatic,
        operator,
        retry=None,
        *,
        operator_required=False,
        **fields,
    ):
        identity = hashlib.sha256(json.dumps([kind, unit, reference]).encode()).hexdigest()
        retired = bool(snapshot.bindings.get("ownership_retirement"))
        return {
            "id": kind + ":" + identity,
            "kind": kind,
            "unit_id": unit,
            "reason": _inbox_text(reason),
            "next_automatic_action": (
                "None; campaign ownership is retired"
                if retired
                else (
                    automatic
                    if snapshot.phase == "active"
                    else "None while campaign is " + snapshot.phase
                )
            ),
            "retry_at": retry,
            "operator_action": (
                "Inspect preserved history and the recorded successor; this campaign is retired"
                if retired
                else operator
            ),
            "operator_action_required": operator_required,
            **fields,
        }

    decisions = {}
    for identity, receipt in snapshot.bindings.get("decision_receipts", {}).items():
        coordinate = receipt.get("coordinates", {})
        unit = snapshot.units.get(coordinate.get("unit"))
        if unit and unit.hold and unit.hold.id == receipt.get("hold_id"):
            key = (unit.id, unit.hold.id)
            refs = decisions.setdefault(key, {"ids": [], "total": 0})
            refs["total"] += 1
            if len(refs["ids"]) < 32:
                refs["ids"].append(identity)
    waits = admit(snapshot, blocked=admission_waits(snapshot)).waits
    inventory = snapshot.bindings.get("inventory", {})
    for unit in snapshot.units.values():
        if unit.hold:
            hold = unit.hold
            automatic = "No automatic continuation is established by current evidence"
            operator = "Inspect this hold and resolve its actual condition; it remains protected"
            if hold.kind == "operator":
                operator = "Use campaign release for this unit only when this explicit operator hold may be released"
            elif hold.kind in {"remote_wait", "external_wait"} and hold.actor == "github":
                automatic = "Reobserve bound GitHub conditions before repeating acceptance"
                operator = "Resolve the recorded remote condition if intervention is required"
            elif hold.kind in {
                "product_choice",
                "planning_decision",
                "design_choice",
                "decision",
                "approval",
                "checkpoint",
            }:
                operator = "Provide authorized evidence for this exact hold or decision; a response does not itself grant approval"
            pause = unit.evidence.get("child_pause")
            parsed_cause = None
            pause_reason = "No supported typed child-pause receipt was recorded"
            if (
                isinstance(pause, dict)
                and type(pause.get("schema_version")) is int
                and pause["schema_version"] == 1
            ):
                try:
                    binding = pause.get("binding")
                    if not isinstance(binding, dict):
                        raise CampaignError("child pause lacks terminal bindings")
                    parsed_cause = pause_cause({**binding, "child_pause": pause})
                    if (
                        binding.get("campaign_id") != snapshot.id
                        or binding.get("unit_id") != unit.id
                    ):
                        raise CampaignError("child pause belongs to another campaign or unit")
                except (CampaignError, ValueError, TypeError):
                    parsed_cause = None
                    pause_reason = (
                        "Recorded child-pause receipt is missing, invalid or bound elsewhere"
                    )
                else:
                    pause_reason = (
                        "Producer did not record a typed pause cause"
                        if parsed_cause is None
                        else (
                            "Producer recorded an unknown pause cause"
                            if parsed_cause.kind == PauseKind.UNKNOWN
                            else ""
                        )
                    )
            typed = parsed_cause is not None and parsed_cause.kind != PauseKind.UNKNOWN
            refs = decisions.get((unit.id, hold.id), {"ids": [], "total": 0})
            yield row(
                "hold",
                unit.id,
                hold.id,
                hold.reason,
                automatic,
                operator,
                unit.retry_at,
                operator_required=(
                    False
                    if hold.kind in {"remote_wait", "external_wait"} and hold.actor == "github"
                    else True
                ),
                hold_id=hold.id,
                hold_kind=hold.kind,
                actor=hold.actor,
                decision_ids=refs["ids"],
                decision_ids_total=refs["total"],
                decision_ids_truncated=refs["total"] > len(refs["ids"]),
                pause_evidence_status="typed" if typed else "unknown",
                pause_evidence_reason=pause_reason,
                pause_cause=(
                    {
                        **parsed_cause.to_dict(),
                        "producer": _inbox_text(parsed_cause.producer, limit=128),
                        "code": _inbox_text(parsed_cause.code, limit=128),
                    }
                    if parsed_cause
                    else None
                ),
            )
        handoff = unit.evidence.get("handoff", {})
        if handoff.get("route_wait"):
            yield row(
                "route_wait",
                unit.id,
                handoff.get("id", ""),
                handoff["route_wait"],
                "Refresh route capacity and reconsider qualified continuation",
                "Inspect route availability; no unqualified fallback or new allowance is authorized",
                unit.retry_at,
                handoff_id=handoff.get("id", ""),
            )
        if not unit.hold and unit.id in waits:
            yield row(
                "admission_wait",
                unit.id,
                unit.id,
                waits[unit.id],
                "Reconsider admission after its recorded constraint clears",
                "Inspect dependencies or capacity when intervention is required",
                unit.retry_at,
            )
        elif not unit.hold and unit.state in {"pending", "ready"} and not inventory.get("complete"):
            yield row(
                "discovery_wait",
                unit.id,
                unit.id,
                (
                    "Repository discovery is incomplete"
                    if inventory
                    else "Repository discovery has not completed"
                ),
                "Refresh repository inventory before admitting this unit",
                "Inspect campaign status discovery details if observation remains unavailable",
            )
    for action in snapshot.outbox:
        if action.state == "confirmed":
            continue
        external = action.kind.startswith("github") or action.kind in {"push", "checkpoint"}
        if not (
            action.error
            or action.next_retry_at
            or action.state == "rejected"
            or (external and action.state == "sent_unknown")
        ):
            continue
        projected = status_waits(SimpleNamespace(outbox=(action,)), limit=1)["items"]
        fault = projected[0].get("github_fault") if projected else None
        if fault:
            fault = {
                key: _inbox_text(value, limit=256) if isinstance(value, str) else value
                for key, value in fault.items()
            }
        yield row(
            "unknown_effect" if external and action.state == "sent_unknown" else "effect_wait",
            action.unit,
            action.id,
            action.error
            or (
                "Effect was refused without a recorded reason"
                if action.state == "rejected"
                else (
                    "External effect acknowledgement is not yet established"
                    if external
                    else "Effect wait reason was not recorded"
                )
            ),
            (
                "No automatic resubmission for a rejected effect"
                if action.state == "rejected"
                else "Reconcile the existing effect after its retry deadline; never blindly repeat a mutation"
            ),
            (
                "Inspect the exact effect receipt and resolve its refusal"
                if action.state == "rejected"
                else "Preserve uncertain effects; inspect their receipts before any intervention"
            ),
            action.next_retry_at or None,
            operator_required=action.state == "rejected",
            effect_id=action.id,
            effect_kind=action.kind,
            effect_state=action.state,
            github_fault=fault,
        )
    for job in snapshot.jobs.values():
        if job.get("state") not in {"capacity_wait", "failed", "exhausted"}:
            continue
        request = job.get("request") or {}
        yield row(
            "job_wait",
            request.get("unit", ""),
            job["id"],
            job.get("reason") or "No typed reason was recorded for this job wait",
            (
                "Refresh capacity before reconsidering this job"
                if job["state"] == "capacity_wait"
                else "No automatic retry is established by this terminal job record"
            ),
            "Inspect the job receipt; retained attempts and approval requirements still apply",
            job.get("retry_at"),
            operator_required=job["state"] != "capacity_wait",
            job_id=job["id"],
            job_state=job["state"],
        )
    batch = snapshot.bindings.get("capacity", {})
    for index, route in enumerate(batch.get("observations", [])):
        capacity = route.get("capacity", {})
        if not isinstance(capacity, dict):
            capacity = {}
        try:
            observed = CapacityObservation(**capacity)
            observed = dataclasses.replace(
                observed,
                observed_at=(
                    observed.observed_at
                    if observed.observed_at is not None
                    else batch.get("observed_at", 0)
                ),
                expires_at=(
                    observed.expires_at
                    if observed.expires_at is not None
                    else batch.get("observed_at", 0) + 120
                ),
            )
            reason = selected_fault(
                snapshot, route.get("profile", ""), route.get("model", ""), now=now
            ) or route_unavailable(observed, now=now)
        except (AttributeError, KeyError, TypeError, ValueError, CampaignError):
            reason = "Stored provider capacity evidence is invalid or incomplete"
        launcher_reasons = [
            entry.get("reason") or "Qualified launcher unavailable"
            for entry in route.get("launchers", [])
            if entry.get("ready") is False
        ]
        if not reason and not launcher_reasons:
            continue
        reason = "; ".join([value for value in [reason, *launcher_reasons] if value])
        if capacity.get("reason") and capacity["reason"] not in reason:
            reason += "; " + str(capacity["reason"])
        yield row(
            "provider_wait",
            "",
            [route.get("profile"), route.get("model"), index],
            reason,
            "Refresh provider capacity before selecting this route",
            "Restore provider access if required; unknown capacity cannot qualify a route",
            None,
            profile=route.get("profile", ""),
            model=route.get("model", ""),
            capacity_known=capacity.get("known") is True,
            capacity_observed_at=capacity.get("observed_at", batch.get("observed_at")),
            capacity_expires_at=capacity.get("expires_at"),
        )


def inbox_document(snapshot: CampaignSnapshot, *, unit="", after="", limit=100, now=None):
    """A bounded page from a single stored revision, without advancing authority."""
    if not 1 <= limit <= 200:
        raise CampaignError("inbox page limit must be between 1 and 200")
    if unit and unit not in snapshot.units:
        raise CampaignError(f"unknown campaign unit {unit}")
    total = 0

    def candidates():
        nonlocal total
        for item in _inbox_items(snapshot, now=time.time() if now is None else now):
            if unit and item["unit_id"] != unit:
                continue
            total += 1
            if item["id"] > after:
                yield item

    page = heapq.nsmallest(limit + 1, candidates(), key=lambda item: item["id"])
    more = len(page) > limit
    items = page[:limit]
    return {
        "campaign_id": snapshot.id,
        "revision": snapshot.revision,
        "generation": snapshot.generation,
        "phase": snapshot.phase,
        "items": items,
        "total": total,
        "has_more": more,
        "next_after": items[-1]["id"] if more else None,
        "unit_filter": unit,
    }


def _emit(args: argparse.Namespace, result: dict[str, Any]) -> int:
    if args.json:
        machine.emit(result)
    else:
        if "campaign" in result:
            campaign = result["campaign"]
            print(
                f"{campaign['id']}: {campaign['phase']} ({campaign['repository']} -> {campaign['target']})"
            )
            discovery = campaign.get("discovery", {})
            if discovery.get("state") in {"pending", "incomplete"}:
                print(f"  discovery: {discovery['state']}")
                for reason in (*discovery.get("unavailable", ()), *discovery.get("truncated", ())):
                    print(f"    {reason}")
            for unit in campaign["units"]:
                reason = unit["hold"]["reason"] if unit["hold"] else unit.get("admission_wait", "")
                suffix = f" — {reason}" if reason else ""
                print(f"  {unit['id']}: {unit['state']}{suffix}")
        elif "campaigns" in result:
            for campaign in result["campaigns"]:
                print(f"{campaign['id']}: {campaign['phase']} ({campaign['name']})")
        elif "request" in result:
            print(f"{result['command']}: request {result['request']['id']} queued")
        elif "inbox" in result:
            inbox = result["inbox"]
            print(f"{inbox['campaign_id']}: {inbox['total']} inbox items ({inbox['phase']})")
            for item in inbox["items"]:
                print(f"  {item['unit_id'] or 'campaign'}: {item['kind']} — {item['reason']}")
                identities = [
                    f"{key}={item[key]}"
                    for key in ("hold_id", "effect_id", "job_id", "handoff_id")
                    if item.get(key)
                ]
                identities.extend("decision_id=" + value for value in item.get("decision_ids", []))
                if identities:
                    print("    " + ", ".join(identities))
                if item.get("pause_evidence_status") == "unknown":
                    print("    Pause evidence: unknown — " + item["pause_evidence_reason"])
                print(f"    Next: {item['next_automatic_action']}")
                if item["retry_at"] is not None:
                    print(f"    Retry at: {item['retry_at']}")
                else:
                    print("    Retry deadline: not recorded")
                print(f"    Operator: {item['operator_action']}")
            if inbox["has_more"]:
                print(f"  More items: --after {inbox['next_after']}")
        elif "jobs" in result:
            for job in result["jobs"]:
                print(f"{job['id']}: {job['state']} ({job['purpose']}) {job.get('reason', '')}")
        elif "events" in result:
            for event in result["events"]:
                print(
                    f"{event['sequence']}: {event.get('unit', '')} {event.get('kind', '')} {event.get('history_path', '')}"
                )
            for session in result.get("sessions", []):
                print(
                    f"{session['unit']}: {session.get('phase', '')} {session.get('state', '')} {session.get('log_path', '')}"
                )
            for gap in result.get("history_gaps", []):
                print(f"history unavailable: {gap['detail']}")
        else:
            print(f"{result['command']}: {'ready' if result['ok'] else 'not ready'}")
            for problem in result.get("problems", []):
                print(f"  {problem}")
            for area, observation in result.get("capabilities", {}).items():
                print(f"  {area}: {'ready' if observation['ready'] else 'not ready'}")
                for problem in observation.get("problems", []):
                    print(f"    {problem}")
    return 0 if result.get("ok") else 1


def _runtime(module, project, store):
    """Normal CLI entry binds trusted transcript evidence, not only test injection."""
    from .campaign_history_sources import TranscriptEvidenceProvider

    runtime = module.CampaignRuntime(project, store)
    runtime.evidence_providers = {
        "transcript": TranscriptEvidenceProvider(),
        **getattr(runtime, "evidence_providers", {}),
    }
    return runtime


def _spec(args: argparse.Namespace) -> CampaignSpec:
    spec = parse_spec(args.spec)
    if getattr(args, "paradev_reflect", None) or getattr(args, "paradev_project", None):
        spec = dataclasses.replace(
            spec,
            paradev=dataclasses.replace(
                spec.paradev,
                reflect=True,
                project_id=getattr(args, "paradev_project", None) or spec.paradev.project_id,
            ),
        )
    return spec


def dispatch(args: argparse.Namespace) -> int:
    command = args.campaign_command
    if command == "trial-handoff":
        from .campaign_history_trial import run_trial

        try:
            report = run_trial(
                Path(args.snapshots).expanduser(),
                Path(args.capability).expanduser(),
                Path(args.directory).expanduser(),
                parallel=args.parallel,
                timeout_s=args.timeout,
                execute=not args.prepare_only,
                reuse_directory=Path(args.reuse).expanduser() if args.reuse else None,
                reducer_capability_path=(
                    Path(args.reducer_capability).expanduser() if args.reducer_capability else None
                ),
                drain=args.drain,
            )
            return _emit(args, _result(command, **report))
        except (CampaignError, OSError, ValueError, KeyError, TypeError) as exc:
            return _emit(args, _result(command, ok=False, problems=[str(exc)]))
    if command in {"qualify-headless", "qualify-writer"}:
        if command == "qualify-writer":
            from .campaign_execution_qualification import qualify
        else:
            from .campaign_qualification import qualify

        try:
            report = qualify(
                Path(args.proposal).expanduser(),
                Path(args.directory).expanduser(),
                input_bytes=args.input_bytes,
                output_tokens=args.output_tokens,
                timeout_s=args.timeout,
            )
            return _emit(args, _result(command, **report))
        except (CampaignError, OSError, ValueError, KeyError, TypeError) as exc:
            return _emit(args, _result(command, ok=False, problems=[str(exc)]))
    if command in {"migration-inventory", "migration-qualify"}:
        from .campaign_migration_cli import inventory_document, qualify_document

        try:
            project, spec = Path(args.project).expanduser().resolve(), _spec(args)
            result = (
                inventory_document(project, spec)
                if command == "migration-inventory"
                else qualify_document(project, spec, Path(args.manifest).expanduser())
            )
            return _emit(args, _result(command, **result))
        except (CampaignError, OSError, ValueError, KeyError, TypeError) as exc:
            return _emit(args, _result(command, ok=False, problems=[str(exc)]))
    if command == "provenance":
        from .campaign_provenance import inspect as installation

        try:
            result = installation(expected_commit=args.expected_commit)
            ok = result["matches_expected"] if args.expected_commit else result["qualified"]
            return _emit(
                args, _result(command, ok=ok, installation=result, problems=result["problems"])
            )
        except (OSError, ValueError) as exc:
            return _emit(args, _result(command, ok=False, problems=[str(exc)]))
    if command in {"validate", "preflight"}:
        try:
            spec = _spec(args)
            if command == "validate":
                return _emit(args, _result(command, spec=spec_to_dict(spec)))
            from .campaign_preflight import inspect

            observed = inspect(Path(args.project).expanduser().resolve(), spec)
            return _emit(args, {**observed, "command": "campaign.preflight"})
        except (CampaignError, OSError, ValueError) as exc:
            return _emit(args, _result(command, ok=False, problems=[str(exc)]))

    from . import campaign_runtime
    from .campaign_supervisor import CampaignSupervisor

    project = Path(args.project).expanduser().resolve()
    if command == "run":
        spec = _spec(args)
        from .campaign_preflight import inspect

        observed = inspect(project, spec)
        if not observed["ok"] and not (
            spec.github.allow_publication_wait and observed.get("can_start") is True
        ):
            return _emit(args, {**observed, "command": "campaign.run"})
        if args.prepare_only:
            store, snapshot = campaign_runtime.create(project, spec, prepare_only=True)
            return _emit(args, _result(command, campaign=status_document(snapshot)))
        store, snapshot = campaign_runtime.create(project, spec)
        runtime = _runtime(campaign_runtime, project, store)
        supervisor = CampaignSupervisor(store, runtime)
        result = supervisor.run(snapshot.id)
        return _emit(args, _result(command, campaign=status_document(result)))
    store = campaign_runtime.store_for(project)
    if command == "list":
        return _emit(
            args,
            _result(command, campaigns=[status_document(snapshot) for snapshot in store.list()]),
        )
    campaign_id = args.campaign_id
    snapshot = store.load(campaign_id)
    from .campaign_transfer import require_not_retired

    if command not in {"status", "inbox", "history", "jobs", "messages", "endpoints", "transfer"}:
        try:
            require_not_retired(snapshot, operation=command)
        except CampaignError as exc:
            return _emit(args, _result(command, ok=False, problems=[str(exc)]))
    if command == "transfer":
        from .campaign_transfer import TransferRequest, preview, release

        request = TransferRequest(
            predecessor=campaign_id,
            unit=args.unit,
            successor=args.successor,
            successor_unit=args.successor_unit,
            pr_number=args.pr,
            expected_revision=args.expected_revision,
            expected_successor_revision=args.expected_successor_revision,
            reason=args.reason,
        )
        try:
            report = (preview if args.preview else release)(store, request, project=project)
        except (CampaignError, OSError, ValueError) as exc:
            return _emit(args, _result(command, ok=False, problems=[str(exc)]))
        return _emit(args, _result(command, **report))
    if command == "reflect":
        from .campaign_reflect import refresh

        report = refresh(
            store,
            campaign_id,
            unit_id=args.unit,
            preview=args.preview,
            expected_revision=args.expected_revision,
        )
        return _emit(args, _result(command, **report))
    if command == "status":
        return _emit(args, _result(command, campaign=status_document(snapshot)))
    if command == "inbox":
        try:
            inbox = inbox_document(snapshot, unit=args.unit, after=args.after, limit=args.limit)
        except CampaignError as exc:
            return _emit(args, _result(command, ok=False, problems=[str(exc)]))
        return _emit(
            args,
            _result(command, inbox=inbox),
        )
    if command == "endpoints":
        from .campaign_managed_communication import ManagedCommunication

        rows = ManagedCommunication(store.directory(campaign_id)).endpoints(
            after=args.after,
            limit=args.limit,
            state=args.state,
        )
        return _emit(
            args,
            _result(
                command,
                endpoints=rows,
                next_after=rows[-1]["sequence"] if rows else args.after,
                has_more=len(rows) == args.limit,
            ),
        )
    if command in {"messages", "steer"}:
        from .campaign_messages import MessageLedger

        ledger = MessageLedger(store.directory(campaign_id))
        if args.unit and args.unit not in snapshot.units:
            raise CampaignError(f"unknown campaign unit {args.unit}")
        if args.job:
            jobs = store.jobs(campaign_id, job_id=args.job, limit=1)
            if not jobs or (args.unit and jobs[0]["request"]["unit"] != args.unit):
                raise CampaignError("unknown reasoning job or different message target")
        if command == "steer":
            import uuid

            from .campaign_jobs import TERMINAL
            from .campaign_model import UnitState

            target_unit = args.unit or (jobs[0]["request"].get("unit", "") if args.job else "")
            if not target_unit and not args.job:
                raise CampaignError("steering requires --unit or --job")
            if (
                target_unit
                and snapshot.units[target_unit].state
                in {
                    UnitState.DELIVERED,
                    UnitState.CANCELED,
                    UnitState.MERGE_REQUESTED,
                    UnitState.MERGED_PENDING_SYNC,
                }
            ) or (args.job and jobs[0]["state"] in TERMINAL):
                raise CampaignError("target has closed; steering cannot reopen it")
            accepted = ledger.accept(
                message_id=args.request_id or uuid.uuid4().hex,
                unit=target_unit or "@campaign",
                job=args.job,
                text=args.text,
            )
            return _emit(args, _result(command, message=accepted, state="queued"))
        rows = ledger.list(unit=args.unit, job=args.job, after=args.after, limit=args.limit)
        return _emit(
            args,
            _result(
                command,
                messages=rows,
                next_after=rows[-1]["sequence"] if rows else args.after,
                has_more=len(rows) == args.limit,
            ),
        )
    if command == "migrate-state":
        migrated = store.migrate_state(campaign_id)
        return _emit(args, _result(command, campaign=status_document(migrated)))
    if command == "reconfigure":
        import tomllib

        from .campaign_reconfiguration import reconfigure

        with Path(args.spec).expanduser().open("rb") as stream:
            proposed = tomllib.load(stream)
        result = reconfigure(
            store,
            campaign_id,
            proposed,
            expected_revision=args.expected_revision,
            reason=args.reason,
            preview=args.preview,
            review_policy_source=args.review_policy_source,
        )
        if isinstance(result, dict):
            return _emit(args, _result(command, proposal=result))
        return _emit(args, _result(command, campaign=status_document(result)))
    if command == "requalify":
        from .campaign_requalification import requalify

        renewed = requalify(
            store,
            campaign_id,
            Path(args.capabilities).expanduser(),
            expected_revision=args.expected_revision,
            reason=args.reason,
        )
        return _emit(args, _result(command, campaign=status_document(renewed)))
    if command == "jobs":
        if not 1 <= args.limit <= 1000:
            raise CampaignError("job page limit must be between 1 and 1000")
        if args.unit and args.unit not in snapshot.units:
            raise CampaignError(f"unknown campaign unit {args.unit}")
        rows = store.jobs(
            campaign_id, after=args.after, limit=args.limit, unit=args.unit, job_id=args.job
        )
        return _emit(
            args,
            _result(
                command,
                jobs=[
                    _job_document(
                        row,
                        details=args.details,
                        launch_wait=_registration_wait(snapshot, row.get("claim")),
                    )
                    for row in rows
                ],
                next_after=rows[-1]["id"] if rows else args.after,
                has_more=len(rows) == args.limit,
            ),
        )
    if command == "history":
        from .campaign_operator import historical_sessions, sessions
        from .sanitize import scrub_text

        if args.unit and args.unit not in snapshot.units:
            raise CampaignError(f"unknown campaign unit {args.unit}")
        if args.actions:
            from .sanitize import scrub_json

            if args.role:
                raise CampaignError("action history does not accept a native session role")
            rows = store.action_history(
                campaign_id, after=args.after, limit=args.limit, unit=args.unit or None
            )
            return _emit(
                args,
                _result(
                    command,
                    actions=scrub_json(rows),
                    next_after=rows[-1]["sequence"] if rows else args.after,
                    has_more=len(rows) == args.limit,
                    scope="archived-actions",
                ),
            )
        if args.observations:
            from .sanitize import scrub_json

            if args.role:
                raise CampaignError("observation history does not accept a native session role")
            rows = store.observations(
                campaign_id, after=args.after, limit=args.limit, unit=args.unit
            )
            return _emit(
                args,
                _result(
                    command,
                    observations=scrub_json(rows),
                    next_after=rows[-1]["sequence"] if rows else args.after,
                    has_more=len(rows) == args.limit,
                    scope="archived-observations",
                ),
            )
        rows = store.history(campaign_id, after=args.after, limit=args.limit, unit=args.unit)
        selected = [
            row
            for row in rows
            if not args.role
            or row.get("role", row.get("stage")) == args.role
            or row.get("history_path")
        ]
        events = [
            {
                key: row[key]
                for key in (
                    "sequence",
                    "kind",
                    "unit",
                    "role",
                    "stage",
                    "at",
                    "time",
                    "action",
                    "reason",
                    "state",
                    "history_path",
                    "job_id",
                    "purpose",
                )
                if key in row
            }
            for row in selected
        ]
        for event in events:
            if isinstance(event.get("reason"), str):
                event["reason"] = scrub_text(event["reason"])
        retained = [
            {"unit": unit.id, **session}
            for unit in snapshot.units.values()
            if not args.unit or unit.id == args.unit
            for session in sessions(unit, role=args.role)
        ]
        older, gaps = historical_sessions(store, snapshot, selected, role=args.role)
        retained = list(
            {
                (row["unit"], row.get("terminal_id"), row.get("task_id")): row
                for row in [*older, *retained]
            }.values()
        )
        return _emit(
            args,
            _result(
                command,
                events=events,
                sessions=retained[: args.limit],
                history_gaps=gaps,
                next_after=rows[-1]["sequence"] if rows else args.after,
                has_more=len(rows) == args.limit,
                session_coverage="truncated" if len(retained) > args.limit else "complete",
            ),
        )
    if (
        command
        in {"hold", "release", "attach", "adopt", "adoption-proposal", "handoff", "authorize-scope"}
        and args.unit not in snapshot.units
    ):
        raise CampaignError(f"unknown campaign unit {args.unit}")
    if command == "authorize-scope":
        from .campaign_scope_authority import proposal

        scope = proposal(snapshot, args.unit, args.expected_revision, args.reason)
        request = store.enqueue(campaign_id, command, unit=args.unit, scope=scope)
        return _emit(args, _result(command, request=request))
    if command == "handoff":
        request = store.enqueue(
            campaign_id,
            "handoff",
            unit=args.unit,
            profile=args.profile,
            model=args.model,
            effort=args.effort,
            reason=args.reason,
        )
        return _emit(args, _result(command, request=request))
    if command in {"adopt", "adoption-proposal"}:
        from .campaign_adoption import prepare_request, propose

        gateway = _runtime(campaign_runtime, project, store).gateway(snapshot)
        if command == "adoption-proposal":
            proposal = propose(
                project, snapshot, args.unit, args.pr, args.observer, gateway=gateway
            )
            return _emit(
                args,
                _result(
                    command,
                    ok=proposal["ready_for_owner_authorization"],
                    proposal=proposal,
                    problems=proposal.get("problems", []),
                ),
            )
        payload = prepare_request(
            project,
            snapshot,
            args.unit,
            Path(args.manifest).expanduser(),
            args.observer,
            gateway=gateway,
        )
        request = store.enqueue(campaign_id, "adopt", unit=args.unit, adoption=payload)
        return _emit(args, _result(command, request=request))
    if command in {"stop", "hold", "release"}:
        request = store.enqueue(
            campaign_id,
            command,
            unit=getattr(args, "unit", ""),
            reason=getattr(args, "reason", ""),
            terminate_workers=getattr(args, "terminate_workers", False),
        )
        return _emit(args, _result(command, request=request))
    runtime = _runtime(campaign_runtime, project, store)
    if command == "resume":
        runtime.authorize_backup_resume(snapshot)
    if command == "attach":
        return runtime.attach(campaign_id, args.unit, role=args.role)
    supervisor = CampaignSupervisor(store, runtime)
    if command == "resume":
        result = supervisor.run(campaign_id)
    elif command == "reconcile":
        result = supervisor.reconcile(campaign_id)
    else:
        raise CampaignError(f"unknown campaign command {command}")
    return _emit(args, _result(command, campaign=status_document(result)))
