"""Campaign skill installation and module upgrades use the owning fork."""

import tomllib
from pathlib import Path


def test_campaign_skill_and_module_updates_resolve_to_fork():
    skills = Path(__file__).resolve().parents[1] / "skills"
    module = tomllib.loads((skills / "bmod-bmad-loop/bmod.toml").read_text(encoding="utf-8"))[
        "bmod"
    ]
    campaign = tomllib.loads(
        (skills / "bmad-loop-campaign-plan/bmod.toml").read_text(encoding="utf-8")
    )["skill"]
    assert "bmad-loop-campaign-plan" in module["skills"]
    assert campaign["source"] == module["update_source"] == "github:open-runtime/bmad-loop/skills"
