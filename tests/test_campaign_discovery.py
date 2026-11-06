"""Inventory coverage and adoption are explicit, read-only evidence."""

from dataclasses import replace

import pytest

from bmad_loop.campaign_discovery import OverlapDisposition, RepositoryDiscovery, validate_overlap


class Gateway:
    repository = "a/b"

    def issues(self):
        return [{"id": 1, "number": 1, "title": "Existing"}]

    def pulls(self, state="open"):
        return [{"id": 2, "number": 2, "title": "Active", "state": state}]


def test_inventory_observes_local_refs_worktree_dirty_content_without_checkouts(project):
    gateway = Gateway()
    discovery = RepositoryDiscovery(gateway, project.project, clock=lambda: 100)
    # No remote is configured in the sandbox; that is a visible coverage failure.
    receipt = discovery.collect()
    assert not receipt.complete
    assert any("remote_git" in reason for reason in receipt.unavailable)
    assert receipt.common_dir.endswith(".git")
    assert receipt.issues[0]["number"] == 1
    assert receipt.local_refs
    assert receipt.worktrees[0]["ownership"] == "unknown"
    detail = discovery.inspect_branch("HEAD", "HEAD")
    assert detail["base"] == detail["head"]
    assert detail["diff"] == ""


def test_partial_inventory_and_active_overlap_do_not_authorize_adoption(project):
    receipt = RepositoryDiscovery(Gateway(), project.project).collect()
    receipt = replace(receipt, complete=True, unavailable=())
    disposition = OverlapDisposition(
        "unit",
        "new_work",
        receipt.digest,
        (),
        ("New acceptance",),
        (),
        (),
        "Acceptance is absent from existing work",
    )
    validate_overlap(disposition, receipt)
    partial = replace(receipt, complete=False)
    with pytest.raises(ValueError, match="Incomplete"):
        validate_overlap(replace(disposition, inventory_digest=partial.digest), partial)
    adoption = replace(
        disposition, disposition="continue_existing_work", evidence_ids=("pr:2@head",), adopted_pr=2
    )
    with pytest.raises(ValueError, match="authorization"):
        validate_overlap(adoption, receipt)
    validate_overlap(adoption, receipt, owned_pr=2)
    with pytest.raises(ValueError, match="merged/source"):
        validate_overlap(replace(adoption, disposition="already_delivered"), receipt)
