"""Opt-in, request-bound TeamRouter inspection refusals; never stderr authority.

The producer stays alive until the worker independently observes its exact
owned process chain. An ACK and producer confirmation bind that observation to
one private transcript. This is not an authentication boundary against another
malicious process running as the owner. No model or tool receives the locator.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any

from . import envvars
from .campaign_model import CampaignError

CONTRACT = "teamrouter-codex-inspection-auth-v1"
MAX_BYTES = 4096
WAIT_MS = 2000
PINS = (
    "producer",
    "producer_helper",
    "producer_executable",
    "carrier",
    "carrier_entry",
    "carrier_helper",
)
BINDING = ("job_id", "launch_id", "generation", "request_digest", "capability_digest", "nonce")
REFUSAL_FIXED = {
    "schema_version": 1,
    "outcome": "authentication",
    "code": "CANONICAL_REFRESH_QUARANTINED",
    "stage": "canonical-access-before-native",
    "mode": "hcom-hook-inspection",
    "exit_code": 3,
    "native_worker_started": False,
}


def _error(message):
    return CampaignError("Prelaunch authentication channel: " + message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _identity(value):
    return type(value) in (int, float) and math.isfinite(value) and value != 0


def _pid(value):
    return type(value) is int and value > 0


def _hex(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def validate(capability, *, verify_files=True):
    """New opt-in recipes pin the producer, carrier and consuming implementation."""
    if "prelaunch_failure" not in capability:
        return
    spec = capability["prelaunch_failure"]
    if (
        not isinstance(spec, dict)
        or set(spec) != {"contract", *PINS}
        or spec["contract"] != CONTRACT
        or capability.get("platform") != "darwin"
        or capability.get("protocol") not in {"codex-app-server", "codex-native-review"}
    ):
        raise _error("unsupported capability contract")
    dependencies = {row["path"]: row["sha256"] for row in capability.get("dependencies", [])}
    from .campaign_headless import file_digest

    for name in PINS:
        pin = spec[name]
        if (
            not isinstance(pin, dict)
            or set(pin) != {"path", "sha256"}
            or not isinstance(pin["path"], str)
            or not Path(pin["path"]).is_absolute()
            or not _hex(pin["sha256"])
            or dependencies.get(pin["path"]) != pin["sha256"]
        ):
            raise _error("missing exact carrier/producer dependency")
        if verify_files and (
            Path(pin["path"]).resolve() != Path(pin["path"])
            or file_digest(Path(pin["path"])) != pin["sha256"]
        ):
            raise _error("carrier/producer dependency changed")
    argv = capability.get("argv", [])
    if argv.count("--native") != 1 or argv[argv.index("--native") + 1 :][:1] != [
        spec["producer"]["path"]
    ]:
        raise _error("producer does not match native recipe")
    for name in ("campaign_prelaunch_failure.py", "campaign_headless_worker.py"):
        path = Path(__file__).resolve().with_name(name)
        if str(path) not in dependencies or (
            verify_files and file_digest(path) != dependencies[str(path)]
        ):
            raise _error("consumer implementation lacks exact dependency")
    if verify_files:
        from .campaign_codex_appserver import validate_recipe

        validate_recipe(capability)


def _directory(path):
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or path.resolve() != path
    ):
        raise _error("private directory is redirected or unprotected")


def _read(path):
    _directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise _error("record is not an owner-private regular file")
        data = os.read(fd, MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise _error("record exceeds byte limit")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise _error("duplicate JSON key")
                result[key] = value
            return result

        value = json.loads(data, object_pairs_hook=pairs)
        if not isinstance(value, dict):
            raise _error("record is not an object")
        return value, _sha(data)
    finally:
        os.close(fd)


def _publish(path, value):
    """Atomic exclusive publication: readers never see a partially written file."""
    _directory(path.parent)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_BYTES:
        raise _error("record exceeds byte limit")
    temporary = path.parent / (".publish-" + secrets.token_hex(16))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)
    return _sha(data)


def observe_chain(pid, root_pid, root_identity, spec, host):
    """Independently read the live inspection -> hcom -> launcher -> recipe chain."""
    import psutil

    try:
        return _observe_chain(pid, root_pid, root_identity, spec, host, psutil)
    except psutil.Error as exc:
        raise _error("producer process identity is unreadable") from exc


def _observe_chain(pid, root_pid, root_identity, spec, host, psutil):

    owned = host.descendants(root_pid)
    owned[root_pid] = root_identity
    chain = []
    cursor = pid
    for _ in range(4):
        stamp = owned.get(cursor)
        if not _pid(cursor) or not _identity(stamp) or host.liveness_of(cursor, stamp) != "alive":
            raise _error("producer ancestry is not live and owned")
        process = psutil.Process(cursor)
        parent = process.ppid()
        executable = str(Path(process.exe()).resolve())
        argv = process.cmdline()
        if host.identity(cursor) != stamp or process.ppid() != parent:
            raise _error("producer ancestry changed during observation")
        chain.append(
            {
                "pid": cursor,
                "identity": stamp,
                "ppid": parent,
                "executable": executable,
                "argv": argv,
            }
        )
        if cursor == root_pid:
            break
        cursor = parent
    if len(chain) != 4 or chain[-1]["pid"] != root_pid:
        raise _error("unexpected inspection ancestry")
    for row in (chain[0], chain[2]):
        argv = row["argv"]
        if (
            row["executable"] != spec["producer_executable"]["path"]
            or len(argv) < 5
            or str(Path(argv[1]).resolve()) != spec["producer"]["path"]
            or argv[-3:] != ["app-server", "--listen", "stdio://"]
        ):
            raise _error("producer image or script does not match pinned recipe")
    if chain[1]["executable"] != spec["carrier"]["path"]:
        raise _error("inspection parent is not the pinned carrier")
    if chain[1]["argv"][1:7] != ["codex", "--stdio", "--run-here", "--go", "--", "app-server"]:
        raise _error("carrier command does not match the foreground pipe recipe")
    return [
        {
            "pid": row["pid"],
            "identity": row["identity"],
            "ppid": row["ppid"],
            "executable": row["executable"],
            "argv_sha256": _sha(json.dumps(row["argv"]).encode()),
        }
        for row in chain
    ]


class Channel:
    def __init__(
        self, directory, envelope, consumer_identity, *, clock=time.time, remaining_s=None
    ):
        self.directory = directory / "prelaunch-failure"
        self.directory.mkdir(mode=0o700)
        _directory(self.directory)
        self.capability = envelope["capability"]
        self.spec = self.capability["prelaunch_failure"]
        self.clock = clock
        self.job_deadline = clock() + (
            envelope["request"]["limits"]["timeout_s"] if remaining_s is None else remaining_s
        )
        self.context = {
            "schema_version": 1,
            "contract": CONTRACT,
            "job_id": envelope["request"]["id"],
            "launch_id": envelope["launch_id"],
            "generation": envelope["generation"],
            "request_digest": envelope["request_digest"],
            "capability_digest": envelope["capability_digest"],
            "nonce": secrets.token_hex(32),
            "consumer_pid": os.getpid(),
            "consumer_identity": consumer_identity,
        }
        if not _identity(consumer_identity):
            raise _error("consumer identity unavailable")
        self.context_sha = _publish(self.directory / "context.json", self.context)
        self.accepted = None
        self.confirmed = None
        self.fault = None
        self.proof: dict[str, Any] | None = None
        self.proof_started = False
        self.proof_refusal_sha = None
        self.proof_done = threading.Event()

    def _start_proof(self, refusal, root_pid, root_identity, host):
        """One read-only daemon; no writes, ACKs, signals or waiting in the loop."""
        self.proof_started = True

        def read_proof():
            try:
                from .campaign_headless import file_digest

                # Full recipe validation already ran before launch. Recheck only
                # the producer/carrier bytes whose live identity is observed.
                for name in PINS:
                    pin = self.spec[name]
                    if file_digest(Path(pin["path"])) != pin["sha256"]:
                        raise _error("observed producer/carrier bytes changed")
                actors = observe_chain(
                    refusal["producer_pid"], root_pid, root_identity, self.spec, host
                )
                if any(host.liveness_of(row["pid"], row["identity"]) != "alive" for row in actors):
                    raise _error("producer ancestry ended during observation")
                self.proof = {"actors": actors}
            except Exception as exc:  # Read-only observation fails closed, never authority.
                self.proof = {"error": f"{type(exc).__name__}: {exc}"}
            finally:
                self.proof_done.set()

        threading.Thread(target=read_proof, name="prelaunch-identity-proof", daemon=True).start()

    def environment(self):
        return {envvars.TEAMROUTER_PREFLIGHT_CONTEXT: str(self.directory / "context.json")}

    def _current_context(self):
        context, sha = _read(self.directory / "context.json")
        if sha != self.context_sha or context != self.context:
            raise _error("context changed")

    def _deadline(self, refusal):
        issued, expires = refusal["issued_at_unix_ms"], refusal["expires_at_unix_ms"]
        now = self.clock() * 1000
        if (
            type(issued) is not int
            or type(expires) is not int
            or not 0 < expires - issued <= WAIT_MS
            or not issued <= now < expires
            or expires > self.job_deadline * 1000
        ):
            raise _error("refusal handshake deadline expired or invalid")

    def poll(self, root_pid, root_identity, host, known, persist):
        """One bounded observation in the existing worker loop, never a retry."""
        if self.fault or self.confirmed:
            return
        try:
            self._poll(root_pid, root_identity, host, known, persist)
        except (OSError, ValueError, TypeError, KeyError, CampaignError) as exc:
            self.fault = f"{type(exc).__name__}: {exc}"

    def _poll(self, root_pid, root_identity, host, known, persist):
        if not (self.directory / "refusal.json").exists():
            return
        self._current_context()
        refusal, refusal_sha = _read(self.directory / "refusal.json")
        required = {
            *REFUSAL_FIXED,
            *BINDING,
            "context_sha256",
            "producer_pid",
            "producer_ppid",
            "issued_at_unix_ms",
            "expires_at_unix_ms",
        }
        if (
            set(refusal) != required
            or any(
                type(refusal[key]) is not type(value) or refusal[key] != value
                for key, value in REFUSAL_FIXED.items()
            )
            or any(
                type(refusal[key]) is not type(self.context[key])
                or refusal[key] != self.context[key]
                for key in BINDING
            )
            or refusal["context_sha256"] != self.context_sha
            or not _pid(refusal["producer_pid"])
            or not _pid(refusal["producer_ppid"])
        ):
            raise _error("refusal does not bind this exact attempt")
        self._deadline(refusal)
        if self.accepted is None:
            if not self.proof_started:
                self.proof_refusal_sha = refusal_sha
                self._start_proof(refusal, root_pid, root_identity, host)
            if refusal_sha != self.proof_refusal_sha:
                raise _error("refusal changed during identity observation")
            if not self.proof_done.is_set():
                return
            if self.proof is None or "error" in self.proof:
                raise _error((self.proof or {}).get("error", "identity proof unavailable"))
            actors = self.proof["actors"]
            if actors[0]["ppid"] != refusal["producer_ppid"]:
                raise _error("producer parent changed")
            self._deadline(refusal)
            known.update({row["pid"]: row["identity"] for row in actors})
            persist()  # Persist every observed actor BEFORE permitting producer exit.
            self._deadline(refusal)
            ack = {
                "schema_version": 1,
                "context_sha256": self.context_sha,
                "refusal_sha256": refusal_sha,
                "producer_pid": refusal["producer_pid"],
                "producer_identity": actors[0]["identity"],
                "consumer_pid": self.context["consumer_pid"],
                "consumer_identity": self.context["consumer_identity"],
            }
            ack_sha = _publish(self.directory / "ack.json", ack)
            self.accepted = {
                "refusal": refusal,
                "refusal_sha256": refusal_sha,
                "ack": ack,
                "ack_sha256": ack_sha,
                "actors": actors,
            }
        if refusal_sha != self.accepted["refusal_sha256"]:
            raise _error("refusal changed after ACK")
        if (self.directory / "confirmed.json").exists():
            confirmed, confirmed_sha = _read(self.directory / "confirmed.json")
            expected = {
                "schema_version": 1,
                "context_sha256": self.context_sha,
                "refusal_sha256": refusal_sha,
                "ack_sha256": self.accepted["ack_sha256"],
                "producer_pid": refusal["producer_pid"],
            }
            ack, ack_sha = _read(self.directory / "ack.json")
            if (
                confirmed != expected
                or any(type(confirmed[key]) is not type(value) for key, value in expected.items())
                or ack != self.accepted["ack"]
                or ack_sha != self.accepted["ack_sha256"]
            ):
                raise _error("producer confirmation does not bind the ACK")
            self._deadline(refusal)
            self.confirmed = {
                "value": confirmed,
                "sha256": confirmed_sha,
                "observed_at": self.clock(),
            }

    def finish(self, receipt, *, returncode, stdout_bytes, ordinary_terminal):
        """Classification never supplies completion, usage or missing lifecycle proof."""
        if self.fault:
            receipt["prelaunch_failure_error"] = self.fault
        if not self.confirmed or not self.accepted or self.fault:
            return
        try:
            self._current_context()
            for name, expected in (
                ("refusal", self.accepted["refusal_sha256"]),
                ("ack", self.accepted["ack_sha256"]),
                ("confirmed", self.confirmed["sha256"]),
            ):
                if _read(self.directory / (name + ".json"))[1] != expected:
                    raise _error("confirmed transcript changed before receipt")
            # The inspection launcher declares exit3. hcom propagates inspection
            # EOF as an error; the pinned Python app-server recipe exits1.
            if not (
                ordinary_terminal
                and returncode == 1
                and stdout_bytes == 0
                and receipt.get("processes_quiescent") is True
                and receipt.get("outcome") == "failed"
            ):
                raise _error("terminal evidence cannot admit prelaunch authentication")
        except (OSError, ValueError, TypeError, CampaignError) as exc:
            receipt["prelaunch_failure_error"] = f"{type(exc).__name__}: {exc}"
            return
        receipt.update(
            outcome="authentication",
            reason="CANONICAL_REFRESH_QUARANTINED: owner sign-in required (verified prelaunch inspection)",
            usage={},
            input_complete=False,
            prelaunch_failure={
                "contract": CONTRACT,
                "context_sha256": self.context_sha,
                "refusal_sha256": self.accepted["refusal_sha256"],
                "ack_sha256": self.accepted["ack_sha256"],
                "confirmed_sha256": self.confirmed["sha256"],
                "actors": self.accepted["actors"],
                "confirmed_observed_at": self.confirmed["observed_at"],
            },
        )
