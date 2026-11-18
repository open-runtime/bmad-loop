"""The accepted campaign plan opens a real decision before the first coding session."""

import dataclasses
import hashlib
import json

import pytest
from conftest import git
from test_campaign_resolution_flow import assert_next_development_prompt, candidate
from test_campaign_runtime_decisions import Discussions

from bmad_loop import campaign_runtime, cli
from bmad_loop.campaign_github import GithubGateway
from bmad_loop.campaign_model import (
    CampaignSnapshot,
    CampaignUnit,
    OutboxAction,
    UnitState,
    spec_from_dict,
    spec_to_dict,
)
from bmad_loop.campaign_planning import StageCoordinates
from bmad_loop.campaign_resolution_flow import proposals
from bmad_loop.campaign_scheduler import admit
from bmad_loop.campaign_store import CampaignStore, add_action


@pytest.mark.parametrize("reply_kind", ["model", "full", "ordinal", "ordinal_comment", "ambiguous"])
def test_planned_choice_reaches_next_development_session(
    project, tmp_path, monkeypatch, capsys, reply_kind
):
    repo = project.project
    unit_id = "s:project:cache"
    policy = repo / "delegate.md"
    policy.write_text(
        "Choose a local SQLite implementation within the cache story's written scope."
    )
    git(repo, "add", policy.name)
    git(repo, "commit", "-qm", "freeze cache delegation")
    pin = hashlib.sha256(policy.read_bytes()).hexdigest() + ":" + policy.name
    question = "Which supported journal mode should the cache use?"
    spec = spec_from_dict(
        {
            "schema_version": 1,
            "name": "cache",
            "sources": [{"id": "s", "kind": "stories", "path": "specs"}],
            "units": [
                {
                    "id": unit_id,
                    "source": "s",
                    "epic": "project",
                    "title": "Cache",
                    "scopes": ["src/cache"],
                    "acceptance": ["Persist cache locally."],
                }
            ],
            "decisions": {
                "implementation_choices": [
                    {"unit": unit_id, "question": question, "delegation_document": pin}
                ]
            },
        }
    )
    assert spec_from_dict(spec_to_dict(spec)) == spec
    head = git(repo, "rev-parse", "HEAD").strip()
    unit = CampaignUnit(
        spec.units[0],
        UnitState.DRAFT,
        bindings={
            "workspace": str(repo),
            "head": head,
            "base": head,
            "branch": git(repo, "branch", "--show-current").strip(),
            "hierarchy_ready": True,
            "issue": {"number": 7},
            "pr": {"number": 8},
        },
    )
    store = CampaignStore(tmp_path / "state")
    snapshot = store.create(
        CampaignSnapshot(
            "choices", "org/repo", spec, {unit.id: unit}, common_dir=str(repo / ".git")
        )
    )
    unit = snapshot.units[unit_id]
    service = Discussions()
    service.items[8] = {
        **service.item,
        "id": 108,
        "number": 8,
        "node_id": "pull-eight",
        "html_url": "https://github.com/org/repo/pull/8",
    }
    runtime = campaign_runtime.CampaignRuntime(
        repo, store, gateway=GithubGateway(service, "org/repo")
    )
    monkeypatch.setattr(runtime, "_role", lambda snapshot, role, **kwargs: candidate().get(role))
    monkeypatch.setattr(runtime, "_launcher_readiness", lambda *args, **kwargs: {"ready": True})
    snapshot.bindings["inventory"] = {"complete": True, "observed_at": runtime.clock()}
    ready = next(item for item in runtime.proposals(snapshot) if item.kind == "ready")
    action = OutboxAction("ready", ready.kind, ready.unit, ready.payload, snapshot.generation)
    runtime.apply(snapshot, action, runtime.perform(snapshot, action))
    assert unit.hold.kind == "product_choice" and unit.hold.reason == question
    hold_id = unit.hold.id
    owner_text = {
        "full": "Use SQLite with WAL; keep cache storage local.",
        "ordinal": "Option 2",
        "ordinal_comment": "Option 2",
        "ambiguous": "Option 1 or option 2",
    }.get(reply_kind, "")

    def scripted_review(disposition):
        nonlocal snapshot, unit
        snapshot = store.save(snapshot, snapshot.revision)
        unit = snapshot.units[unit_id]
        observe = proposals(snapshot, repo, candidate(), now=runtime.clock())[0]
        action = OutboxAction(
            "observe", observe.kind, unit_id, observe.payload, snapshot.generation
        )
        runtime.apply(snapshot, action, runtime.perform(snapshot, action))
        launch = proposals(snapshot, repo, candidate(), now=runtime.clock())[0]
        assert launch.kind == "launch" and launch.payload["role"] == "decision_review"
        packet = launch.payload["context_packet"]
        assert packet["question"] == question
        assert [s["identity"] for s in packet["sources"] if s["authority"] == "delegation"] == [
            str(policy)
        ]
        stage = add_action(snapshot, launch.kind, launch.unit, launch.payload)
        runtime.control(snapshot, {"kind": "prepare-effect", "action": dataclasses.asdict(stage)})
        assert unit.claim is not None, (stage.state, stage.error, stage.receipt, unit.hold)
        assert unit.state == UnitState.READY
        stage.state = "confirmed"
        unit.claim.quiescent = True  # Scripted native stage ended.
        artifact = {
            "schema_version": 1,
            "coordinates": packet["coordinates"],
            "hold_id": hold_id,
            "disposition": disposition,
            "action": "choose",
            "answer": "Use SQLite with WAL.",
            "constraints": ["Keep the cache local."],
            "support": [
                {"source_id": row["id"], "revision": row["revision"], "quote": row["content"]}
                for row in packet["sources"]
                if row["content"]
            ],
        }
        if disposition == "needs_new_owner_decision":
            artifact.update(
                answer="Ask for storage preference",
                options=[
                    {
                        "answer": "Ask for storage preference",
                        "reason": "The source does not settle the tradeoff.",
                        "source_ids": ["hold:" + hold_id],
                    },
                    {
                        "answer": "Use DELETE journal mode",
                        "reason": "Use a single rollback journal.",
                        "source_ids": ["hold:" + hold_id],
                    },
                ],
            )
        receipt = {
            "outcome": "stage_complete",
            "head": head,
            "stage": "decision_review",
            "artifact": artifact,
            "observed_model": "gpt-scripted",
            "observed_model_source": "transcript_primary",
        }
        coords = StageCoordinates(
            snapshot.id,
            unit.id,
            "decision_review",
            snapshot.graph_revision,
            snapshot.config_revision,
            head,
            head,
            "",
        )
        runtime._validate_stage_receipt(
            snapshot,
            unit,
            {"coordinates": dataclasses.asdict(coords), "result_contract": "decision-v1"},
            receipt,
        )
        runtime._apply_stage_result(snapshot, unit, receipt)

    if owner_text:
        scripted_review("needs_new_owner_decision")
        decision = unit.evidence["decision_advice"]["receipt"]
        comment = service.add(400, body="Decision " + decision + "\n" + owner_text)
        snapshot = store.save(snapshot, snapshot.revision)
        if reply_kind == "ordinal_comment":
            from bmad_loop.campaign_intake import GithubIntake, IntakeBatch
            from bmad_loop.campaign_intake_flow import apply_batch

            native = GithubIntake(runtime.gateway(snapshot))._observation("comment", comment, 7)
            apply_batch(snapshot, IntakeBatch((native,), "", True, 0), observed_at=runtime.clock())
        else:
            monkeypatch.setattr(campaign_runtime, "store_for", lambda _: store)
            assert (
                cli.main(
                    [
                        "campaign",
                        "answer",
                        snapshot.id,
                        "--unit",
                        unit_id,
                        "--decision-id",
                        decision,
                        "--number",
                        "7",
                        "--comment-id",
                        "400",
                        "--json",
                    ]
                )
                == 0
            )
            runtime.control(snapshot, json.loads(capsys.readouterr().out)["request"])
            request = snapshot.outbox[-1]
            runtime.apply(snapshot, request, runtime.perform(snapshot, request))
        scripted_review("prior_decision_applies")
        if reply_kind == "ambiguous":
            from bmad_loop.campaign_cli import inbox_document

            item = next(row for row in inbox_document(snapshot)["items"] if row["kind"] == "hold")
            assert unit.hold.id == hold_id and "exactly one" in item["operator_action"]
            assert not unit.evidence.get("pending_delegated_choices")
            return
        assert not unit.evidence.get("developer_families")
        expected = (owner_text, '"decision_author": "5"', '"authority": "owner"')
        forbidden = ("gpt-scripted", "Decision " + decision)
        if reply_kind in {"ordinal", "ordinal_comment"}:
            expected += (
                question,
                '"reviewer_proposed_options": ["Ask for storage preference", "Use DELETE journal mode"]',
                '"owner_selected_number": 2',
                decision,
            )
    else:
        scripted_review("delegated_choice")
        assert unit.evidence["developer_families"] == ["openai"]
        expected = ("Use SQLite with WAL.", '"model": "gpt-scripted"')
        forbidden = ('"decision_author"',)
    assert unit.hold is None and unit.state == UnitState.READY
    assert unit.id in admit(snapshot).units
    publish_decision_summaries(runtime, snapshot, service)
    if reply_kind == "model":
        assert_human_changes_invalidate(runtime, snapshot, unit, service, monkeypatch)
    assert_next_development_prompt(
        project,
        tmp_path,
        monkeypatch,
        snapshot,
        unit,
        policy,
        runtime=runtime,
        expected=expected,
        forbidden=forbidden,
        hold_id=hold_id,
    )


def assert_human_changes_invalidate(runtime, snapshot, unit, service, monkeypatch):
    from bmad_loop.campaign_runtime_decisions import execution_choices

    [pending] = unit.evidence["pending_delegated_choices"].values()
    update = service.add(802, author=5, body="Please change the cache format.")
    assert execution_choices(runtime, snapshot, unit) == ([], pending["receipt"])
    service.comments.remove(update)
    for target, field, value in (
        (service.items[7], "title", "Different cache scope"),
        (service.items[7], "body", "New persistence requirements"),
        (service.items[8], "base", {"ref": "next", "sha": "new-base"}),
        (service.items[7], "labels", [{"id": 29, "name": "on-hold"}]),
        (service.repository, "owner", {"id": 11, "type": "User"}),
    ):
        with monkeypatch.context() as changed:
            changed.setitem(target, field, value)
            assert execution_choices(runtime, snapshot, unit) == ([], pending["receipt"])


def publish_decision_summaries(runtime, snapshot, service):
    """Drive both issue and PR mutations through the ordinary GitHub outbox."""
    from bmad_loop.campaign_github import ApiResponse
    from bmad_loop.campaign_intake import GithubIntake, IntakeBatch
    from bmad_loop.campaign_intake_flow import apply_batch

    original = service.request
    published = []

    def request(method, path, payload=None):
        if method == "POST" and path.endswith("/comments"):
            number = int(path.split("/")[-2])
            row = service.add(900 + len(published), body=payload["body"])
            row.update(
                issue_url=f"https://api.github.com/repos/org/repo/issues/{number}",
                html_url=service.items[number]["html_url"] + "#issuecomment-" + str(row["id"]),
            )
            service.items[number]["updated_at"] = "2026-11-06T10:00:00Z"
            published.append(number)
            return ApiResponse(201, dict(row), {})
        if method == "GET" and path.partition("?")[0].endswith("/comments"):
            number = int(path.partition("?")[0].split("/")[-2])
            return ApiResponse(
                200, [r for r in service.comments if r["issue_url"].endswith("/" + str(number))], {}
            )
        return original(method, path, payload)

    service.request = request
    for effect in list(snapshot.outbox):
        if effect.kind == "decision-project":
            observed = runtime.perform(snapshot, effect)
            assert observed.state == "confirmed"
            runtime.apply(snapshot, effect, observed)
            effect.state, effect.receipt = "confirmed", observed.data
    for effect in snapshot.outbox:
        if effect.kind == "github" and effect.payload.get("purpose") == "decision-summary":
            observed = runtime.perform(snapshot, effect)
            assert observed.state == "confirmed", observed
            runtime.apply(snapshot, effect, observed)
            effect.state, effect.receipt = "confirmed", observed.data
    assert set(published) == {7, 8}
    # Intake may also report first-seen parent versions for this comment activity.
    intake = GithubIntake(runtime.gateway(snapshot))
    rows = tuple(intake._observation("issue", service.items[n], n) for n in (7, 8))
    apply_batch(snapshot, IntakeBatch(rows, "", True, 0), observed_at=runtime.clock())


@pytest.mark.parametrize("condition", ["owner_stop", "existing_work", "other_unit"])
def test_ready_choice_preserves_owner_stops_and_targets_only_new_bound_work(condition):
    from bmad_loop.campaign_model import (
        CampaignSpec,
        DecisionSpec,
        Hold,
        ImplementationChoice,
        UnitSpec,
    )
    from bmad_loop.campaign_resolution_flow import request_implementation_choice

    configured = "s:e:story"
    spec = UnitSpec(
        configured if condition != "other_unit" else "s:e:peer",
        "s",
        "e",
        "Cache",
        scopes=("src/cache",),
    )
    choice = ImplementationChoice(configured, "Which journal mode?", "a" * 64 + ":decisions.md")
    policy = CampaignSpec(
        "choices", units=(spec,), decisions=DecisionSpec(implementation_choices=(choice,))
    )
    unit = CampaignUnit(spec, UnitState.READY)
    snapshot = CampaignSnapshot("choices", "org/repo", policy, {spec.id: unit})
    if condition == "owner_stop":
        unit.hold = Hold("owner_stop", "Keep this held", id="owner-hold")
    if condition == "existing_work":
        snapshot.bindings["source_observations"] = {unit.id: {"state": "done"}}
    request_implementation_choice(snapshot, unit)
    if condition == "owner_stop":
        assert unit.hold.id == "owner-hold"
        pending = unit.evidence["pending_failure_hold"]
        assert (
            pending["kind"] == "product_choice"
            and pending["id"] == unit.evidence["planned_choice_id"]
        )
    else:
        assert unit.hold is None and "planned_choice_id" not in unit.evidence


def test_legacy_freeze_omits_per_unit_choices():
    from bmad_loop.campaign_model import CampaignSpec, DecisionSpec

    assert not DecisionSpec().implementation_choices
    assert "implementation_choices" not in spec_to_dict(CampaignSpec("historical"))["decisions"]
