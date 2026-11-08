"""Read the native review delegate's structured result and usage by lineage.

App-server renders ExitedReviewMode as prose and omits delegate token usage.
The native rollout retains both. Snapshot exact source bytes, never infer model
identity or usage from the requested route or rendered review text.
"""

import json
from pathlib import Path

from .campaign_model import CampaignError
from .platform_util import is_link_like

MAX_BYTES = 32 * 1024 * 1024


def records(path):
    if not path.is_file() or path.stat().st_size > MAX_BYTES or is_link_like(path):
        raise CampaignError("Native review rollout is missing, redirected or oversized")
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise CampaignError("Native review rollout grew beyond its byte bound")
    rows = [json.loads(line) for line in raw.splitlines() if line]
    if any(not isinstance(row, dict) or not isinstance(row.get("payload"), dict) for row in rows):
        raise CampaignError("Native review rollout contains invalid records")
    return raw, rows


def decode(parent_rows, child_rows, *, thread, root_turn, child_turn, item_id):
    """Validate the real parent/child/native-turn links before using telemetry."""
    from .campaign_codex_review import validate_review

    parent_meta = [row["payload"] for row in parent_rows if row.get("type") == "session_meta"]
    child_meta = [row["payload"] for row in child_rows if row.get("type") == "session_meta"]
    if (
        len(parent_meta) != 1
        or parent_meta[0].get("id") != thread
        or len(child_meta) != 1
        or child_meta[0].get("parent_thread_id") != thread
        or child_meta[0].get("source") != {"subagent": "review"}
        or not child_meta[0].get("id")
    ):
        raise CampaignError("Native review delegate lineage differs from the owned thread")
    outputs = [
        row["payload"]["item"].get("review_output")
        for row in parent_rows
        if row.get("type") == "event_msg"
        and row["payload"].get("type") == "item_completed"
        and row["payload"].get("thread_id") == thread
        and row["payload"].get("turn_id") == root_turn
        and row["payload"].get("item", {}).get("type") == "ExitedReviewMode"
        and row["payload"]["item"].get("id") == item_id
    ]
    if len(outputs) != 1:
        raise CampaignError("Native structured review artifact is missing or ambiguous")
    review = validate_review(outputs[0])
    contexts = [row["payload"] for row in child_rows if row.get("type") == "turn_context"]
    starts = [row["payload"] for row in child_rows if row["payload"].get("type") == "task_started"]
    ends = [row["payload"] for row in child_rows if row["payload"].get("type") == "task_complete"]
    if (
        not contexts
        or any(
            row.get("turn_id") != child_turn or row.get("root_turn_id") != root_turn
            for row in contexts
        )
        or len(starts) != 1
        or starts[0].get("turn_id") != child_turn
        or starts[0].get("root_turn_id") != root_turn
        or starts[0].get("turn_attribution", {}).get("parent_turn_id") != root_turn
        or len(ends) != 1
        or ends[0].get("turn_id") != child_turn
        or ends[0].get("root_turn_id") != root_turn
    ):
        raise CampaignError("Native review delegate turn lineage is incomplete")
    models = {row.get("model") for row in contexts}
    efforts = {row.get("effort") for row in contexts}
    if (
        len(models) != 1
        or not all(isinstance(model, str) and model for model in models)
        or len(efforts) != 1
    ):
        raise CampaignError("Native review delegate attribution is ambiguous")
    usage = {}
    for row in child_rows:
        payload = row["payload"]
        if payload.get("type") != "token_count":
            continue
        totals = (payload.get("info") or {}).get("total_token_usage")
        if totals is None:
            continue
        for source, target in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cached_input_tokens", "cache_read_tokens"),
            ("reasoning_output_tokens", "reasoning_tokens"),
        ):
            value = totals.get(source)
            if type(value) is not int or value < usage.get(target, 0):
                raise CampaignError("Native review delegate usage is invalid or decreased")
            usage[target] = value
    if not usage or usage.get("output_tokens", 0) < 1:
        raise CampaignError("Native review delegate lacks actual token telemetry")
    return {
        "review": review,
        "model": next(iter(models)),
        "effort": next(iter(efforts)),
        "session_id": child_meta[0]["id"],
        "usage": usage,
    }


def snapshot(parent_path, directory, *, thread, root_turn, child_turn, item_id):
    """Select one actual review delegate by native parent and turn identifiers."""
    from .campaign_headless import file_digest

    parent_path = Path(parent_path)
    parent_raw, parent_rows = records(parent_path)
    matches = []
    paths = list(parent_path.parent.glob("*.jsonl"))
    if len(paths) > 4096:
        raise CampaignError("Native review rollout directory exceeds its lookup bound")
    for path in paths:
        if path == parent_path or is_link_like(path) or not path.is_file():
            continue
        with path.open("rb") as source:
            first = source.readline(2 * 1024 * 1024 + 1)
        if len(first) > 2 * 1024 * 1024:
            continue
        try:
            meta = json.loads(first)
        except ValueError:
            continue
        payload = meta.get("payload", {})
        if (
            meta.get("type") != "session_meta"
            or payload.get("parent_thread_id") != thread
            or payload.get("source") != {"subagent": "review"}
        ):
            continue
        raw, rows = records(path)
        if any(
            row["payload"].get("type") == "task_started"
            and row["payload"].get("turn_id") == child_turn
            and row["payload"].get("root_turn_id") == root_turn
            for row in rows
        ):
            matches.append((raw, rows))
    if len(matches) != 1:
        raise CampaignError("Native review delegate rollout is missing or ambiguous")
    child_raw, child_rows = matches[0]
    decode(
        parent_rows,
        child_rows,
        thread=thread,
        root_turn=root_turn,
        child_turn=child_turn,
        item_id=item_id,
    )
    directory.mkdir(mode=0o700)
    parent_copy, child_copy = directory / "parent.jsonl", directory / "review-child.jsonl"
    parent_copy.write_bytes(parent_raw)
    child_copy.write_bytes(child_raw)
    return {
        "schema_version": 1,
        "thread": thread,
        "root_turn": root_turn,
        "child_turn": child_turn,
        "item_id": item_id,
        "parent": {"path": str(parent_copy), "sha256": file_digest(parent_copy)},
        "child": {"path": str(child_copy), "sha256": file_digest(child_copy)},
    }


def evidence(binding):
    from .campaign_headless import file_digest

    if not isinstance(binding, dict) or binding.get("schema_version") != 1:
        raise CampaignError("Native review rollout binding is invalid")
    rows = []
    for key in ("parent", "child"):
        source = binding[key]
        path = Path(source["path"])
        if not path.is_absolute() or file_digest(path) != source["sha256"]:
            raise CampaignError("Native review rollout snapshot changed")
        rows.append(records(path)[1])
    return decode(
        *rows,
        thread=binding["thread"],
        root_turn=binding["root_turn"],
        child_turn=binding["child_turn"],
        item_id=binding["item_id"],
    )
