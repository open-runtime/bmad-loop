"""Exact owner-span resolution authenticates raw records with bounded reads."""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_handoff_context import capture, native_row

from bmad_loop import campaign_summary_quotes as quotes
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_transcripts import (
    digest,
    encoded,
    load_events,
)


def reference(event, start=0, end=None):
    end = len(event.record.text) if end is None else end
    return {
        "id": digest([event.id, start, end]),
        "event_id": event.id,
        "start": start,
        "end": end,
        "sha256": hashlib.sha256(event.record.text[start:end].encode()).hexdigest(),
    }


def rewrite_index(snapshot, mutate):
    rows = [json.loads(line) for line in Path(snapshot.events_path).read_bytes().splitlines()]
    mutate(rows)
    data = b"".join(encoded(row) + b"\n" for row in rows)
    path = Path(snapshot.events_path).with_name("altered-index.jsonl")
    path.write_bytes(data)
    return replace(snapshot, events_path=str(path), events_sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("harness", ["codex", "claude", "grok", "antigravity", "muse"])
def test_resolves_exact_unicode_owner_span_with_tail_and_deduplication(tmp_path, harness):
    text = "Preserve café and λ. " + "x" * 130 + " Merge only after owner release."
    snapshot = capture(tmp_path, [native_row(harness, text)], harness)
    event = load_events(snapshot)[0]
    ref = reference(event, 9)
    assert quotes.resolve_owner_quotes((snapshot, snapshot), [ref, dict(ref)]) == {
        ref["id"]: {**ref, "text": text[9:]}
    }


def test_muse_retained_frame_authenticates_exact_subrecord(tmp_path):
    rows = [native_row("muse", "First owner", "m1"), native_row("muse", "Second owner", "m2")]
    frame = {
        "frame_schema_version": 1,
        "retained_frame": "fixture",
        "children": [{"record_json": encoded(row).decode()} for row in rows],
    }
    snapshot = capture(tmp_path, [frame], "muse")
    events = load_events(snapshot)
    assert events[0].offset == events[1].offset
    refs = [reference(event) for event in events]
    result = quotes.resolve_owner_quotes((snapshot,), refs)
    assert [result[ref["id"]]["text"] for ref in refs] == ["First owner", "Second owner"]


def test_no_references_does_not_consume_snapshots_or_open_history(monkeypatch):
    def snapshots():
        raise AssertionError("no-ref fast path consumed history")
        yield

    def forbidden(*args):
        raise AssertionError("no-ref fast path opened history")

    monkeypatch.setattr(quotes, "regular_reader", forbidden)
    assert quotes.resolve_owner_quotes(snapshots(), ()) == {}


@pytest.mark.parametrize(
    "change, message",
    [
        ({"start": True}, "invalid identity or range"),
        ({"end": -1}, "invalid identity or range"),
        ({"id": "f" * 64}, "ID differs"),
        ({"sha256": "not-a-digest"}, "invalid identity or range"),
        ({"end": 513}, "re-extract current bounded spans"),
    ],
)
def test_malformed_quote_references_fail_visibly(tmp_path, change, message):
    snapshot = capture(tmp_path, [native_row("codex", "a" * 600)])
    event = load_events(snapshot)[0]
    ref = reference(event, end=513 if change.get("end") == 513 else 512)
    with pytest.raises(CampaignError, match=message):
        quotes.resolve_owner_quotes((snapshot,), [{**ref, **change}])


def test_conflicting_reference_identity_is_rejected(tmp_path):
    snapshot = capture(tmp_path, [native_row("codex")])
    ref = reference(load_events(snapshot)[0])
    with pytest.raises(CampaignError, match="references conflict"):
        quotes.resolve_owner_quotes((snapshot,), [ref, {**ref, "sha256": "f" * 64}])


@pytest.mark.parametrize("kind", ["quote-hash", "absent-event", "range-past-end"])
def test_unresolvable_exact_span_is_rejected(tmp_path, kind):
    snapshot = capture(tmp_path, [native_row("codex", "Owner directive")])
    ref = reference(load_events(snapshot)[0])
    if kind == "quote-hash":
        ref["sha256"] = "a" * 64
    elif kind == "absent-event":
        ref["event_id"] = "b" * 64
        ref["id"] = digest([ref["event_id"], ref["start"], ref["end"]])
    else:
        ref["end"] += 1
        ref["id"] = digest([ref["event_id"], ref["start"], ref["end"]])
    with pytest.raises(CampaignError, match="native hash|absent from|range exceeds"):
        quotes.resolve_owner_quotes((snapshot,), [ref])


@pytest.mark.parametrize("kind", ["index", "raw"])
def test_tampered_sealed_source_is_rejected(tmp_path, kind):
    snapshot = capture(tmp_path, [native_row("codex")])
    ref = reference(load_events(snapshot)[0])
    path = Path(snapshot.events_path if kind == "index" else snapshot.raw_path)
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(CampaignError):
        quotes.resolve_owner_quotes((snapshot,), [ref])


@pytest.mark.parametrize("kind", ["index", "raw"])
def test_unselected_record_tampering_requires_complete_stream_hash(tmp_path, kind):
    snapshot = capture(
        tmp_path, [native_row("codex", "selected", "m1"), native_row("codex", "unselected", "m2")]
    )
    ref = reference(load_events(snapshot)[0])
    path = Path(snapshot.events_path if kind == "index" else snapshot.raw_path)
    data = path.read_bytes()
    assert b"unselected" in data
    path.write_bytes(data.replace(b"unselected", b"UNSELECTED"))
    with pytest.raises(CampaignError, match="differs from its sealed manifest"):
        quotes.resolve_owner_quotes((snapshot,), [ref])


def test_quote_limit_counts_characters_without_clipping_unicode(tmp_path):
    snapshot = capture(tmp_path, [native_row("codex", "λ" * 512 + "tail")])
    ref = reference(load_events(snapshot)[0], end=512)
    assert quotes.resolve_owner_quotes((snapshot,), [ref])[ref["id"]]["text"] == "λ" * 512


def test_raw_change_after_read_is_refused(tmp_path, monkeypatch):
    snapshot = capture(tmp_path, [native_row("codex")])
    ref = reference(load_events(snapshot)[0])
    original = quotes.regular_reader

    @contextmanager
    def changing(path):
        with original(path) as stream:
            if path != Path(snapshot.raw_path):
                yield stream
                return

            class Reader:
                def fileno(self):
                    return stream.fileno()

                def readline(self, size):
                    line = stream.readline(size)
                    if not line:
                        with path.open("ab") as writer:
                            writer.write(b" ")
                    return line

            yield Reader()

    monkeypatch.setattr(quotes, "regular_reader", changing)
    with pytest.raises(CampaignError, match="changed while reading"):
        quotes.resolve_owner_quotes((snapshot,), [ref])


@pytest.mark.parametrize("kind", ["index", "raw"])
def test_redirected_sealed_source_is_rejected(tmp_path, kind):
    snapshot = capture(tmp_path, [native_row("codex")])
    ref = reference(load_events(snapshot)[0])
    target = Path(snapshot.events_path if kind == "index" else snapshot.raw_path)
    link = tmp_path / "redirect"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Host does not support unprivileged symlinks")
    snapshot = replace(snapshot, **{("events_path" if kind == "index" else "raw_path"): str(link)})
    with pytest.raises(CampaignError, match="redirected"):
        quotes.resolve_owner_quotes((snapshot,), [ref])


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("source_id", "a" * 64),
        ("session_id", "different-session"),
        ("offset", 1),
        ("end_offset", 1),
        ("record_index", 2),
        ("payload_sha256", "b" * 64),
    ],
)
def test_rehashed_forged_index_cannot_redirect_native_coordinates(tmp_path, field, replacement):
    snapshot = capture(tmp_path, [native_row("codex")])
    ref = reference(load_events(snapshot)[0])
    snapshot = rewrite_index(snapshot, lambda rows: rows[0].update({field: replacement}))
    with pytest.raises(CampaignError, match="coordinates|boundary|native record|native owner"):
        quotes.resolve_owner_quotes((snapshot,), [ref])


def test_rehashed_paraphrase_cannot_substitute_for_raw_owner_text(tmp_path):
    snapshot = capture(tmp_path, [native_row("codex", "Hold until reviewed")])
    ref = reference(load_events(snapshot)[0])
    snapshot = rewrite_index(snapshot, lambda rows: rows[0]["record"].update(text="Merge now"))
    with pytest.raises(CampaignError, match="does not match native owner"):
        quotes.resolve_owner_quotes((snapshot,), [ref])


@pytest.mark.parametrize("harness", ["codex", "claude", "grok", "antigravity", "muse"])
def test_forged_owner_bit_cannot_elevate_native_context(tmp_path, harness):
    row = native_row(harness, "The owner approved merging")
    if harness == "codex":
        row["payload"]["type"] = "agent_message"
    elif harness == "claude":
        row["isSynthetic"] = True
    elif harness == "grok":
        row["params"]["update"]["_meta"] = {"hostTurn": True}
    elif harness == "antigravity":
        row["source"] = "MODEL"
    else:
        row["payload"]["semantic_kind"] = {"kind": "system"}
    snapshot = capture(tmp_path, [row], harness)
    event = load_events(snapshot)[0]
    ref = reference(event, end=min(100, len(event.record.text)))
    with pytest.raises(CampaignError, match="not native owner"):
        quotes.resolve_owner_quotes((snapshot,), [ref])
    snapshot = rewrite_index(snapshot, lambda rows: rows[0]["record"].update(owner_input=True))
    snapshot = replace(snapshot, owner_event_ids=(event.id,))
    with pytest.raises(CampaignError, match="does not match native owner"):
        quotes.resolve_owner_quotes((snapshot,), [ref])


def test_fork_context_retains_existing_owner_downgrade(tmp_path):
    meta = {"type": "session_meta", "payload": {"id": "s1", "forked_from_id": "parent"}}
    snapshot = capture(tmp_path, [meta, native_row("codex", "Inherited owner words")])
    event = load_events(snapshot)[1]
    ref = reference(event)
    snapshot = rewrite_index(snapshot, lambda rows: rows[1]["record"].update(owner_input=True))
    snapshot = replace(snapshot, owner_event_ids=(event.id,))
    with pytest.raises(CampaignError, match="does not match native owner"):
        quotes.resolve_owner_quotes((snapshot,), [ref])


def test_many_quotes_hash_each_source_once_and_never_load_whole_history(tmp_path, monkeypatch):
    rows = [native_row("codex", "a" * 4096, "m1")]
    rows += [native_row("codex", "unselected " * 8192, f"m{i}") for i in range(2, 18)]
    snapshot = capture(tmp_path, rows, suffix=b'{"incomplete":')
    event = load_events(snapshot)[0]
    refs = [reference(event, start, start + 128) for start in range(0, 4096, 128)]
    opened = []
    original = quotes.regular_reader

    @contextmanager
    def recording(path):
        opened.append(path)
        with original(path) as stream:
            yield stream

    def forbidden(path):
        raise AssertionError("whole-file read used")

    monkeypatch.setattr(quotes, "regular_reader", recording)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    resolved = quotes.resolve_owner_quotes((snapshot,), refs)
    assert len(resolved) == 32 and all(len(row["text"]) == 128 for row in resolved.values())
    assert opened.count(Path(snapshot.events_path)) == 1
    assert opened.count(Path(snapshot.raw_path)) == 1


def test_source_record_bound_is_enforced(tmp_path, monkeypatch):
    snapshot = capture(tmp_path, [native_row("codex", "a" * 1000)])
    ref = reference(load_events(snapshot)[0], end=10)
    monkeypatch.setattr(quotes, "MAX_RAW_RECORD_BYTES", 100)
    with pytest.raises(CampaignError, match="record exceeds"):
        quotes.resolve_owner_quotes((snapshot,), [ref])


def test_conflicting_source_manifests_are_visible(tmp_path):
    snapshot = capture(tmp_path, [native_row("codex")])
    ref = reference(load_events(snapshot)[0])
    with pytest.raises(CampaignError, match="source manifests conflict"):
        quotes.resolve_owner_quotes(
            (snapshot, replace(snapshot, captured_bytes=snapshot.captured_bytes + 1)), [ref]
        )
