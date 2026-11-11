"""Seal an owned headless launch without treating stream output as a conversation.

The exact request, delivered input, native output and terminal receipt remain
available as private immutable blobs. Injected prompts/context are controller
input, never authenticated owner messages. Missing streams and partial frames
remain visible coverage facts; they are not silently invented native history.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path

from .campaign_model import CampaignError
from .campaign_transcripts import (
    blob,
    digest,
    encoded,
    record_lines,
    seal_source,
    snapshot_history,
    stream_blob,
    stream_hash,
)
from .platform_util import is_link_like

PREFIX_VERSION = 1


MAX_RECORD_BYTES = 64 * 1024 * 1024


def _published_prefix(directory, stdout):
    """Verify the prior source prefix and its immutable normalized archive."""
    marker = directory / "history-prefix.json"
    if not marker.exists() and not is_link_like(marker):
        return None
    value = json.loads(_read(marker, maximum=1024 * 1024))
    if value.get("schema_version") != PREFIX_VERSION:
        raise CampaignError("Managed live prefix has an unsupported schema")
    size = value.get("stdout_bytes")
    if (
        type(size) is not int
        or not 0 <= size <= stdout.stat().st_size
        or stream_hash(stdout, size) != value.get("stdout_sha256")
    ):
        raise CampaignError("Managed live prefix was truncated or rewritten")
    sha = value.get("events_sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise CampaignError("Managed live event archive has an invalid identity")
    target = directory / "history-prefixes" / (sha + ".jsonl")
    if stream_hash(target) != sha:
        raise CampaignError("Managed live event archive changed")
    return value, target


def _same_prefix(previous, current):
    return previous.stat().st_size <= current.stat().st_size and stream_hash(
        current, previous.stat().st_size
    ) == stream_hash(previous)


def _publish_live(directory, exported, raw, file_identity, request_sha256):
    from .platform_util import atomic_write_text

    previous = _published_prefix(directory, raw)
    if previous:
        value, old = previous
        if (
            value.get("request_sha256") != request_sha256
            or value.get("file_identity") != file_identity
            or not _same_prefix(old, exported)
        ):
            raise CampaignError("Managed live history changed its source or normalized prefix")
    atomic_write_text(
        directory / "history-prefix.json",
        json.dumps(
            {
                "schema_version": PREFIX_VERSION,
                "request_sha256": request_sha256,
                "file_identity": file_identity,
                "stdout_bytes": raw.stat().st_size,
                "stdout_sha256": stream_hash(raw),
                "events_sha256": stream_hash(exported),
            },
            sort_keys=True,
        ),
        follow_symlinks=False,
    )
    return exported


def _frames(path, identity, *, live=False):
    # Legacy terminal transports may emit a single JSON document. That document
    # remains capped by the independent record limit; large streams use JSONL.
    if not live and path.stat().st_size <= MAX_RECORD_BYTES:
        try:
            decoded = json.loads(_read(path, maximum=MAX_RECORD_BYTES))
        except (ValueError, UnicodeError, RecursionError):
            pass
        else:
            for frame in decoded if isinstance(decoded, list) else [decoded]:
                yield _native_record(identity, frame)
            return
    for line in record_lines(path, MAX_RECORD_BYTES):
        try:
            frame = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            frame = {"unparsed_native_bytes_hex": line.rstrip(b"\r\n").hex()}
        if live and not isinstance(frame, dict):
            raise CampaignError("Managed live export requires complete object JSONL frames")
        yield _native_record(identity, frame)


def snapshot_managed_prefix(request_path, binding, artifact_root, *, max_bytes=64 * 1024 * 1024):
    """Optional live evidence; never supplies completion or owner authority."""
    from .process_host import get_process_host

    path, root = Path(request_path), Path(artifact_root)
    raw = _read(path, maximum=16 * 1024 * 1024)
    sha = hashlib.sha256(raw).hexdigest()
    envelope = json.loads(raw)
    directory = path.parent
    if (
        envelope.get("schema_version") != 1
        or binding.get("headless_request") != str(path)
        or binding.get("headless_request_sha256") != sha
        or envelope.get("directory") != str(directory.absolute())
        or digest(envelope.get("request")) != envelope.get("request_digest")
    ):
        raise CampaignError("Managed live history lacks its exact request binding")
    startup = json.loads(_read(directory / "startup.json", maximum=1024 * 1024))
    if (
        any(
            startup.get(key) != envelope.get(key)
            for key in ("launch_id", "generation", "request_digest")
        )
        or type(startup.get("pid")) is not int
        or type(startup.get("identity")) not in (int, float)
        or not math.isfinite(startup["identity"])
    ):
        raise CampaignError("Managed live startup belongs to another launch")
    if get_process_host().liveness_of(startup["pid"], startup["identity"]) != "alive":
        raise CampaignError("Managed live launch identity is not confirmed alive")
    delivered = _read(directory / "input.txt", maximum=64 * 1024 * 1024)
    if hashlib.sha256(delivered).hexdigest() != envelope["input"]["sha256"]:
        raise CampaignError("Managed live delivered input changed")
    stdout, _, _, file_identity = seal_source(
        directory / "stdout.jsonl", root / "originals", maximum=max_bytes, complete_lines=True
    )
    identity = "headless:" + envelope["launch_id"]

    def records():
        yield _record(identity, "managed-request", envelope)
        yield _record(identity, "managed-delivered-input", delivered.decode("utf-8"))
        yield from _frames(stdout, identity, live=True)

    exported, _, _ = stream_blob(
        directory / "history-prefixes",
        (encoded(row) + b"\n" for row in records()),
        maximum=max_bytes * 8 + 128 * 1024 * 1024,
    )
    _publish_live(directory, exported, stdout, file_identity, sha)
    return snapshot_history(
        exported,
        "managed-headless",
        identity,
        root,
        max_bytes=max_bytes * 8 + 128 * 1024 * 1024,
        max_record_bytes=MAX_RECORD_BYTES * 8,
        source_identity=str(path.absolute()),
    )


def _record(identity, kind, content, *, role="system", frame=None):
    frame = frame or {}
    return {
        "schema_version": 1,
        "session_id": identity,
        "kind": kind,
        "role": role,
        "content": content,
        "native_id": frame.get("id", ""),
        "parent_id": frame.get("parent_id", frame.get("parentUuid", "")),
        "tool_id": frame.get("call_id", frame.get("toolCallId", "")),
    }


def _native_record(identity, frame):
    kind = (
        str(frame.get("type", frame.get("payload_type", "unknown")))
        if isinstance(frame, dict)
        else "unknown"
    )
    item = frame.get("item", {}) if isinstance(frame, dict) else {}
    item = item if isinstance(item, dict) else {}
    typed_kind = kind + ":" + str(item.get("type", ""))
    role = (
        "tool"
        if any(tag in typed_kind for tag in ("tool", "function_call", "command_execution"))
        else (
            "system"
            if kind in {"system", "init", "user", "session", "thread.started"}
            else "assistant"
        )
    )
    return _record(
        identity,
        "managed-native:" + kind,
        frame,
        role=role,
        frame=frame if isinstance(frame, dict) else None,
    )


def _read(path, *, maximum):
    if is_link_like(path):
        raise CampaignError("Managed headless history is redirected")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise CampaignError("Managed history file exceeds its regular-file bound")
        raw = stream.read(maximum + 1)
        stream.seek(0)
        if stream.read(maximum + 1) != raw:
            raise CampaignError("Managed history changed while sealing")
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise CampaignError("Managed history identity changed while sealing")
    return raw


def snapshot_managed_history(
    request_path, binding, artifact_root, *, session_id="", max_bytes=64 * 1024 * 1024
):
    """Capture only a terminal launch matching its persisted adapter binding."""
    path, root = Path(request_path), Path(artifact_root)
    request_raw = _read(path, maximum=16 * 1024 * 1024)
    if (
        binding.get("headless_request") != str(path)
        or binding.get("headless_request_sha256") != hashlib.sha256(request_raw).hexdigest()
        or binding.get("quiescent") is not True
    ):
        raise CampaignError("Managed history lacks its immutable quiescent launch binding")
    envelope = json.loads(request_raw)
    directory = path.parent
    if (
        envelope.get("schema_version") != 1
        or Path(envelope.get("directory", "")).absolute() != directory.absolute()
        or digest(envelope.get("request")) != envelope.get("request_digest")
    ):
        raise CampaignError("Managed history request coordinates changed")
    receipt_raw = _read(directory / "receipt.json", maximum=16 * 1024 * 1024)
    receipt = json.loads(receipt_raw)
    if (
        receipt.get("processes_quiescent") is not True
        or hashlib.sha256(receipt_raw).hexdigest() != binding.get("headless_receipt_sha256")
        or any(
            receipt.get(key) != envelope.get(key)
            for key in ("launch_id", "generation", "request_digest")
        )
    ):
        raise CampaignError("Managed history receipt belongs to another or uncertain launch")
    actual_session = receipt.get("session_id") or ""
    if session_id and actual_session != session_id:
        raise CampaignError("Managed history native session changed")
    prefix_path = directory / "history-prefix.json"
    warmed = prefix_path.exists() or is_link_like(prefix_path)
    identity = (
        "headless:" + envelope["launch_id"]
        if warmed
        else actual_session or "headless:" + envelope["launch_id"]
    )
    originals = {}
    for name, content in (("request.json", request_raw), ("receipt.json", receipt_raw)):
        original = blob(root / "originals", content, suffix=".txt")
        originals[name] = {
            "path": str(original),
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
    delivered = _read(directory / "input.txt", maximum=64 * 1024 * 1024)
    if hashlib.sha256(delivered).hexdigest() != envelope["input"]["sha256"]:
        raise CampaignError("Managed history input differs from delivered bytes")
    original = blob(root / "originals", delivered, suffix=".txt")
    originals["input.txt"] = {
        "path": str(original),
        "sha256": hashlib.sha256(delivered).hexdigest(),
        "bytes": len(delivered),
    }
    missing, streams, identities = [], {}, {}
    total = 0
    for name in ("stdout.jsonl", "stderr.log"):
        if (directory / name).exists() or is_link_like(directory / name):
            original, sha, size, file_identity = seal_source(
                directory / name, root / "originals", maximum=max_bytes
            )
            total += size
            if total > max_bytes:
                raise CampaignError(
                    "Managed history streams exceed the frozen aggregate byte bound"
                )
            originals[name] = {"path": str(original), "sha256": sha, "bytes": size}
            streams[name], identities[name] = original, file_identity
        else:
            missing.append(name)
    stdout = streams.get("stdout.jsonl")
    if warmed and stdout is None:
        raise CampaignError("Final managed history lost its sealed stdout stream")
    prefix = _published_prefix(directory, stdout) if warmed else None
    if prefix and prefix[0].get("file_identity") != identities["stdout.jsonl"]:
        raise CampaignError("Final managed stream identity differs from its sealed live prefix")

    def records():
        yield _record(identity, "managed-request", envelope)
        yield _record(identity, "managed-delivered-input", delivered.decode("utf-8"))
        if stdout is not None:
            yield from _frames(stdout, identity)
        if "stderr.log" in streams:
            with streams["stderr.log"].open("rb") as stream:
                import codecs

                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                while chunk := stream.read(1024 * 1024):
                    yield _record(identity, "managed-stderr", decoder.decode(chunk), role="tool")
                yield _record(
                    identity, "managed-stderr", decoder.decode(b"", final=True), role="tool"
                )
        yield _record(identity, "managed-terminal-receipt", receipt)
        yield _record(
            identity,
            "managed-source-index",
            {
                "files": originals,
                "missing_streams": missing,
                "coverage": "managed launch input/output; native hidden history not claimed",
            },
        )

    exported, _, _ = stream_blob(
        root / "exports",
        (encoded(row) + b"\n" for row in records()),
        maximum=max_bytes * 8 + 128 * 1024 * 1024,
    )
    if prefix and (
        prefix[0].get("request_sha256") != hashlib.sha256(request_raw).hexdigest()
        or not _same_prefix(prefix[1], exported)
    ):
        raise CampaignError("Final managed history differs from its sealed live prefix")
    return snapshot_history(
        exported,
        "managed-headless",
        identity,
        root / "history",
        max_bytes=max_bytes * 8 + 128 * 1024 * 1024,
        max_record_bytes=MAX_RECORD_BYTES * 8,
        source_identity=str(path.absolute()) if warmed else "",
    )
