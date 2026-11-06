"""Read-only repository work inventory and acceptance-overlap validation.

Worktree/ref observations are evidence, never leases. No inspected branch is
checked out or executed, and archived roots must be explicitly supplied. Missing
or truncated coverage is carried in the receipt and prevents a 'new work' claim.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

from .campaign_github import GithubError, GithubGateway, JsonObject
from .verify import GitError, git_bytes


@dataclass(frozen=True)
class CoverageReceipt:
    schema_version: int
    repository: str
    common_dir: str
    observed_at: float
    scopes: tuple[str, ...]
    complete: bool
    unavailable: tuple[str, ...]
    truncated: tuple[str, ...]
    issues: tuple[JsonObject, ...]
    pulls: tuple[JsonObject, ...]
    remote_refs: tuple[JsonObject, ...]
    local_refs: tuple[JsonObject, ...]
    worktrees: tuple[JsonObject, ...]
    archives: tuple[JsonObject, ...] = ()

    def to_dict(self) -> JsonObject:
        return asdict(self)

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()


OverlapKind = Literal[
    "already_delivered",
    "covered_by_active_work",
    "continue_existing_work",
    "reuse_partial_work",
    "new_work",
    "unresolved_overlap",
]


@dataclass(frozen=True)
class OverlapDisposition:
    unit_id: str
    disposition: OverlapKind
    inventory_digest: str
    evidence_ids: tuple[str, ...]
    acceptance_coverage: tuple[str, ...]
    remaining: tuple[str, ...]
    exclusions: tuple[str, ...]
    reason: str
    adopted_pr: int | None = None


def validate_overlap(
    disposition: OverlapDisposition,
    inventory: CoverageReceipt,
    *,
    owned_pr: int | None = None,
    adoption_authorized: bool = False,
    delivery_verified: bool = False,
) -> None:
    if disposition.inventory_digest != inventory.digest:
        raise ValueError("Overlap disposition uses a different inventory revision")
    if not disposition.reason.strip() or not disposition.acceptance_coverage:
        raise ValueError("Overlap disposition requires acceptance coverage and rationale")
    if not inventory.complete:
        raise ValueError("Incomplete inventory cannot authorize work admission")
    if disposition.disposition != "new_work" and not disposition.evidence_ids:
        raise ValueError("Overlap disposition requires exact source/PR/commit evidence")
    if disposition.disposition == "already_delivered" and not delivery_verified:
        raise ValueError("Delivery requires independent merged/source verification")
    if disposition.disposition == "continue_existing_work":
        if not disposition.adopted_pr or (
            disposition.adopted_pr != owned_pr and not adoption_authorized
        ):
            raise ValueError("Observed PR is not authorization to adopt its branch")
    if disposition.disposition in {"covered_by_active_work", "unresolved_overlap"}:
        raise ValueError("Overlapping active work must remain held/dependent")


class RepositoryDiscovery:
    def __init__(
        self,
        gateway: GithubGateway,
        repo: Path,
        *,
        clock: Callable[[], float] = time.time,
        max_detail_bytes: int = 1_000_000,
    ):
        self.gateway = gateway
        self.repo = repo.resolve()
        self.clock = clock
        self.max_detail_bytes = max_detail_bytes

    def _git(self, args: list[str], *, repo: Path | None = None) -> str:
        result = git_bytes(repo or self.repo, *args)
        if result.returncode:
            raise GithubError("Repository inventory git observation failed: " + args[0])
        return result.stdout.decode("utf-8")

    def collect(
        self,
        *,
        remote: str = "origin",
        recent_since: str = "",
        mapped_prs: tuple[int, ...] = (),
        detailed_prs: tuple[int, ...] = (),
        archive_roots: tuple[Path, ...] = (),
    ) -> CoverageReceipt:
        if remote.startswith("-") or not remote:
            raise ValueError("Invalid discovery remote")
        unavailable: list[str] = []
        truncated: list[str] = []
        common = (self.repo / self._git(["rev-parse", "--git-common-dir"]).strip()).resolve()
        issues: list[JsonObject] = []
        pulls: list[JsonObject] = []
        try:
            issues = self.gateway.issues()
            pulls = self.gateway.pulls()
            if recent_since:
                # Closed work is bounded by an explicit updated-time window, not titles.
                pulls += self.gateway.recent_pulls(recent_since)
            known = {int(row["number"]) for row in pulls}
            for number in mapped_prs:
                if number not in known:
                    pulls.append(self.gateway.pull(number))
            detailed: list[JsonObject] = []
            for row in pulls:
                if row["number"] in detailed_prs:
                    full = self.gateway.pull(row["number"])
                    files = self.gateway.files(row["number"])
                    if len(files) < full.get("changed_files", len(files)) or len(files) >= 3000:
                        truncated.append(f"pr:{row['number']}:files")
                    detail = {
                        **full,
                        "changed_files_detail": files,
                        "reviews": self.gateway.reviews(row["number"]),
                        "review_threads": self.gateway.review_threads(row["number"]),
                    }
                    if len(json.dumps(detail).encode()) > self.max_detail_bytes:
                        truncated.append(f"pr:{row['number']}:detail")
                        # Metadata remains useful; oversized content is visibly unavailable.
                        detail = {
                            **full,
                            "details_truncated": True,
                            "changed_paths": [f["filename"] for f in files],
                        }
                    detailed.append(detail)
                else:
                    detailed.append(row)
            pulls = detailed
        except GithubError as exc:
            unavailable.append(f"github:{exc}")
        remote_refs: list[JsonObject] = []
        local_refs: list[JsonObject] = []
        worktrees: list[JsonObject] = []
        try:
            for row in self._git(["ls-remote", "--heads", "--", remote]).splitlines():
                sha, ref = row.split("\t", 1)
                remote_refs.append({"ref": ref, "sha": sha})
        except (ValueError, UnicodeError, GitError, GithubError) as exc:
            unavailable.append(f"remote_git:{exc}")
        try:
            for row in self._git(
                ["for-each-ref", "--format=%(refname)%09%(objectname)"]
            ).splitlines():
                ref, sha = row.split("\t", 1)
                local_refs.append({"ref": ref, "sha": sha})
            raw = git_bytes(self.repo, "worktree", "list", "--porcelain", "-z")
            if raw.returncode:
                raise GithubError("Cannot enumerate local worktrees")
            for block in raw.stdout.decode("utf-8").split("\0\0"):
                record: JsonObject = {}
                for field in block.split("\0"):
                    if field:
                        key, _, value = field.partition(" ")
                        record[key] = value or True
                if "worktree" not in record:
                    continue
                path = Path(record["worktree"])
                try:
                    observed_common = (
                        path / self._git(["rev-parse", "--git-common-dir"], repo=path).strip()
                    ).resolve()
                    if observed_common != common:
                        raise GithubError(
                            "Registered worktree now belongs to another common directory"
                        )
                    record["dirty_porcelain"] = self._git(
                        ["status", "--porcelain=v1", "--untracked-files=normal"], repo=path
                    )
                    record["ownership"] = "unknown"  # Idle/old does not prove abandonment.
                except (OSError, GitError, GithubError) as exc:
                    record["unavailable"] = str(exc)
                    unavailable.append(f"worktree:{path}")
                worktrees.append(record)
        except (ValueError, UnicodeError, GitError, GithubError) as exc:
            unavailable.append(f"local_git:{exc}")
        archives: list[JsonObject] = []
        for root in archive_roots:
            resolved = root.resolve()
            if not resolved.is_dir():
                unavailable.append(f"archive:{resolved}")
                continue
            # Only owner-selected archive roots, shallow manifests, no symlink traversal.
            manifests = list(resolved.glob("*.json")) + list(resolved.glob("*/manifest.json"))
            for manifest in manifests[:1000]:
                if manifest.is_symlink() or not manifest.resolve().is_relative_to(resolved):
                    continue
                try:
                    data = manifest.read_bytes()
                    if len(data) > self.max_detail_bytes:
                        truncated.append(f"archive:{manifest}")
                        continue
                    archives.append(
                        {
                            "path": str(manifest),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "content": json.loads(data),
                            "authority": "observation_only",
                        }
                    )
                except (OSError, ValueError) as exc:
                    unavailable.append(f"archive:{manifest}:{exc}")
            if len(manifests) > 1000:
                truncated.append(f"archive:{resolved}:manifest_limit")
        return CoverageReceipt(
            1,
            self.gateway.repository,
            str(common),
            self.clock(),
            (
                "all_open_issues",
                "all_open_draft_prs",
                "mapped_prs",
                "remote_branch_tips",
                "common_dir_refs_worktrees",
                "explicit_archive_manifests",
            ),
            not unavailable and not truncated,
            tuple(unavailable),
            tuple(truncated),
            tuple(issues),
            tuple(pulls),
            tuple(remote_refs),
            tuple(local_refs),
            tuple(worktrees),
            tuple(archives),
        )

    def inspect_branch(self, base: str, head: str) -> JsonObject:
        """Targeted diff evidence; references resolve to immutable commits first."""
        resolved: list[str] = []
        for ref in (base, head):
            if not ref or ref.startswith("-"):
                raise ValueError("Invalid inventory revision")
            resolved.append(
                self._git(["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"]).strip()
            )
        before, after = resolved
        merge_base = self._git(["merge-base", before, after]).strip()
        diff = self._git(["diff", "--no-ext-diff", "--no-textconv", f"{before}...{after}", "--"])
        encoded = diff.encode()
        return {
            "base": before,
            "head": after,
            "merge_base": merge_base,
            "diff": encoded[: self.max_detail_bytes].decode("utf-8", errors="replace"),
            "truncated": len(encoded) > self.max_detail_bytes,
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
