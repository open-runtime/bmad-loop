"""Exact external commits never masquerade as observed campaign model sessions."""

import base64
import copy
import dataclasses
import hashlib
import subprocess

import pytest
from test_campaign_publication import publishable as publishable

from bmad_loop import campaign_external_provenance as provenance
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    UnitSpec,
)
from bmad_loop.campaign_publication_flow import record_stage_writer


def git(repo, *args):
    return (
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
        .stdout.decode()
        .strip()
    )


@pytest.fixture
def case(project):
    repo = project.project
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "external-provenance-remote")
    git(
        repo,
        "-c",
        "user.name=Cursor Agent",
        "-c",
        "user.email=bot@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "Bot claims model=claude; that is not telemetry",
    )
    remote = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "campaign-contribution", base)
    git(repo, "commit", "--allow-empty", "-m", "Campaign writer checkpoint")
    local = git(repo, "rev-parse", "HEAD")
    git(repo, "merge", "--no-edit", "external-provenance-remote")
    combined = git(repo, "rev-parse", "HEAD")
    unit = CampaignUnit(UnitSpec("unit", "source", "epic", "Unit"))
    unit.bindings.update(
        pr={"number": 42, "node_id": "PR_42"},
        branch="campaign-contribution",
        source_revision="source-revision",
        head=combined,
    )
    snapshot = CampaignSnapshot("campaign", "org/repo", CampaignSpec("proof"), {unit.id: unit})
    contribution = provenance.collect(
        repo,
        repository=snapshot.repository,
        pr_number=42,
        pr_node_id="PR_42",
        branch=unit.bindings["branch"],
        local_before=local,
        remote_head=remote,
        target_head=base,
    )
    receipt = {
        "launch_id": "integration-worker",
        "outcome": "integrated",
        "head": combined,
        "target_head": base,
        "external_contribution": contribution,
        "remote_head_sync": {
            "schema_version": 1,
            "id": "remote-debt",
            "repository": snapshot.repository,
            "pr_number": 42,
            "pr_node_id": "PR_42",
            "branch": unit.bindings["branch"],
            "target": snapshot.spec.target,
            "anchor": base,
            "head": remote,
            "local_head": local,
            "graph_revision": snapshot.graph_revision,
            "config_revision": snapshot.config_revision,
            "source_revision": "source-revision",
        },
    }
    return repo, snapshot, unit, receipt


def test_exact_git_authorship_retained_without_guessing_bot_model(case):
    repo, snapshot, unit, receipt = case
    value = receipt["external_contribution"]
    assert value["commit_count"] == 1
    row = value["commits"][0]
    raw = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "commit", row["sha"]],
        capture_output=True,
        check=True,
    ).stdout
    assert row["sha"] == value["remote_head"]
    assert row["parents"] == [value["target_head"]]
    assert row["object_sha256"] == hashlib.sha256(raw).hexdigest()
    author = next(line[7:] for line in raw.splitlines() if line.startswith(b"author "))
    assert base64.b64decode(row["author"]["raw_base64"]) == author
    assert "Cursor Agent" in row["author"]["display"]
    assert row["observed_models"] == row["provider_families"] == []
    assert row["model_status"] == "unobserved_external"
    assert provenance.record(snapshot, unit, receipt)
    first = copy.deepcopy(unit.evidence["external_contributions"])
    receipt["external_contribution"]["commits"][0]["author"]["display"] = "Changed caller"
    assert unit.evidence["external_contributions"] == first


def test_record_replay_and_conflict_retain_provenance_without_claiming_delivery(case):
    _, snapshot, unit, receipt = case
    receipt["outcome"] = "integration_conflict"
    receipt["head"] = receipt["external_contribution"]["local_before"]
    before = copy.deepcopy(unit.bindings)
    assert provenance.record(snapshot, unit, receipt)
    assert not provenance.record(snapshot, unit, receipt)
    assert len(snapshot.events) == 1
    assert unit.bindings == before
    assert unit.evidence["external_contributions"][0]["outcome"] == "integration_conflict"
    assert unit.evidence["external_contributions"][0]["external_model_independence"] == "unproven"
    assert "existing_source_verified" not in unit.evidence
    assert "developer_family" not in unit.evidence


def test_already_integrated_remote_keeps_first_observation_contributors(case):
    repo, snapshot, unit, receipt = case
    original = receipt["external_contribution"]
    combined = receipt["head"]
    updated = provenance.collect(
        repo,
        repository=snapshot.repository,
        pr_number=42,
        pr_node_id="PR_42",
        branch=unit.bindings["branch"],
        local_before=combined,
        remote_head=original["remote_head"],
        target_head=original["target_head"],
        provenance_before=original["local_before"],
    )
    assert updated["commits"] == original["commits"]
    assert updated["commit_count"] == 1
    receipt["external_contribution"] = updated
    receipt["remote_head_sync"].update(
        local_head=combined,
        provenance_before=original["local_before"],
    )
    assert provenance.record(snapshot, unit, receipt)
    assert unit.evidence["external_contributions"][0]["provenance"] == updated


@pytest.mark.parametrize("unknown", [False, True])
def test_external_ledger_and_actual_astra_writer_preserve_campaign_gate(case, publishable, unknown):
    _, snapshot, unit, receipt = case
    unit.evidence.update(
        developer_family="meta",
        developer_families=["meta"],
        developer_attribution_incomplete=unknown,
    )
    before = copy.deepcopy(unit.evidence)
    provenance.record(snapshot, unit, receipt)
    assert {key: unit.evidence[key] for key in before} == before
    record_stage_writer(unit, {"provider_family": "openai", "observed_model": "gpt-6-astra"})
    assert unit.evidence["developer_families"] == ["meta", "openai"]
    assert unit.evidence["developer_attribution_incomplete"] is unknown
    assert (
        unit.evidence["external_contributions"][0]["provenance"]["commits"][0]["provider_families"]
        == []
    )
    controller, candidate, evidence, observation = publishable
    evidence = dataclasses.replace(
        evidence,
        developer_family=unit.evidence["developer_family"],
        developer_families=tuple(unit.evidence["developer_families"]),
        attribution_incomplete=unit.evidence["developer_attribution_incomplete"],
    )
    assert (
        controller.evaluate(candidate, evidence, observation, independent_families=True).ready
        is not unknown
    )
    for family in ("meta", "openai"):
        assert not controller.evaluate(
            candidate,
            dataclasses.replace(evidence, reviewer_family=family),
            observation,
            independent_families=True,
        ).ready


@pytest.mark.parametrize(
    "field", ["repository", "pr_number", "pr_node_id", "branch", "local_before", "remote_head"]
)
def test_coordinate_mismatch_cannot_record_external_authority(case, field):
    _, snapshot, unit, receipt = case
    receipt["external_contribution"][field] = "different"
    with pytest.raises(CampaignError, match="coordinates"):
        provenance.record(snapshot, unit, receipt)
    assert not unit.evidence and not snapshot.events


@pytest.mark.parametrize("field", ["graph_revision", "config_revision", "source_revision"])
def test_stale_ownership_revision_refuses_before_recording(case, field):
    _, snapshot, unit, receipt = case
    receipt["remote_head_sync"][field] = "stale"
    with pytest.raises(CampaignError, match="coordinates"):
        provenance.record(snapshot, unit, receipt)
    assert not unit.evidence


def test_git_claims_cannot_supply_model_family(case):
    _, snapshot, unit, receipt = case
    receipt["external_contribution"]["commits"][0]["provider_families"] = ["anthropic"]
    with pytest.raises(CampaignError, match="Git facts"):
        provenance.record(snapshot, unit, receipt)
    assert not unit.evidence


def test_display_identity_cannot_differ_from_preserved_bytes(case):
    _, snapshot, unit, receipt = case
    receipt["external_contribution"]["commits"][0]["author"]["display"] = "Pretend human"
    with pytest.raises(CampaignError, match="display differs"):
        provenance.record(snapshot, unit, receipt)
    assert not unit.evidence


def test_git_replace_cannot_substitute_another_author_for_exact_commit(case):
    repo, _, _, receipt = case
    value = receipt["external_contribution"]
    sha = value["remote_head"]
    raw = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "commit", sha], capture_output=True, check=True
    ).stdout
    altered = raw.replace(b"Cursor Agent", b"Imposter Author")
    replacement = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
            input=altered,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    git(repo, "replace", sha, replacement)
    assert "Imposter Author" in git(repo, "show", "-s", "--format=%an", sha)
    observed = provenance.collect(
        repo,
        **{
            key: value[key]
            for key in (
                "repository",
                "pr_number",
                "pr_node_id",
                "branch",
                "local_before",
                "remote_head",
                "target_head",
            )
        },
    )
    assert observed == value


def test_commit_count_overflow_is_visible_and_reads_no_commit_contents(case, monkeypatch):
    repo, snapshot, unit, receipt = case
    value = receipt["external_contribution"]
    monkeypatch.setattr(provenance, "MAX_COMMITS", 0)
    original = provenance._git
    calls = []

    def observed(*args):
        calls.append(args[1:])
        return original(*args)

    monkeypatch.setattr(provenance, "_git", observed)
    with pytest.raises(CampaignError, match="commit-count"):
        provenance.collect(
            repo,
            **{
                key: value[key]
                for key in (
                    "repository",
                    "pr_number",
                    "pr_node_id",
                    "branch",
                    "local_before",
                    "remote_head",
                    "target_head",
                )
            },
        )
    assert len(calls) == 1 and calls[0][0] == "rev-list"


def test_commit_size_guard_precedes_content_read(case, monkeypatch):
    repo, _, _, receipt = case
    value = receipt["external_contribution"]
    monkeypatch.setattr(provenance, "MAX_COMMIT_BYTES", value["commit_bytes"] - 1)
    original = provenance._git
    calls = []

    def observed(*args):
        calls.append(args[1:])
        return original(*args)

    monkeypatch.setattr(provenance, "_git", observed)
    with pytest.raises(CampaignError, match="commit-byte"):
        provenance.collect(
            repo,
            **{
                key: value[key]
                for key in (
                    "repository",
                    "pr_number",
                    "pr_node_id",
                    "branch",
                    "local_before",
                    "remote_head",
                    "target_head",
                )
            },
        )
    assert not any(args[:2] == ("cat-file", "commit") for args in calls)


def test_accepted_target_history_is_not_reclassified_as_new_external_work(case):
    repo, _, _, receipt = case
    value = receipt["external_contribution"]
    kwargs = {
        key: value[key]
        for key in (
            "repository",
            "pr_number",
            "pr_node_id",
            "branch",
            "local_before",
            "remote_head",
            "target_head",
        )
    }
    kwargs["target_head"] = value["remote_head"]
    empty = provenance.collect(repo, **kwargs)
    assert empty["complete"] and empty["commits"] == [] and empty["commit_bytes"] == 0


def test_git_error_remains_observation_failure(case, monkeypatch):
    repo, _, _, receipt = case
    value = receipt["external_contribution"]
    monkeypatch.setattr(
        provenance.verify,
        "git_bytes",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, b"", b"ref unavailable"),
    )
    with pytest.raises(CampaignError, match="Git observation failed"):
        provenance.collect(
            repo,
            **{
                key: value[key]
                for key in (
                    "repository",
                    "pr_number",
                    "pr_node_id",
                    "branch",
                    "local_before",
                    "remote_head",
                    "target_head",
                )
            },
        )


def test_public_status_preserves_bounded_provenance_and_current_debt(case):
    from bmad_loop.campaign_cli import status_document
    from bmad_loop.campaign_model import Hold

    _, snapshot, unit, receipt = case
    provenance.record(snapshot, unit, receipt)
    entry = unit.evidence["external_contributions"][0]
    unit.evidence["external_contributions"] = [copy.deepcopy(entry) for _ in range(20)]
    unit.evidence["remote_head_sync"] = receipt["remote_head_sync"]
    unit.evidence["remote_head_refreshes"] = [receipt["remote_head_sync"]]
    unit.evidence["developer_attribution_incomplete"] = True
    unit.hold = Hold("remote_recovery_fence", "Private failure path must not enter summary")
    public = status_document(snapshot)["units"][0]["external_contributions"]
    assert public["observations"] == 20 and public["distinct_commits"] == 1
    assert public["unobserved_external_model_commits"] == 1
    assert public["campaign_attribution_incomplete"] is True
    assert public["remote_head_changes"] == 1
    assert public["pending"]["head"] == receipt["remote_head_sync"]["head"]
    assert public["hold_kind"] == "remote_recovery_fence"
    assert len(public["recent"]) == 16 and public["omitted_observations"] == 4
    assert public["recent"][0]["receipt"] == "workers/integration-worker/receipt.json"
    assert public["receipt_path_basis"] == "campaign_directory"
    assert "unobserved external models" in public["limitation"]
    text = str(public)
    assert "Cursor Agent" not in text and "raw_base64" not in text and "Private failure" not in text
