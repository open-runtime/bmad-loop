"""Both native review passes survive mapping and bind the exact candidate/base."""

import copy
import json

import pytest
from conftest import git
from test_campaign_codex_appserver import frames
from test_campaign_codex_review import finding, review
from test_campaign_codex_review_evidence import records

from bmad_loop.campaign_codex_review import fold_result
from bmad_loop.campaign_codex_review_evidence import snapshot
from bmad_loop.campaign_codex_review_pair import assert_target, stage_evidence, validate_receipt
from bmad_loop.campaign_headless_protocol import parse_output
from bmad_loop.campaign_model import CampaignError


@pytest.fixture
def pair(tmp_path):
    parent = [{"type": "session_meta", "payload": {"id": "native"}}]
    children = []
    for index, kind in enumerate(("main", "custom"), 1):
        source, child = records()
        output = copy.deepcopy(source[1])
        output["payload"].update(thread_id="native", turn_id=kind)
        output["payload"]["item"]["id"] = kind + "-item"
        issue = finding()
        issue["title"] = f"[P{index}] {kind} finding"
        output["payload"]["item"]["review_output"] = review([issue])
        parent.append(output)
        child[0]["payload"].update(id=kind + "-delegate", parent_thread_id="native")
        for row in child:
            payload = row["payload"]
            if "turn_id" in payload:
                payload.update(turn_id=kind + "-child", root_turn_id=kind)
            if "turn_attribution" in payload:
                payload["turn_attribution"]["parent_turn_id"] = kind
            if "model" in payload:
                payload["model"] = "gpt-test"
        children.append(child)
    source_dir = tmp_path / "rollouts"
    source_dir.mkdir()
    parent_path = source_dir / "parent.jsonl"
    parent_path.write_text("".join(json.dumps(row) + "\n" for row in parent))
    for index, child in enumerate(children):
        (source_dir / f"child-{index}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in child)
        )
    result = [
        frames()[0],
        {"method": "bmad/nativeReviewPair", "params": {"head": "a" * 40, "base": "b" * 40}},
    ]
    for index, (identifier, kind) in enumerate(((3, "main"), (6, "custom"), (4, "mapping")), 1):
        segment = frames()[1:]
        segment[0] = {"id": identifier, "result": {"turn": {"id": kind}}}
        segment[1]["params"]["turnId"] = kind
        segment[1]["params"]["tokenUsage"]["total"]["outputTokens"] = 20 * index
        segment[2]["params"]["turn"]["id"] = kind
        if kind != "mapping":
            segment[2]["params"]["turn"]["items"] = [
                {
                    "id": kind + "-item",
                    "type": "exitedReviewMode",
                    "review": "native rendered prose",
                }
            ]
            segment.insert(
                1,
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "native",
                        "turn": {"id": kind + "-child", "rootTurnId": kind},
                    },
                },
            )
            binding = snapshot(
                parent_path,
                tmp_path / kind,
                thread="native",
                root_turn=kind,
                child_turn=kind + "-child",
                item_id=kind + "-item",
            )
            segment.append({"method": "bmad/nativeReviewEvidence", "params": binding})
        result.extend(segment)
    return result


def parse(rows, **kwargs):
    return parse_output(
        json.dumps(rows), protocol="codex-native-review", selected_model="gpt-test", **kwargs
    )


def test_main_and_custom_actual_reviews_keep_both_findings_delegate_spend_and_three_turns(pair):
    receipt = parse(pair)
    assert receipt["outcome"] == "completed" and receipt["turns"] == 3
    assert receipt["usage"]["output_tokens"] == 60 + 595 * 2
    binding = receipt["native_review_evidence"]
    assert [row["kind"] for row in binding["reviews"]] == ["main", "custom"]
    assert binding["target"] == {"head": "a" * 40, "base": "b" * 40}
    result = fold_result({"verdict": "approved", "findings": []}, receipt["native_review"])
    assert result["verdict"] == "changes_requested"
    assert [row["title"] for row in result["findings"]] == [
        "[P1] main finding",
        "[P2] custom finding",
    ]
    assert all(row["required"] and not row["resolved"] for row in result["findings"])


@pytest.mark.parametrize("kind", ["main", "custom"])
def test_a_mapping_or_one_review_cannot_replace_either_native_pass(pair, kind):
    pair[:] = [
        row
        for row in pair
        if not (
            row.get("method") == "bmad/nativeReviewEvidence" and row["params"]["root_turn"] == kind
        )
    ]
    with pytest.raises(CampaignError, match="structured JSON|snapshots"):
        parse(pair)


@pytest.mark.parametrize(
    "fault", ["missing-main", "missing-custom", "mapping-only", "duplicate", "order", "target"]
)
def test_native_pair_incomplete_or_rebound_identity_is_refused(pair, fault):
    if fault == "missing-main":
        pair[:] = [row for row in pair if row.get("id") != 3]
    elif fault == "missing-custom":
        pair[:] = [row for row in pair if row.get("id") != 6]
    elif fault == "mapping-only":
        pair[:] = pair[:2] + pair[-3:]
    elif fault == "duplicate":
        pair.append(next(row for row in pair if row.get("id") == 6))
    elif fault == "order":
        for row in pair:
            if row.get("id") in {3, 6}:
                row["id"] = 6 if row["id"] == 3 else 3
    else:
        pair[1]["params"]["head"] = "main"
    with pytest.raises(CampaignError):
        parse(pair)


def test_native_pair_must_match_exact_frozen_candidate_and_base(pair):
    receipt = parse(pair)
    cap = {
        "protocol": "codex-native-review",
        "argv": ["--review-pair"],
        "confinement": "codex-native-read-only-v1",
        "model": "gpt-test",
        "efforts": ["high"],
    }
    frozen = {"head": "a" * 40, "base": "b" * 40}
    validate_receipt(receipt, cap, frozen)
    for key in ("head", "base"):
        with pytest.raises(CampaignError, match="frozen delegate"):
            validate_receipt(receipt, cap, {**frozen, key: "c" * 40})
    receipt["turns"] = 2
    with pytest.raises(CampaignError, match="bounded turns"):
        validate_receipt(receipt, cap, frozen)


def test_native_pair_checks_actual_candidate_and_integrated_base(project):
    root = project.project
    baseline = git(root, "rev-parse", "HEAD")
    frozen = {"head": baseline, "base": baseline}
    assert assert_target(root, frozen) == frozen
    git(root, "commit", "--allow-empty", "-m", "new candidate")
    with pytest.raises(CampaignError, match="candidate changed"):
        assert_target(root, frozen)
    current = git(root, "rev-parse", "HEAD")
    assert assert_target(root, {"head": current, "base": baseline})["head"] == current
    with pytest.raises(CampaignError, match="not integrated"):
        assert_target(root, {"head": current, "base": "f" * 40})


def test_pending_custom_review_is_not_completion(pair):
    boundary = next(i for i, row in enumerate(pair) if row.get("id") == 6)
    assert parse(pair[:boundary], partial=True)["outcome"] == "failed"


def test_stage_pair_proof_binds_controller_request_and_preserved_receipt(pair, tmp_path):
    from bmad_loop.campaign_headless import file_digest
    from bmad_loop.campaign_jobs import digest
    from bmad_loop.platform_util import session_task_id

    cap = {
        "protocol": "codex-native-review",
        "argv": ["--review-pair"],
        "confinement": "codex-native-read-only-v1",
        "model": "gpt-test",
        "efforts": ["high"],
    }
    coordinates = {
        "campaign": "campaign",
        "unit": "unit",
        "stage": "native_review_pair",
        "head": "a" * 40,
        "base": "b" * 40,
        "graph_revision": 2,
        "config_revision": 3,
    }
    launch = {
        "execution": str(tmp_path / "execution.json"),
        "run_id": "run",
        "role": "native_review_pair",
        "attempt": 1,
        "generation": 2,
        "campaign_id": "campaign",
        "unit_id": "unit",
        "launch_id": "worker-launch",
    }
    job_root = tmp_path / "native/run/headless/job"
    job_root.mkdir(parents=True)
    native_task = session_task_id("campaign", "native_review_pair", 1, 2)
    request = {"coordinates": coordinates}
    native = {
        **parse(pair),
        "request_digest": digest(request),
        "launch_id": native_task,
        "generation": 2,
    }
    envelope = {
        "request": request,
        "request_digest": digest(request),
        "capability": cap,
        "launch_id": native_task,
        "generation": 2,
    }
    request_path, receipt_path = job_root / "request.json", job_root / "receipt.json"
    request_path.write_text(json.dumps(envelope))
    receipt_path.write_text(json.dumps(native))
    stage = {
        **launch,
        "artifact": {"native_review": native["native_review"]},
        "native_binding": {
            "headless_request": str(request_path),
            "headless_request_sha256": file_digest(request_path),
            "reasoning_receipt": str(receipt_path),
            "headless_receipt_sha256": file_digest(receipt_path),
        },
    }
    assert stage_evidence(stage, cap, coordinates, launch=launch)["target"] == {
        "head": "a" * 40,
        "base": "b" * 40,
    }
    for changed in ({**coordinates, "graph_revision": 4}, {**coordinates, "head": "c" * 40}):
        with pytest.raises(CampaignError, match="frozen candidate"):
            stage_evidence(stage, cap, changed, launch=launch)
    tampered = copy.deepcopy(stage)
    tampered["artifact"]["native_review"] = review()
    with pytest.raises(CampaignError, match="frozen candidate"):
        stage_evidence(tampered, cap, coordinates, launch=launch)
    for key in ("unit_id", "launch_id", "attempt", "generation"):
        changed_launch = {**launch, key: "other"}
        with pytest.raises(CampaignError, match="another worker launch"):
            stage_evidence(stage, cap, coordinates, launch=changed_launch)
    receipt_path.write_text(json.dumps({**native, "turns": 1}))
    with pytest.raises(CampaignError, match="request or receipt changed"):
        stage_evidence(stage, cap, coordinates, launch=launch)
