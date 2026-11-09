"""Read-only repository work inventory and acceptance-overlap validation.

Worktree/ref observations are evidence, never leases. No inspected branch is
checked out or executed, and archived roots must be explicitly supplied. Missing
or truncated coverage is carried in the receipt and prevents a 'new work' claim.

Finite ownership scopes retain global issue/PR intent and every changed-file
identity, while patch text covers selected paths (either side of a rename).
Scoped patch omission is explicit; this inventory is not a full code review.
Reviews, threads and check observations remain complete for expanded PRs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

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
    github_coverage: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        result = asdict(self)
        if not self.github_coverage:
            # Preserve the digest of receipts saved before this optional field.
            result.pop("github_coverage")
        return result

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


def evidence_ids(inventory: CoverageReceipt) -> frozenset[str]:
    """Exact observed object versions a disposition may cite."""
    values = set()
    for row in inventory.issues:
        values.add(f"issue:{row['number']}@{row.get('updated_at', '')}")
        if row.get("html_url"):
            values.add(row["html_url"])
    for row in inventory.pulls:
        head = row.get("head", {}).get("sha", "")
        values.add(f"pr:{row['number']}@{head}")
        if head:
            values.add(head)
        if row.get("html_url"):
            values.add(row["html_url"])
    for row in (*inventory.local_refs, *inventory.remote_refs):
        values.add(f"ref:{row['ref']}@{row['sha']}")
        values.add(row["sha"])
        detail = row.get("branch_evidence", {})
        for commit in detail.get("commits", []):
            values.add(commit["sha"])
            values.add("commit:" + commit["sha"])
    for row in inventory.worktrees:
        if row.get("HEAD"):
            values.add(f"worktree:{row['worktree']}@{row['HEAD']}")
            values.add(row["HEAD"])
    for row in inventory.archives:
        values.add(f"archive:{row['path']}@{row['sha256']}")
    return frozenset(values)


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
    if set(disposition.evidence_ids) - evidence_ids(inventory):
        raise ValueError("Overlap disposition cites evidence absent from the observed inventory")
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
        max_metadata_bytes: int = 64 * 1024 * 1024,
        cache: dict[str, str] | None = None,
        max_cache_entries: int = 64,
        max_cache_bytes: int = 8_000_000,
    ):
        self.gateway = gateway
        self.repo = repo.resolve()
        self.clock = clock
        self.max_detail_bytes = max_detail_bytes
        if type(max_metadata_bytes) is not int or max_metadata_bytes < 1:
            raise ValueError("GitHub metadata byte limit must be a positive integer")
        self.max_metadata_bytes = max_metadata_bytes
        self.cache = cache if cache is not None else {}
        self.max_cache_entries = max_cache_entries
        self.max_cache_bytes = max_cache_bytes

    def _cached(self, kind: str, coordinates: tuple[str, ...], load: Callable[[], Any]) -> Any:
        """Cache only immutable objects; every mutable native identity stays fresh."""
        key = hashlib.sha256(
            json.dumps((self.gateway.repository, kind, coordinates)).encode()
        ).hexdigest()
        if key in self.cache:
            encoded = self.cache.pop(key)
            self.cache[key] = encoded
            return json.loads(encoded)
        value = load()
        encoded = json.dumps(value, sort_keys=True)
        if len(encoded.encode()) <= min(self.max_detail_bytes, self.max_cache_bytes):
            self.cache[key] = encoded
            while (
                len(self.cache) > self.max_cache_entries
                or sum(len(item.encode()) for item in self.cache.values()) > self.max_cache_bytes
            ):
                self.cache.pop(next(iter(self.cache)))
        return value

    def _references(self, row: JsonObject) -> tuple[int, ...]:
        repository = re.escape(self.gateway.repository)
        pattern = rf"https://github\.com/{repository}/(?:issues|pull)/(\d+)(?!\d)|(?<![\w/])#([1-9][0-9]*)"
        return tuple(
            sorted(
                {
                    int(first or second)
                    for first, second in re.findall(pattern, str(row.get("body") or ""))
                }
            )
        )

    def _bounded_metadata(
        self, rows: list[JsonObject], label: str, truncated: list[str], *, limit: int | None = None
    ) -> list[JsonObject]:
        selected: list[JsonObject] = []
        size = 0
        for row in rows:
            size += len(json.dumps(row).encode())
            if size > (self.max_metadata_bytes if limit is None else limit):
                truncated.append(label + ":metadata_byte_limit")
                break
            selected.append(row)
        return selected

    def _metadata(self, issues, pulls, truncated):
        """Apply one visible bound without rewriting any public object fields."""
        original = {"issues": len(issues), "pulls": len(pulls)}
        sizes = {
            "issues": sum(len(json.dumps(row).encode()) for row in issues),
            "pulls": sum(len(json.dumps(row).encode()) for row in pulls),
        }
        issues = self._bounded_metadata(issues, "issues", truncated)
        remaining = self.max_metadata_bytes - sum(len(json.dumps(row).encode()) for row in issues)
        pulls = self._bounded_metadata(pulls, "pulls", truncated, limit=remaining)
        return (
            issues,
            pulls,
            {
                "limit_bytes": self.max_metadata_bytes,
                "observed_rows": original,
                "observed_bytes": sizes,
                "retained_rows": {"issues": len(issues), "pulls": len(pulls)},
            },
        )

    def _linked(
        self,
        issues: list[JsonObject],
        pulls: list[JsonObject],
        truncated: list[str],
        *,
        seeds: set[int] | None = None,
    ) -> JsonObject:
        """Observe a declared closure; inferred missing references never imply absence.

        The caller first obtains complete fresh repository issue/PR collections.
        Explicit selected/mapped objects and native parents remain required;
        only inferred body references may record an ambiguous HTTP404.
        """
        known = {row["number"]: row for row in (*issues, *pulls)}
        scanned: set[int] = set()
        reachable = set(known) if seeds is None else set(seeds)
        if seeds is not None:
            reachable.update(
                number for number, row in known.items() if seeds.intersection(self._references(row))
            )
        attempted: set[int] = set()
        missing: set[int] = set()
        coverage: JsonObject = {
            "mode": "all_observed_bodies" if seeds is None else "selected_frontier",
            "seeds": sorted(known if seeds is None else seeds),
            "attempt_limit": 64,
            "attempted": 0,
            "not_found_or_inaccessible": 0,
        }
        while pending := [
            row for number, row in known.items() if number in reachable and number not in scanned
        ]:
            for row in pending:
                scanned.add(row["number"])
                row["discovery_link_coverage"] = "expanded_body_and_native_parent_references"
                references = {number: "inferred_body" for number in self._references(row)}
                if row.get("parent_issue_url"):
                    match = re.fullmatch(
                        rf"https://api\.github\.com/{re.escape(self.gateway.root)}/issues/([1-9][0-9]*)",
                        row["parent_issue_url"],
                    )
                    if match is None:
                        raise GithubError("Native parent issue identity is outside the repository")
                    references[int(match[1])] = "native_parent"
                for number, kind in references.items():
                    if number in known:
                        known[number]["discovery_linked"] = True
                        reachable.add(number)
                        continue
                    if number in missing:
                        if kind == "native_parent":
                            raise GithubError(
                                "Required native parent issue is inaccessible", status=404
                            )
                        row.setdefault("discovery_reference_observations", []).append(
                            {
                                "source": row["number"],
                                "number": number,
                                "kind": kind,
                                "status": "not_found_or_inaccessible",
                                "http_status": 404,
                            }
                        )
                        continue
                    if len(attempted) >= 64:
                        truncated.append("github:linked_object_limit")
                        return {
                            **coverage,
                            "attempted": len(attempted),
                            "scanned": len(scanned),
                            "not_found_or_inaccessible": len(missing),
                            "unexpanded": len(known) - len(scanned),
                        }
                    attempted.add(number)
                    try:
                        item = self.gateway.issue(number)
                    except GithubError as exc:
                        if exc.status != 404 or kind != "inferred_body":
                            raise
                        missing.add(number)
                        row.setdefault("discovery_reference_observations", []).append(
                            {
                                "source": row["number"],
                                "number": number,
                                "kind": kind,
                                "status": "not_found_or_inaccessible",
                                "http_status": 404,
                            }
                        )
                        continue
                    if item.get("number") != number or not item.get("id"):
                        raise GithubError("Linked issue/PR native identity is unavailable")
                    if "pull_request" in item:
                        item = self.gateway.pull(number)
                        if item.get("number") != number or not item.get("id"):
                            raise GithubError("Linked PR native identity is unavailable")
                        pulls.append(item)
                    else:
                        issues.append(item)
                    item["discovery_linked"] = True
                    known[number] = item
                    reachable.add(number)
        for number, row in known.items():
            if number not in scanned:
                row["discovery_link_coverage"] = "not_expanded_outside_selected_frontier"
        return {
            **coverage,
            "attempted": len(attempted),
            "scanned": len(scanned),
            "not_found_or_inaccessible": len(missing),
            "unexpanded": len(known) - len(scanned),
        }

    @staticmethod
    def _same_pull_identity(before: JsonObject, after: JsonObject) -> bool:
        """Bind scoped evidence to the observed native object and intent revision."""
        return all(
            before.get(field) == after.get(field)
            for field in (
                "id",
                "number",
                "body",
                "updated_at",
                "state",
                "draft",
                "merged_at",
                "merged",
            )
            if field in before
        ) and all(
            before.get(role, {}).get("sha") == after.get(role, {}).get("sha")
            for role in ("head", "base")
        )

    def _pull_paths(self, row: JsonObject) -> JsonObject:
        """Bind changed paths to exact Git objects or a stable native PR readback."""
        number = row["number"]
        head, base = row.get("head", {}).get("sha", ""), row.get("base", {}).get("sha", "")
        if not all(
            isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)
            for sha in (head, base)
        ):
            raise GithubError("PR path inventory lacks exact head/base identity")

        def observe():
            if all(
                re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)
                and git_bytes(self.repo, "cat-file", "-e", sha + "^{commit}").returncode == 0
                for sha in (base, head)
            ):
                bases = self._git(["merge-base", "--all", base, head]).splitlines()
                if not bases:
                    raise GithubError("PR paths lack a merge base")
                names = set()
                for ancestor in bases:
                    names.update(
                        path
                        for path in self._git(
                            [
                                "diff",
                                "--no-renames",
                                "--name-only",
                                "-z",
                                "--no-ext-diff",
                                "--no-textconv",
                                ancestor,
                                head,
                                "--",
                            ]
                        ).split("\0")
                        if path
                    )
                return {
                    "head": head,
                    "base": base,
                    "merge_bases": sorted(bases),
                    "changed_paths": sorted(names),
                    "source": "exact_local_git_objects",
                }
            full = self.gateway.pull(number)
            if not self._same_pull_identity(row, full):
                raise GithubError("PR identity moved before file classification")
            files = self.gateway.files(number)
            if len(files) >= 3000 or len(files) < full.get("changed_files", len(files)):
                raise GithubError("PR file classification is incomplete")
            names = set()
            for item in files:
                if not isinstance(item.get("filename"), str) or not item["filename"]:
                    raise GithubError("PR file classification has no filename")
                names.add(item["filename"])
                if item.get("status") == "renamed":
                    if (
                        not isinstance(item.get("previous_filename"), str)
                        or not item["previous_filename"]
                    ):
                        raise GithubError("PR rename classification lacks its source path")
                    names.add(item["previous_filename"])
            fresh = self.gateway.pull(number)
            if not self._same_pull_identity(full, fresh):
                raise GithubError("PR identity moved during file classification")
            return {
                "head": head,
                "base": base,
                "changed_paths": sorted(names),
                "source": "github_files_with_identity_readback",
            }

        result = self._cached("pr-changed-paths", (str(number), head, base), observe)
        if len(json.dumps(result).encode()) > self.max_detail_bytes:
            raise GithubError("PR path classification exceeds its byte bound")
        return result

    @staticmethod
    def _scoped_file_patches(
        files: list[JsonObject], paths: tuple[str, ...]
    ) -> tuple[list[JsonObject], JsonObject]:
        """Keep file identities intact and disclose patches outside ownership."""
        projected, retained, omitted, unavailable = [], [], [], []
        omitted_bytes = 0
        for item in files:
            name = item.get("filename")
            previous = item.get("previous_filename")
            if not isinstance(name, str) or not name:
                raise GithubError("PR file details lack a filename")
            if item.get("status") == "renamed" and (not isinstance(previous, str) or not previous):
                raise GithubError("PR file details lack a rename source path")
            relevant = any(
                changed == path or changed.startswith(path + "/")
                for changed in (name, previous)
                if isinstance(changed, str)
                for path in paths
            )
            result = dict(item)
            if "patch" not in item:
                unavailable.append(name)
            elif relevant:
                retained.append(name)
            else:
                omitted.append(name)
                omitted_bytes += len(str(result.pop("patch")).encode())
            projected.append(result)
        return projected, {
            "mode": "selected_owned_paths",
            "selected_paths": list(paths),
            "changed_file_count": len(files),
            "retained_patch_files": retained,
            "omitted_patch_files": omitted,
            "omitted_patch_bytes": omitted_bytes,
            "native_patch_unavailable_files": unavailable,
            "full_code_review": False,
        }

    def _observe_ref(self, remote: str, ref: str, sha: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) or not ref.startswith("refs/heads/"):
            raise GithubError("Discovery branch identity is malformed")
        if git_bytes(self.repo, "cat-file", "-e", sha + "^{commit}").returncode == 0:
            return sha
        observation = "refs/bmad-campaign/discovery/" + sha
        self._git(
            [
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--refmap=",
                "--",
                remote,
                ref + ":" + observation,
            ]
        )
        actual = self._git(["rev-parse", "--verify", observation + "^{commit}"]).strip()
        if actual != sha:
            raise GithubError("Remote branch moved during discovery; repeat its observation")
        return actual

    def _branches(
        self,
        remote: str,
        target: str,
        remote_refs: list[JsonObject],
        local_refs: list[JsonObject],
        paths: tuple[str, ...],
        unavailable: list[str],
        truncated: list[str],
    ) -> None:
        target_ref = "refs/heads/" + target
        main = next((row for row in remote_refs if row["ref"] == target_ref), None)
        if main is None:
            unavailable.append("branch_base:canonical target branch was not observed")
            return
        try:
            base = (
                self._observe_ref(remote, target_ref, main["sha"])
                if main
                else self._git(["rev-parse", "HEAD"]).strip()
            )
        except GithubError as exc:
            unavailable.append("branch_base:" + str(exc))
            return
        # A preserved checkpoint namespace can contain thousands of aliases. Walk
        # target ancestry once for locally referenced objects, and inspect each
        # distinct immutable head once. Ref names remain in the receipt.
        try:
            merged = set(
                self._git(
                    ["for-each-ref", "--merged=" + base, "--format=%(objectname)"]
                ).splitlines()
            )
        except (GitError, GithubError) as exc:
            unavailable.append("branch_ancestry:" + str(exc))
            return
        observed: dict[str, JsonObject] = {}
        detail_bytes = 0
        for row in (*remote_refs, *local_refs):
            if not row["ref"].startswith("refs/heads/"):
                continue
            if row["sha"] == base or row["sha"] in merged:
                row["inspection"] = "already_in_target"
                continue
            if previous := observed.get(row["sha"]):
                row["inspection"] = "same_immutable_head"
                row["branch_evidence_ref"] = f"ref:{previous['ref']}@{previous['sha']}"
                continue
            observed[row["sha"]] = row
            try:
                head = self._observe_ref(remote, row["ref"], row["sha"])
                ancestry = git_bytes(self.repo, "merge-base", "--is-ancestor", head, base)
                if ancestry.returncode == 0:
                    row["inspection"] = "already_in_target"
                    continue
                if ancestry.returncode != 1:
                    raise GithubError("Branch ancestry observation unavailable")
                detail = self._cached(
                    "scoped-branch-detail",
                    (base, head, str(self.max_detail_bytes), *paths),
                    lambda: self.inspect_branch(base, head, relevant_paths=paths),
                )
                if not detail["changed_paths"] and paths and not detail["truncated"]:
                    row["inspection"] = "outside_declared_paths"
                    # These are *scoped* changes, not an assertion that the
                    # branch has no changes elsewhere in the repository.
                    row["changed_paths"] = []
                    row["changed_paths_scope"] = list(paths)
                    row["merge_base"] = detail["merge_base"]
                    row["merge_bases"] = detail["merge_bases"]
                    continue
                detail_bytes += len(json.dumps(detail).encode())
                if detail_bytes > self.max_detail_bytes * 4:
                    if "git:branch_detail_byte_limit" not in truncated:
                        truncated.append("git:branch_detail_byte_limit")
                    row["inspection"] = "detail_byte_limit"
                    # Continue classifying every remaining ref; a byte budget
                    # must not conceal whether later refs overlap this scope.
                    continue
                row["branch_evidence"] = detail
                if detail["truncated"] or detail.get("commits_truncated"):
                    truncated.append("ref:" + row["ref"] + ":detail")
            except (GithubError, GitError, ValueError) as exc:
                unavailable.append("ref:" + row["ref"] + ":" + str(exc))

    def _git(
        self, args: list[str], *, repo: Path | None = None, input_data: bytes | None = None
    ) -> str:
        # Status/diff observations must not refresh another live worker's index.
        result = git_bytes(
            repo or self.repo,
            "-c",
            "diff.autoRefreshIndex=false",
            *args,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            input_data=input_data,
        )
        if result.returncode:
            raise GithubError("Repository inventory git observation failed: " + args[0])
        return result.stdout.decode("utf-8")

    def collect(
        self,
        *,
        remote: str = "origin",
        recent_since: str = "",
        mapped_prs: tuple[int, ...] = (),
        selected_issues: tuple[int, ...] = (),
        detailed_prs: tuple[int, ...] | None = (),
        archive_roots: tuple[Path, ...] = (),
        target: str = "main",
        relevant_paths: tuple[str, ...] = (),
    ) -> CoverageReceipt:
        if remote.startswith("-") or not remote:
            raise ValueError("Invalid discovery remote")
        unavailable: list[str] = []
        truncated: list[str] = []
        common = (self.repo / self._git(["rev-parse", "--git-common-dir"]).strip()).resolve()
        issues: list[JsonObject] = []
        pulls: list[JsonObject] = []
        github_coverage: JsonObject = {}
        paths = tuple(sorted({path.rstrip("/") for path in relevant_paths}))
        scoped = (
            detailed_prs is None
            and bool(paths)
            and not any(path in {"*", ".", ""} for path in paths)
        )
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
                    known.add(number)
            known_issues = {int(row["number"]) for row in issues}
            for number in selected_issues:
                if number not in known_issues:
                    item = self.gateway.issue(number)
                    if item.get("number") != number or not item.get("id") or "pull_request" in item:
                        raise GithubError("Selected issue native identity is unavailable")
                    issues.append(item)
                    known_issues.add(number)
            issues, pulls, github_coverage["metadata"] = self._metadata(issues, pulls, truncated)
            if truncated:
                raise GithubError("Global GitHub metadata coverage exceeds its byte bound")
            seeds = set(selected_issues) | set(mapped_prs)
            if scoped:
                overlap = 0
                historical_overlap = 0
                for row in pulls:
                    classified = self._pull_paths(row)
                    relevant = any(
                        changed == path or changed.startswith(path + "/")
                        for changed in classified["changed_paths"]
                        for path in paths
                    )
                    row["discovery_path_coverage"] = {
                        **classified,
                        "selected_paths": list(paths),
                        "overlaps": relevant,
                    }
                    if relevant:
                        overlap += 1
                        if row.get("state") == "closed" and (
                            row.get("merged") is True or row.get("merged_at")
                        ):
                            historical_overlap += 1
                        else:
                            seeds.add(row["number"])
                github_coverage["pr_paths"] = {
                    "mode": "all_open_and_recent_exact_changed_paths",
                    "classified": len(pulls),
                    "overlapping": overlap,
                    "historical_merged_overlapping": historical_overlap,
                    "outside_selected_paths": len(pulls) - overlap,
                }
            github_coverage["links"] = self._linked(
                issues,
                pulls,
                truncated,
                seeds=seeds if scoped else None,
            )
            if scoped:
                # A selected link can discover a PR outside the recent window.
                for row in pulls:
                    if "discovery_path_coverage" not in row:
                        classified = self._pull_paths(row)
                        row["discovery_path_coverage"] = {
                            **classified,
                            "selected_paths": list(paths),
                            "overlaps": any(
                                changed == path or changed.startswith(path + "/")
                                for changed in classified["changed_paths"]
                                for path in paths
                            ),
                        }
                github_coverage["pr_paths"]["linked_outside_initial_collection"] = (
                    len(pulls) - github_coverage["pr_paths"]["classified"]
                )
            issues, pulls, github_coverage["metadata"] = self._metadata(issues, pulls, truncated)
            metadata = github_coverage["metadata"]
            retained_metadata_bytes = sum(
                len(json.dumps(row).encode()) for row in (*issues, *pulls)
            )
            if scoped:
                metadata.update(retained_bytes=retained_metadata_bytes, file_metadata_rows=0)
                metadata["observed_bytes"].update(pr_enrichment=0, file_metadata=0)
            issues_by_number = {row["number"]: row for row in issues}
            detailed: list[JsonObject] = []
            detail_bytes = 0
            detail_count = 0
            for row in pulls:
                if (
                    detailed_prs is None
                    and (
                        (
                            scoped
                            and row.get("discovery_link_coverage")
                            == "expanded_body_and_native_parent_references"
                        )
                        or (
                            not scoped
                            and (
                                row.get("state", "open") == "open"
                                or row["number"] in mapped_prs
                                or row.get("discovery_linked")
                            )
                        )
                    )
                ) or (detailed_prs is not None and row["number"] in detailed_prs):
                    detail_count += 1
                    number = int(row["number"])
                    full = self.gateway.pull(number)
                    if not self._same_pull_identity(row, full):
                        raise GithubError("PR identity moved before detail observation")
                    head, base = str(full.get("head", {}).get("sha", "")), str(
                        full.get("base", {}).get("sha", "")
                    )
                    if not head or not base:
                        raise GithubError("Detailed PR inventory lacks exact head/base identity")
                    files = self._cached(
                        "pr-files",
                        (str(row["number"]), head, base),
                        lambda number=number: self.gateway.files(number),
                    )
                    if len(files) < full.get("changed_files", len(files)) or len(files) >= 3000:
                        truncated.append(f"pr:{row['number']}:files")
                    patch_coverage = None
                    file_metadata = []
                    if scoped:
                        files, patch_coverage = self._scoped_file_patches(files, paths)
                        file_metadata = [
                            {key: value for key, value in item.items() if key != "patch"}
                            for item in files
                        ]
                        enrichment_bytes = len(json.dumps({**row, **full}).encode()) - len(
                            json.dumps(row).encode()
                        )
                        file_metadata_bytes = len(json.dumps(file_metadata).encode())
                        metadata["observed_bytes"]["pr_enrichment"] += enrichment_bytes
                        metadata["observed_bytes"]["file_metadata"] += file_metadata_bytes
                        metadata["observed_total_bytes"] = sum(metadata["observed_bytes"].values())
                        metadata["file_metadata_rows"] += len(file_metadata)
                        if (
                            retained_metadata_bytes + enrichment_bytes + file_metadata_bytes
                            > self.max_metadata_bytes
                        ):
                            truncated.append(f"pr:{number}:metadata_byte_limit")
                            detailed.append(
                                {
                                    **row,
                                    "details_truncated": True,
                                    "discovery_detail_coverage": "metadata_byte_limit",
                                }
                            )
                            continue
                        retained_metadata_bytes += enrichment_bytes + file_metadata_bytes
                        metadata["retained_bytes"] = retained_metadata_bytes
                    facts = {
                        "changed_files_detail": files,
                        "reviews": self.gateway.reviews(row["number"]),
                        "review_threads": self.gateway.review_threads(row["number"]),
                        "check_runs": self.gateway.pages(
                            f"{self.gateway.root}/commits/{head}/check-runs",
                            collection="check_runs",
                        ),
                        "statuses": self.gateway.pages(
                            f"{self.gateway.root}/commits/{head}/statuses"
                        ),
                        "linked_issues": [
                            issues_by_number[number]
                            for number in self._references(full)
                            if number in issues_by_number
                        ],
                        "link_coverage": "explicit_repository_body_references",
                        **({"patch_coverage": patch_coverage} if patch_coverage else {}),
                    }
                    detail = {**row, **full, **facts}
                    fresh = self.gateway.pull(number)
                    if not self._same_pull_identity(full, fresh):
                        raise GithubError("PR identity moved during discovery detail observation")
                    content = facts
                    if scoped:
                        # Public file metadata and linked issue rows are already
                        # indexed in the shared metadata budget. Charge their
                        # references and selected patches as expansion content.
                        content = {
                            **facts,
                            "changed_files_detail": [
                                {"filename": item["filename"], "patch": item["patch"]}
                                for item in files
                                if "patch" in item
                            ],
                            "linked_issues": [
                                {"id": item["id"], "number": item["number"]}
                                for item in facts["linked_issues"]
                            ],
                        }
                    observed_bytes = len(json.dumps(content).encode())
                    metadata_detail = {
                        **row,
                        **full,
                        **(
                            {
                                "changed_files_detail": file_metadata,
                                "patch_coverage": patch_coverage,
                            }
                            if scoped
                            else {}
                        ),
                    }
                    if observed_bytes > self.max_detail_bytes:
                        truncated.append(f"pr:{row['number']}:detail")
                        # Metadata remains useful; oversized content is visibly unavailable.
                        detail = {
                            **metadata_detail,
                            "details_truncated": True,
                            "changed_paths": sorted(
                                {
                                    name
                                    for item in files
                                    for name in (item["filename"], item.get("previous_filename"))
                                    if name
                                }
                            ),
                        }
                    detail_bytes += observed_bytes
                    if detail_bytes > self.max_detail_bytes * 4:
                        if "github:pr_detail_byte_limit" not in truncated:
                            truncated.append("github:pr_detail_byte_limit")
                        detail = {**metadata_detail, "details_truncated": True}
                    detailed.append(detail)
                else:
                    detailed.append(
                        {
                            **row,
                            "discovery_detail_coverage": (
                                "metadata_paths_ancestry_only_historical_merged"
                                if scoped
                                and row.get("state") == "closed"
                                and (row.get("merged") is True or row.get("merged_at"))
                                else "metadata_only_outside_selected_frontier"
                            ),
                        }
                        if scoped
                        else row
                    )
            pulls = detailed
            github_coverage["details"] = {
                "mode": "selected_and_path_related" if scoped else "broad_or_explicit",
                "detailed": detail_count,
                "metadata_only": len(pulls) - detail_count,
                "observed_bytes": detail_bytes,
                "limit_bytes": self.max_detail_bytes * 4,
                "byte_scope": (
                    "selected_patches_reviews_threads_checks_and_references"
                    if scoped
                    else "expanded_facts"
                ),
            }
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
        self._branches(
            remote, target, remote_refs, local_refs, relevant_paths, unavailable, truncated
        )
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
                "relevant_branch_diffs_commits",
                "selected_linked_objects" if scoped else "explicit_linked_objects",
                *(
                    ("all_pr_changed_paths", "unrelated_body_closures_not_expanded")
                    if scoped
                    else ()
                ),
                *(("recent_closed_prs",) if recent_since else ()),
                *(
                    (("relevant_pr_diffs_reviews",) if scoped else ("active_pr_diffs_reviews",))
                    if detailed_prs is None
                    else ("selected_pr_details",) if detailed_prs else ()
                ),
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
            github_coverage,
        )

    def inspect_branch(
        self, base: str, head: str, *, relevant_paths: tuple[str, ...] = ()
    ) -> JsonObject:
        """Observe branch-authored changes, scoped before loading patch/history.

        Target-only changes are not unfinished branch work. Merge-base diffs
        establish that distinction even when an old preserved branch is far
        behind main. Empty/broad scopes retain repository-wide inspection.
        """
        resolved: list[str] = []
        for ref in (base, head):
            if not ref or ref.startswith("-"):
                raise ValueError("Invalid inventory revision")
            resolved.append(
                self._git(["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"]).strip()
            )
        before, after = resolved
        merge_bases = sorted(self._git(["merge-base", "--all", before, after]).splitlines())
        if not merge_bases:
            raise GithubError("Branch has no merge base; overlap requires explicit review")
        paths = (
            tuple(sorted({path.rstrip("/") for path in relevant_paths}))
            if not any(path in {"*", "."} for path in relevant_paths)
            else ()
        )
        # Criss-cross histories have several equally valid best bases. Inspect
        # each rather than choose one silently. Identical deltas share evidence.
        deltas: list[JsonObject] = []
        expanded: set[str] = set()
        diff_bytes = 0
        for merge_base in merge_bases:
            changed, diff = self._branch_delta(merge_base, after, paths)
            expanded.update(changed)
            encoded = diff.encode()
            digest = hashlib.sha256(encoded).hexdigest()
            duplicate = next((item for item in deltas if item["sha256"] == digest), None)
            if duplicate is not None:
                duplicate["merge_bases"].append(merge_base)
                continue
            remaining = max(0, self.max_detail_bytes - diff_bytes)
            deltas.append(
                {
                    "merge_bases": [merge_base],
                    "changed_paths": changed,
                    "diff": encoded[:remaining].decode("utf-8", errors="replace"),
                    "sha256": digest,
                    "truncated": len(encoded) > remaining,
                }
            )
            diff_bytes += len(encoded)
        primary = deltas[0]
        shared = {
            "base": before,
            "head": after,
            "merge_base": merge_bases[0],
            "merge_bases": merge_bases,
            "selected_paths": list(paths),
            "changed_paths": sorted(expanded),
            "diff": primary["diff"],
            "sha256": primary["sha256"],
            "equivalent_merge_bases": primary["merge_bases"],
            "alternate_diffs": deltas[1:],
            "truncated": any(item["truncated"] for item in deltas),
        }
        if paths and not expanded:
            return {**shared, "commits": [], "commits_truncated": False}
        pathspec = [":(literal)" + path for path in sorted(expanded)] if paths else []
        commits, commits_truncated = self._branch_commits(before, after, pathspec)
        return {**shared, "commits": commits, "commits_truncated": commits_truncated}

    def _branch_commits(
        self, before: str, after: str, pathspec: list[str]
    ) -> tuple[list[JsonObject], bool]:
        """Bound Git's output before capture, including arbitrarily large subjects.

        Fixed-width IDs include one more row than could fit the evidence budget.
        Measure immutable objects in one batch before formatting a bounded prefix;
        both source-byte and retained-byte overflow remain incomplete coverage.
        """
        count = max(1, self.max_detail_bytes // len(after) + 1)
        hashes = self._git(
            [
                "log",
                "--no-show-signature",
                "--no-notes",
                "--format=%H",
                f"--max-count={count}",
                before + ".." + after,
                "--",
                *pathspec,
            ]
        ).splitlines()
        commits_truncated = len(hashes) == count
        # An empty selected-path checkpoint still contributes its exact tip.
        hashes = list(dict.fromkeys(([after] if pathspec else []) + hashes))
        if not hashes:
            return [], commits_truncated
        if any(not re.fullmatch(r"[0-9a-f]{" + str(len(after)) + "}", sha) for sha in hashes):
            raise GithubError("Branch commit inventory returned malformed identities")
        sizes = self._git(
            ["cat-file", "--batch-check=%(objectname) %(objectsize)"],
            input_data="".join(sha + "\n" for sha in hashes).encode(),
        ).splitlines()
        if len(sizes) != len(hashes):
            raise GithubError("Branch commit size coverage is incomplete")
        selected: list[str] = []
        source_bytes = 0
        for sha, row in zip(hashes, sizes):
            fields = row.split()
            if len(fields) != 2 or fields[0] != sha or not fields[1].isdigit():
                raise GithubError("Branch commit size observation is malformed")
            source_bytes += int(fields[1])
            if source_bytes > self.max_detail_bytes:
                commits_truncated = True
                break
            selected.append(sha)
        if not selected:
            return [], commits_truncated
        raw_commits = self._git(
            [
                "log",
                "--no-walk=unsorted",
                "--no-show-signature",
                "--no-notes",
                "--format=%H%x00%P%x00%s%x00",
                "--stdin",
            ],
            input_data="".join(sha + "\n" for sha in selected).encode(),
        )
        fields = raw_commits.split("\0")
        commits: list[JsonObject] = []
        seen: set[str] = set()
        commit_bytes = 0
        for index in range(0, len(fields) - 2, 3):
            sha = fields[index].strip()
            if sha in seen:
                continue
            seen.add(sha)
            commit = {
                "sha": sha,
                "parents": fields[index + 1].split(),
                "subject": fields[index + 2][:2000],
            }
            commit_bytes += len(json.dumps(commit).encode())
            if commit_bytes > self.max_detail_bytes:
                commits_truncated = True
                break
            commits.append(commit)
        return commits, commits_truncated

    def _branch_delta(
        self, merge_base: str, after: str, paths: tuple[str, ...]
    ) -> tuple[list[str], str]:
        """Read a selected delta and both endpoints of any selected rename."""
        pathspec = [":(literal)" + path for path in paths]
        names = [
            name
            for name in self._git(
                [
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "-z",
                    "--no-ext-diff",
                    "--no-textconv",
                    merge_base,
                    after,
                    "--",
                    *pathspec,
                ]
            ).split("\0")
            if name
        ]
        if paths and not names:
            return [], ""
        # Expand a selected rename to both endpoints. A scoped diff alone would
        # show a deletion/addition and lose the other path's ownership evidence.
        expanded = set(names)
        if paths:
            fields = self._git(
                [
                    "diff",
                    "--name-status",
                    "--find-renames",
                    "-z",
                    "--no-ext-diff",
                    "--no-textconv",
                    merge_base,
                    after,
                    "--",
                ]
            ).split("\0")
            index = 0
            while index < len(fields) - 1:
                status, first = fields[index : index + 2]
                index += 2
                if status.startswith(("R", "C")):
                    second = fields[index]
                    index += 1
                    if first in expanded or second in expanded:
                        expanded.update((first, second))
            pathspec = [":(literal)" + path for path in sorted(expanded)]
        diff = self._git(
            [
                "diff",
                "--find-renames",
                "--no-ext-diff",
                "--no-textconv",
                merge_base,
                after,
                "--",
                *pathspec,
            ]
        )
        return sorted(expanded), diff
