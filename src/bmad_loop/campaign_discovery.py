"""Read-only repository work inventory and acceptance-overlap validation.

Worktree/ref observations are evidence, never leases. No inspected branch is
checked out or executed, and archived roots must be explicitly supplied. Missing
or truncated coverage is carried in the receipt and prevents a 'new work' claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
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
        cache: dict[str, str] | None = None,
        max_cache_entries: int = 64,
        max_cache_bytes: int = 8_000_000,
    ):
        self.gateway = gateway
        self.repo = repo.resolve()
        self.clock = clock
        self.max_detail_bytes = max_detail_bytes
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
        self, rows: list[JsonObject], label: str, truncated: list[str]
    ) -> list[JsonObject]:
        selected: list[JsonObject] = []
        size = 0
        for row in rows:
            size += len(json.dumps(row).encode())
            if size > self.max_detail_bytes * 4:
                truncated.append(label + ":metadata_byte_limit")
                break
            selected.append(row)
        return selected

    def _linked(
        self, issues: list[JsonObject], pulls: list[JsonObject], truncated: list[str]
    ) -> None:
        """Expand exact repository references, including closed work outside the window."""
        known = {row["number"]: row for row in (*issues, *pulls)}
        scanned: set[int] = set()
        fetched = 0
        while pending := [row for number, row in known.items() if number not in scanned]:
            for row in pending:
                scanned.add(row["number"])
                for number in self._references(row):
                    if number in known:
                        known[number]["discovery_linked"] = True
                        continue
                    if fetched >= 64:
                        truncated.append("github:linked_object_limit")
                        return
                    item = self.gateway.issue(number)
                    if item.get("number") != number or not item.get("id"):
                        raise GithubError("Linked issue/PR native identity is unavailable")
                    fetched += 1
                    if "pull_request" in item:
                        item = self.gateway.pull(number)
                        pulls.append(item)
                    else:
                        issues.append(item)
                    item["discovery_linked"] = True
                    known[number] = item

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
            self._linked(issues, pulls, truncated)
            issues = self._bounded_metadata(issues, "issues", truncated)
            pulls = self._bounded_metadata(pulls, "pulls", truncated)
            issues_by_number = {row["number"]: row for row in issues}
            detailed: list[JsonObject] = []
            detail_bytes = 0
            for row in pulls:
                if (
                    detailed_prs is None
                    and (
                        row.get("state", "open") == "open"
                        or row["number"] in mapped_prs
                        or row.get("discovery_linked")
                    )
                ) or (detailed_prs is not None and row["number"] in detailed_prs):
                    number = int(row["number"])
                    full = self.gateway.pull(number)
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
                    detail = {
                        **full,
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
                    }
                    fresh = self.gateway.pull(number)
                    if any(
                        fresh.get(field) != full.get(field)
                        for field in ("head", "base", "updated_at", "state", "draft", "merged")
                    ):
                        raise GithubError("PR identity moved during discovery detail observation")
                    if len(json.dumps(detail).encode()) > self.max_detail_bytes:
                        truncated.append(f"pr:{row['number']}:detail")
                        # Metadata remains useful; oversized content is visibly unavailable.
                        detail = {
                            **full,
                            "details_truncated": True,
                            "changed_paths": [f["filename"] for f in files],
                        }
                    detail_bytes += len(json.dumps(detail).encode())
                    if detail_bytes > self.max_detail_bytes * 4:
                        truncated.append("github:pr_detail_byte_limit")
                        detail = {**full, "details_truncated": True}
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
                "explicit_linked_objects",
                *(("recent_closed_prs",) if recent_since else ()),
                *(
                    ("active_pr_diffs_reviews",)
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
