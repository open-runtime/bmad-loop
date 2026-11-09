"""Epic closure keeps every native child immutable until accepted delivery."""

import pytest
from conftest import DEFAULT_TREE, git, write_plan, write_ticket_tree

from bmad_loop import campaign_closure, platform_util, ticketlayout, verify
from bmad_loop.campaign_model import CampaignError, SourceSelector, UnitSpec
from bmad_loop.campaign_source_publication import SourcePublication


@pytest.fixture
def closure_case(project_v7, tmp_path):
    if not platform_util.HANDLE_ANCHORED_WRITES:
        pytest.skip("closure requires anchored private source reads")
    layout = project_v7
    write_ticket_tree(layout, DEFAULT_TREE)
    first = write_plan(layout, "epic-cart", "story-cart-model-plan", 1, "done")
    second = write_plan(layout, "epic-cart", "story-cart-rules-plan", 2, "done")
    git(layout.project, "add", "-A")
    git(layout.project, "commit", "-qm", "accepted child baseline")
    source = SourceSelector("tickets", id="t", initiative=layout.initiative, epics=("epic-cart",))
    unit = UnitSpec(
        "t:initiative-demo:closure/epic-cart",
        "t",
        "epic-cart",
        "Close cart",
        "epic-cart",
        kind="closure",
        initiative=layout.initiative,
    )
    publisher = SourcePublication(layout.project, tmp_path / "publication")
    binding = publisher.capture(source, unit)
    manifest = campaign_closure.prepare(
        layout.project,
        source,
        unit,
        canonical=binding.to_dict(),
        canonical_head=verify.rev_parse_head(layout.project),
    )
    return layout, source, unit, manifest, first, second


def author(case):
    layout, source, unit, manifest, first, second = case
    epic, retrospective = (layout.project / manifest[key] for key in ("epic", "retrospective"))
    epic.write_text(
        epic.read_text().replace("status: in-progress", "status: done"), encoding="utf-8"
    )
    retrospective.write_text(
        "# Cart retrospective\n\nAll children are accepted. Preserve the reviewed implementation and record lessons for the next epic.\n",
        encoding="utf-8",
    )
    return {"artifacts": [manifest["epic"], manifest["retrospective"]]}


def test_native_epic_and_retrospective_produce_exact_committed_artifact_receipts(closure_case):
    layout, source, unit, manifest, first, second = closure_case
    child = first.read_bytes()
    artifact = author(closure_case)
    campaign_closure.validate(layout.project, unit, manifest, artifact)
    paths = [layout.project / path for path in artifact["artifacts"]]
    verify.commit_paths(layout.project, "docs: close cart", paths)
    receipts = campaign_closure.validate(
        layout.project,
        unit,
        manifest,
        artifact,
        committed_head=verify.rev_parse_head(layout.project),
    )
    assert [row["kind"] for row in receipts] == ["closure", "closure-retrospective"]
    assert all(row["path"] == row["review_path"] for row in receipts)
    assert receipts[1]["canonical_path"] == str(paths[1])
    assert first.read_bytes() == child


def test_unselected_unfinished_child_refuses_closure_before_actor_starts(closure_case):
    layout, source, unit, manifest, first, second = closure_case
    second.write_bytes(second.read_bytes().replace(b"status: done", b"status: ready-for-dev"))
    git(layout.project, "add", "-A")
    git(layout.project, "commit", "-qm", "unselected child still pending")
    with pytest.raises(CampaignError, match="unfinished selected or unselected child"):
        campaign_closure.prepare(
            layout.project,
            source,
            unit,
            canonical={"path": str(layout.project / manifest["epic"])},
            canonical_head=verify.rev_parse_head(layout.project),
        )


@pytest.mark.parametrize(
    "mutation", ["child", "manifest", "implementation", "missing-retrospective", "wrong-epic"]
)
def test_closure_refuses_collateral_changes_and_missing_native_products(closure_case, mutation):
    layout, source, unit, manifest, first, second = closure_case
    artifact = author(closure_case)
    if mutation == "child":
        first.write_bytes(first.read_bytes() + b"\nUnreviewed child body.\n")
    elif mutation == "manifest":
        (first.parent / ticketlayout.BREAKDOWN).write_text(
            "# altered child metadata\n", encoding="utf-8"
        )
    elif mutation == "implementation":
        (layout.project / "src.txt").write_text("closure changed code\n", encoding="utf-8")
    elif mutation == "missing-retrospective":
        artifact["artifacts"].pop()
    else:
        artifact["artifacts"][0] = "other-epic.md"
    with pytest.raises(CampaignError):
        campaign_closure.validate(layout.project, unit, manifest, artifact)


def test_closure_requires_done_and_substantive_retrospective(closure_case):
    layout, source, unit, manifest, first, second = closure_case
    artifact = author(closure_case)
    path = layout.project / manifest["epic"]
    path.write_text(
        path.read_text().replace("status: done", "status: in-progress"), encoding="utf-8"
    )
    with pytest.raises(CampaignError, match="must be done"):
        campaign_closure.validate(layout.project, unit, manifest, artifact)
    author(closure_case)
    (layout.project / manifest["retrospective"]).write_text("done", encoding="utf-8")
    with pytest.raises(CampaignError, match="substantive"):
        campaign_closure.validate(layout.project, unit, manifest, artifact)


def test_historical_done_epic_without_retrospective_still_requires_closure_writer(closure_case):
    from dataclasses import asdict
    from types import SimpleNamespace

    from bmad_loop.campaign_existing import requires_existing, terminal_path

    layout, source, unit, manifest, first, second = closure_case
    author(closure_case)
    (layout.project / manifest["retrospective"]).unlink()
    current = SimpleNamespace(
        spec=unit,
        id=unit.id,
        bindings={"workspace": str(layout.project), "effective_source": asdict(source)},
    )
    snapshot = SimpleNamespace(bindings={"source_observations": {unit.id: {"status": "done"}}})
    assert requires_existing(snapshot, current) is False
    with pytest.raises(CampaignError, match="substantive native retrospective"):
        terminal_path(layout.project, source, unit)


def test_historical_complete_closure_verifies_both_native_products_without_development(
    closure_case, tmp_path
):
    import sys
    from dataclasses import asdict
    from types import SimpleNamespace

    from bmad_loop import policy
    from bmad_loop.campaign_existing import verify_existing

    layout, source, unit, manifest, first, second = closure_case
    artifact = author(closure_case)
    verify.commit_paths(
        layout.project,
        "accepted historical closure",
        [layout.project / path for path in artifact["artifacts"]],
    )
    request = {
        "workspace": str(layout.project),
        "source": asdict(source),
        "unit": asdict(unit),
        "commands": [[sys.executable, "-c", "pass"]],
    }
    result = verify_existing(
        request, SimpleNamespace(directory=tmp_path / "worker", policy=policy.Policy())
    )
    assert result["outcome"] == "existing_verified"
    assert [row["kind"] for row in result["source_artifacts"]] == [
        "closure",
        "closure-retrospective",
    ]
    assert result["sessions"] == []


@pytest.mark.parametrize("mutation", ["owner-private", "canonical"])
def test_prior_closure_delivery_is_reused_only_for_both_exact_current_native_products(
    closure_case, tmp_path, monkeypatch, mutation
):
    import hashlib
    from types import SimpleNamespace

    from bmad_loop.campaign_existing import prior_delivery
    from bmad_loop.campaign_model import CampaignSnapshot, CampaignSpec, CampaignUnit, UnitState
    from bmad_loop.campaign_publication import MergeReceipt, PublicationController
    from bmad_loop.campaign_store import CampaignStore

    layout, source, unit, manifest, first, second = closure_case
    artifact = author(closure_case)
    paths = [layout.project / path for path in artifact["artifacts"]]
    verify.commit_paths(layout.project, "accepted canonical closure", paths)
    merged = verify.rev_parse_head(layout.project)
    artifacts = [
        {"kind": kind, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for kind, path in zip(("closure", "closure-retrospective"), paths)
    ]
    prior = CampaignUnit(
        unit,
        UnitState.DELIVERED,
        bindings={"issue": {"number": 2, "id": 22}, "pr": {"number": 3}},
        evidence={
            "source_artifacts": artifacts,
            "merge": {"merge_commit": merged},
            "integrated": {"head": merged, "base": merged},
            "verification": {"outcome": "verified", "launch_id": "verify"},
            "review": {
                "validated": {"verdict": "approved"},
                "launch_id": "review",
                "observed_model": "gpt-6-astra",
            },
        },
    )
    spec = CampaignSpec("Closure proof", sources=(source,))
    store = CampaignStore(tmp_path / "prior-campaigns")
    store.create(
        CampaignSnapshot("previous", "a/b", spec, {unit.id: prior}, common_dir="/same-common")
    )
    current = CampaignSnapshot(
        "current", "a/b", spec, {unit.id: CampaignUnit(unit)}, common_dir="/same-common"
    )
    gateway = SimpleNamespace(root="repos/a/b", get=lambda _: {"id": 22, "state": "closed"})
    runtime = SimpleNamespace(
        project=layout.project, store=store, source=lambda *a: source, gateway=lambda *a: gateway
    )
    canonical = {"head": merged}
    monkeypatch.setattr(
        PublicationController,
        "merge_status",
        lambda *a, **kw: MergeReceipt("merged_pending_sync", merge_commit=merged),
    )
    monkeypatch.setattr(
        PublicationController,
        "confirm_canonical",
        lambda *a, **kw: MergeReceipt(
            "merged_pending_sync", merge_commit=merged, canonical_head=canonical["head"]
        ),
    )
    selected = current.units[unit.id]
    proof = prior_delivery(runtime, current, selected)
    assert proof is not None and len(proof.source_products) == 2
    retrospective = paths[1]
    original = retrospective.read_bytes()
    retrospective.write_bytes(original + b"\nOwner changed the retrospective acceptance.\n")
    if mutation == "canonical":
        verify.commit_paths(
            layout.project, "owner updated canonical retrospective", [retrospective]
        )
        canonical["head"] = verify.rev_parse_head(layout.project)
        retrospective.write_bytes(original)  # an older private/source observation
    assert prior_delivery(runtime, current, selected) is None
