"""Identify the distribution actually loaded by this CLI process.

Version strings alone cannot distinguish fork commits. PEP 610 Git installs
provide the immutable commit; editable checkouts additionally require clean Git
state and an exact loaded-module path. Directory-built wheels do not prove their
build commit and remain explicitly unqualified.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from . import __version__, verify


def _url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme == "file":
        return urlunsplit(("file", parsed.hostname or "", parsed.path, "", ""))
    if parsed.hostname:
        host = parsed.hostname + (":" + str(parsed.port) if parsed.port else "")
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    return ""  # unfamiliar credential-bearing locators are not diagnostic text


def inspect(
    *, expected_commit: str = "", distribution: Any = None, module_file: Path | None = None
) -> dict[str, Any]:
    if expected_commit and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", expected_commit):
        raise ValueError("expected commit must be a complete Git object ID")
    module = (module_file or Path(__file__)).resolve()
    problems = []
    if distribution is None:
        try:
            distribution = importlib.metadata.distribution("bmad-loop")
        except importlib.metadata.PackageNotFoundError:
            problems.append("loaded module has no installed distribution metadata")
    direct: dict[str, Any] = {}
    if distribution is not None:
        try:
            raw = distribution.read_text("direct_url.json")
            direct = json.loads(raw) if raw else {}
            if not isinstance(direct, dict):
                raise ValueError("direct URL metadata is not an object")
        except (OSError, ValueError) as exc:
            problems.append("installation metadata unavailable: " + type(exc).__name__)
            direct = {}
    url = direct.get("url", "")
    if not isinstance(url, str):
        url = ""
        problems.append("installation URL is invalid")
    vcs = direct.get("vcs_info", {})
    directory = direct.get("dir_info", {})
    kind = "wheel"
    commit = ""
    dirty = None
    qualified = False
    if isinstance(vcs, dict) and vcs.get("vcs") == "git":
        kind = "git"
        claimed = vcs.get("commit_id", "")
        if distribution is None:
            raise ValueError("Git provenance has no owning distribution")
        installed_module = Path(
            distribution.locate_file("bmad_loop/campaign_provenance.py")
        ).resolve()
        if not isinstance(claimed, str) or not re.fullmatch(
            r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", claimed
        ):
            problems.append("Git installation metadata has no complete pinned commit")
        elif installed_module != module:
            problems.append("loaded module differs from the selected installed distribution")
        else:
            commit, qualified = claimed.lower(), True
    elif isinstance(directory, dict) and directory.get("editable") is True:
        kind = "editable"
        parsed = urlsplit(url)
        root = Path(unquote(parsed.path)).resolve() if parsed.scheme == "file" else None
        if root is None or not module.is_relative_to(root / "src" / "bmad_loop"):
            problems.append("editable installation does not own the loaded module")
        else:
            try:
                commit = verify.rev_parse_head(root)
                dirty = not verify.worktree_clean(root)
                qualified = not dirty
                if dirty:
                    problems.append(
                        "editable checkout contains changes outside its recorded commit"
                    )
            except verify.GitError as exc:
                problems.append("editable Git identity unavailable: " + type(exc).__name__)
    else:
        if url:
            kind = "directory-build" if "dir_info" in direct else "archive"
        problems.append("this installation does not record an immutable Git build commit")
    matches = qualified and bool(expected_commit) and commit == expected_commit.lower()
    if expected_commit and not matches:
        problems.append("loaded installation does not prove the expected commit")
    return {
        "schema_version": 1,
        "version": __version__,
        "executable": sys.executable,
        "module": str(module),
        "installation_kind": kind,
        "source_url": _url(url),
        "commit": commit,
        "dirty": dirty,
        "qualified": qualified,
        "expected_commit": expected_commit.lower(),
        "matches_expected": matches,
        "problems": problems,
    }
