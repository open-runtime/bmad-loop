"""Native review evidence uses exact parent/delegate lineage, never prose."""

import copy
import json

import pytest
from test_campaign_codex_review import finding, review

from bmad_loop.campaign_codex_review_evidence import decode, evidence, snapshot
from bmad_loop.campaign_model import CampaignError


def records():
    parent = [
        {"type": "session_meta", "payload": {"id": "parent"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "parent",
                "turn_id": "root",
                "item": {
                    "type": "ExitedReviewMode",
                    "id": "review-item",
                    "review_output": review([finding()]),
                },
            },
        },
    ]
    child = [
        {
            "type": "session_meta",
            "payload": {
                "id": "child",
                "parent_thread_id": "parent",
                "source": {"subagent": "review"},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": "delegate",
                "root_turn_id": "root",
                "turn_attribution": {"parent_turn_id": "root"},
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "delegate",
                "root_turn_id": "root",
                "model": "native-model",
                "effort": "high",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 33242,
                        "output_tokens": 595,
                        "cached_input_tokens": 25088,
                        "reasoning_output_tokens": 223,
                    }
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "delegate", "root_turn_id": "root"},
        },
    ]
    return parent, child


def decode_rows(parent, child):
    return decode(
        parent,
        child,
        thread="parent",
        root_turn="root",
        child_turn="delegate",
        item_id="review-item",
    )


def test_native_delegate_snapshot_retains_actual_findings_usage_and_identity(tmp_path):
    parent, child = records()
    for name, rows in [("parent", parent), ("child", child)]:
        (tmp_path / (name + ".jsonl")).write_text("".join(json.dumps(row) + "\n" for row in rows))
    binding = snapshot(
        tmp_path / "parent.jsonl",
        tmp_path / "snapshots",
        thread="parent",
        root_turn="root",
        child_turn="delegate",
        item_id="review-item",
    )
    observed = evidence(binding)
    assert observed["model"] == "native-model" and observed["session_id"] == "child"
    assert (
        observed["usage"]["output_tokens"] == 595 and observed["usage"]["reasoning_tokens"] == 223
    )
    assert observed["review"]["findings"] == [finding()]
    (tmp_path / "snapshots/review-child.jsonl").write_text("{}\n")
    with pytest.raises(CampaignError, match="snapshot changed"):
        evidence(binding)


@pytest.mark.parametrize(
    "change", ["parent", "root", "child", "item", "source", "missing-end", "usage-decrease"]
)
def test_delegate_records_cannot_borrow_another_review_or_incomplete_usage(change):
    parent, child = records()
    if change == "parent":
        child[0]["payload"]["parent_thread_id"] = "other"
    elif change == "root":
        child[1]["payload"]["root_turn_id"] = "other"
    elif change == "child":
        child[2]["payload"]["turn_id"] = "other"
    elif change == "item":
        parent[1]["payload"]["item"]["id"] = "other"
    elif change == "source":
        child[0]["payload"]["source"] = {"subagent": "ordinary"}
    elif change == "missing-end":
        child.pop()
    else:
        decreased = copy.deepcopy(child[3])
        decreased["payload"]["info"]["total_token_usage"]["output_tokens"] = 1
        child.insert(4, decreased)
    with pytest.raises(CampaignError, match="lineage|artifact|usage"):
        decode_rows(parent, child)
