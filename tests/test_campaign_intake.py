"""Intake cursor durability, edited inputs and triage authority boundaries."""

from dataclasses import replace

import pytest

from bmad_loop.campaign_intake import GithubIntake, TriageProposal, validate_triage


class Gateway:
    repository = "a/b"

    def __init__(self):
        self.rows = []
        self.comment_rows = []
        self.since = None

    def issues(self, *, state, since):
        self.since = since
        return self.rows

    def issue(self, number):
        return next(row for row in self.rows if row["number"] == number)

    def comments(self, number):
        return self.comment_rows


def issue(number, body="Acceptance", updated="2026-11-05T10:00:00Z"):
    return {
        "id": 100 + number,
        "number": number,
        "title": "New work",
        "body": body,
        "updated_at": updated,
        "labels": [],
        "user": {"login": "owner", "id": 5},
        "html_url": f"https://github.com/a/b/issues/{number}",
    }


def test_backlog_batches_equal_timestamps_never_skip_after_restart():
    gateway = Gateway()
    gateway.rows = [issue(number) for number in range(1, 8)]
    intake = GithubIntake(gateway)
    seen = set()
    cursor = ""
    for _ in range(3):
        batch = intake.poll(cursor=cursor, seen_versions=seen, max_batch=3)
        seen.update(row.version_id for row in batch.observations)
        cursor = batch.next_cursor
    assert len(seen) == 7
    assert gateway.since == "2026-11-05T09:58:00Z"
    assert intake.poll(cursor=cursor, seen_versions=seen).observations == ()


def test_edits_are_new_versions_and_pr_records_do_not_become_issues():
    gateway = Gateway()
    gateway.rows = [issue(1), {**issue(2), "pull_request": {}}]
    intake = GithubIntake(gateway)
    first = intake.poll()
    assert len(first.observations) == 1
    gateway.rows[0] = issue(1, "Changed acceptance", "2026-11-05T10:01:00Z")
    second = intake.poll(cursor=first.next_cursor, seen_versions={first.observations[0].version_id})
    assert second.observations[0].digest != first.observations[0].digest
    assert second.observations[0].payload["body"] == "Changed acceptance"


def test_markers_only_suppress_verified_own_author_comments():
    gateway = Gateway()
    gateway.rows = [issue(1)]
    gateway.comment_rows = [
        {**issue(2), "body": "Own marker <!-- receipt -->", "user": {"id": 9, "login": "bot"}},
        {**issue(3), "body": "Human cites <!-- receipt -->", "user": {"id": 5, "login": "owner"}},
    ]
    batch = GithubIntake(gateway).poll(
        mapped_numbers=(1,), own_comment_markers=("<!-- receipt -->",), own_comment_author_ids=(9,)
    )
    comments = [row for row in batch.observations if row.kind == "comment"]
    assert [row.author for row in comments] == ["owner"]


def test_triage_validates_current_versions_scope_and_same_pr_review():
    gateway = Gateway()
    gateway.rows = [issue(1)]
    observations = GithubIntake(gateway).poll().observations
    proposal = TriageProposal(
        1,
        "new_story",
        (observations[0].version_id,),
        "new-unit",
        "Existing issue needs implementation",
        ("Works",),
        within_scope=True,
        graph_revision=3,
    )
    validate_triage(
        proposal, observations, graph_revision=3, known_units={"existing"}, allow_new_units=True
    )
    with pytest.raises(ValueError, match="scope"):
        validate_triage(
            replace(proposal, within_scope=False),
            observations,
            graph_revision=3,
            known_units={"existing"},
            allow_new_units=True,
        )
    with pytest.raises(ValueError, match="identities"):
        validate_triage(
            replace(proposal, input_versions=("stale",)),
            observations,
            graph_revision=3,
            known_units={"existing"},
            allow_new_units=True,
        )
    with pytest.raises(ValueError, match="existing PR"):
        validate_triage(
            proposal,
            observations,
            graph_revision=3,
            known_units={"existing"},
            required_review=True,
            current_pr=7,
            allow_new_units=True,
        )
    repair = replace(proposal, disposition="repair_current_pr", unit_id="existing", related_pr=7)
    validate_triage(
        repair,
        observations,
        graph_revision=3,
        known_units={"existing"},
        required_review=True,
        current_pr=7,
    )
