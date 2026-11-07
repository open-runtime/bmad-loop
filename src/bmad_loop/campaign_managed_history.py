"""Seal an owned headless launch without treating stream output as a conversation.

The exact request, delivered input, native output and terminal receipt remain
available as private immutable blobs. Injected prompts/context are controller
input, never authenticated owner messages. Missing streams and partial frames
remain visible coverage facts; they are not silently invented native history.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .campaign_model import CampaignError
from .campaign_transcripts import blob, digest, encoded, snapshot_history
from .platform_util import is_link_like


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


def snapshot_managed_history(request_path, binding, artifact_root, *, session_id=""):
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
    identity = actual_session or "headless:" + envelope["launch_id"]
    raw_files = {"request.json": request_raw, "receipt.json": receipt_raw}
    raw_files["input.txt"] = _read(directory / "input.txt", maximum=64 * 1024 * 1024)
    if hashlib.sha256(raw_files["input.txt"]).hexdigest() != envelope["input"]["sha256"]:
        raise CampaignError("Managed history input differs from delivered bytes")
    missing = []
    for name in ("stdout.jsonl", "stderr.log"):
        if (directory / name).exists():
            raw_files[name] = _read(directory / name, maximum=64 * 1024 * 1024)
        else:
            missing.append(name)
    originals = {
        name: {
            "path": str(blob(root / "originals", raw, suffix=".txt")),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
        for name, raw in raw_files.items()
    }
    rows = []

    def record(kind, content, *, role="system", frame=None):
        frame = frame or {}
        rows.append(
            {
                "schema_version": 1,
                "session_id": identity,
                "kind": kind,
                "role": role,
                "content": content,
                "native_id": frame.get("id", ""),
                "parent_id": frame.get("parent_id", frame.get("parentUuid", "")),
                "tool_id": frame.get("call_id", frame.get("toolCallId", "")),
            }
        )

    record("managed-request", envelope)
    record("managed-delivered-input", raw_files["input.txt"].decode("utf-8"))
    stdout = raw_files.get("stdout.jsonl", b"")
    try:
        decoded = json.loads(stdout)
        frames = decoded if isinstance(decoded, list) else [decoded]
    except (ValueError, UnicodeError):
        frames = []
        for line in stdout.splitlines():
            try:
                frames.append(json.loads(line))
            except (ValueError, UnicodeError):
                frames.append({"unparsed_native_bytes_hex": line.hex()})
    for frame in frames:
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
        record(
            "managed-native:" + kind,
            frame,
            role=role,
            frame=frame if isinstance(frame, dict) else None,
        )
    if "stderr.log" in raw_files:
        record(
            "managed-stderr", raw_files["stderr.log"].decode("utf-8", errors="replace"), role="tool"
        )
    record("managed-terminal-receipt", receipt)
    record(
        "managed-source-index",
        {
            "files": originals,
            "missing_streams": missing,
            "coverage": "managed launch input/output; native hidden history not claimed",
        },
    )
    exported = blob(
        root / "exports", b"".join(encoded(row) + b"\n" for row in rows), suffix=".jsonl"
    )
    return snapshot_history(exported, "managed-headless", identity, root / "history")
