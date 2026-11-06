"""Explicit native owner-message evidence for the normal campaign CLI.

Bindings are ``HARNESS:/absolute/native.jsonl#SESSION:EVENT``. EVENT is the native
message ID, ``line-N`` (one-based), or ``sha256-HEX`` of the exact record bytes.
Only Codex/Claude stores are qualified here. Imported JSON and assistant/tool
messages cannot self-assert owner authority. Other harness readers extend this
boundary only when their native provenance contract is qualified.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .campaign_decisions import EvidenceLocation, EvidenceSource
from .campaign_model import CampaignError
from .campaign_transcripts import read_record
from .platform_util import is_link_like


class TranscriptEvidenceProvider:
    def __init__(
        self,
        *,
        roots: dict[str, tuple[Path, ...]] | None = None,
        max_scan_bytes: int = 128 * 1024 * 1024,
        max_message_bytes: int = 64000,
    ):
        self.roots = (
            roots
            if roots is not None
            else {
                "codex": (
                    Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions",
                ),
                "claude": (
                    Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
                    / "projects",
                ),
            }
        )
        self.max_scan_bytes, self.max_message_bytes = max_scan_bytes, max_message_bytes

    def __call__(self, location: EvidenceLocation) -> EvidenceSource:
        harness, separator, rest = location.location.partition(":")
        raw_path, fragment, selector = rest.rpartition("#")
        session, colon, event = selector.partition(":")
        if (
            location.kind != "transcript"
            or not separator
            or not fragment
            or not colon
            or not session
            or not event
        ):
            raise CampaignError(
                "native transcript binding requires harness:absolute-path#session:event"
            )
        if harness not in {"codex", "claude"} or harness not in self.roots:
            raise CampaignError("native transcript harness is not qualified")
        path = Path(raw_path)
        if (
            not path.is_absolute()
            or is_link_like(path)
            or not path.is_file()
            or not any(
                path.resolve().is_relative_to(root.resolve()) for root in self.roots[harness]
            )
        ):
            raise CampaignError("transcript is not a regular file in an authorized native store")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or (
                hasattr(os, "getuid") and before.st_uid != os.getuid()
            ):
                raise CampaignError("native transcript ownership is invalid")
            if before.st_size > self.max_scan_bytes:
                raise CampaignError(
                    "native transcript exceeds bounded scan; use an indexed sealed source"
                )
            metadata = None
            matches = []
            consumed = 0
            for number in range(1, 100001):
                raw = stream.readline(1024 * 1024 + 1)
                if not raw:
                    break
                consumed += len(raw)
                if len(raw) > 1024 * 1024 or consumed > self.max_scan_bytes:
                    raise CampaignError("native transcript coverage exceeds bound")
                if not raw.endswith(b"\n"):
                    raise CampaignError("native transcript has an incomplete active record")
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise CampaignError("native transcript record is invalid") from None
                if not isinstance(row, dict):
                    raise CampaignError("native transcript record schema is invalid")
                payload = row.get("payload") or {}
                if harness == "codex" and row.get("type") == "session_meta":
                    if (
                        not isinstance(payload, dict)
                        or payload.get("id") != session
                        or metadata is not None
                    ):
                        raise CampaignError("native transcript session binding differs")
                    metadata = payload
                digest = hashlib.sha256(raw).hexdigest()
                native_id = (
                    row.get("uuid")
                    if harness == "claude"
                    else (payload.get("id") if isinstance(payload, dict) else None)
                )
                if event not in {native_id, f"line-{number}", f"sha256-{digest}"}:
                    continue
                normalized = read_record(harness, row, session)
                if not normalized.owner_input:
                    raise CampaignError(
                        "transcript selection is not an owner message (synthetic or tool content)"
                    )
                if harness == "claude":
                    if (
                        row.get("sessionId") != session
                        or row.get("type") != "user"
                        or row.get("isMeta")
                    ):
                        raise CampaignError(
                            "transcript selection is not an owner message for this session"
                        )
                    message = row.get("message") or {}
                    if not isinstance(message, dict) or message.get("role") != "user":
                        raise CampaignError("native owner message role is missing")
                    content = message.get("content")
                else:
                    if metadata is None or not isinstance(payload, dict):
                        raise CampaignError("Codex native session metadata is missing")
                    # response_item/user also includes harness-injected context;
                    # only the native input event establishes original user input.
                    if row.get("type") == "event_msg" and payload.get("type") == "user_message":
                        content = payload.get("message")
                    else:
                        raise CampaignError("transcript selection is not an owner message")
                if isinstance(content, list):
                    if any(
                        not isinstance(item, dict)
                        or item.get("type") not in {"text", "input_text"}
                        or not isinstance(item.get("text"), str)
                        for item in content
                    ):
                        raise CampaignError("owner message includes unsupported or tool content")
                    content = "\n".join(item["text"] for item in content)
                if (
                    not isinstance(content, str)
                    or not content
                    or len(content.encode()) > self.max_message_bytes
                ):
                    raise CampaignError("owner message is empty or exceeds evidence bound")
                matches.append((content, digest, number))
            else:
                raise CampaignError("native transcript record count exceeds bound")
            after = os.fstat(stream.fileno())
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise CampaignError("native transcript changed during evidence collection")
        if len(matches) != 1:
            raise CampaignError("native owner message is missing or ambiguous")
        content, digest, number = matches[0]
        return EvidenceSource(
            location.id,
            location.kind,
            location.location,
            f"{harness}:{session}:{number}:{digest}",
            content,
            authority=location.authority,
            scope=location.scope,
            author_id=f"native:{harness}:{session}:user",
            origin_verified=True,
            private=True,
            approved_head=location.approved_head,
            release_hold=location.release_hold,
            supersedes=location.supersedes,
        )
