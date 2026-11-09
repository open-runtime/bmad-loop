"""Compare selected native source/config blobs with freshly observed main.

Only an isolated observation ref is fetched. The caller checkout, index, branch,
source stores and active workers are never updated here. Unrelated code changes
do not prevent planning; changed/deleted/unmerged native source metadata does.
Ignored/external stores retain their separate source-CAS/publication contract.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import bmadconfig, stories, verify
from .campaign_model import CampaignError, CampaignSpec


@dataclass(frozen=True)
class SourceFreshness:
    ready: bool
    head: str
    main_head: str
    observation_ref: str
    tracked_roots: tuple[str, ...]
    changed: tuple[str, ...]
    private_roots: tuple[str, ...]
    observed_at: float
    reason: str = ""


def _git(project: Path, *args: str) -> bytes:
    result = verify.git_bytes(project, *args)
    if result.returncode:
        raise CampaignError("native source freshness observation failed: git " + args[0])
    return result.stdout


def inspect(
    project: Path,
    spec: CampaignSpec,
    *,
    action_id: str = "",
    clock: Callable[[], float] = time.time,
) -> SourceFreshness:
    """Fail closed on source/config drift without requiring unrelated code parity."""
    project = project.resolve()
    if not spec.sources:
        return SourceFreshness(True, "", "", "", (), (), (), clock())
    if (
        not spec.remote
        or spec.remote.startswith("-")
        or not spec.target
        or spec.target.startswith("-")
    ):
        raise CampaignError("native source freshness requires explicit remote and target")
    head = _git(project, "rev-parse", "--verify", "HEAD").decode().strip()
    token = hashlib.sha256((action_id or uuid.uuid4().hex).encode()).hexdigest()
    observation_ref = "refs/bmad-campaign/source-freshness/" + token
    _git(
        project,
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        "--refmap=",
        "--",
        spec.remote,
        f"refs/heads/{spec.target}:{observation_ref}",
    )
    current = _git(project, "rev-parse", "--verify", observation_ref).decode().strip()
    candidates: set[Path] = {
        project / relative
        for relative in (
            *bmadconfig.CENTRAL_LAYERS_REL,
            bmadconfig.LEGACY_CONFIG_REL,
            bmadconfig.V7_MARKER_REL,
            bmadconfig.V7_CONFIG_UTILS_REL,
        )
    }
    private: set[str] = set()
    for source in spec.sources:
        paths = bmadconfig.load_paths(project, want_tickets=source.kind == "tickets")
        if source.kind == "tickets":
            if source.initiative:
                paths = paths.with_initiative(source.initiative)
            if paths.initiative_folder is None:
                raise CampaignError("ticket source freshness requires a selected initiative")
            candidates.add(paths.initiative_folder)
        elif source.kind == "stories":
            candidates.add(stories.resolve_spec_folder(project, source.path))
        elif source.kind == "sprint-status":
            candidates.add(paths.sprint_status)
            candidates.add(paths.implementation_artifacts)
        if source.scheduler_config:
            selected = Path(source.scheduler_config).expanduser()
            candidates.add(selected if selected.is_absolute() else project / selected)
    roots: set[str] = set()
    for path in candidates:
        if (
            ".." in path.parts
            or path.is_symlink()
            or any(
                parent.is_symlink()
                for parent in path.parents
                if parent != project and parent.is_relative_to(project)
            )
        ):
            raise CampaignError(
                "native source freshness refuses redirected or parent-relative input"
            )
        if not path.is_relative_to(project):
            private.add(str(path))
            continue
        relative = path.relative_to(project).as_posix()
        if relative == ".":
            # Root stories mode has a defined native footprint; do not compare
            # every implementation file in the repository as planning metadata.
            roots.update({"SPEC.md", "stories.yaml", "stories"})
        else:
            roots.add(relative)
    selected_roots = tuple(sorted(roots))
    literal_roots = tuple(":(literal)" + value for value in selected_roots)
    changes: set[str] = set()
    if selected_roots:
        for args in (
            ("diff", "--name-only", "-z", head, current, "--", *literal_roots),
            ("diff", "--name-only", "-z", current, "--", *literal_roots),
            ("ls-files", "--others", "--exclude-standard", "-z", "--", *literal_roots),
        ):
            changes.update(
                value.decode("utf-8") for value in _git(project, *args).split(b"\0") if value
            )
        for relative in selected_roots:
            path = project / relative
            if path.exists() and not _git(project, "ls-files", "-z", "--", ":(literal)" + relative):
                ignored = (
                    verify.git_bytes(project, "check-ignore", "-q", "--", relative).returncode == 0
                )
                if ignored:
                    private.add(str(path))
    # A parallel owner must not swap the checkout while the two comparisons run.
    if _git(project, "rev-parse", "--verify", "HEAD").decode().strip() != head:
        raise CampaignError("caller branch moved during native source freshness observation")
    changed = tuple(sorted(changes))
    reason = (
        (
            "Native source/configuration differs from fresh "
            + spec.remote
            + "/"
            + spec.target
            + "; update this project base or deliver the planning PR before campaign admission: "
            + ", ".join(changed[:20])
        )
        if changed
        else ""
    )
    return SourceFreshness(
        not changed,
        head,
        current,
        observation_ref,
        selected_roots,
        changed,
        tuple(sorted(private)),
        clock(),
        reason,
    )


def require(project: Path, spec: CampaignSpec, *, action_id: str = "") -> SourceFreshness:
    observed = inspect(project, spec, action_id=action_id)
    if not observed.ready:
        raise CampaignError(observed.reason)
    return observed


def document(observed: SourceFreshness) -> dict:
    return asdict(observed)
