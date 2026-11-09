"""Quiescent append-only integration of an exact owned PR head and fresh main.

An authenticated remote-head debt is not permission to overwrite a worker or
import unrelated paths. Observe the exact branch, verify its accepted ancestor
and novel scope first, then retain all three inputs for ordinary conflict repair.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import verify
from .campaign_model import CampaignError, product_scopes, unit_from_dict

MAX_CHANGED_PATH_BYTES = 1024 * 1024
MAX_CHANGED_PATHS = 4096


class RemoteOwnershipError(CampaignError):
    """Observed external content has not earned product-write authority."""


def _preserve(workspace: Path, reference: str, head: str) -> None:
    if verify.ref_exists_checked(workspace, reference):
        if verify.rev_parse_revision(workspace, reference) != head:
            raise CampaignError("integration preservation reference changed")
    else:
        verify.create_ref(workspace, reference, head)


def _entry(workspace: Path, head: str, path: str) -> bytes:
    result = verify.git_bytes(workspace, "ls-tree", "-z", head, "--", ":(literal)" + path)
    if result.returncode:
        raise CampaignError("remote contribution tree observation failed")
    return result.stdout


def _remote_paths(workspace, request, debt, target, protected_paths, directory):
    directory.mkdir(parents=True, exist_ok=True)
    # Git writes its potentially large name list directly to our disposable
    # artifact; the Python capture path never buffers arbitrary repository data.
    with tempfile.TemporaryDirectory(prefix="remote-paths-", dir=directory) as temporary:
        output = Path(temporary) / "paths"
        result = verify.git_bytes(
            workspace,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--name-only",
            "--no-renames",
            "-z",
            "--output=" + str(output),
            debt["anchor"],
            debt["head"],
        )
        if result.returncode:
            raise RemoteOwnershipError("remote contribution ownership observation failed")
        with output.open("rb") as stream:
            raw = stream.read(MAX_CHANGED_PATH_BYTES + 1)
        if len(raw) > MAX_CHANGED_PATH_BYTES:
            raise RemoteOwnershipError("remote contribution changed-path byte bound exceeded")
    try:
        paths = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    except UnicodeError as exc:
        raise RemoteOwnershipError("remote contribution has unsupported path encoding") from exc
    if len(paths) > MAX_CHANGED_PATHS:
        raise RemoteOwnershipError("remote contribution changed-path count bound exceeded")
    scopes = product_scopes(unit_from_dict(request["unit"]))
    bases = verify.git_bytes(workspace, "merge-base", "--all", debt["anchor"], target)
    if bases.returncode or not bases.stdout.strip():
        raise RemoteOwnershipError("canonical carry-forward ancestry is unavailable")
    canonical_bases = bases.stdout.decode("ascii").splitlines()
    protected = {
        ".bmad-loop/policy.toml",
        ".bmad-loop/profiles",
        "_bmad/custom/config.user.toml",
        *protected_paths,
    }
    for artifact in request.get("source_artifacts", []):
        protected.update(artifact[key] for key in ("path", "review_path") if artifact.get(key))
    for path in paths:
        protected_path = any(
            path == root or path.startswith(root.rstrip("/") + "/") for root in protected
        )
        owned = any(
            scope == "*" or path == scope or path.startswith(scope.rstrip("/") + "/")
            for scope in scopes
        )
        if owned and not protected_path:
            continue
        # Exact canonical-main content is already accepted independently of the
        # bot. Modes/deletions/renames are compared as tree entries, not prose.
        target_entry = _entry(workspace, target, path)
        if _entry(workspace, debt["head"], path) == target_entry and all(
            _entry(workspace, base, path) != target_entry for base in canonical_bases
        ):
            continue
        if protected_path:
            raise RemoteOwnershipError(
                "remote contribution changed protected source or policy: " + path
            )
        raise RemoteOwnershipError("remote contribution escaped declared ownership: " + path)
    return paths


def _validate_debt(request, envelope, before, git):
    debt = request.get("remote_head_sync")
    if debt is None:
        return None
    if (
        not isinstance(debt, dict)
        or type(debt.get("schema_version")) is not int
        or debt["schema_version"] != 1
        or type(debt.get("pr_number")) is not int
        or debt["pr_number"] < 1
        or not all(
            isinstance(debt.get(key), str) and debt[key]
            for key in ("id", "repository", "pr_node_id", "branch", "target")
        )
        or debt["branch"] != request.get("branch")
        or debt["target"] != request["target"]
        or debt.get("local_head") != before
        or type(debt.get("config_revision")) is not int
        or debt["config_revision"] != envelope.config_revision
        or type(debt.get("graph_revision")) is not int
        or not isinstance(debt.get("source_revision"), str)
        or any(
            not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(debt.get(key, "")))
            for key in ("anchor", "head", "local_head")
        )
    ):
        raise CampaignError("remote head integration debt binding changed")
    workspace = Path(request["workspace"])
    if git(workspace, "symbolic-ref", "--short", "HEAD") != debt["branch"]:
        raise CampaignError("remote head integration does not own the checked-out branch")
    return debt


def integrate(
    request: dict[str, Any], envelope: Any, *, git: Callable[..., str], protected_paths=()
) -> dict[str, Any]:
    workspace = Path(request["workspace"])
    if not verify.worktree_clean(workspace):
        raise CampaignError("integration requires a clean quiescent candidate")
    before = verify.rev_parse_head(workspace)
    debt = _validate_debt(request, envelope, before, git)
    prefix = f"refs/bmad-campaign/{envelope.campaign_id}/integration/{request['launch_id']}"

    def fetch(branch, name):
        reference = f"{prefix}/observed-{name}"
        git(
            workspace,
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--refmap=",
            request["remote"],
            f"refs/heads/{branch}:{reference}",
        )
        return verify.rev_parse_revision(workspace, reference)

    remote = fetch(debt["branch"], "remote") if debt else None
    if debt and remote != debt["head"]:
        return {
            "outcome": "remote_head_changed",
            "remote_head_sync": debt,
            "observed_remote_head": remote,
            "head": before,
        }
    if debt and not all(
        verify.is_ancestor(workspace, debt["anchor"], head) for head in (debt["head"], before)
    ):
        return {
            "outcome": "ownership_uncertain",
            "remote_head_sync": debt,
            "head": before,
            "error": "integration input no longer preserves its authenticated anchor",
        }
    target = fetch(request["target"], "target")
    extra = {}
    if debt:
        from .campaign_external_provenance import collect

        try:
            paths = _remote_paths(
                workspace, request, debt, target, protected_paths, envelope.directory
            )
        except RemoteOwnershipError as exc:
            return {
                "outcome": "ownership_uncertain",
                "remote_head_sync": debt,
                "head": before,
                "base": target,
                "error": str(exc),
            }
        extra = {
            "remote_head_sync": debt,
            "external_contribution": collect(
                workspace,
                repository=debt["repository"],
                pr_number=debt["pr_number"],
                pr_node_id=debt["pr_node_id"],
                branch=debt["branch"],
                local_before=before,
                provenance_before=debt.get("provenance_before", debt["local_head"]),
                remote_head=debt["head"],
                target_head=target,
            ),
            "remote_changed_paths": paths,
        }
    if verify.rev_parse_head(workspace) != before or not verify.worktree_clean(workspace):
        raise CampaignError("candidate changed while observing integration inputs")
    heads = {"candidate": before, **({"remote": remote} if remote else {}), "target": target}
    for name, head in heads.items():
        _preserve(workspace, f"{prefix}/{name}", head)
    base = git(workspace, "merge-base", before, target)
    common = {
        "base": target,
        "target_head": target,
        "preserved_heads": list(heads.values()),
        "preserved_refs": [f"{prefix}/{name}" for name in heads],
        "merge_base": base,
        **extra,
    }
    for name in (("remote", "target") if remote else ("target",)):
        head = heads[name]
        if verify.is_ancestor(workspace, head, verify.rev_parse_head(workspace)):
            continue
        result = verify.git_bytes(workspace, "merge", "--no-edit", head)
        if result.returncode:
            conflicts = git(workspace, "diff", "--name-only", "--diff-filter=U").splitlines()
            if conflicts:
                return {
                    **common,
                    "outcome": "integration_conflict",
                    "head": verify.rev_parse_head(workspace),
                    "conflicts": conflicts,
                    "conflict_source": name,
                }
            raise CampaignError("integration failed without a resolvable conflict")
    head = verify.rev_parse_head(workspace)
    if not all(verify.is_ancestor(workspace, parent, head) for parent in heads.values()):
        raise CampaignError("integration lost one of its preserved heads")
    return {**common, "outcome": "integrated", "head": head}
