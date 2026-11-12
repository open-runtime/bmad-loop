"""Resolve bounded owner quotations from sealed indexes and native records.

Indexes locate evidence; their normalized text alone cannot grant owner authority.
Each selected archive and index is streamed once. Only selected event coordinates,
record fingerprints and bounded quotation text survive that scan.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from dataclasses import asdict, replace
from pathlib import Path

from .campaign_model import CampaignError
from .campaign_transcripts import (
    READER_VERSIONS,
    HistoryEvent,
    HistorySnapshot,
    NormalizedRecord,
    _records,
    digest,
    read_record,
    regular_reader,
)
from .platform_util import is_link_like

MAX_QUOTE_CHARS = 512
MAX_RAW_RECORD_BYTES = 64 * 1024 * 1024
MAX_INDEX_RECORD_BYTES = 512 * 1024 * 1024
_REFERENCE_FIELDS = ("id", "event_id", "start", "end", "sha256")


def _sha(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _references(references):
    selected = {}
    try:
        references = iter(references)
    except TypeError as exc:
        raise CampaignError("owner quotation references are not iterable") from exc
    for reference in references:
        if not isinstance(reference, dict) or any(
            key not in reference for key in _REFERENCE_FIELDS
        ):
            raise CampaignError("owner quotation reference is malformed")
        item = {key: reference[key] for key in _REFERENCE_FIELDS}
        previous = selected.get(item["id"]) if isinstance(item["id"], str) else None
        if previous is not None and previous != item:
            raise CampaignError("owner quotation references conflict")
        start, end = item["start"], item["end"]
        if (
            not all(_sha(item[key]) for key in ("id", "event_id", "sha256"))
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end
        ):
            raise CampaignError("owner quotation reference has invalid identity or range")
        if end - start > MAX_QUOTE_CHARS:
            raise CampaignError(
                "owner quotation exceeds 512 characters; re-extract current bounded spans"
            )
        if item["id"] != digest([item["event_id"], start, end]):
            raise CampaignError("owner quotation ID differs from its event and range")
        selected[item["id"]] = item
    return selected


def _lines(path, maximum):
    """Read bounded records from one stable descriptor, detecting concurrent changes."""
    with regular_reader(path) as stream:
        before = os.fstat(stream.fileno())
        while line := stream.readline(maximum + 1):
            if len(line) > maximum:
                raise CampaignError("owner quotation history record exceeds its bound")
            yield line
        after = os.fstat(stream.fileno())
        current = path.stat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if is_link_like(path) or any(
            getattr(before, key) != getattr(after, key)
            or getattr(before, key) != getattr(current, key)
            for key in fields
        ):
            raise CampaignError("owner quotation history changed while reading")


def _snapshot(snapshot):
    if (
        not isinstance(snapshot, HistorySnapshot)
        or snapshot.harness not in READER_VERSIONS
        or type(snapshot.reader_version) is not int
        or snapshot.reader_version != READER_VERSIONS[snapshot.harness]
        or not _sha(snapshot.source_id)
        or not isinstance(snapshot.session_id, str)
        or not snapshot.session_id
        or not all(_sha(value) for value in (snapshot.raw_sha256, snapshot.events_sha256))
        or any(
            type(value) is not int
            for value in (snapshot.captured_bytes, snapshot.complete_bytes, snapshot.event_count)
        )
        or not 0 <= snapshot.complete_bytes <= snapshot.captured_bytes
        or snapshot.event_count < 0
    ):
        raise CampaignError("owner quotation source manifest is invalid or stale")


def _index(snapshot, wanted):
    checksum = hashlib.sha256()
    selected = {}
    owners = set(snapshot.owner_event_ids)
    count = 0
    for line in _lines(Path(snapshot.events_path), MAX_INDEX_RECORD_BYTES):
        checksum.update(line)
        count += 1
        if not line.endswith(b"\n"):
            raise CampaignError("owner quotation index has an incomplete record")
        row = json.loads(line)
        if not isinstance(row, dict):
            raise CampaignError("owner quotation index record is malformed")
        if row.get("id") not in wanted:
            continue
        record = NormalizedRecord(**row["record"])
        event = HistoryEvent(**{**row, "record": record})
        if (
            event.id in selected
            or event.source_id != snapshot.source_id
            or event.session_id != snapshot.session_id
            or type(event.offset) is not int
            or type(event.end_offset) is not int
            or type(event.record_index) is not int
            or not 0 <= event.offset < event.end_offset <= snapshot.complete_bytes
            or event.record_index < 1
            or not _sha(event.payload_sha256)
        ):
            raise CampaignError("owner quotation event coordinates differ from the sealed source")
        if (
            record.owner_input is not True
            or not isinstance(record.text, str)
            or event.duplicate_of
            or event.id not in owners
        ):
            raise CampaignError("owner quotation event is not native owner input")
        # Do not retain arbitrarily large normalized messages for tiny quote spans.
        selected[event.id] = {
            "id": event.id,
            "offset": event.offset,
            "end_offset": event.end_offset,
            "record_index": event.record_index,
            "payload_sha256": event.payload_sha256,
            "record_sha256": digest(asdict(record)),
        }
    if checksum.hexdigest() != snapshot.events_sha256 or count != snapshot.event_count:
        raise CampaignError("owner quotation index differs from its sealed manifest")
    return selected


def _native_record(snapshot, item):
    record = read_record(snapshot.harness, item, snapshot.session_id)
    if snapshot.harness == "codex":
        record = replace(
            record,
            metadata={
                **(record.metadata or {}),
                "native_payload_sha256": digest(item.get("payload", {})),
            },
        )
    if snapshot.parent_session_id:
        record = replace(
            record,
            owner_input=False,
            metadata={
                **(record.metadata or {}),
                "parent_session_id": snapshot.parent_session_id,
                "input_origin": "subagent-context",
            },
        )
    return record


def _raw(snapshot, events, references):
    checksum = hashlib.sha256()
    by_offset = defaultdict(dict)
    for event_id, event in events.items():
        by_offset[event["offset"]][event_id] = event
    offset = 0
    result = {}
    found = set()
    for line_number, line in enumerate(_lines(Path(snapshot.raw_path), MAX_RAW_RECORD_BYTES), 1):
        checksum.update(line)
        end = offset + len(line)
        if offset in by_offset:
            selected = by_offset[offset]
            if not line.endswith(b"\n") or any(
                event["end_offset"] != end or event["record_index"] != line_number
                for event in selected.values()
            ):
                raise CampaignError("owner quotation raw record boundary differs from its index")
            row = json.loads(line)
            if not isinstance(row, dict):
                raise CampaignError("owner quotation raw record is malformed")
            for subindex, item in enumerate(_records(row, snapshot.harness)):
                payload_sha = digest(item)
                event_id = digest([snapshot.source_id, offset, subindex, payload_sha])
                if event_id not in selected:
                    continue
                event = selected[event_id]
                record = _native_record(snapshot, item)
                if (
                    payload_sha != event["payload_sha256"]
                    or digest(asdict(record)) != event["record_sha256"]
                    or record.owner_input is not True
                ):
                    raise CampaignError(
                        "owner quotation index does not match native owner evidence"
                    )
                found.add(event_id)
                for reference in references[event_id]:
                    start, stop = reference["start"], reference["end"]
                    if stop > len(record.text):
                        raise CampaignError("owner quotation range exceeds native text")
                    text = record.text[start:stop]
                    if hashlib.sha256(text.encode()).hexdigest() != reference["sha256"]:
                        raise CampaignError("owner quotation text differs from its native hash")
                    result[reference["id"]] = {**reference, "text": text}
        offset = end
    if checksum.hexdigest() != snapshot.raw_sha256 or offset != snapshot.captured_bytes:
        raise CampaignError("owner quotation raw source differs from its sealed manifest")
    if found != set(events):
        raise CampaignError("owner quotation event is absent from its native record")
    return result


def resolve_owner_quotes(snapshots, references):
    """Return exact ≤512-character spans, or fail visibly without partial evidence."""
    selected = _references(references)
    if not selected:
        return {}
    by_event = defaultdict(list)
    for reference in selected.values():
        by_event[reference["event_id"]].append(reference)
    sources = {}
    result = {}
    found = set()
    try:
        for snapshot in snapshots:
            _snapshot(snapshot)
            if snapshot.source_id in sources:
                if sources[snapshot.source_id] != snapshot:
                    raise CampaignError("owner quotation source manifests conflict")
                continue
            sources[snapshot.source_id] = snapshot
            events = _index(snapshot, by_event)
            if found.intersection(events):
                raise CampaignError("owner quotation event occurs in conflicting sources")
            if events:
                result.update(_raw(snapshot, events, by_event))
                found.update(events)
        if found != set(by_event):
            raise CampaignError("owner quotation event is absent from the sealed indexes")
    except CampaignError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise CampaignError("owner quotation source is unavailable or malformed") from exc
    return result
