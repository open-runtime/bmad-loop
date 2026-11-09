"""Private source generations keep unmerged history and exact approved inputs."""

import dataclasses
import hashlib

import pytest
from conftest import git

from bmad_loop.campaign_hold import promote_pending
from bmad_loop.campaign_intake_amendment import (
    _new_files,
    _retire_drained_failures,
    _validate_generation,
    validate_initial_source,
)
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignUnit,
    Hold,
    SourceSelector,
    UnitSpec,
    WorkerClaim,
)
from bmad_loop.campaign_runtime import CampaignRuntime


def initial_request(project, tmp_path):
    root = project.project
    config = root / "_bmad" / "bmm" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "output_folder: _bmad-output\nimplementation_artifacts: _bmad-output/implementation-artifacts\nplanning_artifacts: _bmad-output/planning-artifacts\n",
        encoding="utf-8",
    )
    folder = root / "specs"
    path = folder / "stories" / "u-story.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nstatus: ready-for-dev\n---\nApproved acceptance\n", encoding="utf-8")
    source = SourceSelector("stories", path="specs", id="source")
    unit = UnitSpec("source:project:u", "source", "epic", "U", selector="u")
    return {
        "workspace": str(root),
        "execution": str(tmp_path / "worker" / "execution.json"),
        "source": dataclasses.asdict(source),
        "unit": dataclasses.asdict(unit),
        "initial_source": {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
    }, path


def test_initial_source_is_checked_before_actual_native_dispatch(project, tmp_path):
    request, path = initial_request(project, tmp_path)
    validate_initial_source(request)
    path.write_text(path.read_text(encoding="utf-8") + "unreviewed owner edit\n", encoding="utf-8")
    with pytest.raises(CampaignError, match="changed after reviewed"):
        validate_initial_source(request)


def test_configured_selector_cannot_point_at_another_identical_private_source(project, tmp_path):
    request, path = initial_request(project, tmp_path)
    other = project.project / "other-specs" / "stories" / path.name
    other.parent.mkdir(parents=True)
    other.write_bytes(path.read_bytes())
    request["source"]["path"] = "other-specs"
    with pytest.raises(CampaignError, match="configuration points at a different"):
        validate_initial_source(request)


def test_initial_source_rejects_outside_paths_even_when_the_digest_matches(project, tmp_path):
    request, path = initial_request(project, tmp_path)
    request["initial_source"]["path"] = str(path)
    with pytest.raises(CampaignError, match="redirected"):
        validate_initial_source(request)


def test_fresh_generation_reads_immutable_main_blob_and_preserves_local_source_edits(project):
    root = project.project / "specs"
    root.mkdir()
    path = root / "source.md"
    path.write_text("reviewed canonical intent", encoding="utf-8")
    git(project.project, "add", "specs")
    git(project.project, "commit", "-qm", "reviewed source amendment")
    head = git(project.project, "rev-parse", "HEAD")
    path.write_text("different local owner work", encoding="utf-8")
    assert _new_files(project.project, root, head)["source.md"] == b"reviewed canonical intent"
    assert path.read_text(encoding="utf-8") == "different local owner work"


def test_unknown_refresh_receipt_cannot_adopt_an_edited_private_generation(tmp_path):
    generation = tmp_path / "generation"
    generation.mkdir()
    path = generation / "source.md"
    path.write_bytes(b"reviewed")
    record = {"files": {"source.md": hashlib.sha256(b"reviewed").hexdigest()}}
    _validate_generation(record, generation)
    path.write_bytes(b"new owner changes")
    with pytest.raises(CampaignError, match="changed before acknowledgement"):
        _validate_generation(record, generation)


def draining_unit():
    unit = CampaignUnit(
        UnitSpec("source:project:u", "source", "epic", "U", selector="u"),
        hold=Hold("intake_plan_change", "reviewed source amendment pending", id="amendment"),
    )
    unit.claim = WorkerClaim(unit.id, 1, 1, "old-actor")
    unit.evidence.update(dev_attempts_spent=2, publication_faults=1)
    return unit


def test_amendment_barrier_survives_old_actor_pause_until_reviewed_source_activation():
    unit = draining_unit()
    CampaignRuntime._set_hold(unit, Hold("paused", "old source actor paused", id="old-actor"))
    assert unit.hold.kind == "intake_plan_change" and unit.hold.id == "amendment"
    assert unit.evidence["pending_failure_hold"]["kind"] == "paused"
    unit.claim.quiescent = True
    retired = _retire_drained_failures(unit)
    assert retired["pending_failure_hold"]["id"] == "old-actor"
    unit.hold = None  # the verified amendment apply removes only its own barrier
    assert not promote_pending(unit) and unit.hold is None
    assert unit.evidence["dev_attempts_spent"] == 2 and unit.evidence["publication_faults"] == 1


def test_amendment_preserves_unrelated_and_protected_failures_instead_of_waiving_them():
    unit = draining_unit()
    unit.claim.quiescent = True
    unit.evidence["pending_failure_hold"] = dataclasses.asdict(
        Hold("paused", "old source pause", id="old-actor")
    )
    unit.evidence["pending_failure_holds"] = [
        dataclasses.asdict(Hold("required_review", "approval required", id="old-actor")),
        dataclasses.asdict(Hold("failed", "different actor", id="other-actor")),
        dataclasses.asdict(Hold("ownership_uncertain", "writer not released", id="old-actor")),
        dataclasses.asdict(Hold("verification_failed", "old source verification", id="old-actor")),
    ]
    unit.evidence["failure"] = {
        "id": "old-actor",
        "code": "verification_failed",
        "detail": "old source",
    }
    retired = _retire_drained_failures(unit)
    assert retired["failure"]["id"] == "old-actor"
    assert [item["kind"] for item in retired["pending_failure_holds"]] == ["verification_failed"]
    unit.hold = None
    assert promote_pending(unit) and unit.hold.kind == "required_review"
    assert [item["kind"] for item in unit.evidence["pending_failure_holds"]] == [
        "failed",
        "ownership_uncertain",
    ]


def test_amendment_cannot_retire_faults_of_an_actor_still_writing():
    unit = draining_unit()
    unit.evidence["pending_failure_hold"] = dataclasses.asdict(
        Hold("paused", "old source pause", id="old-actor")
    )
    before = dict(unit.evidence)
    with pytest.raises(CampaignError, match="requires a drained actor"):
        _retire_drained_failures(unit)
    assert unit.evidence == before
