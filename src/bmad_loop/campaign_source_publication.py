"""Canonical BMAD source delivery after remote code merge.

Tracked source is observed at a freshly fetched immutable main commit; this
module never edits a live code checkout to repair tracking. Shared stores receive
unit-scoped semantic writes through their existing sole writer. A versioned
store remains publication debt until its exact commit is remotely reachable.
Accepted private plans require a coordinator receipt outside editing trees,
bound to actual merged code and independently accepted artifact bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from . import (
    bmadconfig,
    frontmatter,
    runs,
    sprintstatus,
    stories,
    ticketlayout,
    ticketstore,
    verify,
)
from .campaign_model import SourceSelector, UnitSpec
from .campaign_recovery import CheckpointPusher
from .campaign_sources import source_paths
from .journal import Journal
from .platform_util import atomic_write_bytes_confined, atomic_write_text, file_lock, is_link_like
from .tickettree import TicketTree, _tree_id


class SourcePublicationHold(RuntimeError):
    """Canonical source changed or its delivery cannot be independently proven."""


def digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(value, sort_keys=True), follow_symlinks=False)


def _read(path: Path) -> dict:
    if is_link_like(path) or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise SourcePublicationHold("Publication receipt is not a bounded regular file")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise SourcePublicationHold("Unsupported source publication receipt")
    return raw


def _bytes(path: Path) -> bytes | None:
    if is_link_like(path):
        raise SourcePublicationHold("Source publication refuses redirected files")
    if path.exists() and not path.is_file():
        raise SourcePublicationHold("Source publication requires regular files")
    return path.read_bytes() if path.exists() else None


def _status(data: bytes | None, kind: str, selector: str) -> str:
    if data is None:
        return ""
    if kind == "sprint-status":
        return sprintstatus.status_in_bytes(data, selector) or ""
    text = data.decode("utf-8-sig")
    fault = frontmatter.frontmatter_fault(text)
    if fault:
        raise SourcePublicationHold("Source frontmatter is invalid: " + fault)
    return frontmatter.status_of(frontmatter.parse_frontmatter(text))


def completed_plan_bytes(data: bytes) -> bytes:
    """Compute accepted plan's sole lifecycle change without a temporary file."""
    text = data.decode("utf-8")
    split = frontmatter._split_frontmatter(text)
    if split is None or frontmatter.frontmatter_fault(text):
        raise SourcePublicationHold("Accepted plan has invalid frontmatter")
    before, block, after = split
    fm = frontmatter.parse_frontmatter(text)
    if (
        frontmatter.status_of(fm) not in ("built", "done")
        or fm.get("blocked_at")
        or fm.get("blocked_reason")
    ):
        raise SourcePublicationHold("Accepted plan is not built/done without an owner hold")
    edited = frontmatter._edit_frontmatter_block(block, "status", "done")
    return (before + (block if edited is None else edited) + after).encode("utf-8")


@dataclass(frozen=True)
class AcceptedPlanPublication:
    """External coordinator capability consumed only by TicketTree.mark.

    A boolean merged/approved flag cannot create this authority. Validation ties
    the receipt digest, accepted source digest, expected canonical preimage,
    project/tree identity and actual merged commit/ref before entering the mark
    lock. The exact preimage is checked again under that lock.
    """

    receipt_path: Path
    receipt_digest: str
    expected_preimage: bytes | None
    artifact: bytes
    code_repo: Path
    source_root: Path
    plan: Path
    ref: str
    purpose: str = "execution"
    source_kind: str = "tickets"
    selector_path: str = ""
    owned_rows: tuple[str, ...] = ()

    def validate(self, project: Path, ref: str, plan: Path) -> bytes:
        receipt_path = self.receipt_path.resolve()
        if (
            receipt_path.is_relative_to(project.resolve())
            or receipt_path.is_relative_to(self.source_root.resolve())
            or receipt_path.is_relative_to(self.code_repo.resolve())
        ):
            raise SourcePublicationHold(
                "Publication capability must live outside editing/source trees"
            )
        raw = _bytes(self.receipt_path)
        if raw is None or digest(raw) != self.receipt_digest:
            raise SourcePublicationHold("Coordinator publication receipt digest differs")
        doc = _read(self.receipt_path)
        required_kind = (
            "accepted-campaign-planning-artifact"
            if self.purpose == "planning"
            else "accepted-campaign-source-artifact"
        )
        if doc.get("kind") != required_kind:
            raise SourcePublicationHold("Receipt does not grant accepted source publication")
        actual_root = (
            stories.resolve_spec_folder(project, self.selector_path).resolve()
            if self.source_kind in ("stories", "artifact-stories")
            else bmadconfig.load_paths(
                project, want_tickets=self.source_kind != "sprint-status"
            ).output_folder.resolve()
        )
        if (
            self.source_root.resolve() != actual_root
            or not plan.resolve().is_relative_to(actual_root)
            or self.plan.resolve() != plan.resolve()
            or self.ref != ref
            or doc.get("source_root") != str(actual_root)
            or doc.get("plan") != str(plan.resolve())
            or doc.get("ref") != ref
            or doc.get("project") != str(project.resolve())
            or doc.get("owned_rows", []) != list(self.owned_rows)
        ):
            raise SourcePublicationHold(
                "Accepted source identity differs from canonical project/ref/path"
            )
        if doc.get("artifact_digest") != digest(self.artifact) or doc.get(
            "preimage_digest"
        ) != digest(self.expected_preimage):
            raise SourcePublicationHold("Accepted artifact or canonical preimage digest differs")
        acceptance = doc.get("acceptance") or {}
        merge = doc.get("merge") or {}
        if (
            acceptance.get("verdict") != "approved"
            or not all(
                isinstance(merge.get(key), str)
                and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", merge[key])
                for key in ("head", "merged_commit", "target_head")
            )
            or not str(merge.get("target_ref", "")).startswith("refs/campaign-source-publication/")
            or not acceptance.get("review_id")
            or acceptance.get("artifact_digest") != digest(self.artifact)
            or acceptance.get("head") != merge.get("head")
            or not acceptance.get("graph_revision")
            or not acceptance.get("config_revision")
            or not merge.get("merged_commit")
            or not merge.get("target_head")
            or not merge.get("target_ref")
            or type(merge.get("pr_number")) is not int
            or merge["pr_number"] <= 0
        ):
            raise SourcePublicationHold("Accepted source lacks exact review/merge coordinates")
        if str(self.code_repo.resolve()) != doc.get("code_repo"):
            raise SourcePublicationHold("Accepted code repository identity differs")
        target = verify.ref_revision(self.code_repo, merge["target_ref"])
        if target != merge["target_head"] or not verify.is_ancestor(
            self.code_repo, merge["merged_commit"], target
        ):
            raise SourcePublicationHold(
                "Actual canonical Git ref does not prove merged code ancestry"
            )
        review_path = acceptance.get("review_path")
        if (
            not isinstance(review_path, str)
            or not review_path
            or Path(review_path).is_absolute()
            or ".." in Path(review_path).parts
        ):
            raise SourcePublicationHold("Accepted source lacks its exact tracked review mirror")
        mirror = verify.file_bytes_at_revision(self.code_repo, merge["merged_commit"], review_path)
        if mirror != self.artifact:
            raise SourcePublicationHold(
                "Actually merged source mirror differs from accepted private artifact"
            )
        owner = runs.owner_run_dir()
        if owner and (owner / "campaign-worker.json").exists():
            raise SourcePublicationHold(
                "An engine child cannot exercise coordinator source publication"
            )
        if self.source_kind == "sprint-status":
            if self.purpose != "planning":
                raise SourcePublicationHold(
                    "Board artifact import requires accepted planning purpose"
                )
            configured = bmadconfig.load_paths(project)
            if self.selector_path and self.selector_path != configured.active_initiative:
                configured = configured.with_initiative(self.selector_path)
            if plan.resolve() != configured.sprint_status.resolve():
                raise SourcePublicationHold("Accepted board artifact names another source path")
            return self.artifact
        if self.source_kind in ("artifact", "artifact-stories"):
            if self.purpose != "planning":
                raise SourcePublicationHold(
                    "Product artifact import requires accepted planning purpose"
                )
            return self.artifact
        if self.source_kind == "closure":
            if self.purpose != "execution" or _status(self.artifact, "tickets", ref) != "done":
                raise SourcePublicationHold(
                    "Closure import requires an already-authored done artifact"
                )
            return self.artifact
        if self.source_kind == "stories":
            if plan.parent.resolve() != (
                actual_root / "stories"
            ).resolve() or not plan.name.startswith(ref + "-"):
                raise SourcePublicationHold(
                    "Accepted story artifact has a different native selector/path"
                )
            if _status(self.artifact, "stories", ref) not in (
                ("draft", "ready-for-dev") if self.purpose == "planning" else ("done",)
            ):
                raise SourcePublicationHold(
                    "Accepted story artifact lifecycle does not match publication purpose"
                )
            return self.artifact
        if self.purpose == "planning":
            fm = frontmatter.parse_frontmatter(self.artifact.decode("utf-8"))
            if frontmatter.frontmatter_fault(
                self.artifact.decode("utf-8")
            ) or frontmatter.status_of(fm) not in ("draft", "ready-for-dev"):
                raise SourcePublicationHold(
                    "Planning publication accepts only draft/ready plan artifacts"
                )
            return self.artifact
        return completed_plan_bytes(self.artifact)

    def check_preimage(self, current: bytes | None, done: bytes) -> bool:
        if current == done:
            return False  # Exact replay after a successful write, never a new rewrite.
        if current != self.expected_preimage:
            raise SourcePublicationHold("Canonical source was edited since its captured preimage")
        return True


class _PinnedTree(TicketTree):
    def __init__(self, *args, expected: bytes | None, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected = expected

    @contextmanager
    def _plan_lock(self, plan: Path) -> Iterator[None]:
        with super()._plan_lock(plan):
            if _bytes(plan) != self.expected:
                raise SourcePublicationHold(
                    "Canonical plan changed before its semantic publication lock"
                )
            yield


@dataclass(frozen=True)
class SourceBinding:
    kind: str
    unit_id: str
    selector: str
    project: str
    source_root: str
    path: str
    versioning: str
    revision: str | None
    status: str
    preimage: str | None
    code_relative: str = ""
    store_repo: str = ""
    children: tuple[tuple[str, str], ...] = ()
    manifests: tuple[tuple[str, str], ...] = ()
    native_ticket: str = ""
    selector_path: str = ""
    # ref, native leaf, exact leaf revision, native ID, absent plan path.
    # A missing plan alone never proves a dropped ticket.
    dropped_children: tuple[tuple[str, str, str, str, str], ...] = ()
    retrospective: tuple[str, str | None] | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> SourceBinding:
        data = dict(raw)
        for field_name in ("children", "manifests", "dropped_children"):
            data[field_name] = tuple(tuple(pair) for pair in data.get(field_name, ()))
        if data.get("retrospective") is not None:
            data["retrospective"] = tuple(data["retrospective"])
        return cls(**data)


@dataclass(frozen=True)
class SourcePublicationResult:
    state: str
    canonical_head: str
    source_revision: str = ""
    source_commit: str = ""
    source_remote_head: str = ""
    proposal: dict[str, Any] = field(default_factory=dict)
    detail: str = ""


@dataclass(frozen=True)
class ArtifactChange:
    path: str
    data: bytes
    preimage: bytes | None
    review_path: str
    selector: str = ""
    owned_rows: tuple[str, ...] = ()


class SourcePublication:
    def __init__(
        self, project: Path, state_dir: Path, *, remote: str = "origin", target: str = "main"
    ):
        self.project, self.root = project.resolve(), state_dir.resolve()
        if self.root.is_relative_to(self.project):
            raise SourcePublicationHold("Publication state must be outside editing trees")
        if not remote or remote.startswith("-") or not target or target.startswith("-"):
            raise SourcePublicationHold("Explicit source publication remote/target required")
        self.remote, self.target = remote, target

    def _git(self, root: Path, *args: str) -> bytes:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        result = verify.git_bytes(root, *args, timeout_s=30, env=env)
        if result.returncode:
            raise SourcePublicationHold("Source publication Git operation failed: " + args[0])
        return result.stdout

    def _canonical(self, action_id: str, merged_commit: str) -> tuple[str, str]:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", merged_commit):
            raise SourcePublicationHold(
                "Canonical publication requires a full merged commit identity"
            )
        target_ref = (
            "refs/campaign-source-publication/observed-"
            + hashlib.sha256(action_id.encode()).hexdigest()
        )
        self._git(
            self.project,
            "fetch",
            "--no-tags",
            self.remote,
            "refs/heads/" + self.target + ":" + target_ref,
        )
        head = verify.ref_revision(self.project, target_ref)
        if not verify.is_ancestor(self.project, merged_commit, head):
            raise SourcePublicationHold("Merged commit is not reachable from fresh canonical main")
        stable_ref = (
            "refs/campaign-source-publication/accepted-"
            + hashlib.sha256((action_id + head).encode()).hexdigest()
        )
        if not verify.ref_exists_checked(self.project, stable_ref):
            verify.create_ref(self.project, stable_ref, head)
        if verify.ref_revision(self.project, stable_ref) != head:
            raise SourcePublicationHold("Canonical source observation ref changed")
        return head, stable_ref

    def prepare_artifact(
        self,
        action_id: str,
        binding: SourceBinding,
        artifact: bytes,
        *,
        merge: dict,
        acceptance: dict,
        purpose: str = "execution",
        canonical_path: Path | None = None,
        owned_rows: tuple[str, ...] = (),
    ) -> AcceptedPlanPublication:
        """Mint the exact external capability after controller-verified acceptance.

        The review receipt must explicitly cover the source artifact digest;
        reviewing only a code PR diff cannot approve ignored private plan bytes.
        """
        if binding.kind not in (
            "tickets",
            "stories",
            "closure",
            "artifact",
            "artifact-stories",
            "sprint-status",
        ) or not merge.get("merged_commit"):
            raise SourcePublicationHold(
                "Accepted artifact publication requires native source/merge identity"
            )
        if canonical_path is not None:
            if binding.path and canonical_path.resolve() != Path(binding.path).resolve():
                raise SourcePublicationHold(
                    "Accepted artifact path differs from captured canonical source"
                )
            binding = replace(binding, path=str(canonical_path.resolve()))
        if not binding.path:
            raise SourcePublicationHold(
                "New story publication requires an explicitly mapped canonical artifact path"
            )
        head, target_ref = self._canonical(action_id + ":capability", merge["merged_commit"])
        if purpose == "execution" and binding.kind == "tickets":
            completed_plan_bytes(artifact)
        elif purpose not in ("planning", "execution"):
            raise SourcePublicationHold("Unknown accepted source publication purpose")
        if (
            acceptance.get("artifact_digest") != digest(artifact)
            or acceptance.get("verdict") != "approved"
            or not acceptance.get("review_id")
            or acceptance.get("head") != merge.get("head")
        ):
            raise SourcePublicationHold(
                "Review receipt does not cover this accepted source artifact"
            )
        payload = {
            "schema_version": 1,
            "kind": (
                "accepted-campaign-planning-artifact"
                if purpose == "planning"
                else "accepted-campaign-source-artifact"
            ),
            "action_id": action_id,
            "project": str(self.project),
            "code_repo": str(self.project),
            "source_root": binding.source_root,
            "source_kind": binding.kind,
            "selector_path": binding.selector_path,
            "owned_rows": list(owned_rows),
            "plan": binding.path,
            "ref": binding.selector,
            "artifact_digest": digest(artifact),
            "preimage_digest": binding.revision,
            "acceptance": acceptance,
            "merge": {**merge, "target_head": head, "target_ref": target_ref},
        }
        path = (
            self.root / "capabilities" / (hashlib.sha256(action_id.encode()).hexdigest() + ".json")
        )
        if path.exists():
            previous = _read(path)
            # Main may advance between a crash and resume. The immutable prior
            # observation remains valid after fresh ancestry was re-established.
            payload["merge"]["target_head"] = previous["merge"]["target_head"]
            payload["merge"]["target_ref"] = previous["merge"]["target_ref"]
            if payload != previous:
                raise SourcePublicationHold("Accepted source capability action identity reused")
        else:
            _save(path, payload)
        capability = AcceptedPlanPublication(
            path,
            digest(path.read_bytes()) or "",
            base64.b64decode(binding.preimage) if binding.preimage is not None else None,
            artifact,
            self.project,
            Path(binding.source_root),
            Path(binding.path),
            binding.selector,
            purpose,
            binding.kind,
            binding.selector_path,
            owned_rows,
        )
        capability.validate(self.project, binding.selector, Path(binding.path))
        return capability

    def prepare_plan(
        self, action_id: str, binding: SourceBinding, artifact: bytes, **kwargs
    ) -> AcceptedPlanPublication:
        if binding.kind != "tickets":
            raise SourcePublicationHold("prepare_plan requires a ticket binding")
        return self.prepare_artifact(action_id, binding, artifact, **kwargs)

    def _own_store_repo(self, path: Path) -> Path | None:
        directory = path if path.is_dir() else path.parent
        for parent in (directory, *directory.parents):
            marker = parent / ".git"
            try:
                marker.lstat()
            except FileNotFoundError:
                continue
            root = Path(
                self._git(parent, "rev-parse", "--show-toplevel").decode().strip()
            ).resolve()
            return None if root == self.project else root
        return None

    def capture(self, source: SourceSelector, unit: UnitSpec) -> SourceBinding:
        if source.kind == "explicit":
            return SourceBinding(
                "explicit",
                unit.id,
                unit.selector,
                str(self.project),
                str(self.project),
                "",
                "code-repo",
                None,
                "",
                None,
            )
        paths = source_paths(self.project, source)
        native_ticket = ""
        children: list[tuple[str, str]] = []
        dropped_children: list[tuple[str, str, str, str, str]] = []
        retrospective: tuple[str, str | None] | None = None
        manifests: list[tuple[str, str]] = []
        topology = ticketstore.assess_store(paths, "worktree") if source.kind == "tickets" else None
        if isinstance(topology, ticketstore.StoreRefusal):
            raise SourcePublicationHold("Source topology refused: " + topology.code)
        if source.kind == "tickets":
            tree = TicketTree(self.project, folder=paths.initiative_folder)
            if unit.kind == "closure":
                board = tree.status()
                if board.problems:
                    raise SourcePublicationHold("Closure source tree has unresolved problems")
                epic = next((row for row in board.epics if row.slug == unit.selector), None)
                if epic is None or epic.folder is None:
                    raise SourcePublicationHold("Closure epic source identity is missing")
                if paths.initiative_folder is None:
                    raise SourcePublicationHold("Closure initiative source identity is missing")
                path = ticketlayout.epic_file(paths.initiative_folder, unit.selector)
                retrospective_path = ticketlayout.retrospective_doc(
                    paths.initiative_folder, unit.selector
                ).resolve()
                retrospective = (str(retrospective_path), digest(_bytes(retrospective_path)))
                for row in board.tickets:
                    if row.epic == unit.selector and row.ref:
                        found = tree.find(row.ref)
                        if row.state == "dropped" and _bytes(Path(found.plan)) is None:
                            if not found.story_file:
                                raise SourcePublicationHold(
                                    "Dropped unplanned child lacks its native leaf proof"
                                )
                            leaf = Path(found.story_file).resolve()
                            leaf_revision = digest(_bytes(leaf))
                            if leaf_revision is None:
                                raise SourcePublicationHold(
                                    "Dropped unplanned child has no native leaf bytes"
                                )
                            dropped_children.append(
                                (
                                    row.ref,
                                    str(leaf),
                                    leaf_revision,
                                    _tree_id(row.id) or "",
                                    found.plan,
                                )
                            )
                        else:
                            children.append((row.ref, found.plan))
                manifest = ticketlayout.breakdown_dir(path.parent) / ticketlayout.BREAKDOWN
                manifests.append((str(manifest.resolve()), digest(_bytes(manifest)) or ""))
                parent_manifest = (
                    ticketlayout.breakdown_dir(paths.initiative_folder) / ticketlayout.BREAKDOWN
                )
                if parent_manifest != manifest:
                    manifests.append(
                        (str(parent_manifest.resolve()), digest(_bytes(parent_manifest)) or "")
                    )
            else:
                found = tree.find(unit.selector)
                native_ticket = str(found.row.id or Path(found.row.file or "").stem)
                path = Path(found.plan)
        elif source.kind == "sprint-status":
            path = paths.sprint_status
        elif source.kind == "stories":
            resolved = stories.resolve_story_spec(
                stories.resolve_spec_folder(self.project, source.path), unit.selector
            )
            if resolved.path is None:
                if resolved.kind != stories.KIND_PENDING:
                    raise SourcePublicationHold("Story spec source identity is ambiguous")
                folder = stories.resolve_spec_folder(self.project, source.path).resolve()
                manifest = folder / stories.STORIES_FILENAME
                relative = (
                    manifest.relative_to(self.project).as_posix()
                    if manifest.is_relative_to(self.project)
                    else ""
                )
                tracked = (
                    self._git(self.project, "ls-tree", "-z", "HEAD", "--", ":(literal)" + relative)
                    if relative
                    else b""
                )
                own_repo = self._own_store_repo(folder)
                return SourceBinding(
                    "stories",
                    unit.id,
                    unit.selector,
                    str(self.project),
                    str(folder),
                    "",
                    "store-repo" if own_repo else "code-repo" if tracked else "none",
                    None,
                    "",
                    None,
                    selector_path=source.path,
                    store_repo=str(own_repo) if own_repo else "",
                )
            path = resolved.path
        else:
            raise SourcePublicationHold("Unknown canonical source kind")
        path = path.resolve()
        root = (
            stories.resolve_spec_folder(self.project, source.path).resolve()
            if source.kind == "stories"
            else paths.output_folder.resolve()
        )
        data = _bytes(path)
        own_repo = self._own_store_repo(path) if topology is None else topology.store_repo
        versioning = topology.versioning if topology else "store-repo" if own_repo else "none"
        code_relative = (
            path.relative_to(self.project).as_posix() if path.is_relative_to(self.project) else ""
        )
        if topology is None and code_relative:
            tracked = self._git(
                self.project, "ls-tree", "-z", "HEAD", "--", ":(literal)" + code_relative
            )
            if not tracked and source.kind == "stories":
                ignored = verify.git_bytes(
                    self.project, "check-ignore", "-q", "--no-index", code_relative
                )
                if ignored.returncode not in (0, 1):
                    raise SourcePublicationHold("Story source ownership is unavailable")
                manifest = (
                    stories.resolve_spec_folder(self.project, source.path)
                    / stories.STORIES_FILENAME
                )
                manifest_rel = manifest.relative_to(self.project).as_posix()
                tracked = (
                    b""
                    if ignored.returncode == 0
                    else self._git(
                        self.project, "ls-tree", "-z", "HEAD", "--", ":(literal)" + manifest_rel
                    )
                )
            if tracked:
                versioning = "code-repo"
        return SourceBinding(
            "closure" if unit.kind == "closure" else source.kind,
            unit.id,
            unit.selector,
            str(self.project),
            str(root),
            str(path),
            versioning,
            digest(data),
            _status(data, source.kind, unit.selector),
            base64.b64encode(data).decode() if data is not None else None,
            code_relative,
            str(own_repo) if own_repo else "",
            tuple(children),
            tuple(manifests),
            native_ticket,
            source.path,
            tuple(dropped_children),
            retrospective,
        )

    def _mapped_story_binding(self, binding: SourceBinding, path: Path, head: str) -> SourceBinding:
        """Classify the exact newly mapped spec; a tracked manifest is not its file."""
        relative = (
            path.relative_to(self.project).as_posix() if path.is_relative_to(self.project) else ""
        )
        versioning = binding.versioning
        if (
            versioning == "code-repo"
            and relative
            and not self._git(self.project, "ls-tree", "-z", head, "--", ":(literal)" + relative)
        ):
            ignored = verify.git_bytes(self.project, "check-ignore", "-q", "--no-index", relative)
            if ignored.returncode not in (0, 1):
                raise SourcePublicationHold("Mapped story source ownership is unavailable")
            if ignored.returncode == 0:
                versioning = "none"
        return replace(binding, path=str(path), code_relative=relative, versioning=versioning)

    def _source_bytes(self, binding: SourceBinding, head: str, path: str) -> bytes | None:
        if binding.versioning == "code-repo":
            absolute = Path(path)
            if not absolute.is_absolute() or ".." in absolute.parts:
                raise SourcePublicationHold(
                    "Tracked source identity is not a canonical absolute path"
                )
            if not absolute.is_relative_to(self.project):
                raise SourcePublicationHold("Tracked source escaped its canonical code repository")
            return verify.file_bytes_at_revision(
                self.project, head, absolute.relative_to(self.project).as_posix()
            )
        return _bytes(Path(path))

    def _tracking(
        self, binding: SourceBinding, head: str, *, detail: str
    ) -> SourcePublicationResult:
        return SourcePublicationResult(
            "tracking_pr_required",
            head,
            proposal={
                "kind": "tracking",
                "unit_id": "tracking/" + binding.unit_id,
                "original_unit": binding.unit_id,
                "source_kind": binding.kind,
                "selector": binding.selector,
                "path": binding.code_relative or binding.path,
                "target": self.target,
                "base": head,
                "draft": True,
                "expected_revision": digest(self._source_bytes(binding, head, binding.path)),
                "acceptance": [
                    "Publish and verify canonical source completion; preserve unrelated source content."
                ],
            },
            detail=detail,
        )

    def _store_delivery(
        self,
        action_id: str,
        unit: UnitSpec,
        binding: SourceBinding,
        paths: list[Path],
        *,
        merged_commit: str,
        record: dict,
        record_path: Path,
        remote: str,
        branch: str,
    ) -> tuple[str, str]:
        if not remote or not branch:
            raise SourcePublicationHold(
                "Versioned source requires an explicit publication remote and branch"
            )
        repo = Path(binding.store_repo)
        if self._git(repo, "symbolic-ref", "--short", "HEAD").decode().strip() != branch:
            raise SourcePublicationHold("Versioned source branch differs from publication policy")
        message = "BMAD-Source-Action: " + action_id
        rels = {path.resolve().relative_to(repo.resolve()).as_posix() for path in paths}
        wanted = {rel: _bytes(repo / rel) for rel in rels}
        pusher = CheckpointPusher(repo, self.root / "store-pushes", remote, branch)
        remote_head = pusher.remote_head()
        if remote_head is not None:
            self._git(repo, "fetch", "--no-tags", remote, remote_head)
            if all(
                verify.file_bytes_at_revision(repo, remote_head, rel) == value
                for rel, value in wanted.items()
            ):
                commit = record.get("source_commit") or remote_head
                if not pusher._ancestor(commit, remote_head):
                    raise SourcePublicationHold(
                        "Remote source no longer contains its recorded publication"
                    )
                record.update(source_commit=commit, source_remote_head=remote_head)
                _save(record_path, record)
                push_action = "source:" + action_id
                if any(row.action_id == push_action for row in pusher.observations()):
                    reconciled = pusher.reconcile(push_action)
                    if reconciled.state != "confirmed":
                        raise SourcePublicationHold(
                            "Source push remains uncertain despite matching remote source bytes"
                        )
                return commit, remote_head
        commit = record.get("source_commit", "")
        if not commit:
            candidates = (
                self._git(repo, "log", "--format=%H", "--fixed-strings", "--grep=" + message)
                .decode()
                .splitlines()
            )
            candidates = [
                sha
                for sha in candidates
                if message
                in self._git(repo, "show", "-s", "--format=%B", sha).decode().splitlines()
            ]
            if len(candidates) > 1:
                raise SourcePublicationHold("Source commit action has multiple matches")
            if candidates:
                commit = candidates[0]
            else:
                record["state"] = "store_committing"
                record["store_parent"] = verify.rev_parse_head(repo)
                _save(record_path, record)
                made = ticketstore.StoreCommitter(repo, self.project).commit(
                    unit.selector,
                    paths,
                    "Publish accepted campaign source\n\n" + message,
                    code_commit=merged_commit,
                )
                if made.foreign:
                    raise SourcePublicationHold("Versioned source has foreign staged content")
                commit = made.sha or verify.rev_parse_head(repo)
            changed = set(
                self._git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit)
                .decode()
                .splitlines()
            )
            # A no-change source may already be published. Otherwise the action
            # commit can include only this action's explicit source paths.
            if changed - rels or any(
                verify.file_bytes_at_revision(repo, commit, rel) != value
                for rel, value in wanted.items()
            ):
                raise SourcePublicationHold(
                    "Source action commit differs from its exact accepted paths/bytes"
                )
            record.update(state="store_committed", source_commit=commit)
            _save(record_path, record)
        pusher = CheckpointPusher(repo, self.root / "store-pushes", remote, branch)
        remote_head = pusher.remote_head()
        parent = self._git(repo, "show", "-s", "--format=%P", commit).decode().strip()
        if (
            remote_head is not None
            and not pusher._ancestor(commit, remote_head)
            and parent != remote_head
        ):
            raise SourcePublicationHold(
                "Source publication waits for preceding foreign/unpublished store commits"
            )
        pushed = pusher.push("source:" + action_id, commit)
        if pushed.state != "confirmed" or not pushed.remote_head:
            return commit, ""
        if any(
            verify.file_bytes_at_revision(repo, pushed.remote_head, rel) != value
            for rel, value in wanted.items()
        ):
            raise SourcePublicationHold(
                "Remote source bytes changed before publication confirmation"
            )
        return commit, pushed.remote_head

    def publish_artifacts(
        self,
        action_id: str,
        source: SourceSelector,
        unit: UnitSpec,
        changes: tuple[ArtifactChange, ...],
        *,
        merge: dict,
        acceptance: dict,
        journal: Journal,
        store_remote: str = "",
        store_branch: str = "",
    ) -> SourcePublicationResult:
        """Publish a reviewed planning artifact manifest; activate no graph here.

        Every file has an exact preimage and a tracked review mirror. Ticket
        plans go through the guarded mark API, including their initial import;
        other product artifacts are copied without rewriting neighboring files.
        """
        if not changes:
            raise SourcePublicationHold("Planning source publication manifest is empty")
        head, _ = self._canonical(action_id, merge.get("merged_commit", ""))
        paths = (
            source_paths(self.project, source)
            if source.kind != "explicit"
            else bmadconfig.load_paths(self.project)
        )
        source_root = (
            stories.resolve_spec_folder(self.project, source.path).resolve()
            if source.kind == "stories"
            else paths.output_folder.resolve()
        )
        topology = ticketstore.assess_store(paths, "worktree") if source.kind == "tickets" else None
        if isinstance(topology, ticketstore.StoreRefusal):
            raise SourcePublicationHold("Planning source topology is not publishable")
        prepared = []
        tracking = []
        own_repos = set()
        for change in changes:
            path = Path(change.path)
            if (
                not path.is_absolute()
                or not path.resolve().is_relative_to(source_root)
                or is_link_like(path)
            ):
                raise SourcePublicationHold("Planning artifact escaped its canonical source root")
            if path.suffix not in (".md", ".toml", ".yaml", ".yml", ".json"):
                raise SourcePublicationHold("Planning artifact is not a product document")
            if (
                not change.review_path
                or verify.file_bytes_at_revision(
                    self.project, merge["merged_commit"], change.review_path
                )
                != change.data
            ):
                raise SourcePublicationHold(
                    "Planning source artifact differs from actually merged review mirror"
                )
            if (
                acceptance.get("head") != merge.get("head")
                or acceptance.get("verdict") != "approved"
                or not acceptance.get("review_id")
                or not acceptance.get("graph_revision")
                or not acceptance.get("config_revision")
            ):
                raise SourcePublicationHold(
                    "Planning artifact lacks exact independent acceptance coordinates"
                )
            own_repo = self._own_store_repo(path)
            if own_repo:
                own_repos.add(str(own_repo))
            relative = (
                path.resolve().relative_to(self.project).as_posix()
                if path.resolve().is_relative_to(self.project)
                else ""
            )
            tracked = bool(
                relative
                and self._git(self.project, "ls-tree", "-z", head, "--", ":(literal)" + relative)
            )
            code_owned = tracked or (
                isinstance(topology, ticketstore.StoreTopology)
                and topology.versioning == "code-repo"
            )
            if relative and not own_repo and not code_owned:
                ignored = verify.git_bytes(
                    self.project, "check-ignore", "-q", "--no-index", relative
                )
                if ignored.returncode not in (0, 1):
                    raise SourcePublicationHold("Planning source ownership probe failed")
                code_owned = ignored.returncode == 1
            if code_owned:
                actual = verify.file_bytes_at_revision(self.project, head, relative)
                if actual != change.data:
                    tracking.append(
                        {
                            "path": relative,
                            "review_path": change.review_path,
                            "sha256": digest(change.data),
                            "expected_revision": digest(actual),
                        }
                    )
            else:
                prepared.append((change, path.resolve()))
        if tracking:
            return SourcePublicationResult(
                "tracking_pr_required",
                head,
                proposal={
                    "kind": "planning-tracking",
                    "original_unit": unit.id,
                    "draft": True,
                    "base": head,
                    "target": self.target,
                    "artifacts": tracking,
                },
            )
        if len(own_repos) > 1:
            raise SourcePublicationHold(
                "One source publication action cannot span independent store repositories"
            )
        record_path = (
            self.root / "artifacts" / (hashlib.sha256(action_id.encode()).hexdigest() + ".json")
        )
        specification = [
            {
                "path": str(path),
                "before": digest(change.preimage),
                "after": digest(change.data),
                "review_path": change.review_path,
                "selector": change.selector,
                "owned_rows": list(change.owned_rows),
            }
            for change, path in prepared
        ]
        with file_lock(
            self.root
            / "artifact-leases"
            / (hashlib.sha256(str(source_root).encode()).hexdigest() + ".lock")
        ):
            record = _read(record_path) if record_path.exists() else None
            if record is None:
                record = {
                    "schema_version": 1,
                    "action_id": action_id,
                    "state": "prepared",
                    "source_root": str(source_root),
                    "specification": specification,
                    "merged_commit": merge["merged_commit"],
                }
                _save(record_path, record)
            elif (
                record["specification"] != specification
                or record["source_root"] != str(source_root)
                or record["merged_commit"] != merge["merged_commit"]
            ):
                raise SourcePublicationHold("Planning publication action identity reused")
            for change, path in prepared:
                if _bytes(path) not in (change.preimage, change.data):
                    raise SourcePublicationHold(
                        "Canonical planning artifact was edited after capture"
                    )
            # Metadata establishes the native new unit identity before a plan's
            # sole writer verifies find(ref)/expected_plan under its lock.
            for change, path in sorted(
                prepared, key=lambda item: (item[1].suffix == ".md", str(item[1]))
            ):
                if _bytes(path) == change.data:
                    continue
                fm = (
                    frontmatter.parse_frontmatter(change.data.decode("utf-8"))
                    if path.suffix == ".md"
                    else {}
                )
                if path == paths.sprint_status.resolve():
                    before_rows = sprintstatus.planning_rows_in_bytes(change.preimage)
                    after_rows = sprintstatus.planning_rows_in_bytes(change.data)
                    owned = change.owned_rows or tuple(
                        sorted((after_rows.keys() - before_rows.keys()) | set(source.units))
                    )
                    binding = SourceBinding(
                        "sprint-status",
                        unit.id,
                        unit.selector,
                        str(self.project),
                        str(source_root),
                        str(path),
                        "store-repo" if own_repos else "none",
                        digest(change.preimage),
                        "",
                        (
                            base64.b64encode(change.preimage).decode()
                            if change.preimage is not None
                            else None
                        ),
                        selector_path=source.initiative,
                    )
                    accepted = self.prepare_artifact(
                        action_id + ":board",
                        binding,
                        change.data,
                        merge=merge,
                        acceptance={
                            **acceptance,
                            "artifact_digest": digest(change.data),
                            "review_path": change.review_path,
                        },
                        purpose="planning",
                        owned_rows=owned,
                    )
                    sprintstatus.advance(path, "", "", publication=accepted)
                elif "ticket" in fm:
                    if not change.selector:
                        raise SourcePublicationHold(
                            "Planning ticket artifact lacks its native selector"
                        )
                    binding = SourceBinding(
                        "tickets",
                        unit.id + ":" + change.selector,
                        change.selector,
                        str(self.project),
                        str(source_root),
                        str(path),
                        "store-repo" if own_repos else "none",
                        digest(change.preimage),
                        _status(change.preimage, "tickets", change.selector),
                        (
                            base64.b64encode(change.preimage).decode()
                            if change.preimage is not None
                            else None
                        ),
                        store_repo=next(iter(own_repos), ""),
                    )
                    accepted = self.prepare_plan(
                        action_id + ":" + change.selector,
                        binding,
                        change.data,
                        merge=merge,
                        acceptance={
                            **acceptance,
                            "artifact_digest": digest(change.data),
                            "review_path": change.review_path,
                        },
                        purpose="planning",
                    )
                    TicketTree(self.project, folder=paths.initiative_folder).mark(
                        change.selector,
                        frontmatter.status_of(fm),
                        reason="campaign-plan",
                        expected_plan=path,
                        journal=journal,
                        publication=accepted,
                    )
                else:
                    if change.preimage is not None and path.suffix == ".md":
                        previous = frontmatter.parse_frontmatter(change.preimage.decode("utf-8"))
                        if (
                            frontmatter.status_of(previous) in ("done", "dropped")
                            and change.data != change.preimage
                        ):
                            raise SourcePublicationHold(
                                "Planning cannot redefine a landed source artifact"
                            )
                    with file_lock(runs.lock_path_for(path)):
                        if _bytes(path) not in (change.preimage, change.data):
                            raise SourcePublicationHold(
                                "Planning artifact changed before its publication lock"
                            )
                        path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_bytes_confined(path, change.data, confine_root=source_root)
            source_commit = source_remote = ""
            if own_repos and prepared:
                binding = SourceBinding(
                    "artifact",
                    unit.id,
                    unit.selector,
                    str(self.project),
                    str(source_root),
                    "",
                    "store-repo",
                    None,
                    "",
                    None,
                    store_repo=next(iter(own_repos)),
                )
                source_commit, source_remote = self._store_delivery(
                    action_id,
                    unit,
                    binding,
                    [path for _, path in prepared],
                    merged_commit=merge["merged_commit"],
                    record=record,
                    record_path=record_path,
                    remote=store_remote,
                    branch=store_branch,
                )
                if not source_remote:
                    return SourcePublicationResult(
                        "store_push_pending", head, source_commit=source_commit
                    )
            record["state"] = "confirmed"
            _save(record_path, record)
            return SourcePublicationResult(
                "confirmed",
                head,
                source_revision=digest(json.dumps(specification, sort_keys=True).encode()) or "",
                source_commit=source_commit,
                source_remote_head=source_remote,
            )

    def _tracking_checkout(self, workspace: Path, merge: dict | None, recovery=None) -> Path:
        workspace = workspace.resolve()
        if recovery is not None:
            from .campaign_runtime_tracking import TrackingRecovery

            if not isinstance(recovery, TrackingRecovery):
                raise SourcePublicationHold(
                    "Tracking recovery requires its exact durable transaction"
                )
            recovery.validate(workspace)
        if workspace == self.project or (not verify.worktree_clean(workspace) and recovery is None):
            raise SourcePublicationHold("Tracking repair requires an isolated clean owned checkout")

        def common(path: Path) -> Path:
            return Path(
                self._git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
                .decode()
                .strip()
            ).resolve()

        if common(workspace) != common(self.project):
            raise SourcePublicationHold("Tracking checkout belongs to another Git repository")
        branch = self._git(workspace, "symbolic-ref", "--short", "HEAD").decode().strip()
        admin = Path(
            self._git(workspace, "rev-parse", "--absolute-git-dir").decode().strip()
        ).resolve()
        if branch == self.target or admin == common(workspace):
            raise SourcePublicationHold("Tracking repair cannot edit a canonical branch/checkout")
        if merge and not verify.is_ancestor(
            workspace, merge["merged_commit"], verify.rev_parse_head(workspace)
        ):
            raise SourcePublicationHold(
                "Tracking checkout does not include the original merged code"
            )
        return workspace

    def _tracking_ticket_paths(
        self, workspace: Path, source: SourceSelector, local_root: Path
    ) -> bmadconfig.ProjectPaths:
        config = workspace / ticketstore.USER_CONFIG_REL
        if is_link_like(config):
            raise SourcePublicationHold("Tracking source override refuses redirected configuration")
        if verify.path_tracked(workspace, ticketstore.USER_CONFIG_REL.as_posix()):
            configured = source_paths(workspace, source)
            if configured.output_folder.resolve() != local_root.resolve():
                raise SourcePublicationHold(
                    "Tracking source overrides require untracked native user configuration"
                )
            return configured
        from .install import _worktree_local_exclude

        refusal = _worktree_local_exclude(workspace, ["/" + ticketstore.USER_CONFIG_REL.as_posix()])
        if refusal:
            raise SourcePublicationHold("Cannot shield tracking source configuration: " + refusal)
        ticketstore.redirect_mount_config(
            workspace, main_store=local_root, initiative=source.initiative
        )
        return source_paths(workspace, source)

    def prepare_tracking_artifacts(
        self,
        workspace: Path,
        source: SourceSelector,
        unit: UnitSpec,
        changes: tuple[ArtifactChange, ...],
        *,
        merge: dict,
        acceptance: dict,
        journal: Journal,
        recovery=None,
    ) -> tuple[Path, ...]:
        """Stage reviewed planning source in a fresh owned tracking branch.

        This applies the same per-file CAS and ticket sole-writer rules as
        canonical publication. The caller supplies the ordinary draft PR,
        exact-head verification, review, merge and final source readback.
        """
        workspace = self._tracking_checkout(workspace, merge, recovery)
        if not changes:
            raise SourcePublicationHold("Tracking planning artifact manifest is empty")
        original_paths = (
            source_paths(self.project, source)
            if source.kind != "explicit"
            else bmadconfig.load_paths(self.project)
        )
        root = (
            stories.resolve_spec_folder(self.project, source.path).resolve()
            if source.kind == "stories"
            else original_paths.output_folder.resolve()
        )
        if not root.is_relative_to(self.project):
            raise SourcePublicationHold("Tracking planning source is not code-owned")
        local_root = workspace / root.relative_to(self.project)
        if source.kind == "tickets":
            local_paths = self._tracking_ticket_paths(workspace, source, local_root)
        else:
            local_paths = (
                source_paths(workspace, source)
                if source.kind != "explicit"
                else bmadconfig.load_paths(workspace)
            )
        prepared = []
        for change in changes:
            original = Path(change.path)
            if not original.is_absolute() or not original.resolve().is_relative_to(root):
                raise SourcePublicationHold("Tracking planning path escaped canonical source")
            path = workspace / original.resolve().relative_to(self.project)
            if is_link_like(path):
                raise SourcePublicationHold("Tracking planning source path is a link")
            if (
                not change.review_path
                or Path(change.review_path).is_absolute()
                or ".." in Path(change.review_path).parts
                or verify.file_bytes_at_revision(
                    workspace, merge["merged_commit"], change.review_path
                )
                != change.data
                or acceptance.get("head") != merge.get("head")
                or acceptance.get("verdict") != "approved"
                or not acceptance.get("review_id")
                or not acceptance.get("graph_revision")
                or not acceptance.get("config_revision")
            ):
                raise SourcePublicationHold(
                    "Tracking planning artifact lacks exact merged review proof"
                )
            current = _bytes(path)
            if current not in (change.preimage, change.data):
                raise SourcePublicationHold("Tracking planning source has newer owner edits")
            prepared.append((change, path, current))
        local = SourcePublication(
            workspace, self.root / "tracking-planning", remote=self.remote, target=self.target
        )
        changed = []
        for change, path, current in sorted(
            prepared, key=lambda item: (item[1].suffix == ".md", str(item[1]))
        ):
            if current == change.data:
                continue
            fm = (
                frontmatter.parse_frontmatter(change.data.decode("utf-8"))
                if path.suffix == ".md"
                else {}
            )
            if path == workspace / original_paths.sprint_status.relative_to(self.project):
                before_rows = sprintstatus.planning_rows_in_bytes(current)
                after_rows = sprintstatus.planning_rows_in_bytes(change.data)
                owned = change.owned_rows or tuple(
                    sorted((after_rows.keys() - before_rows.keys()) | set(source.units))
                )
                binding = SourceBinding(
                    "sprint-status",
                    unit.id,
                    unit.selector,
                    str(workspace),
                    str(local_root),
                    str(path),
                    "code-repo",
                    digest(current),
                    "",
                    base64.b64encode(current).decode() if current is not None else None,
                    selector_path=source.initiative,
                )
                accepted = local.prepare_artifact(
                    "tracking-board:" + unit.id + ":" + verify.rev_parse_head(workspace),
                    binding,
                    change.data,
                    merge=merge,
                    acceptance={
                        **acceptance,
                        "artifact_digest": digest(change.data),
                        "review_path": change.review_path,
                    },
                    purpose="planning",
                    owned_rows=owned,
                )
                sprintstatus.advance(path, "", "", publication=accepted)
            elif "ticket" in fm:
                if source.kind != "tickets" or not change.selector:
                    raise SourcePublicationHold("Tracking ticket plan lacks native source identity")
                binding = SourceBinding(
                    "tickets",
                    unit.id + ":" + change.selector,
                    change.selector,
                    str(workspace),
                    str(local_root),
                    str(path),
                    "code-repo",
                    digest(current),
                    _status(current, "tickets", change.selector),
                    base64.b64encode(current).decode() if current is not None else None,
                )
                cap = local.prepare_plan(
                    "tracking-plan:"
                    + unit.id
                    + ":"
                    + change.selector
                    + ":"
                    + verify.rev_parse_head(workspace),
                    binding,
                    change.data,
                    merge=merge,
                    acceptance={
                        **acceptance,
                        "artifact_digest": digest(change.data),
                        "review_path": change.review_path,
                    },
                    purpose="planning",
                )
                TicketTree(workspace, folder=local_paths.initiative_folder).mark(
                    change.selector,
                    frontmatter.status_of(fm),
                    reason="campaign-plan",
                    expected_plan=path,
                    journal=journal,
                    publication=cap,
                )
            else:
                if (
                    current is not None
                    and path.suffix == ".md"
                    and frontmatter.status_of(
                        frontmatter.parse_frontmatter(current.decode("utf-8"))
                    )
                    in ("done", "dropped")
                ):
                    raise SourcePublicationHold("Planning cannot redefine a landed source artifact")
                with file_lock(runs.lock_path_for(path)):
                    if _bytes(path) != current:
                        raise SourcePublicationHold("Tracking artifact changed during publication")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes_confined(path, change.data, confine_root=local_root)
            if _bytes(path) != change.data:
                raise SourcePublicationHold("Tracking planning source failed exact readback")
            changed.append(path)
        return tuple(changed)

    def prepare_tracking_checkout(
        self,
        workspace: Path,
        source: SourceSelector,
        unit: UnitSpec,
        binding: SourceBinding,
        *,
        journal: Journal,
        accepted_plan: AcceptedPlanPublication | None = None,
        mirror_bytes: bytes | None = None,
        merge: dict | None = None,
        acceptance: dict | None = None,
        canonical_path: Path | None = None,
        recovery=None,
    ) -> tuple[Path, ...]:
        """Prepare source completion only in an owned linked tracking checkout.

        The caller creates its draft/spec first and runs the normal exact-head
        verification/review/merge pipeline afterward. This method neither commits
        nor pushes and never repairs the canonical code checkout in place.
        """
        workspace = self._tracking_checkout(workspace, merge, recovery)
        target = canonical_path or (
            accepted_plan.plan if accepted_plan else Path(binding.path) if binding.path else None
        )
        if target is None:
            raise SourcePublicationHold("Tracking repair requires the actual canonical source path")
        if not target.is_absolute() or not target.is_relative_to(self.project):
            raise SourcePublicationHold("Tracking repair only stages code-owned canonical sources")
        path = workspace / target.relative_to(self.project)
        current = _bytes(path)
        before = base64.b64decode(binding.preimage) if binding.preimage is not None else None
        artifact = (
            mirror_bytes
            if mirror_bytes is not None
            else accepted_plan.artifact if accepted_plan else None
        )
        if binding.kind == "sprint-status":
            at_merge = (
                verify.file_bytes_at_revision(
                    workspace, merge["merged_commit"], target.relative_to(self.project).as_posix()
                )
                if merge
                else before
            )
            expected = tuple(
                dict.fromkeys(
                    (binding.status, _status(at_merge, "sprint-status", unit.selector), "done")
                )
            )
            sprintstatus.advance(path, unit.selector, "done", expected_current=expected)
        elif binding.kind == "tickets":
            # Native v7 user configuration is commonly ignored, so a fresh
            # linked checkout may have no active initiative at all. Bind the
            # worker-local overlay before attempting to resolve its tree.
            relative_root = Path(binding.source_root).relative_to(self.project)
            configured = self._tracking_ticket_paths(workspace, source, workspace / relative_root)
            tree = _PinnedTree(workspace, folder=configured.initiative_folder, expected=current)
            if artifact is None:
                # A built source already in the accepted code merge needs no
                # body import; only the existing accept transition is used.
                if _status(current, "tickets", unit.selector) == "done":
                    return ()
                if _status(current, "tickets", unit.selector) != "built":
                    raise SourcePublicationHold(
                        "Tracking ticket requires its reviewed private plan artifact"
                    )
                if merge and current != verify.worktree_file_bytes_at_revision(
                    workspace, merge["merged_commit"], target.relative_to(self.project).as_posix()
                ):
                    raise SourcePublicationHold(
                        "Tracking ticket body changed since its accepted code merge"
                    )
                tree.mark(
                    unit.selector, "done", reason="accept", expected_plan=path, journal=journal
                )
            else:
                if current not in (before, artifact, completed_plan_bytes(artifact)):
                    raise SourcePublicationHold(
                        "Tracking repair would replace a newer canonical ticket body"
                    )
                if current == completed_plan_bytes(artifact):
                    return ()
                if merge is None or acceptance is None:
                    raise SourcePublicationHold(
                        "Tracking artifact requires exact merged review coordinates"
                    )
                local_binding = replace(
                    binding,
                    project=str(workspace),
                    source_root=str(configured.output_folder.resolve()),
                    path=str(path),
                    revision=digest(current),
                    preimage=base64.b64encode(current).decode() if current is not None else None,
                )
                local = SourcePublication(
                    workspace,
                    self.root / "tracking-capabilities",
                    remote=self.remote,
                    target=self.target,
                )
                cap = local.prepare_plan(
                    "tracking:" + unit.id + ":" + verify.rev_parse_head(workspace),
                    local_binding,
                    artifact,
                    merge=merge,
                    acceptance=acceptance,
                )
                tree.mark(
                    unit.selector,
                    "done",
                    reason="campaign-publish",
                    expected_plan=path,
                    journal=journal,
                    publication=cap,
                )
        elif binding.kind in ("stories", "closure"):
            if artifact is None or merge is None or acceptance is None:
                raise SourcePublicationHold(
                    "Tracking source requires its exact reviewed artifact and merge coordinates"
                )
            if current not in (before, artifact):
                raise SourcePublicationHold(
                    "Tracking repair would replace a newer canonical source artifact"
                )
            if (
                acceptance.get("artifact_digest") != digest(artifact)
                or acceptance.get("head") != merge.get("head")
                or acceptance.get("verdict") != "approved"
            ):
                raise SourcePublicationHold("Tracking artifact lacks its exact review receipt")
            if (
                verify.file_bytes_at_revision(
                    workspace, merge["merged_commit"], acceptance.get("review_path", "")
                )
                != artifact
            ):
                raise SourcePublicationHold(
                    "Tracking artifact differs from its actual merged mirror"
                )
            if (
                _status(
                    artifact, "tickets" if binding.kind == "closure" else "stories", unit.selector
                )
                != "done"
            ):
                raise SourcePublicationHold("Tracking artifact was not authored as complete")
            if binding.kind == "closure":
                local_binding = replace(
                    binding,
                    project=str(workspace),
                    path=str(path),
                    children=tuple(
                        (ref, str(workspace / Path(child).relative_to(self.project)))
                        for ref, child in binding.children
                    ),
                    manifests=tuple(
                        (str(workspace / Path(manifest).relative_to(self.project)), revision)
                        for manifest, revision in binding.manifests
                    ),
                    dropped_children=tuple(
                        (
                            ref,
                            str(workspace / Path(leaf).relative_to(self.project)),
                            revision,
                            native_id,
                            str(workspace / Path(plan).relative_to(self.project)),
                        )
                        for ref, leaf, revision, native_id, plan in binding.dropped_children
                    ),
                    retrospective=(
                        (
                            str(
                                workspace / Path(binding.retrospective[0]).relative_to(self.project)
                            ),
                            binding.retrospective[1],
                        )
                        if binding.retrospective is not None
                        else None
                    ),
                )
                local = SourcePublication(
                    workspace,
                    self.root / "tracking-closure",
                    remote=self.remote,
                    target=self.target,
                )
                local._closure_ready(local_binding, verify.rev_parse_head(workspace))
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes_confined(path, artifact, confine_root=workspace)
        else:
            raise SourcePublicationHold("Unsupported tracking source kind")
        if (
            _status(
                _bytes(path),
                "tickets" if binding.kind == "closure" else binding.kind,
                unit.selector,
            )
            != "done"
        ):
            raise SourcePublicationHold("Tracking repair did not produce canonical done source")
        return (path,) if _bytes(path) != current else ()

    def _closure_ready(self, binding: SourceBinding, head: str) -> None:
        if binding.retrospective is None:
            raise SourcePublicationHold("Closure lacks its captured retrospective proof")
        retrospective, revision = binding.retrospective
        if digest(self._source_bytes(binding, head, retrospective)) != revision:
            raise SourcePublicationHold("Closure canonical retrospective changed since capture")
        for manifest, revision in binding.manifests:
            if digest(self._source_bytes(binding, head, manifest)) != revision:
                raise SourcePublicationHold("Closure source children changed since planning")
        for ref, leaf, revision, native_id, plan in binding.dropped_children:
            data = self._source_bytes(binding, head, leaf)
            manifest = str(Path(leaf).parent / ticketlayout.BREAKDOWN)
            if (
                data is None
                or not revision
                or digest(data) != revision
                or manifest not in dict(binding.manifests)
                or self._source_bytes(binding, head, plan) is not None
            ):
                raise SourcePublicationHold("Dropped unplanned child proof changed: " + ref)
            fm = frontmatter.parse_frontmatter(data.decode("utf-8-sig"))
            state = fm.get("tracker_status") or frontmatter.status_of(fm)
            if state != "dropped" or (_tree_id(fm.get("id")) or "") != native_id:
                raise SourcePublicationHold(
                    "Dropped unplanned child lacks native dropped state: " + ref
                )
        for ref, child_plan in binding.children:
            if _status(self._source_bytes(binding, head, child_plan), "tickets", ref) not in (
                "done",
                "dropped",
            ):
                raise SourcePublicationHold(
                    "Closure still has an unfinished selected or unselected child: " + ref
                )

    def _publish_closure(
        self,
        action_id: str,
        unit: UnitSpec,
        binding: SourceBinding,
        *,
        head: str,
        merged_commit: str,
        artifact: AcceptedPlanPublication | None,
        store_remote: str,
        store_branch: str,
    ) -> SourcePublicationResult:
        self._closure_ready(binding, head)
        retrospective = (
            self._source_bytes(binding, head, binding.retrospective[0])
            if binding.retrospective
            else None
        )
        if retrospective is None or len(retrospective.strip()) < 40:
            raise SourcePublicationHold(
                "Canonical epic completion requires its substantive native retrospective"
            )
        data = self._source_bytes(binding, head, binding.path)
        if binding.versioning == "code-repo":
            if _status(data, "tickets", binding.selector) == "done":
                return SourcePublicationResult("confirmed", head, digest(data) or "")
            return self._tracking(
                binding, head, detail="Tracked epic closure must land through its draft PR"
            )
        if artifact is None or artifact.source_kind != "closure":
            if (
                _status(data, "tickets", binding.selector) == "done"
                and binding.versioning != "store-repo"
            ):
                return SourcePublicationResult("confirmed", head, digest(data) or "")
            raise SourcePublicationHold(
                "Canonical closure requires its exact authored and merged epic artifact"
            )
        path = Path(binding.path)
        desired = artifact.validate(self.project, binding.selector, path)
        record_path = (
            self.root / "closures" / (hashlib.sha256(action_id.encode()).hexdigest() + ".json")
        )
        with file_lock(
            self.root
            / "closure-leases"
            / (hashlib.sha256(binding.path.encode()).hexdigest() + ".lock")
        ):
            record = (
                _read(record_path)
                if record_path.exists()
                else {
                    "schema_version": 1,
                    "action_id": action_id,
                    "state": "prepared",
                    "binding": binding.to_dict(),
                    "artifact_digest": digest(desired),
                    "merged_commit": merged_commit,
                }
            )
            if (
                record.get("artifact_digest") != digest(desired)
                or record.get("merged_commit") != merged_commit
            ):
                raise SourcePublicationHold("Closure publication action identity reused")
            _save(record_path, record)
            with ExitStack() as locks:
                source_files: set[str] = {
                    binding.path,
                    *(path for _, path in binding.children),
                    *(leaf for _, leaf, _, _, _ in binding.dropped_children),
                    *(plan for _, _, _, _, plan in binding.dropped_children),
                    *(path for path, _ in binding.manifests),
                }
                if binding.retrospective is not None:
                    source_files.add(binding.retrospective[0])
                for source_file in sorted(source_files):
                    locks.enter_context(file_lock(runs.lock_path_for(Path(source_file))))
                self._closure_ready(binding, head)
                if artifact.check_preimage(_bytes(path), desired):
                    atomic_write_bytes_confined(
                        path, desired, confine_root=Path(binding.source_root)
                    )
                if (
                    _bytes(path) != desired
                    or _status(_bytes(path), "tickets", binding.selector) != "done"
                ):
                    raise SourcePublicationHold("Canonical closure artifact did not read back")
                self._closure_ready(binding, head)
                record.update(state="copied", published_revision=digest(desired))
                _save(record_path, record)
            commit = remote_head = ""
            if binding.versioning == "store-repo":
                commit, remote_head = self._store_delivery(
                    action_id,
                    unit,
                    binding,
                    [path],
                    merged_commit=merged_commit,
                    record=record,
                    record_path=record_path,
                    remote=store_remote,
                    branch=store_branch,
                )
                if not remote_head:
                    return SourcePublicationResult(
                        "store_push_pending", head, digest(desired) or "", commit
                    )
            record["state"] = "confirmed"
            _save(record_path, record)
            return SourcePublicationResult(
                "confirmed", head, digest(desired) or "", commit, remote_head
            )

    def publish(
        self,
        action_id: str,
        source: SourceSelector,
        unit: UnitSpec,
        binding: SourceBinding,
        *,
        merged_commit: str,
        journal: Journal,
        accepted_plan: AcceptedPlanPublication | None = None,
        store_remote: str = "",
        store_branch: str = "",
    ) -> SourcePublicationResult:
        head, target_ref = self._canonical(action_id, merged_commit)
        if (
            binding.unit_id != unit.id
            or binding.selector != unit.selector
            or binding.project != str(self.project)
        ):
            raise SourcePublicationHold("Source binding belongs to another unit or code repository")
        if source.kind == "explicit":
            if unit.kind == "planning":
                raise SourcePublicationHold(
                    "Planning activation requires its validated source artifact publication manifest"
                )
            return SourcePublicationResult("confirmed", head)
        if source.kind == "stories" and not binding.path:
            if accepted_plan is not None:
                if accepted_plan.source_root.resolve() != Path(binding.source_root).resolve():
                    raise SourcePublicationHold(
                        "Accepted story artifact belongs to a different canonical source root"
                    )
                binding = self._mapped_story_binding(binding, accepted_plan.plan.resolve(), head)
            elif binding.versioning == "code-repo":
                prefix = (
                    (Path(binding.source_root) / "stories").relative_to(self.project).as_posix()
                )
                listing = self._git(
                    self.project, "ls-tree", "-r", "--name-only", "-z", head, "--", prefix
                )
                matches = [
                    raw.decode("utf-8")
                    for raw in listing.split(b"\0")
                    if raw and Path(raw.decode("utf-8")).name.startswith(unit.selector + "-")
                ]
                if len(matches) != 1:
                    raise SourcePublicationHold("Merged story source is missing or ambiguous")
                binding = replace(
                    binding, path=str(self.project / matches[0]), code_relative=matches[0]
                )
            else:
                raise SourcePublicationHold(
                    "New ignored/external story requires its accepted artifact and canonical path mapping"
                )
        path = Path(binding.path)
        current = self._source_bytes(binding, head, binding.path)
        if binding.kind == "closure":
            return self._publish_closure(
                action_id,
                unit,
                binding,
                head=head,
                merged_commit=merged_commit,
                artifact=accepted_plan,
                store_remote=store_remote,
                store_branch=store_branch,
            )
        if binding.versioning == "code-repo":
            if source.kind == "tickets" and current is not None:
                native = str(
                    frontmatter.parse_frontmatter(current.decode("utf-8-sig")).get("ticket", "")
                )
                if not binding.native_ticket or native != binding.native_ticket:
                    raise SourcePublicationHold(
                        "Immutable canonical plan names another native ticket"
                    )
            if _status(current, source.kind, unit.selector) == "done":
                return SourcePublicationResult("confirmed", head, digest(current) or "")
            return self._tracking(
                binding,
                head,
                detail="Tracked source completion must land through a tracking draft PR",
            )
        # External/shared binding is re-resolved before any mutation. No stale
        # whole-board or whole-store restoration is performed.
        fresh = self.capture(source, unit)
        if source.kind == "stories" and not fresh.path:
            fresh = self._mapped_story_binding(fresh, Path(binding.path), head)
        if (fresh.source_root, fresh.path or binding.path, fresh.versioning, fresh.store_repo) != (
            binding.source_root,
            binding.path,
            binding.versioning,
            binding.store_repo,
        ):
            raise SourcePublicationHold("Canonical source topology or identity changed")
        record_path = (
            self.root / "actions" / (hashlib.sha256(action_id.encode()).hexdigest() + ".json")
        )
        with file_lock(
            self.root / "locks" / (hashlib.sha256(binding.path.encode()).hexdigest() + ".lock")
        ):
            record = _read(record_path) if record_path.exists() else None
            if record is None:
                if (
                    source.kind == "sprint-status"
                    and _status(current, source.kind, unit.selector) != binding.status
                ) or (
                    source.kind != "sprint-status"
                    and current
                    != (
                        base64.b64decode(binding.preimage) if binding.preimage is not None else None
                    )
                ):
                    raise SourcePublicationHold(
                        "Canonical source changed since planning; publication held"
                    )
                record = {
                    "schema_version": 1,
                    "action_id": action_id,
                    "state": "prepared",
                    "binding": binding.to_dict(),
                    "merged_commit": merged_commit,
                    "canonical_head": head,
                    "target_ref": target_ref,
                }
                _save(record_path, record)
            elif (
                record["binding"] != json.loads(json.dumps(binding.to_dict()))
                or record["merged_commit"] != merged_commit
            ):
                raise SourcePublicationHold("Source publication action identity reused")
            if record["state"] == "confirmed":
                if (
                    (_status(_bytes(path), source.kind, unit.selector) != "done")
                    if source.kind == "sprint-status"
                    else digest(_bytes(path)) != record["published_revision"]
                ):
                    raise SourcePublicationHold("Previously published source changed after receipt")
                if binding.versioning == "store-repo":
                    commit, remote_head = self._store_delivery(
                        action_id,
                        unit,
                        binding,
                        [path],
                        merged_commit=merged_commit,
                        record=record,
                        record_path=record_path,
                        remote=store_remote,
                        branch=store_branch,
                    )
                    if not remote_head:
                        return SourcePublicationResult(
                            "store_push_pending", head, record["published_revision"], commit
                        )
                    record.update(source_commit=commit, source_remote_head=remote_head)
                    _save(record_path, record)
                return SourcePublicationResult(
                    "confirmed",
                    head,
                    record["published_revision"],
                    record.get("source_commit", ""),
                    record.get("source_remote_head", ""),
                )
            expected_done = None
            if source.kind in ("tickets", "stories"):
                before = (
                    base64.b64decode(binding.preimage) if binding.preimage is not None else None
                )
                if accepted_plan is not None:
                    expected_done = accepted_plan.validate(self.project, unit.selector, path)
                elif before is not None and _status(before, source.kind, unit.selector) in (
                    "built",
                    "done",
                ):
                    expected_done = (
                        completed_plan_bytes(before) if source.kind == "tickets" else before
                    )
                if current != before and current != expected_done:
                    raise SourcePublicationHold(
                        "Canonical source edit differs from accepted publication artifact"
                    )
                if expected_done is None:
                    raise SourcePublicationHold(
                        "Accepted private plan artifact is required for canonical delivery"
                    )
                record["expected_published_revision"] = digest(expected_done)
                _save(record_path, record)
            if _status(current, source.kind, unit.selector) != "done":
                if source.kind == "tickets":
                    tree = _PinnedTree(
                        self.project,
                        folder=source_paths(self.project, source).initiative_folder,
                        expected=(
                            base64.b64decode(binding.preimage)
                            if binding.preimage is not None
                            else None
                        ),
                    )
                    if (
                        accepted_plan is None
                        and _status(current, source.kind, unit.selector) != "built"
                    ):
                        raise SourcePublicationHold(
                            "Private built source artifact and coordinator acceptance receipt required"
                        )
                    tree.mark(
                        unit.selector,
                        "done",
                        reason="campaign-publish" if accepted_plan else "accept",
                        expected_plan=path,
                        journal=journal,
                        publication=accepted_plan,
                    )
                elif source.kind == "sprint-status":
                    sprintstatus.advance(
                        path, unit.selector, "done", expected_current=(binding.status, "done")
                    )
                elif source.kind == "stories":
                    if accepted_plan is None or accepted_plan.source_kind != "stories":
                        raise SourcePublicationHold(
                            "Ignored/external story requires its exact merged source artifact"
                        )
                    desired = accepted_plan.validate(self.project, unit.selector, path)
                    with file_lock(runs.lock_path_for(path)):
                        if accepted_plan.check_preimage(_bytes(path), desired):
                            from .platform_util import atomic_write_bytes_confined

                            path.parent.mkdir(parents=True, exist_ok=True)
                            atomic_write_bytes_confined(
                                path, desired, confine_root=Path(binding.source_root)
                            )
                else:
                    raise SourcePublicationHold("Unsupported unversioned canonical source")
            data = _bytes(path)
            if expected_done is not None and data != expected_done:
                raise SourcePublicationHold(
                    "Canonical plan bytes differ from accepted publication output"
                )
            if _status(data, source.kind, unit.selector) != "done":
                raise SourcePublicationHold("Canonical source completion did not read back")
            record.update(state="marked", published_revision=digest(data))
            _save(record_path, record)
            if binding.versioning == "store-repo":
                commit, remote_head = self._store_delivery(
                    action_id,
                    unit,
                    binding,
                    [path],
                    merged_commit=merged_commit,
                    record=record,
                    record_path=record_path,
                    remote=store_remote,
                    branch=store_branch,
                )
                record.update(source_commit=commit, source_remote_head=remote_head)
                if not remote_head:
                    return SourcePublicationResult(
                        "store_push_pending",
                        head,
                        digest(data) or "",
                        commit,
                        detail="Versioned source publication remains remote debt",
                    )
            record["state"] = "confirmed"
            _save(record_path, record)
            return SourcePublicationResult(
                "confirmed",
                head,
                digest(data) or "",
                record.get("source_commit", ""),
                record.get("source_remote_head", ""),
            )
