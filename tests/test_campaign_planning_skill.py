"""Planning adapts explicit installed capabilities, never version-name guesses."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import install_bmad_config
from test_campaign_planning_flow import snapshot as snapshot

from bmad_loop import campaign_planning_skill as skill
from bmad_loop.campaign_model import CampaignError, PlanningSpec


@pytest.fixture(autouse=True)
def native_config(project):
    install_bmad_config(project)


def install(
    project,
    name,
    text="# Native workflow\nFollow its documented fast path.\n",
    tree=".agents/skills",
):
    path = project / tree / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "era,name,mode",
    [("v6", "bmad-bmm-create-prd", "YOLO"), ("v7", "bmad-prd", "unattended")],
)
def test_prompt_uses_only_explicit_native_capability_and_documented_mode(
    project, monkeypatch, era, name, mode
):
    install(project.project, name, f"# {name}\nUse {mode} mode.\n")
    monkeypatch.setattr(skill.bmadconfig, "load_paths", lambda _: SimpleNamespace(era=era))
    planning = PlanningSpec(skills=(("requirements", name),))
    prompt, native = skill.packet(
        project.project, planning, "requirements", '{"stage":"requirements"}'
    )
    assert native["era"] == era
    assert native["invocation"] == "/" + name
    assert native["path"] == f".agents/skills/{name}/SKILL.md"
    assert len(native["sha256"]) == 64
    assert "Never invent CLI flags" in prompt
    assert "Do not install" in prompt
    assert "decisions: []" in prompt
    assert '{"stage":"requirements"}' in prompt
    assert ("bmad-prd" if era == "v6" else "bmad-bmm-create-prd") not in prompt


@pytest.mark.parametrize("name", ["", "../bmad-prd", "bmad-prd --yolo"])
def test_unqualified_or_invented_mapping_refused(project, name):
    install(project.project, "bmad-prd")
    with pytest.raises(CampaignError, match="qualify an installed"):
        skill.resolve(
            project.project, PlanningSpec(skills=(("requirements", name),)), "requirements"
        )


def test_missing_decomposition_never_substitutes_product_owner_or_build_auto(project):
    install(project.project, "bmad-agent-po")
    install(project.project, "bmad-build-auto")
    with pytest.raises(CampaignError, match="decomposition: qualify an installed"):
        skill.resolve(project.project, PlanningSpec(), "decomposition")


def test_different_native_skill_copies_are_ambiguous(project):
    install(project.project, "native-prd", "# v6\n")
    install(project.project, "native-prd", "# v7\n", tree=".claude/skills")
    with pytest.raises(CampaignError, match="differs between CLI"):
        skill.resolve(
            project.project, PlanningSpec(skills=(("requirements", "native-prd"),)), "requirements"
        )


def test_redirected_native_skill_is_not_capability(project, tmp_path):
    other = tmp_path / "outside.md"
    other.write_text("# External skill\n")
    path = install(project.project, "native-prd")
    path.unlink()
    path.symlink_to(other)
    with pytest.raises((CampaignError, OSError)):
        skill.resolve(
            project.project, PlanningSpec(skills=(("requirements", "native-prd"),)), "requirements"
        )


def test_preflight_reports_missing_review_mapping_and_pins_other_capabilities(
    project, tmp_path, monkeypatch
):
    from test_campaign_preflight import inspect, spec

    mappings = tuple(
        (stage, "native-" + stage) for stage in ("requirements", "architecture", "decomposition")
    )
    for _, name in mappings:
        install(project.project, name)
    monkeypatch.setattr(skill.bmadconfig, "load_paths", lambda _: project)
    selected = replace(spec(tmp_path), planning=PlanningSpec(enabled=True, skills=mappings))
    result = inspect(project, tmp_path, monkeypatch, selected)
    planning = result["capabilities"]["planning"]
    assert not planning["ready"]
    assert any("planning_review: qualify an installed" in value for value in planning["problems"])
    assert (
        planning["observations"]["native_skills"]["requirements"]["name"] == "native-requirements"
    )


def test_blocking_decision_preserves_stage_for_authorized_retry(snapshot):
    from test_campaign_planning_flow import receipt

    from bmad_loop.campaign_planning_flow import apply_stage, next_stage, prepare_stage

    unit = snapshot.units["campaign:plan"]
    prepare_stage(snapshot, unit.id, "requirements", launch_id="decision-stage")
    result = receipt(snapshot, "requirements")
    question = {
        "id": "retention",
        "question": "Which retention period is authorized?",
        "evidence": ["docs/brief.md"],
        "blocking": True,
    }
    result["validated"]["result"]["decisions"] = [question]
    apply_stage(snapshot, unit.id, "requirements", result, launch_id="decision-stage")
    assert unit.hold.kind == "planning_decision"
    assert "requirements" not in unit.evidence["planning_flow"]["stages"]
    assert next_stage(snapshot, unit) is None
    unit.hold = None  # Represents an independently authorized decision release.
    assert next_stage(snapshot, unit).stage == "requirements"
    assert unit.evidence["planning_decisions"][0]["decisions"] == [question]


@pytest.mark.parametrize(
    "row", [{"id": "x"}, {"id": "x", "question": "?", "evidence": [], "blocking": "yes"}]
)
def test_decision_requires_typed_question_and_evidence(row):
    with pytest.raises(CampaignError, match="planning decision requires"):
        skill.decisions({"decisions": [row]})
