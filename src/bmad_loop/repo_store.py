"""Repository-bound audit authority, short CAS transactions and preserved evidence.

This storage does not migrate, normalize or adopt any existing campaign state.
Stop requests are independent of the long-running reader/model coordinator.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .campaign_jobs import digest
from .campaign_model import CampaignError
from .platform_util import atomic_write_text, file_lock, is_link_like
from .verify import git_bytes


class RepoStopped(CampaignError):
    """The durable stop preceded admission of this effect."""


def common_dir(project):
    result = git_bytes(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode:
        raise CampaignError("Repository audit requires a Git working tree")
    return Path(result.stdout.decode("utf-8").strip()).resolve()


class RepoStore:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.common = common_dir(self.project)
        self.root = self.common / "bmad-repo-audits"
        if is_link_like(self.root):
            raise CampaignError("Repository audit state is redirected")

    def directory(self, identity):
        if not re.fullmatch(r"audit-[a-f0-9]{32}", identity):
            raise CampaignError("Invalid repository audit identity")
        path = self.root / identity
        if is_link_like(path):
            raise CampaignError("Repository audit directory is redirected")
        return path

    @contextmanager
    def connection(self, identity):
        path = self.directory(identity) / "authority.sqlite"
        if is_link_like(path) or not path.is_file():
            raise CampaignError("Repository audit authority is missing or redirected")
        with sqlite3.connect(path, timeout=5) as connection:
            yield connection

    def create(self, policy, capabilities, policy_digest):
        self.root.mkdir(parents=True, exist_ok=True)
        identity = "audit-" + uuid.uuid4().hex
        directory = self.directory(identity)
        directory.mkdir(mode=0o700)
        state = {
            "schema_version": 1,
            "id": identity,
            "revision": 1,
            "generation": 1,
            "repository": policy["repository"],
            "common_dir": str(self.common),
            "project": str(self.project),
            "policy": policy,
            "policy_digest": policy_digest,
            "capabilities": capabilities,
            "phase": "collecting",
            "mode": "audit",
            "inventory": None,
            "jobs": {},
            "actions": [],
            "mutations": 0,
            "closures": 0,
            "reason": "",
            "created_at": time.time(),
        }
        with sqlite3.connect(directory / "authority.sqlite") as connection:
            connection.executescript(
                "CREATE TABLE authority(id INTEGER PRIMARY KEY, revision INTEGER, payload TEXT); CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT); CREATE TABLE control(id INTEGER PRIMARY KEY, stopped INTEGER);"
            )
            connection.execute("INSERT INTO authority VALUES(1,1,?)", (json.dumps(state),))
            connection.execute("INSERT INTO control VALUES(1,0)")
            connection.execute(
                "INSERT INTO events(payload) VALUES(?)",
                (json.dumps({"kind": "created", "revision": 1, "policy_digest": policy_digest}),),
            )
        return state

    def load(self, identity):
        with self.connection(identity) as connection:
            row = connection.execute("SELECT payload FROM authority WHERE id=1").fetchone()
        state = json.loads(row[0])
        if (
            state.get("schema_version") != 1
            or state.get("common_dir") != str(self.common)
            or state.get("id") != identity
            or digest(state["policy"]) != state["policy_digest"]
        ):
            raise CampaignError("Repository audit identity/schema/policy changed")
        return state

    def save(self, snapshot, kind, *, admit=False, **details):
        expected = snapshot["revision"]
        updated = {**snapshot, "revision": expected + 1}
        with self.connection(snapshot["id"]) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if admit and connection.execute("SELECT stopped FROM control WHERE id=1").fetchone()[0]:
                raise RepoStopped("Repository audit stop requested")
            changed = connection.execute(
                "UPDATE authority SET revision=?,payload=? WHERE id=1 AND revision=?",
                (expected + 1, json.dumps(updated), expected),
            )
            if changed.rowcount != 1:
                raise CampaignError("Repository audit revision changed")
            connection.execute(
                "INSERT INTO events(payload) VALUES(?)",
                (
                    json.dumps(
                        {"kind": kind, "revision": expected + 1, "at": time.time(), **details}
                    ),
                ),
            )
        snapshot.update(updated)

    @contextmanager
    def admission(self, identity):
        """Serialize only the durable launch-intent boundary with stop requests."""
        with self.connection(identity) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT stopped FROM control WHERE id=1").fetchone()[0]:
                raise RepoStopped("Repository audit stop requested")
            yield

    def stop(self, identity, stopped=True):
        with self.connection(identity) as connection:
            connection.execute("UPDATE control SET stopped=? WHERE id=1", (int(stopped),))
            connection.execute(
                "INSERT INTO events(payload) VALUES(?)",
                (
                    json.dumps(
                        {
                            "kind": "stop-requested" if stopped else "resume-requested",
                            "at": time.time(),
                        }
                    ),
                ),
            )

    def stopped(self, identity):
        with self.connection(identity) as connection:
            return bool(connection.execute("SELECT stopped FROM control WHERE id=1").fetchone()[0])

    def history(self, identity, after=0, limit=100):
        if after < 0 or not 1 <= limit <= 1000:
            raise CampaignError("Invalid history page")
        with self.connection(identity) as connection:
            return [
                {"sequence": n, **json.loads(raw)}
                for n, raw in connection.execute(
                    "SELECT sequence,payload FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
                    (after, limit),
                )
            ]

    def artifact(self, identity, kind, value):
        raw = json.dumps(value, sort_keys=True, ensure_ascii=True)
        sha = digest(value)
        path = self.directory(identity) / f"{kind}-{sha}.json"
        if is_link_like(path):
            raise CampaignError("Audit artifact is redirected")
        if path.exists():
            if path.read_text(encoding="utf-8") != raw:
                raise CampaignError("Immutable audit artifact changed")
        else:
            atomic_write_text(path, raw, follow_symlinks=False)
        return {"path": str(path), "digest": sha}

    def read(self, reference):
        path = Path(reference["path"])
        if (
            is_link_like(path)
            or not path.is_file()
            or not path.resolve().is_relative_to(self.root.resolve())
            or path.stat().st_size > 128 * 1024 * 1024
        ):
            raise CampaignError("Audit evidence path is missing, redirected or oversized")
        value = json.loads(path.read_text(encoding="utf-8"))
        if digest(value) != reference["digest"]:
            raise CampaignError("Audit evidence changed")
        return value

    def coordinator(self):
        self.root.mkdir(parents=True, exist_ok=True)
        return file_lock(self.root / "coordinator.lock", blocking=False)
