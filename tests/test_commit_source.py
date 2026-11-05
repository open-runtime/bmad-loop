"""Finalization recovery consumes independent verification-source evidence."""

import copy
import json
from datetime import datetime, timezone

import pytest
import test_commit_recovery as recovery_tests
from conftest import git

from bmad_loop import runs, verify

commit_case = recovery_tests.commit_case
_engine = recovery_tests._engine


def _rows(case):
    return [json.loads(line) for line in (case.run_dir / "journal.jsonl").read_text().splitlines()]


def _write_rows(case, rows):
    (case.run_dir / "journal.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def _record_source(case):
    identity = {
        "head": case.head,
        "tree": git(case.root, "rev-parse", "HEAD^{tree}"),
        "clean": True,
    }
    source = {"before": identity.copy(), "after": identity.copy()}
    rows = _rows(case)
    for row in rows:
        if row.get("kind") == "verify-command-result" and row.get("verification_stage") == "review":
            row["verification_source"] = copy.deepcopy(source)
    _write_rows(case, rows)
    return rows


def test_review_sink_records_source_around_verification(commit_case):
    case = commit_case
    engine, _, task = _engine(case)
    before = {"head": case.head, "tree": git(case.root, "rev-parse", "HEAD^{tree}"), "clean": True}
    sink = engine._review_command_sink(task)
    (case.root / "implementation.txt").write_text("Changed by the verification command.\n")
    git(case.root, "add", "-A")
    git(case.root, "commit", "-qm", "change during verification")
    after = {
        "head": git(case.root, "rev-parse", "HEAD"),
        "tree": git(case.root, "rev-parse", "HEAD^{tree}"),
        "clean": True,
    }
    sink((verify.CommandResult("true", 0, ""),))
    assert _rows(case)[-1]["verification_source"] == {"before": before, "after": after}


def test_review_sink_records_unavailable_source_without_losing_result(commit_case, monkeypatch):
    case = commit_case
    engine, _, task = _engine(case)

    def unavailable(_root):
        raise verify.GitError("cannot read repository")

    monkeypatch.setattr(verify, "rev_parse_head", unavailable)
    sink = engine._review_command_sink(task)
    sink((verify.CommandResult("true", 0, "verifier completed"),))
    row = _rows(case)[-1]
    assert row["returncode"] == 0
    assert row["verification_source"]["before"]["error"]
    assert row["verification_source"]["after"]["error"]


def test_recorded_source_needs_no_legacy_file(commit_case):
    case = commit_case
    _record_source(case)
    (case.run_dir / "sync-with-main.json").unlink()
    case.arm()


@pytest.mark.parametrize(
    "change", ["missing_row", "null", "head", "tree", "dirty", "drift", "error"]
)
def test_bad_recorded_source_cannot_fall_back_to_legacy_proof(commit_case, change):
    case = commit_case
    rows = _record_source(case)
    row = next(r for r in rows if "verification_source" in r)
    source = row["verification_source"]
    if change == "missing_row":
        del row["verification_source"]
    elif change == "null":
        row["verification_source"] = None
    elif change == "head":
        source["before"]["head"] = source["after"]["head"] = case.task.baseline_commit
    elif change == "tree":
        source["before"]["tree"] = source["after"]["tree"] = "0" * 40
    elif change == "dirty":
        source["before"]["clean"] = source["after"]["clean"] = False
    elif change == "drift":
        source["before"]["head"] = case.task.baseline_commit
    else:
        source["before"] = source["after"] = {"error": "GitError"}
    _write_rows(case, rows)
    before = (case.run_dir / "state.json").read_bytes()
    with pytest.raises(runs.RearmError, match="verification source"):
        case.arm()
    assert (case.run_dir / "state.json").read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "absent",
        "invalid_json",
        "head",
        "old",
        "new",
        "naive",
        "missing_hook",
        "wrong_task",
        "failed_hook",
    ],
)
def test_legacy_source_requires_matching_contemporaneous_hook(commit_case, change):
    case = commit_case
    path = case.run_dir / "sync-with-main.json"
    record = json.loads(path.read_text())
    chain = record["chains"][case.task.story_key]
    rows = _rows(case)
    hook = next(r for r in rows if r.get("plugin") == "sync-with-main")
    if change == "absent":
        path.unlink()
    elif change == "invalid_json":
        path.write_text("{")
    else:
        if change == "head":
            chain["chain_head"] = case.task.baseline_commit
        elif change in ("old", "new"):
            chain["recorded_at"] = datetime.fromtimestamp(
                hook["ts"] + (-3600 if change == "old" else 3600), timezone.utc
            ).isoformat()
        elif change == "naive":
            chain["recorded_at"] = chain["recorded_at"].replace("+00:00", "")
        elif change == "missing_hook":
            rows.remove(hook)
        elif change == "wrong_task":
            hook["log_task"] = "205.8-review-1"
        else:
            hook["rc"] = 1
        path.write_text(json.dumps(record))
    _write_rows(case, rows)
    before = (case.run_dir / "state.json").read_bytes()
    with pytest.raises(runs.RearmError, match="verification source"):
        case.arm()
    assert (case.run_dir / "state.json").read_bytes() == before
