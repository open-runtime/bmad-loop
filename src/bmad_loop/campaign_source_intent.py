"""Keep fresh Git input authority separate from canonical private/dirty intent.

The tracked main candidate may be newer than the owner's clean checkout. Local
overlays still belong to that owner. Shared containers are compared by selected
product entry/section, so an unrelated story edit cannot hold this unit.
"""

import dataclasses
import hashlib
import json
import re
import tomllib
from pathlib import Path

from . import verify
from .campaign_model import CampaignError
from .campaign_product_input import MAX_BYTES, _document, collect

Input = tuple[str, str, str]


def parts(locator: str) -> tuple[Path, str]:
    for marker in ("#entry:", "#Story-"):
        if marker in locator:
            path, value = locator.rsplit(marker, 1)
            return Path(path), marker + value
    return Path(locator), ""


def projection(data: bytes | None, marker: str) -> str | None:
    if data is None:
        return None
    if len(data) > MAX_BYTES:
        raise CampaignError("canonical product input exceeds its byte bound")
    text = data.decode("utf-8-sig")
    if marker.startswith("#entry:"):
        rows = tomllib.loads(text).get("entry", [])
        selected = [row for row in rows if str(row.get("id")) == marker[7:]]
        if len(selected) != 1:
            return None
        return json.dumps(
            {
                key: selected[0][key]
                for key in ("id", "type", "title", "description", "verify", "references", "notes")
                if key in selected[0]
            },
            ensure_ascii=False,
            indent=2,
        )
    if marker.startswith("#Story-"):
        pattern = re.compile(
            r"^(#{1,6})\s+Story\s+" + re.escape(marker[7:]) + r"\s*:\s+.+$",
            re.MULTILINE | re.IGNORECASE,
        )
        headings = list(pattern.finditer(text))
        if len(headings) != 1:
            return None
        start = headings[0]
        end = re.search(
            r"^#{1," + str(len(start.group(1))) + r"}\s+", text[start.end() :], re.MULTILINE
        )
        return text[start.start() : start.end() + end.start() if end else len(text)]
    return text


def states(project: Path, locator: str) -> tuple[str | None, str | None, str | None]:
    """Read selected HEAD, index and working input without updating any ref."""
    path, marker = parts(locator)
    if not path.is_relative_to(project) or ".." in path.parts:
        raise CampaignError("tracked product input escaped its canonical repository")
    relative = path.relative_to(project).as_posix()
    head = verify.file_bytes_at_revision(project, "HEAD", relative)
    indexed = verify.git_bytes(project, "ls-files", "--stage", "-z", "--", ":(literal)" + relative)
    if indexed.returncode:
        raise CampaignError("canonical product index observation failed")
    index = None
    if indexed.stdout:
        entries = indexed.stdout.split(b"\0")
        if len([row for row in entries if row]) != 1 or b" 0\t" not in indexed.stdout:
            raise CampaignError("canonical product input has unresolved index stages")
        shown = verify.git_bytes(project, "show", ":0:" + relative)
        if shown.returncode:
            raise CampaignError("canonical product index bytes are unavailable")
        index = shown.stdout
    work = _document(project, path) if path.exists() or path.is_symlink() else None
    return projection(head, marker), projection(index, marker), projection(work, marker)


def overlay(project: Path, locator: str) -> bool:
    head, index, work = states(project, locator)
    return head != index or head != work


def capture(project: Path, source, unit) -> tuple[Input, ...]:
    rows = []
    for product in collect(project, source, unit, canonical=True):
        locator, text = product["path"], product["text"]
        path, _ = parts(locator)
        authority = "canonical-private"
        if path.is_relative_to(project):
            head, index, work = states(project, locator)
            if work != text:
                raise CampaignError("canonical product input changed during capture")
            if head is not None or index is not None:
                authority = "tracked-clean" if head == index == work else "tracked-overlay"
        rows.append((locator, hashlib.sha256(text.encode("utf-8")).hexdigest(), authority))
    return tuple(rows)


def private_inputs(rows) -> tuple[Input, ...]:
    return tuple(row for row in rows if row[2] != "tracked-clean")


def rebind(
    binding,
    observed,
    canonical,
    *,
    project: Path,
    workspace: Path,
    copied_root: Path | None = None,
    origin_root: Path | None = None,
    revision: str = "",
):
    """Bind private product intent without relocating publication authority.

    Ordinary views retain tracked-main authority from their checkout. Amendment
    generations instead copy a mixture of immutable main blobs and private owner
    files into an untracked directory; classify each selected input against the
    exact copied revision, never against that directory's Git tracking status.
    """
    if observed.product_inputs is None:
        return binding
    watched = []
    private = []
    for row in observed.product_inputs:
        path, fragment = parts(row[0])
        tracked = row[2] == "tracked-clean"
        if copied_root is not None:
            if origin_root is None or not revision or not path.is_relative_to(copied_root):
                raise CampaignError("selected amendment input is outside its copied source")
            origin = origin_root / path.relative_to(copied_root)
            if origin.is_relative_to(project):
                data = verify.file_bytes_at_revision(
                    project, revision, origin.relative_to(project).as_posix()
                )
                text = projection(data, fragment)
                tracked = text is not None
                if text is not None and hashlib.sha256(text.encode("utf-8")).hexdigest() != row[1]:
                    raise CampaignError(
                        "selected amendment input differs from its canonical revision"
                    )
        else:
            origin = project / path.relative_to(workspace) if tracked else path
        if tracked:
            locator = str(origin) + fragment
            if overlay(project, locator):
                raise CampaignError("canonical selected product has uncommitted owner changes")
            watched.append((locator, row[1], "tracked-clean"))
        else:
            private.append(row)
    if any(row[2] == "tracked-overlay" for row in canonical.product_inputs or ()) or tuple(
        row[1] for row in private
    ) != tuple(row[1] for row in private_inputs(canonical.product_inputs or ())):
        raise CampaignError(
            "canonical product input changed while preparing its private source view"
        )
    return dataclasses.replace(
        binding,
        product_inputs=tuple(dict.fromkeys([*(canonical.product_inputs or ()), *watched])),
        product_project=canonical.product_project,
    )
