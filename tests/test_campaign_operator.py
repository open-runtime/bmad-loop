"""Normal operator paths retain roles, diagnostics and exact source authority."""

import dataclasses
import json
from types import SimpleNamespace

import pytest

from bmad_loop.campaign_decisions import EvidenceLocation
from bmad_loop.campaign_history_sources import TranscriptEvidenceProvider
from bmad_loop.campaign_model import (
    CampaignError,
    CampaignSnapshot,
    CampaignSpec,
    CampaignUnit,
    UnitSpec,
    WorkerClaim,
)
from bmad_loop.campaign_operator import attach, diagnostics, sessions


def test_native_source_keeps_owner_input_distinct_from_injected_context(tmp_path):
    path = tmp_path / "native.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "session", "cwd": "/repo"}},
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Keep every required test."},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Injected context"}],
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    provider = TranscriptEvidenceProvider(roots={"codex": (tmp_path,)})
    location = EvidenceLocation(
        "owner", "transcript", f"codex:{path}#session:line-2", authority="owner"
    )
    result = provider(location)
    assert result.origin_verified and result.content == "Keep every required test."
    assert result.private and result.author_id == "native:codex:session:user"
    with pytest.raises(CampaignError, match="not an owner"):
        provider(dataclasses.replace(location, location=f"codex:{path}#session:line-3"))
    with pytest.raises(CampaignError, match="binding differs"):
        provider(dataclasses.replace(location, location=f"codex:{path}#foreign:line-2"))


def test_imported_json_cannot_claim_native_owner_authority(tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    imported = tmp_path / "imported.jsonl"
    imported.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "s",
                "uuid": "u",
                "message": {"role": "user", "content": "approve"},
            }
        )
        + "\n"
    )
    provider = TranscriptEvidenceProvider(roots={"claude": (native,)})
    with pytest.raises(CampaignError, match="authorized native store"):
        provider(
            EvidenceLocation("owner", "transcript", f"claude:{imported}#s:u", authority="owner")
        )


def test_claude_tool_result_is_not_owner_input(tmp_path):
    path = tmp_path / "native.jsonl"
    row = {
        "type": "user",
        "sessionId": "s",
        "uuid": "u",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "approve"}]},
    }
    path.write_text(json.dumps(row) + "\n")
    provider = TranscriptEvidenceProvider(roots={"claude": (tmp_path,)})
    with pytest.raises(CampaignError, match="tool content"):
        provider(EvidenceLocation("owner", "transcript", f"claude:{path}#s:u", authority="owner"))
    row["message"]["content"] = "Keep the hold."
    path.write_text(json.dumps(row) + "\n")
    assert (
        provider(
            EvidenceLocation("owner", "transcript", f"claude:{path}#s:u", authority="owner")
        ).content
        == "Keep the hold."
    )


def test_claude_synthetic_user_never_confers_owner_authority(tmp_path):
    path = tmp_path / "native.jsonl"
    row = {
        "type": "user",
        "sessionId": "s",
        "uuid": "u",
        "message": {"role": "user", "content": "Keep the hold."},
    }
    provider = TranscriptEvidenceProvider(roots={"claude": (tmp_path,)})
    location = EvidenceLocation("owner", "transcript", f"claude:{path}#s:u", authority="owner")
    path.write_text(json.dumps(row) + "\n")
    assert provider(location).origin_verified
    row["isSynthetic"] = True
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(CampaignError, match="not an owner message"):
        provider(location)


def test_diagnostics_expose_nested_failure_without_private_hooks():
    unit = CampaignUnit(UnitSpec("unit", "source", "epic", "Story"))
    unit.bindings["reflection"] = {
        "stage": {
            "state": "unknown",
            "detail": "missing registration",
            "windows": [
                {
                    "phase": "review",
                    "terminal_id": "t",
                    "task_id": "task",
                    "hook": {"cli_session_id": "native-id", "private_prompt": "do not leak"},
                }
            ],
        },
        "lifecycle": {"state": "unknown", "detail": "refused retirement", "private": "do not leak"},
    }
    result = diagnostics(unit)
    assert result["reflection"]["stage"]["detail"] == "missing registration"
    assert result["reflection"]["lifecycle"]["detail"] == "refused retirement"
    assert sessions(unit, role="review")[0]["cli_session_id"] == "native-id"
    assert "do not leak" not in json.dumps(result)


def test_attach_honors_requested_role_and_completed_history(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("bmad_loop.campaign_operator.subprocess.call", lambda argv: 0)
    run = tmp_path / "run"
    log = run / "logs" / "review.log"
    log.parent.mkdir(parents=True)
    log.write_text("completed review output")
    claim = WorkerClaim("unit", 1, 1, "launch", role="execution", run_id="run", quiescent=True)
    unit = CampaignUnit(UnitSpec("unit", "source", "epic", "Story"), claim=claim)
    unit.bindings["reflection"] = {
        "stage": {
            "windows": [
                {
                    "phase": "review",
                    "terminal_id": "terminal",
                    "task_id": "review",
                    "state": "stopped",
                    "run_dir": str(run),
                    "log_path": str(log),
                }
            ]
        }
    }
    snapshot = CampaignSnapshot(
        "campaign", "owner/repo", CampaignSpec(name="test"), units={"unit": unit}
    )
    runtime = SimpleNamespace(store=SimpleNamespace(load=lambda _: snapshot))
    assert attach(runtime, "campaign", "unit", "review") == 0
    assert "completed review output" in capsys.readouterr().out
    with pytest.raises(CampaignError, match="no live session or retained history"):
        attach(runtime, "campaign", "unit", "planning")
    unit.claim = dataclasses.replace(claim, quiescent=False)
    with pytest.raises(CampaignError, match="differs from the live worker role"):
        attach(runtime, "campaign", "unit", "publication_review")


def test_group_identity_separates_sources_and_initiatives():
    from bmad_loop.campaign_reflection_flow import group_identity

    left = UnitSpec("unit", "source", "epic", "Story", initiative="one")
    right = dataclasses.replace(left, initiative="two")
    assert group_identity(left, "epic") != group_identity(right, "epic")
    assert group_identity(left, "epic") == group_identity(
        dataclasses.replace(left, id="sibling"), "epic"
    )


def test_current_codex_provenance_distinguishes_owner_and_harness_input(tmp_path):
    path = tmp_path / "native.jsonl"
    meta = {"type": "session_meta", "payload": {"id": "session", "source": "cli"}}
    row = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "id": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Keep required acceptance."}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["user.text"],
                "content_item_metadata": [{"provenance": {"type": "user"}}],
            },
        },
    }
    provider = TranscriptEvidenceProvider(roots={"codex": (tmp_path,)})
    location = EvidenceLocation(
        "owner", "transcript", f"codex:{path}#session:message", authority="owner"
    )
    path.write_text(json.dumps(meta) + "\n" + json.dumps(row) + "\n")
    assert provider(location).content == "Keep required acceptance."
    row["payload"]["internal_chat_message_metadata_passthrough"]["content_item_metadata"][0][
        "provenance"
    ]["type"] = "harness"
    path.write_text(json.dumps(meta) + "\n" + json.dumps(row) + "\n")
    with pytest.raises(CampaignError, match="not an owner"):
        provider(location)


def test_parent_agent_task_cannot_be_owner_evidence(tmp_path):
    path = tmp_path / "native.jsonl"
    rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": "child",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}},
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Delegated task, not owner approval"},
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    provider = TranscriptEvidenceProvider(roots={"codex": (tmp_path,)})
    with pytest.raises(CampaignError, match="subagent"):
        provider(
            EvidenceLocation("owner", "transcript", f"codex:{path}#child:line-2", authority="owner")
        )
