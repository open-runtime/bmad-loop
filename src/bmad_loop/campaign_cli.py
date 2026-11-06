"""Campaign CLI and read-only projections; runtime imports remain lazy.

Offline validation never opens the target repository or configures a multiplexer.
Commands use the machine envelope contract and established exit codes. Controls
append requests to the inbox; only the coordinator applies state transitions.
"""

from __future__ import annotations

import argparse
import dataclasses
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
        ("migration-inventory", "inspect current work and cache an exact migration review receipt"),
        (
            "migration-qualify",
            "qualify an owner migration manifest against fresh service observations",
        ),
        ("run", "create and supervise a campaign in the foreground"),
        ("resume", "reconcile and supervise an existing campaign"),
        ("list", "list repository campaigns"),
        ("status", "show campaign units, holds and links"),
        ("stop", "close admissions and drain existing bounded stages"),
        ("hold", "hold one unit at a safe stage boundary"),
        ("release", "release an explicit operator hold"),
        ("attach", "attach the existing owned unit terminal"),
        ("reconcile", "refresh an existing campaign without starting workers"),
        ("adoption-proposal", "inspect exact existing PR and service handoff coordinates"),
        ("adopt", "queue an owner-authorized, independently fenced existing PR handoff"),
        ("provenance", "verify the currently loaded installation and exact fork commit"),
    ):
        child = commands.add_parser(command, help=help_text)
        child.add_argument("--project", default=argparse.SUPPRESS, help="target project root")
        child.set_defaults(func=dispatch)
        if command in {"validate", "preflight", "run", "migration-inventory", "migration-qualify"}:
            child.add_argument("spec", metavar="SPEC", help="version 1 campaign TOML")
            if command == "migration-qualify":
                child.add_argument(
                    "manifest", metavar="MANIFEST", help="owner-selected migration JSON"
                )
        elif command not in {"list", "provenance"}:
            child.add_argument("campaign_id", metavar="ID")
        if command == "provenance":
            child.add_argument("--expected-commit", default="", metavar="SHA")
        if command in {"hold", "release", "attach", "adopt", "adoption-proposal"}:
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
        if command == "hold":
            child.add_argument("--reason", required=True)
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
        if command == "run":
            child.add_argument("--paradev-reflect", action="store_true", default=None)
            child.add_argument("--paradev-project", metavar="PROJECT_ID")


def _result(command: str, *, ok: bool = True, **fields: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "command": f"campaign.{command}", "ok": ok, **fields}


def status_document(snapshot: CampaignSnapshot) -> dict[str, Any]:
    """Project execution identities; omit private configuration and evidence."""
    from .campaign_metrics import document as timing_document

    units = []
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
                "retry_at": unit.retry_at,
                "links": {
                    name: value for name, value in unit.bindings.items() if name in public_bindings
                },
                "legacy_outcome": unit.legacy_outcome,
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
    return {
        "id": snapshot.id,
        "name": snapshot.spec.name,
        "repository": snapshot.repository,
        "target": snapshot.spec.target,
        "phase": snapshot.phase,
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
        "tracking": {
            "hierarchy_complete": snapshot.bindings.get("hierarchy_finalized_graph")
            == snapshot.graph_revision,
            "remaining_external_scope": snapshot.bindings.get("hierarchy_remaining_scope", ""),
        },
        "resources": {
            "max_in_flight_units": snapshot.spec.max_in_flight_units,
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
        "outbox": {
            state: sum(action.state == state for action in snapshot.outbox)
            for state in ("prepared", "sent_unknown", "confirmed", "rejected")
        },
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
            for unit in campaign["units"]:
                suffix = f" — {unit['hold']['reason']}" if unit["hold"] else ""
                print(f"  {unit['id']}: {unit['state']}{suffix}")
        elif "campaigns" in result:
            for campaign in result["campaigns"]:
                print(f"{campaign['id']}: {campaign['phase']} ({campaign['name']})")
        elif "request" in result:
            print(f"{result['command']}: request {result['request']['id']} queued")
        else:
            print(f"{result['command']}: {'ready' if result['ok'] else 'not ready'}")
            for problem in result.get("problems", []):
                print(f"  {problem}")
            for area, observation in result.get("capabilities", {}).items():
                print(f"  {area}: {'ready' if observation['ready'] else 'not ready'}")
                for problem in observation.get("problems", []):
                    print(f"    {problem}")
    return 0 if result.get("ok") else 1


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
        if not observed["ok"]:
            return _emit(args, {**observed, "command": "campaign.run"})
        store, snapshot = campaign_runtime.create(project, spec)
        runtime = campaign_runtime.CampaignRuntime(project, store)
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
    if command == "status":
        return _emit(args, _result(command, campaign=status_document(snapshot)))
    if (
        command in {"hold", "release", "attach", "adopt", "adoption-proposal"}
        and args.unit not in snapshot.units
    ):
        raise CampaignError(f"unknown campaign unit {args.unit}")
    if command in {"adopt", "adoption-proposal"}:
        from .campaign_adoption import prepare_request, propose

        gateway = campaign_runtime.CampaignRuntime(project, store).gateway(snapshot)
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
    runtime = campaign_runtime.CampaignRuntime(project, store)
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
