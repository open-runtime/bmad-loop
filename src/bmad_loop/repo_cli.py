"""Repository audit CLI: separate local authority and explicit reconciliation."""

import argparse
import sys
from pathlib import Path

from . import machine
from .campaign_github import GithubError
from .campaign_model import CampaignError
from .repo_policy import load_policy
from .repo_runtime import RepoRuntime, read_catalog, status
from .repo_store import RepoStore


def register(subparsers):
    parser = subparsers.add_parser("repo", help="audit and organize existing repository work")
    parser.add_argument("--project", default=".")
    commands = parser.add_subparsers(dest="repo_command", required=True)
    for name in ("audit", "reconcile", "status", "history", "stop", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--project", default=argparse.SUPPRESS)
        machine.add_json_flag(command, "repository operation")
        command.set_defaults(func=dispatch)
        if name == "audit":
            command.add_argument("--policy", required=True)
        else:
            command.add_argument("id")
        if name == "reconcile":
            command.add_argument("--expected-revision", required=True, type=int)
        if name == "history":
            command.add_argument("--after", type=int, default=0)
            command.add_argument("--limit", type=int, default=100)


def _report(store, state):
    try:
        catalog = read_catalog(store, state) if state["inventory"] is not None else None
    except CampaignError as exc:
        # Observation faults must not hide settled issued outcomes or turn a
        # bounded --json response into an exception-only failure.
        return status(state, inventory_error=str(exc))
    return status(state, catalog=catalog)


def dispatch(args):
    try:
        store = RepoStore(Path(args.project).expanduser())
        if args.repo_command == "stop":
            store.stop(args.id)
            report = {"schema_version": 1, "id": args.id, "stop_requested": True}
        elif args.repo_command == "history":
            report = {
                "schema_version": 1,
                "id": args.id,
                "events": store.history(args.id, args.after, args.limit),
            }
        elif args.repo_command == "status":
            state = store.load(args.id)
            report = _report(store, state)
        else:
            with store.coordinator():
                if args.repo_command == "audit":
                    policy, caps, sha = load_policy(Path(args.policy).expanduser())
                    state = store.create(policy, caps, sha)
                else:
                    state = store.load(args.id)
                if args.repo_command == "reconcile" and state["revision"] != args.expected_revision:
                    raise CampaignError("Repository audit revision changed")
                if args.repo_command == "resume":
                    store.stop(args.id, False)
                    state["resume_epoch"] = state.get("resume_epoch", 0) + 1
                    store.save(state, "resuming")
                runtime = RepoRuntime(store, state)
                if args.repo_command == "reconcile" or state["mode"] == "reconcile":
                    runtime.reconcile()
                else:
                    runtime.audit()
                report = _report(store, state)
        if args.json:
            machine.emit(report)
        else:
            print(f"{report['id']}: {report.get('phase', 'request recorded')}")
        return 1 if report.get("phase") in {"held", "capacity_wait", "observation_wait"} else 0
    except (CampaignError, GithubError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Repository operation failed: {exc}", file=sys.stderr)
        return 1
