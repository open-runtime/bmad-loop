"""Post-worker ownership checks for an independently observed candidate.

Unknown footprints retain the scheduler's exclusive claim. Declared file scopes
are checked against actual Git changes; a model's affected-path list does not
grant permission. Exact accepted source artifacts and native source bookkeeping
are separate from product implementation ownership.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import bmadconfig, sprintstatus, verify
from .campaign_model import CampaignError, normalize_scope


def validate_execution(unit: Any, receipt: dict[str, Any], source: dict[str, Any]) -> list[str]:
    workspace = Path(unit.bindings["workspace"])
    base, head = receipt["base"], receipt["head"]
    if not verify.is_ancestor(workspace, base, head):
        raise CampaignError("execution rewrote its accepted candidate history")
    result = verify.git_bytes(workspace, "diff", "--name-only", "--no-renames", "-z", base, head)
    if result.returncode:
        raise CampaignError("execution ownership observation failed")
    paths = [raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw]
    scopes = tuple(
        normalize_scope(value)
        for value in (unit.spec.paths or unit.spec.scopes)
        if not value.startswith("resource:")
    )
    bookkeeping = set()
    for artifact in receipt.get("source_artifacts", []):
        bookkeeping.update(artifact[name] for name in ("path", "review_path"))
    if source.get("kind") == "sprint-status":
        board = bmadconfig.load_paths(workspace).sprint_status.resolve()
        if not board.is_relative_to(workspace.resolve()):
            raise CampaignError("execution source board escaped its owned checkout")
        bookkeeping.add(board.relative_to(workspace).as_posix())
        relative = board.relative_to(workspace).as_posix()
        before = sprintstatus.planning_rows_in_bytes(
            verify.file_bytes_at_revision(workspace, base, relative)
        )
        after = sprintstatus.planning_rows_in_bytes(
            verify.file_bytes_at_revision(workspace, head, relative)
        )
        for key in before.keys() | after.keys():
            if key != unit.spec.selector and before.get(key) != after.get(key):
                raise CampaignError("execution changed an unowned sprint board row: " + key)
    if not scopes:
        return paths  # unknown product footprint was exclusively admitted
    escaped = [
        path
        for path in paths
        if path not in bookkeeping
        and not any(
            scope == "*" or path == scope or path.startswith(scope.rstrip("/") + "/")
            for scope in scopes
        )
    ]
    if escaped:
        raise CampaignError("execution escaped declared ownership: " + ", ".join(escaped[:20]))
    return paths


def canonical_unchanged(unit: Any) -> None:
    """Ignored/shared canonical source must still match its dispatch preimage."""
    from .campaign_model import SourceSelector
    from .campaign_source_publication import SourceBinding, SourcePublication

    raw = unit.bindings.get("canonical_source")
    if not raw:
        return
    binding = SourceBinding.from_dict(raw)
    if binding.versioning == "code-repo":
        return  # fresh-main integration and exact artifact review cover this path
    source = SourceSelector(**unit.bindings["canonical_selector"])
    observed = SourcePublication(
        Path(binding.project), Path(unit.bindings["source_authority_root"])
    ).capture(source, unit.spec)
    if binding.kind == "sprint-status" and observed.kind == binding.kind:
        import base64

        import yaml

        def selected_board(encoded):
            value = yaml.safe_load(base64.b64decode(encoded, validate=True)) if encoded else None
            if not isinstance(value, dict) or not isinstance(value.get("development_status"), dict):
                raise CampaignError("canonical sprint source has no unambiguous selected row")
            return {
                **value,
                "development_status": {
                    unit.spec.selector: value["development_status"].get(unit.spec.selector)
                },
            }

        if (observed.path, observed.source_root, observed.versioning, observed.store_repo) == (
            binding.path,
            binding.source_root,
            binding.versioning,
            binding.store_repo,
        ) and selected_board(observed.preimage) == selected_board(binding.preimage):
            return

    def identity(value):
        return (
            value.kind,
            value.path,
            value.source_root,
            value.versioning,
            value.store_repo,
            value.revision,
            value.children,
            value.manifests,
            value.native_ticket,
        )

    if identity(observed) != identity(binding):
        raise CampaignError("canonical source changed since its accepted dispatch preimage")
