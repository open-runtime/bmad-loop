"""Navigation prefixes preserve facts without granting identity or ownership."""

import dataclasses

import pytest

from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    ParadevSpec,
    UnitSpec,
    WorkerClaim,
)
from bmad_loop.campaign_titles import (
    group_navigation,
    navigation_tag,
    navigation_title,
    unit_navigation,
)


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


@pytest.mark.parametrize("discarded", ["\ufffd", "\u2800", "\u28ff"])
def test_navigation_matches_native_terminal_normalization_without_losing_unicode(discarded):
    title = navigation_title(
        names=("tina",),
        epics=("École" + discarded + "中文🚀",),
        issues=("#529",),
        prs=("#2286",),
        description="début" + discarded + "fin🛰",
    )
    assert title == "@tina · E:École中文🚀 · I:#529 · PR:#2286 · débutfin🛰"
    # Characterization of the installed host's terminal.rename normalization.
    observed = "".join(
        char
        for char in title
        if not (
            ord(char) <= 31
            or 127 <= ord(char) <= 159
            or ord(char) == 65533
            or 10240 <= ord(char) <= 10495
        )
    ).strip()[:200]
    assert observed == title


def test_native_discarded_only_field_retains_explicit_pending_navigation():
    assert navigation_title(epics=("\ufffd\u2800\u28ff",)) == (
        "hcom:pending · E:pending · I:pending · PR:pending"
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


@pytest.fixture
def pending_issue_navigation():
    work = unit()
    work.claim = None
    work.bindings.pop("issue")
    work.bindings.pop("pr")
    work.bindings["hierarchy_ready"] = False
    shared = {
        "kind": "issue",
        "number": 529,
        "database_id": 6090790724,
        "url": "https://github.com/owner/repo/issues/529",
    }
    snapshot = CampaignSnapshot(
        "campaign",
        "owner/repo",
        CampaignSpec("test"),
        units={work.id: work},
        bindings={"github_hierarchy": {"story:" + work.id: shared}},
    )
    return work, snapshot, shared


def test_navigation_uses_exact_shared_story_before_hierarchy_is_ready(pending_issue_navigation):
    work, snapshot, _ = pending_issue_navigation
    expected = {"names": (), "epics": ("epic",), "issues": ("#529",), "prs": ()}
    assert unit_navigation(work, snapshot=snapshot) == expected
    assert group_navigation(snapshot, work) == expected
    assert unit_navigation(work)["issues"] == ()  # Prior API has no shared context.
    assert work.bindings["hierarchy_ready"] is False and "issue" not in work.bindings


def test_shared_story_url_match_is_case_insensitive(pending_issue_navigation):
    work, snapshot, shared = pending_issue_navigation
    snapshot.repository = "Owner/Repo"
    shared["url"] = "https://github.com/owner/repo/issues/529"
    assert unit_navigation(work, snapshot=snapshot)["issues"] == ("#529",)


@pytest.mark.parametrize(
    "fault", ["other-unit", "foreign-repository", "no-native-id", "pr", "bool-number"]
)
def test_shared_navigation_never_guesses_unconfirmed_or_foreign_identity(
    pending_issue_navigation, fault
):
    work, snapshot, shared = pending_issue_navigation
    if fault == "other-unit":
        snapshot.bindings["github_hierarchy"] = {"story:another": shared}
    elif fault == "foreign-repository":
        shared["url"] = "https://github.com/foreign/repo/issues/529"
    elif fault == "no-native-id":
        shared.pop("database_id")
    elif fault == "pr":
        shared["kind"] = "pr"
    else:
        shared["number"] = True
    assert unit_navigation(work, snapshot=snapshot)["issues"] == ()


@pytest.mark.parametrize(
    "local", [{"issue": {"number": 42}}, {"issue_url": "https://github.com/owner/repo/issues/43"}]
)
def test_unit_issue_identity_keeps_precedence_over_shared_navigation(
    pending_issue_navigation, local
):
    work, snapshot, _ = pending_issue_navigation
    work.bindings.update(local)
    expected = ("#42",) if "issue" in local else ("#43",)
    assert unit_navigation(work, snapshot=snapshot)["issues"] == expected


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


def test_compact_tag_keeps_short_known_coordinates_and_explicit_pending():
    assert (
        navigation_tag(names=("tina",), epics=("303",), issues=("#529",), prs=("#2286",))
        == "@tina · e:303 · i:#529 · pr:#2286"
    )
    assert navigation_tag(epics=("intake",)) == "hcom:? · e:intake · i:? · pr:?"


@pytest.mark.parametrize("epic", ["İ" * 60, "🚀" * 60, "İ🚀" * 30])
def test_compact_tag_counts_lowercase_expansion_and_utf16_before_allocation(epic):
    result = navigation_tag(
        names=("longactor" * 3,), epics=(epic,), issues=("#123456789",), prs=("#987654321",)
    )
    assert len(result.encode("utf-16-le")) // 2 <= 35
    assert result == result.lower()
    assert result.startswith("@")
    assert all(label in result for label in (" · e:", " · i:", " · pr:"))
    assert "…" in result
