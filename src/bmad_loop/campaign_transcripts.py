"""Versioned, source-addressable native history; normalized text is not authority.

The explicit source/session binding comes from the caller. Readers never discover
"the latest" conversation or write native stores. Complete raw bytes, including
an incomplete append tail, are archived before normalization. Unknown event kinds
retain their JSON, and native imports/compactions remain visible lineage records.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .campaign_model import CampaignError
from .platform_util import atomic_write_bytes, is_link_like

READER_VERSIONS = {"codex": 3, "claude": 1, "grok": 1, "antigravity": 1, "muse": 1}


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def blob(root: Path, content: bytes, *, suffix: str = ".json") -> Path:
    """Install immutable private content; a collision or redirected path is a fault."""
    if suffix not in {".json", ".jsonl", ".txt"} or is_link_like(root):
        raise CampaignError("invalid history artifact directory or suffix")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (hashlib.sha256(content).hexdigest() + suffix)
    if is_link_like(path):
        raise CampaignError("history artifact is redirected")
    if path.exists():
        if path.read_bytes() != content:
            raise CampaignError("immutable history artifact identity changed")
    else:
        atomic_write_bytes(path, content, follow_symlinks=False)
        path.chmod(0o600)
    return path


def text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_content(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            if key in value:
                return text_content(value[key])
        return encoded(value).decode()
    return encoded(value).decode()


@dataclass(frozen=True)
class NormalizedRecord:
    role: str
    kind: str
    text: str
    native_id: str = ""
    parent_id: str = ""
    tool_id: str = ""
    owner_input: bool = False
    metadata: dict[str, Any] | None = None


def read_record(harness: str, row: dict[str, Any], session_id: str) -> NormalizedRecord:
    """Parse a qualified v1 layout, retaining unknown records without owner authority."""
    if harness not in READER_VERSIONS or not isinstance(row, dict):
        raise CampaignError("unsupported native history reader or record")
    role, kind, content, native, parent, tool, owner = "system", "unknown", row, "", "", "", False
    if harness == "codex":
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            raise CampaignError("invalid Codex record payload")
        kind = str(payload.get("type", row.get("type", "unknown")))
        if row.get("type") == "session_meta" and payload.get("id") != session_id:
            raise CampaignError("Codex history belongs to another native session")
        native = str(payload.get("id") or "")
        parent = str(payload.get("parent_id") or payload.get("forked_from_id") or "")
        tool = str(payload.get("call_id") or "")
        if row.get("type") == "event_msg" and kind == "user_message":
            role, content, owner = "user", payload.get("message", ""), True
        elif row.get("type") == "response_item":
            role = str(payload.get("role", "tool" if "output" in kind else "assistant"))
            content = payload.get("content", payload)
            provenance = payload.get("internal_chat_message_metadata_passthrough") or {}
            kinds = provenance.get("content_item_kinds", [])
            metadata = provenance.get("content_item_metadata", [])
            owner = (
                kind == "message"
                and role == "user"
                and isinstance(content, list)
                and bool(content)
                and isinstance(kinds, list)
                and isinstance(metadata, list)
                and len(content) == len(kinds) == len(metadata)
                and all(
                    tag == "user.text"
                    and isinstance(block, dict)
                    and block.get("type") in {"input_text", "text"}
                    and isinstance(origin, dict)
                    and (origin.get("provenance") or {}).get("type") == "user"
                    for block, tag, origin in zip(content, kinds, metadata, strict=True)
                )
            )
        else:
            content = payload
    elif harness == "claude":
        if row.get("sessionId") and row["sessionId"] != session_id:
            raise CampaignError("Claude history belongs to another native session")
        kind = str(row.get("type", "unknown"))
        message = row.get("message", {})
        if not isinstance(message, dict):
            raise CampaignError("invalid Claude message")
        role = str(message.get("role", "system"))
        content = message.get("content", row)
        owner = (
            kind == "user"
            and role == "user"
            and not row.get("isMeta")
            and not row.get("isSynthetic")
        )
        if isinstance(content, list) and any(
            isinstance(x, dict) and x.get("type") == "tool_result" for x in content
        ):
            role, owner = "tool", False
        native, parent = str(row.get("uuid") or ""), str(row.get("parentUuid") or "")
    elif harness == "grok":
        params = row.get("params", {})
        if not isinstance(params, dict):
            raise CampaignError("invalid Grok ACP record")
        if params.get("sessionId") and params["sessionId"] != session_id:
            raise CampaignError("Grok history belongs to another native session")
        update = params.get("update", {})
        if not isinstance(update, dict):
            raise CampaignError("invalid Grok ACP update")
        kind = str(update.get("sessionUpdate", "unknown"))
        content = update.get("content", update)
        owner = (
            row.get("method") == "session/update"
            and kind == "user_message_chunk"
            and (update.get("_meta") or {}).get("hostTurn") is not True
        )
        role = (
            "user"
            if owner
            else (
                "assistant"
                if kind.startswith("agent_")
                else "tool" if kind.startswith("tool_") else "system"
            )
        )
        tool = str(update.get("toolCallId") or "")
        native = str(row.get("id") or "")
    elif harness == "antigravity":
        kind = str(row.get("type", "unknown"))
        owner = kind == "USER_INPUT" and row.get("source") == "USER_EXPLICIT"
        role = "user" if owner else "assistant" if row.get("source") == "MODEL" else "tool"
        content = row.get("content", row)
        native = str(row["step_index"]) if type(row.get("step_index")) is int else ""
    elif harness == "muse":
        if row.get("schema_version") != 1:
            raise CampaignError("unsupported Muse durable-record schema")
        stream = row.get("stream") or {}
        if stream.get("kind") == "session" and stream.get("id") != session_id:
            raise CampaignError("Muse history belongs to another native session")
        kind = str(row.get("payload_type", "unknown"))
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            raise CampaignError("invalid Muse durable-record payload")
        owner = (
            kind == "runtime.user_intent.accepted"
            and payload.get("source_session_id") == session_id
            and (payload.get("semantic_kind") or {}).get("kind") == "chat"
        )
        content = payload.get("refill_blocks", payload) if owner else payload
        role = "user" if owner else "tool" if kind.startswith("tool_") else "system"
        native, parent = str(row.get("id") or ""), str(row.get("causation_id") or "")
    return NormalizedRecord(
        role,
        kind,
        text_content(content),
        native,
        parent,
        tool,
        owner,
        {
            "reader_version": READER_VERSIONS[harness],
            "timestamp": row.get("timestamp", row.get("recorded_at")),
        },
    )


@dataclass(frozen=True)
class HistoryEvent:
    id: str
    source_id: str
    session_id: str
    offset: int
    end_offset: int
    record_index: int
    payload_sha256: str
    record: NormalizedRecord
    duplicate_of: str = ""


@dataclass(frozen=True)
class HistorySnapshot:
    source_id: str
    harness: str
    session_id: str
    reader_version: int
    raw_path: str
    raw_sha256: str
    events_path: str
    events_sha256: str
    captured_bytes: int
    complete_bytes: int
    event_count: int
    owner_event_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    parent_session_id: str = ""
    lineage_verified: bool = False


def _records(row: dict[str, Any], harness: str) -> list[dict[str, Any]]:
    if harness != "muse" or "retained_frame" not in row:
        return [row]
    if row.get("frame_schema_version") != 1 or not isinstance(row.get("children"), list):
        raise CampaignError("unsupported Muse retained-frame schema")
    children = []
    for child in row["children"]:
        raw = child.get("record_json") if isinstance(child, dict) else None
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict) or "retained_frame" in value:
            raise CampaignError("invalid Muse retained-frame child")
        children.append(value)
    return children


def snapshot_history(
    path: Path,
    harness: str,
    session_id: str,
    artifact_root: Path,
    *,
    max_bytes: int = 1024 * 1024 * 1024,
    max_record_bytes: int = 64 * 1024 * 1024,
    prefix_bytes: int | None = None,
) -> HistorySnapshot:
    """Seal an append-only prefix, validating the same bytes again before publishing."""
    if harness not in READER_VERSIONS or not session_id or max_bytes < 1 or is_link_like(path):
        raise CampaignError("invalid native history binding")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        initial = os.fstat(stream.fileno())
        selected = initial.st_size if prefix_bytes is None else prefix_bytes
        if (
            not stat.S_ISREG(initial.st_mode)
            or type(selected) is not int
            or selected < 1
            or selected > initial.st_size
            or selected > max_bytes
        ):
            raise CampaignError("native history is not regular or exceeds capture bound")
        raw = stream.read(selected)
        stream.seek(0)
        if stream.read(selected) != raw or os.fstat(stream.fileno()).st_size < selected:
            raise CampaignError("native history prefix changed while sealing")
    current = path.stat()
    if (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino):
        raise CampaignError("native history file identity changed while sealing")
    source_id = digest([harness, session_id, str(path.resolve()), READER_VERSIONS[harness]])
    raw_path = blob(artifact_root, raw, suffix=".jsonl")
    rows, owners, seen, warnings = [], [], {}, []
    parent_session = ""
    declared_ancestors = set()
    lineage_verified = False
    offset = complete = 0
    for line_number, line in enumerate(raw.splitlines(keepends=True), 1):
        end = offset + len(line)
        if len(line) > max_record_bytes:
            raise CampaignError("native history record exceeds bound")
        if not line.endswith(b"\n"):
            warnings.append(f"incomplete append tail at byte {offset}")
            break
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise CampaignError(f"invalid native history record at byte {offset}") from exc
        if not isinstance(row, dict):
            raise CampaignError("native history record is not an object")
        for subindex, item in enumerate(_records(row, harness)):
            bound_session = session_id
            if harness == "codex" and item.get("type") == "session_meta":
                metadata = item.get("payload") or {}
                identity = metadata.get("id")
                if not isinstance(identity, str) or not identity:
                    raise CampaignError("Codex session metadata lacks an identity")
                origin = metadata.get("source") or {}
                subagent = origin.get("subagent", {}) if isinstance(origin, dict) else {}
                spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
                declared_parent = (
                    metadata.get("forked_from_id") or spawn.get("parent_thread_id") or ""
                )
                if (
                    metadata.get("forked_from_id")
                    and spawn.get("parent_thread_id")
                    and metadata["forked_from_id"] != spawn["parent_thread_id"]
                ):
                    raise CampaignError("Codex fork metadata has conflicting parent identity")
                if identity == session_id:
                    if lineage_verified and declared_parent != parent_session:
                        raise CampaignError("Codex session lineage changed within archive")
                    lineage_verified = True
                    parent_session = declared_parent
                elif not lineage_verified or identity not in declared_ancestors:
                    raise CampaignError("Codex inherited metadata lacks declared ancestor binding")
                if declared_parent:
                    if declared_parent == session_id or not isinstance(declared_parent, str):
                        raise CampaignError("invalid Codex fork ancestry")
                    declared_ancestors.add(declared_parent)
                bound_session = identity
            record = read_record(harness, item, bound_session)
            if harness == "codex":
                record = replace(
                    record,
                    metadata={
                        **(record.metadata or {}),
                        "native_payload_sha256": digest(item.get("payload", {})),
                    },
                )
            if parent_session:
                record = replace(
                    record,
                    owner_input=False,
                    metadata={
                        **(record.metadata or {}),
                        "parent_session_id": parent_session,
                        "input_origin": "subagent-context",
                    },
                )
            sha = digest(item)
            event_id = digest([source_id, offset, subindex, sha])
            key = (record.kind, record.native_id, sha)
            duplicate = seen.get(key, "") if record.native_id else ""
            if record.native_id and not duplicate:
                seen[key] = event_id
            event = HistoryEvent(
                event_id, source_id, session_id, offset, end, line_number, sha, record, duplicate
            )
            rows.append(asdict(event))
            if record.owner_input and not duplicate:
                owners.append(event_id)
        offset = complete = end
    events = b"".join(encoded(row) + b"\n" for row in rows)
    events_path = blob(artifact_root, events, suffix=".jsonl")
    result = HistorySnapshot(
        source_id,
        harness,
        session_id,
        READER_VERSIONS[harness],
        str(raw_path),
        hashlib.sha256(raw).hexdigest(),
        str(events_path),
        hashlib.sha256(events).hexdigest(),
        len(raw),
        complete,
        len(rows),
        tuple(owners),
        tuple(warnings),
        parent_session,
        lineage_verified,
    )
    blob(artifact_root, encoded(asdict(result)))
    return result


def load_events(snapshot: HistorySnapshot) -> tuple[HistoryEvent, ...]:
    path = Path(snapshot.events_path)
    if is_link_like(path):
        raise CampaignError("history index is redirected")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != snapshot.events_sha256:
        raise CampaignError("history index hash differs from sealed manifest")
    events = []
    for line in data.splitlines():
        row = json.loads(line)
        row["record"] = NormalizedRecord(**row["record"])
        events.append(HistoryEvent(**row))
    return tuple(events)


def merge_lineage_events(snapshots: tuple[HistorySnapshot, ...]) -> tuple[HistoryEvent, ...]:
    """Deduplicate verified ancestor copies while retaining every native source.

    Text equality is never identity. Named records require the same native ID
    and complete payload hash; unnamed records require both original timestamp
    and complete record hash. Unrelated sessions are never deduplicated.
    """
    by_session = {(snapshot.harness, snapshot.session_id): snapshot for snapshot in snapshots}
    if len(by_session) != len(snapshots):
        raise CampaignError("history lineage contains duplicate session manifests")
    loaded = {key: load_events(value) for key, value in by_session.items()}

    def identity(event):
        metadata = event.record.metadata or {}
        payload_hash = metadata.get("native_payload_sha256", event.payload_sha256)
        if event.record.native_id:
            return ("id", event.record.native_id, payload_hash)
        if event.record.tool_id and event.record.kind in {"function_call", "function_call_output"}:
            return (event.record.kind, event.record.tool_id, payload_hash)
        timestamp = metadata.get("timestamp")
        return ("timestamp", timestamp, event.payload_sha256) if timestamp else None

    result = []
    for snapshot in snapshots:
        ancestors = []
        parent = snapshot.parent_session_id if snapshot.lineage_verified else ""
        seen = {snapshot.session_id}
        while parent:
            if parent in seen:
                raise CampaignError("history lineage contains a cycle")
            seen.add(parent)
            ancestor = by_session.get((snapshot.harness, parent))
            if ancestor is None or not ancestor.lineage_verified:
                break
            ancestors.extend(loaded[(ancestor.harness, parent)])
            parent = ancestor.parent_session_id
        identities = {}
        for event in ancestors:
            key = identity(event)
            if key is not None:
                identities.setdefault(
                    key,
                    event.duplicate_of or event.id,
                )
        for event in loaded[(snapshot.harness, snapshot.session_id)]:
            key = identity(event)
            duplicate = identities.get(key, "") if key is not None else ""
            result.append(replace(event, duplicate_of=duplicate) if duplicate else event)
    return tuple(result)


def source_record(snapshot: HistorySnapshot, event: HistoryEvent) -> bytes:
    if event.source_id != snapshot.source_id:
        raise CampaignError("event belongs to another history source")
    path = Path(snapshot.raw_path)
    if is_link_like(path):
        raise CampaignError("raw history is redirected")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != snapshot.raw_sha256:
        raise CampaignError("raw history hash differs from sealed manifest")
    return raw[event.offset : event.end_offset]
