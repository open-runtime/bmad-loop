"""Navigation prefixes preserve facts without granting identity or ownership."""

import dataclasses

from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    ParadevSpec,
    UnitSpec,
    WorkerClaim,
)
from bmad_loop.campaign_titles import group_navigation, navigation_title, unit_navigation


def unit(name="story", *, initiative="first", actor="tina"):
    work = CampaignUnit(
        UnitSpec(name, "source", "epic", "Story", initiative=initiative),
        claim=WorkerClaim(name, 1, 1, "launch-" + name),
        bindings={"native": {"workspace_id": name}, "issue": {"number": 42}, "pr": {"number": 18}},
    )
    work.bindings["reflection"] = {
        "stage": {
            "launch_id": work.claim.launch_id,
            "windows": [{"state": "active", "hook": {"hcom_name": actor}}],
        }
    }
    return work


def test_navigation_prefix_reserves_all_fields_ahead_of_long_description():
    title = navigation_title(
        names=("tina",),
        epics=("epic-" + "e" * 200,),
        issues=("#123456",),
        prs=("#18",),
        description="very long story " * 100,
    )
    assert title.startswith("@tina · E:epic-")
    assert "… · I:#123456 · PR:#18 · very long story" in title
    assert len(title) <= 180


def test_unknown_fields_are_explicit_and_control_characters_cannot_hide_navigation():
    assert navigation_title(description="hello\n\x1bworld") == (
        "hcom:pending · E:pending · I:pending · PR:pending · hello world"
    )


def test_stale_actor_never_labels_successor_launch():
    work = unit()
    assert unit_navigation(work)["names"] == ("tina",)
    work.claim = dataclasses.replace(work.claim, launch_id="successor")
    assert unit_navigation(work)["names"] == ()
    current = {
        "launch_id": "successor",
        "windows": [{"state": "active", "hook": {"hcom_name": "nava"}}],
    }
    assert unit_navigation(work, current)["names"] == ("nava",)


def test_group_aggregates_actual_members_without_merging_other_initiatives():
    first, second, foreign = (
        unit(),
        unit("two", actor="nava"),
        unit("foreign", initiative="elsewhere", actor="luna"),
    )
    second.bindings["issue"] = {"number": 43}
    second.bindings["pr"] = {"number": 19}
    snapshot = CampaignSnapshot(
        "campaign",
        "owner/repo",
        CampaignSpec("test", paradev=ParadevSpec(group_by="epic")),
        units={work.id: work for work in (first, second, foreign)},
    )
    nav = group_navigation(snapshot, first)
    assert nav == {
        "names": ("nava", "tina"),
        "epics": ("epic",),
        "issues": ("#42", "#43"),
        "prs": ("#18", "#19"),
    }
    assert navigation_title(**nav) == "@nava/@tina · E:epic · I:#42/#43 · PR:#18/#19"


def test_managed_bridge_precedes_private_native_actor_and_is_generation_bound():
    work = unit()
    work.bindings["managed_communication"] = {
        "kind": "bridge",
        "name": "nava",
        "target": {
            "unit_id": work.id,
            "launch_id": work.claim.launch_id,
            "generation": work.claim.generation,
        },
    }
    assert navigation_title(**unit_navigation(work)) == "@nava · E:epic · I:#42 · PR:#18"
    work.claim = dataclasses.replace(work.claim, generation=2, launch_id="replacement")
    assert unit_navigation(work)["names"] == ()


def test_aggregate_overflow_reports_omitted_members_in_each_field():
    title = navigation_title(
        names=("nava", "tina", "luna"),
        epics=("epic",),
        issues=("#1", "#2", "#3"),
        prs=("#4", "#5", "#6"),
    )
    assert title == "@luna/@nava +1 · E:epic · I:#1/#2 +1 · PR:#4/#5 +1"
