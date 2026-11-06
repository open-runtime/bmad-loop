"""Resolve explicitly mapped installed BMAD workflows for unattended planning.

Names are project capabilities, never inferred from the BMAD version. The
packaged wrapper adapts the native workflow inside a coding session; Python
only reads bounded documents and supplies the exact selected capability.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from . import bmadconfig
from .campaign_closure import _read
from .campaign_model import CampaignError, PlanningSpec
from .install import skills_root

STAGES = frozenset({"requirements", "architecture", "decomposition", "planning_review"})
WRAPPER = "bmad-loop-campaign-plan"
MAX_SKILL_BYTES = 256 * 1024


def resolve(project: Path, planning: PlanningSpec, stage: str) -> dict[str, str]:
    """Pin one explicitly authorized native skill without assuming v6/v7 names."""
    name = dict(planning.skills).get(stage, "")
    if stage not in STAGES or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise CampaignError(f"{stage}: qualify an installed BMAD skill mapping")
    candidates = []
    for tree in (".agents/skills", ".claude/skills"):
        path = project / tree / name / "SKILL.md"
        if not path.exists() and not path.is_symlink():
            continue
        data = _read(project, path)
        if not data.strip() or len(data) > MAX_SKILL_BYTES:
            raise CampaignError(f"{stage}: installed BMAD skill is empty or unbounded")
        candidates.append((path, data))
    if not candidates:
        raise CampaignError(f"{stage}: qualify an installed BMAD skill mapping ({name})")
    if len({data for _, data in candidates}) != 1:
        raise CampaignError(f"{stage}: mapped BMAD skill differs between CLI skill trees")
    path, data = candidates[0]
    try:
        era = bmadconfig.load_paths(project).era
    except bmadconfig.BmadConfigError as exc:
        raise CampaignError(
            f"{stage}: cannot qualify the installed BMAD configuration: {exc}"
        ) from exc
    return {
        "name": name,
        "path": path.relative_to(project).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "era": era,
        "invocation": "/" + name,
    }


def packet(
    project: Path, planning: PlanningSpec, stage: str, prompt: str
) -> tuple[str, dict[str, Any]]:
    """Embed the bundled wrapper and exact native capability into the stage packet."""
    native = resolve(project, planning, stage)
    wrapper = skills_root().joinpath(WRAPPER, "SKILL.md").read_text(encoding="utf-8")
    return (
        wrapper
        + "\n\nQualified native workflow (read its exact SKILL.md before invoking):\n"
        + "\n".join(f"{key}: {value}" for key, value in sorted(native.items()))
        + "\n\nStage packet:\n"
        + prompt,
        native,
    )


def decisions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate unresolved questions; their existence never authorizes an answer."""
    values = raw.get("decisions", [])
    if not isinstance(values, list) or len(values) > 100:
        raise CampaignError("planning decisions must be a bounded list")
    seen = set()
    for row in values:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not row["id"].strip()
            or len(row["id"]) > 200
            or row["id"] in seen
            or not isinstance(row.get("question"), str)
            or not row["question"].strip()
            or len(row["question"]) > 16_384
            or type(row.get("blocking")) is not bool
            or not isinstance(row.get("evidence"), list)
            or len(row["evidence"]) > 100
            or any(not isinstance(value, str) or len(value) > 4096 for value in row["evidence"])
        ):
            raise CampaignError(
                "planning decision requires unique ID, question, evidence and blocking flag"
            )
        seen.add(row["id"])
    return values
