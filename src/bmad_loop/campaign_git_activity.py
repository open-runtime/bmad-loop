"""Keep campaign writer reservations distinct from uncertain Git effects.

Only exact finalization records supply actor evidence. Worker/window death does
not replace it. Fresh observation can settle recorded, stamped actors only when
the original census and streams were complete; it cannot recover lost coverage
or prove universal daemon containment. This module never signals a process,
replays Git, changes a recovery record, or clears an effect hold.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import stat
import threading
from pathlib import Path
from typing import Any

from . import finalization_recovery
from .campaign_model import CampaignError
from .owned_capture import ActorEvidence, CaptureOwnership
from .platform_util import is_link_like
from .process_host import get_process_host

MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_COMMANDS = 128
MAX_ACTORS = 4096
_PROBE_SLOTS = threading.BoundedSemaphore(4)
_CONTEXT = "campaign_git_activity"
_ENGINE_SOURCES = frozenset({"stories", "tickets", "sprint-status"})
_NON_ENGINE_WORKERS = frozenset(
    {"stage", "verify", "tracking", "integrate", "queue_verify", "verify_existing"}
)
_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "campaign_id",
        "unit_id",
        "launch_id",
        "attempt",
        "generation",
        "run_id",
        "workspace",
        "branch",
        "common_dir",
        "execution",
        "digest",
        "config_revision",
        "task_selector",
        "source",
        "source_revision",
        "unit_digest",
    }
)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def binding(request, envelope) -> dict[str, Any]:
    """Freeze launch, exact task selector, source and executable configuration.

    Mutable task phase/counters are deliberately not launch identity. Their
    snapshot may be retained by ``context``; continuation uses its own task fence.
    """
    source, unit = request.get("source"), request.get("unit")
    if (
        not isinstance(source, dict)
        or source.get("kind") not in _ENGINE_SOURCES
        or not isinstance(unit, dict)
        or not unit.get("selector")
        or not isinstance(request.get("source_revision"), str)
    ):
        raise CampaignError("Git activity source/task authority is unknown")
    result = {
        key: request.get(key)
        for key in (
            "campaign_id",
            "unit_id",
            "launch_id",
            "attempt",
            "generation",
            "run_id",
            "workspace",
            "branch",
            "common_dir",
            "execution",
            "digest",
        )
    }
    if any(value is None or value == "" for value in result.values()):
        raise CampaignError("Git activity launch binding is incomplete")
    if any(type(result[key]) is not int for key in ("attempt", "generation")):
        raise CampaignError("Git activity launch counters are not typed integers")
    if (
        any(
            result[key] != getattr(envelope, key)
            for key in ("campaign_id", "unit_id", "attempt", "generation")
        )
        or unit.get("id") != envelope.unit_id
    ):
        raise CampaignError("Git activity launch differs from execution authority")
    if Path(result["execution"]).resolve() != envelope.directory.resolve() / "execution.json":
        raise CampaignError("Git activity execution path differs from its frozen envelope")
    result.update(
        schema_version=1,
        config_revision=envelope.config_revision,
        task_selector=unit["selector"],
        source=source,
        source_revision=request["source_revision"],
        unit_digest=_digest(unit),
    )
    _validate_binding(result)
    return json.loads(json.dumps(result, sort_keys=True, allow_nan=False))


def context(bound, task=None) -> dict[str, Any]:
    """Value for finalization recovery_context['campaign_git_activity']."""
    _validate_binding(bound)
    if task is not None and task.story_key != bound["task_selector"]:
        raise CampaignError("Git activity task differs from its launch selector")
    return {
        "schema_version": 1,
        "binding": json.loads(json.dumps(bound, sort_keys=True)),
        "task_digest": _digest(task.to_dict()) if task is not None else None,
    }


def _validate_binding(bound) -> None:
    if not isinstance(bound, dict) or set(bound) != _BINDING_FIELDS:
        raise CampaignError("Git activity launch/source binding is unknown")
    if bound["schema_version"] != 1 or type(bound["schema_version"]) is not int:
        raise CampaignError("Git activity launch binding version is unsupported")
    for key in _BINDING_FIELDS - {
        "schema_version",
        "source",
        "attempt",
        "generation",
        "config_revision",
        "source_revision",
    }:
        if not isinstance(bound[key], str) or not bound[key] or "\0" in bound[key]:
            raise CampaignError("Git activity launch/source binding is unknown")
    if any(
        type(bound[k]) is not int or bound[k] < 1
        for k in ("attempt", "generation", "config_revision")
    ):
        raise CampaignError("Git activity launch counters are unknown")
    if not isinstance(bound["source_revision"], str) or "\0" in bound["source_revision"]:
        raise CampaignError("Git activity source revision is not explicitly typed")
    if not isinstance(bound["source"], dict) or bound["source"].get("kind") not in _ENGINE_SOURCES:
        raise CampaignError("Git activity source authority is unknown")
    if any(not Path(bound[k]).is_absolute() for k in ("workspace", "common_dir", "execution")):
        raise CampaignError("Git activity launch paths are not absolute")
    if any(
        len(bound[k]) != 64 or any(c not in "0123456789abcdef" for c in bound[k])
        for k in ("digest", "unit_digest")
    ):
        raise CampaignError("Git activity configuration/source digest is unknown")


def worker_receipt(request, envelope, *, required: bool = False) -> dict[str, Any]:
    """Optional terminal metadata without replacing the worker's real outcome.

    Non-Engine workers do not acquire Engine finalization authority from an
    ``explicit`` selector or an empty tracking selector. Their unavailable
    metadata is not a quiescence proof. A typed finalization pause always makes
    the check applicable, including contradictory or missing worker metadata.
    First execution keeps an explicitly empty source revision exactly as frozen;
    it does not invent a semantic revision or approval.
    """
    kind = request.get("kind")
    if kind in _NON_ENGINE_WORKERS and required is False:
        return {
            "schema_version": 1,
            "state": "unavailable",
            "applicable": False,
            "code": "not_engine_finalization",
        }
    if kind not in {"execution", "recovery"}:
        return {
            "schema_version": 1,
            "state": "unavailable",
            "applicable": True,
            "code": "finalization_producer_unknown",
        }
    try:
        bound = binding(request, envelope)
        sealed = seal(Path(request["workspace"]), bound, required=required)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return {
            "schema_version": 1,
            "state": "unavailable",
            "applicable": True,
            "code": "finalization_authority_unavailable",
            "reason": str(exc) if type(exc) is CampaignError else type(exc).__name__,
        }
    return {"schema_version": 1, "state": "available", "applicable": True, "activity": sealed}


def _bytes(path: Path, maximum: int = MAX_RECORD_BYTES) -> bytes:
    """Bound bytes and reject redirected/nonregular/racing authority files."""
    if is_link_like(path) or is_link_like(path.parent):
        raise CampaignError("Git activity authority is redirected")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise CampaignError("Git activity authority is not a bounded regular file")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    current = path.stat()

    def identity(item):
        return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)

    if (
        len(data) > maximum
        or identity(before) != identity(after)
        or identity(after) != identity(current)
    ):
        raise CampaignError("Git activity authority changed during observation")
    return data


def _read(path: Path) -> tuple[dict[str, Any], str]:
    data = _bytes(path)
    value = json.loads(data)
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise CampaignError("Git activity authority version is unsupported")
    return value, hashlib.sha256(data).hexdigest()


def _workspace_git_dir(workspace: Path) -> Path:
    marker = workspace / ".git"
    if is_link_like(marker):
        raise CampaignError("Git activity workspace Git pointer is redirected")
    if stat.S_ISDIR(marker.lstat().st_mode):
        return marker.resolve()
    text = _bytes(marker, 8192).decode("utf-8").rstrip("\n")
    if not text.startswith("gitdir: ") or "\n" in text or "\0" in text:
        raise CampaignError("Git activity workspace Git pointer is invalid")
    return (workspace / text[len("gitdir: ") :]).resolve()


def _inode(path: Path) -> list[int]:
    if is_link_like(path):
        raise CampaignError("Git activity subject is redirected")
    value = path.stat()
    return [value.st_dev, value.st_ino]


def _subject(workspace: Path, *, common_dir, branch) -> dict[str, Any]:
    """Actual checkout authority, independent of any Engine source/task binding."""
    if (
        not workspace.is_absolute()
        or not Path(common_dir).is_absolute()
        or not isinstance(branch, str)
        or not branch
        or "\0" in branch
    ):
        raise CampaignError("Git activity checkout authority is incomplete")
    repo_identity = _inode(workspace)
    common = Path(common_dir).resolve()
    git_dir = _workspace_git_dir(workspace)
    if git_dir != common and git_dir.parent != common / "worktrees":
        raise CampaignError("Git activity Git directory escaped the launch repository")
    git_identity = _inode(git_dir)
    if _bytes(git_dir / "HEAD", 1024).decode("utf-8").strip() != "ref: refs/heads/" + branch:
        raise CampaignError("Git activity current branch differs from its launch")
    return {
        "workspace": str(workspace.resolve()),
        "common_dir": str(common),
        "branch": branch,
        "repo_identity": repo_identity,
        "git_dir": str(git_dir),
        "git_dir_identity": git_identity,
    }


def _current(
    workspace: Path, bound, *, absence_only=False
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if str(workspace.resolve()) != bound["workspace"]:
        raise CampaignError("Git activity workspace differs from launch")
    _subject(workspace, common_dir=bound["common_dir"], branch=bound["branch"])
    root = finalization_recovery._root(workspace)
    try:
        (root / "active.json").lstat()
    except FileNotFoundError:
        return None, None
    pointer, pointer_digest = _read(root / "active.json")
    operation = pointer.get("id")
    if (
        not isinstance(operation, str)
        or len(operation) != 32
        or any(c not in "0123456789abcdef" for c in operation)
    ):
        raise CampaignError("Git activity operation identity is invalid")
    path = root / operation / "record.json"
    record, record_digest = _read(path)
    if (
        record.get("id") != operation
        or record.get("repo") != str(workspace.resolve())
        or record.get("repo_identity") != _inode(workspace)
        or not isinstance(record.get("git_dir"), str)
        or record.get("git_dir_identity") != _inode(Path(record["git_dir"]))
        or record.get("branch_ref") != "refs/heads/" + bound["branch"]
    ):
        raise CampaignError("Git activity record belongs to another subject")
    # The Git directory may be the common directory or one exact linked-worktree
    # child. Never accept an arbitrary external directory with a copied inode.
    common = Path(bound["common_dir"]).resolve()
    git_dir = Path(record["git_dir"]).resolve()
    if git_dir != common and git_dir.parent != common / "worktrees":
        raise CampaignError("Git activity Git directory escaped the launch repository")
    if git_dir != _workspace_git_dir(workspace):
        raise CampaignError("Git activity record names another worktree Git directory")
    if (
        _bytes(git_dir / "HEAD", 1024).decode("utf-8").strip()
        != "ref: refs/heads/" + bound["branch"]
    ):
        raise CampaignError("Git activity current branch differs from its launch")
    witness = {
        "id": operation,
        "path": str(path),
        "sha256": record_digest,
        "pointer_sha256": pointer_digest,
    }
    settled = (record.get("state") == "completed" and bool(record.get("committed_head"))) or (
        record.get("state") == "refused" and record.get("refusal_acknowledged") is True
    )
    lease = record.get("index_lock")
    if settled and lease is not None:
        expected = str(
            Path(record["index_path"]).with_name(Path(record["index_path"]).name + ".lock")
        )
        if not isinstance(lease, dict) or lease.get("path") != expected:
            raise CampaignError("Git activity index lease is invalid")
        settled = lease.get("state") == "release-authorized"
        try:
            Path(expected).lstat()
        except FileNotFoundError:
            pass
        else:
            settled = False
    if settled:
        # A settled earlier operation need not belong to the current launch.
        # It still cannot hide incomplete process evidence or an in-flight write.
        if _actors(record)[1]:
            raise CampaignError("Settled Git activity lacks complete actor cleanup")
        return None, witness
    if absence_only:
        raise CampaignError("Pending Git finalization prevents an absence-only proof")
    authority = (record.get("context") or {}).get(_CONTEXT)
    if (
        not isinstance(authority, dict)
        or set(authority) != {"schema_version", "binding", "task_digest"}
        or authority.get("schema_version") != 1
        or authority.get("binding") != bound
        or authority.get("task_digest") is not None
        and (not isinstance(authority["task_digest"], str) or len(authority["task_digest"]) != 64)
    ):
        raise CampaignError("Pending Git activity has missing or foreign typed authority")
    return record, witness


def _absence(workspace, subject) -> dict[str, Any]:
    before = _subject(workspace, common_dir=subject["common_dir"], branch=subject["branch"])
    _record, witness = _current(workspace, subject, absence_only=True)
    after = _subject(workspace, common_dir=subject["common_dir"], branch=subject["branch"])
    if before != after:
        raise CampaignError("Git activity checkout changed during absence observation")
    return {"schema_version": 1, "mode": "absence-only", "subject": after, "record": witness}


def seal(workspace: Path, bound, *, required: bool = False) -> dict[str, Any]:
    """Seal exact on-disk evidence; a pending record never borrows prose authority.

    ``required`` is selected from the typed finalization pause producer. Missing
    operation data for that producer cannot become an ordinary no-debt receipt.
    """
    _validate_binding(bound)
    if type(required) is not bool:
        raise CampaignError("Git activity launch/source binding is unknown")
    record, witness = _current(workspace, bound)
    if required and record is None:
        raise CampaignError("Typed finalization pause has no exact pending operation")
    body = {
        "schema_version": 1,
        "binding": bound,
        "record": witness,
        "pending": record is not None,
        "required": required,
    }
    return {**body, "id": _digest(body)}


def validate_release(workspace: Path, authority: dict, observation: dict, *, required=False) -> None:
    """Recheck a controller-created observation before committing claim release.

    Recorded process death is monotonic for its birth identity. This validates
    the unchanged authority file, not an external assertion of process death.
    """
    if observation.get("state") != "quiescent":
        raise CampaignError("Git activity has no completed quiescence observation")
    evidence = observation.get("evidence", {})
    if authority.get("mode") == "absence-only":
        if required or _absence(workspace, authority) != evidence.get("absence"):
            raise CampaignError("Git finalization absence evidence changed before release")
    elif seal(workspace, authority, required=required) != evidence.get("receipt"):
        raise CampaignError("Git activity authority changed before claim release")


def _actors(record) -> tuple[tuple[ActorEvidence, ...], bool]:
    """Authenticate complete capture facts before considering fresh liveness."""
    if any(
        record.get(key)
        for key in (
            "stage_observation_pending",
            "commit_observation_pending",
            "restore_writer_pending",
        )
    ):
        raise CampaignError("Git activity command observation is incomplete")
    commands = record.get("commands")
    if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_COMMANDS:
        raise CampaignError("Git activity has no bounded command ownership evidence")
    actors: dict[tuple[int, float | None], ActorEvidence] = {}
    uncertain = False
    for command in commands:
        capture = command.get("capture") if isinstance(command, dict) else None
        raw = capture.get("ownership") if isinstance(capture, dict) else None
        if not isinstance(raw, dict) or command.get("known") is not True:
            raise CampaignError("Git activity lacks typed stream/ownership evidence")
        fields = {field.name for field in dataclasses.fields(CaptureOwnership)}
        if set(raw) != fields | {"quiescent"}:
            raise CampaignError("Git activity ownership schema is unsupported")
        for key in ("root_reaped", "census_complete", "stdout_eof", "stderr_eof", "io_settled"):
            if raw.get(key) is not True:
                raise CampaignError("Git activity coverage or streams remain unsettled")
        if (
            raw.get("problems") != []
            or type(raw.get("census_count")) is not int
            or raw["census_count"] < 0
        ):
            raise CampaignError("Git activity census has unresolved observation faults")
        rows = raw.get("actors")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ACTORS:
            raise CampaignError("Git activity actor inventory is incomplete or unbounded")
        parsed = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {field.name for field in dataclasses.fields(ActorEvidence)}
                or type(row.get("pid")) is not int
                or row["pid"] <= 0
                or (
                    row.get("identity") is not None
                    and (
                        type(row["identity"]) not in (int, float)
                        or not math.isfinite(row["identity"])
                    )
                )
                or row.get("state") not in {"alive", "dead", "unknown"}
                or type(row.get("terminate_sent")) is not bool
                or type(row.get("kill_sent")) is not bool
            ):
                raise CampaignError("Git activity actor identity is unknown")
            if row["identity"] is None and not (
                row["pid"] == raw["root_pid"] and row["state"] == "dead"
            ):
                raise CampaignError("Git activity descendant has no birth identity")
            actor = ActorEvidence(**row)
            parsed.append(actor)
            key = (actor.pid, actor.identity)
            # Retain a non-dead observation if another command saw the same
            # stamped actor alive; only fresh observation can settle disagreement.
            if key not in actors or actor.state != "dead":
                actors[key] = actor
        if type(raw["root_pid"]) is not int or (
            raw["root_identity"] is not None and type(raw["root_identity"]) not in (int, float)
        ):
            raise CampaignError("Git activity root identity is unknown")
        if not any(
            row.pid == raw["root_pid"]
            and row.identity == raw["root_identity"]
            and row.state == "dead"
            for row in parsed
        ):
            raise CampaignError("Git activity root lacks exact stopped ownership")
        ownership = CaptureOwnership(
            **{**{k: raw[k] for k in fields}, "actors": tuple(parsed), "problems": ()}
        )
        if (
            raw["quiescent"] is not ownership.quiescent
            or command.get("quiescent") is not ownership.quiescent
        ):
            raise CampaignError("Git activity cleanup summary contradicts its actors")
        uncertain |= not ownership.quiescent
        if len(actors) > MAX_ACTORS:
            raise CampaignError("Git activity aggregate actor inventory exceeds its bound")
    return tuple(actors.values()), uncertain


@dataclasses.dataclass(frozen=True)
class GitActivityObservation:
    state: str  # quiescent | waiting | unknown; only this additional Git debt
    effect: str  # none | unknown | settled; never permission to clear a hold
    reason: str
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _Probe:
    key: str
    done: threading.Event = dataclasses.field(default_factory=threading.Event)
    stopped: threading.Event = dataclasses.field(default_factory=threading.Event)
    result: GitActivityObservation | None = None


class GitActivityObserver:
    """At most one outstanding, read-only host probe per long-lived observer.

    A global finite admission guard also covers coordinator-object replacement.
    A stuck OS call retains its slot, never producing a new thread every tick.
    ``close`` fences further calls; an already-running OS call cannot be canceled.
    File reads are bounded regular-file reads, not a hard OS filesystem deadline.
    """

    def __init__(self, *, host=None, wait_s: float = 0.05):
        if not math.isfinite(wait_s) or not 0 <= wait_s <= 1:
            raise ValueError("Git activity wait must be between zero and one second")
        self.host = host
        self.wait_s = wait_s
        self._probe: _Probe | None = None
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._probe:
                self._probe.stopped.set()

    def _evaluate(self, workspace, bound, receipt, required, probe, *, absence=False):
        try:
            if absence:
                proof = _absence(workspace, bound)
                return GitActivityObservation(
                    "quiescent",
                    "none",
                    "No pending Git finalization",
                    {"absence": proof, "mode": "absence-only"},
                )
            current = seal(workspace, bound, required=required)
            if receipt is not None and receipt != current:
                raise CampaignError("Git activity receipt differs from exact stored evidence")
            record, witness = _current(workspace, bound)
            if current["record"] != witness or current["pending"] != (record is not None):
                raise CampaignError("Git activity changed during observation")
            if record is None:
                return GitActivityObservation(
                    "quiescent", "none", "No pending Git activity", {"receipt": current}
                )
            actors, uncertain = _actors(record)
            states = []
            host = (self.host or get_process_host()) if uncertain else None
            for actor in actors:
                if probe.stopped.is_set():
                    raise CampaignError("Git activity observation was closed")
                state = actor.state
                if state != "dead":
                    assert host is not None
                    state = host.liveness_of(actor.pid, actor.identity)
                if state not in {"alive", "dead", "unknown"}:
                    raise CampaignError("Git actor liveness returned an invalid state")
                states.append({"pid": actor.pid, "identity": actor.identity, "state": state})
            if seal(workspace, bound, required=required) != current:
                raise CampaignError("Git activity record changed during actor observation")
            effect = "settled" if record.get("state") in {"completed", "refused"} else "unknown"
            state = "quiescent" if all(row["state"] == "dead" for row in states) else "waiting"
            return GitActivityObservation(
                state,
                effect,
                "Recorded Git actors stopped"
                if state == "quiescent"
                else "Recorded Git actors remain alive or unknown",
                {
                    "receipt": current,
                    "actors": states,
                    "scope": "recorded observed tree; not universal daemon containment",
                },
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            # Raw process or filesystem exceptions can contain sensitive data.
            reason = (
                str(exc)
                if type(exc) is CampaignError
                else "Git activity observation failed: " + type(exc).__name__
            )
            return GitActivityObservation("unknown", "unknown", reason)

    def observe(
        self, workspace: Path, bound, receipt=None, *, required: bool = False
    ) -> GitActivityObservation:
        """Wait briefly; every non-quiescent result retains the writer reservation."""
        return self._observe(workspace, bound, receipt, required=required)

    def observe_absence(
        self, workspace: Path, *, common_dir, branch: str
    ) -> GitActivityObservation:
        """Prove only absence of pending finalization, never general quiescence.

        Non-Engine and historical explicit-source workers can use this additional
        gate alongside independently proven worker/native/mux termination. Every
        pending, foreign, unreadable or incomplete operation remains unknown.
        A required typed finalization pause must use ``observe``, not this gate.
        """
        subject = {
            "workspace": str(workspace.resolve()),
            "common_dir": str(common_dir),
            "branch": branch,
        }
        return self._observe(workspace, subject, None, required=False, absence=True)

    def _observe(
        self, workspace, bound, receipt, *, required, absence=False
    ) -> GitActivityObservation:
        key = _digest(
            {
                "workspace": str(workspace),
                "binding": bound,
                "receipt": receipt,
                "required": required,
                "absence": absence,
            }
        )
        bound_copy = json.loads(json.dumps(bound))
        receipt_copy = json.loads(json.dumps(receipt))
        with self._lock:
            if self._closed:
                return GitActivityObservation(
                    "unknown", "unknown", "Git activity observer is closed"
                )
            probe = self._probe
            if probe is not None and probe.key != key:
                if not probe.done.is_set():
                    return GitActivityObservation(
                        "unknown", "unknown", "Git activity observer has an outstanding query"
                    )
                self._probe = probe = None
            if probe is None:
                slots = _PROBE_SLOTS
                if not slots.acquire(blocking=False):
                    return GitActivityObservation(
                        "unknown", "unknown", "Git activity observation capacity is unavailable"
                    )
                probe = _Probe(key)
                self._probe = probe

                # Freeze caller-owned mappings before crossing the thread seam.
                def run():
                    try:
                        probe.result = self._evaluate(
                            workspace, bound_copy, receipt_copy, required, probe, absence=absence
                        )
                    except Exception as exc:
                        probe.result = GitActivityObservation(
                            "unknown", "unknown", "Git activity query failed: " + type(exc).__name__
                        )
                    finally:
                        slots.release()
                        probe.done.set()

                try:
                    threading.Thread(target=run, daemon=True, name="bmad-git-activity").start()
                except BaseException:
                    self._probe = None
                    slots.release()
                    raise
        if not probe.done.wait(self.wait_s):
            return GitActivityObservation(
                "unknown", "unknown", "Git activity ownership query is still pending"
            )
        with self._lock:
            if self._probe is probe:
                self._probe = None
            if self._closed or probe.result is None:
                return GitActivityObservation(
                    "unknown", "unknown", "Git activity observation did not finish"
                )
        result = probe.result
        # A completed slow query may be consumed on a later tick. Revalidate the
        # bounded exact record before returning its old liveness observation.
        if result.state == "quiescent":
            try:
                if absence:
                    current = _absence(workspace, bound)
                    expected = result.evidence["absence"]
                else:
                    current = seal(workspace, bound, required=required)
                    expected = result.evidence["receipt"]
                if current != expected:
                    raise CampaignError("Git activity changed after actor observation")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                return GitActivityObservation(
                    "unknown", "unknown", "Git activity proof is stale: " + type(exc).__name__
                )
        return result
