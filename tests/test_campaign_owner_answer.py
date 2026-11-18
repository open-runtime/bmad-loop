"""Owner replies are verified observations and still pass through decision review."""

import dataclasses
import json

import pytest
from test_campaign_decision_sources import Service
from test_campaign_resolution_flow import candidate, state

from bmad_loop import campaign_runtime, cli
from bmad_loop.campaign_github import GithubGateway
from bmad_loop.campaign_model import Hold, SourceSelector
from bmad_loop.campaign_resolution_flow import _coordinates, proposals
from bmad_loop.campaign_store import CampaignStore


@pytest.mark.parametrize(
    "fault", ["none", "same-account-controller", "wrong-question", "wrong-author", "new-hold"]
)
def test_cli_answer_wakes_only_the_exact_owner_decision(
    project, tmp_path, monkeypatch, capsys, fault
):
    snapshot = state(project, hold="product_choice")
    unit = next(iter(snapshot.units.values()))
    unit.bindings["issue"] = {"number": 7, "database_id": 100}
    snapshot.spec = dataclasses.replace(
        snapshot.spec, sources=(SourceSelector("stories", id="s"),), units=(unit.spec,)
    )
    decision = "a" * 64
    receipt = {
        "id": decision,
        "hold_id": unit.hold.id,
        "coordinates": dataclasses.asdict(_coordinates(snapshot, unit.id)),
        "clear_hold": False,
        "disposition": "needs_new_owner_decision",
    }
    snapshot.bindings.update(
        decision_receipts={decision: receipt}, decision_requests={"q": {"receipt": decision}}
    )
    service = Service()
    comment = service.item
    comment.update(
        id=11,
        html_url="https://github.com/org/repo/issues/7#issuecomment-11",
        issue_url="https://api.github.com/repos/org/repo/issues/7",
        body=f"Decision {decision}\nUse SQLite within the approved cache scope.",
    )
    if fault == "wrong-question":
        comment["body"] = "Choose SQLite."
    elif fault == "wrong-author":
        comment["user"]["id"] = 17
    elif fault == "same-account-controller":
        from bmad_loop.campaign_model import OutboxAction

        snapshot.outbox.append(
            OutboxAction(
                "own-comment",
                "github",
                unit.id,
                {"intent": {"kind": "comment", "payload": {"body": comment["body"]}}},
                snapshot.generation,
                state="confirmed",
                receipt={
                    "intent": {
                        "kind": "comment",
                        "state": "confirmed",
                        "receipt": dict(comment),
                        "payload": {"body": comment["body"]},
                    }
                },
            )
        )
    store = CampaignStore(tmp_path / "state")
    snapshot = store.create(snapshot)
    monkeypatch.setattr(campaign_runtime, "store_for", lambda _: store)
    runtime = campaign_runtime.CampaignRuntime(
        project.project, store, gateway=GithubGateway(service, "org/repo")
    )
    assert (
        cli.main(
            [
                "campaign",
                "answer",
                snapshot.id,
                "--unit",
                unit.id,
                "--decision-id",
                decision,
                "--number",
                "7",
                "--comment-id",
                "11",
                "--json",
            ]
        )
        == 0
    )
    queued = json.loads(capsys.readouterr().out)["request"]
    runtime.control(snapshot, queued)
    action = snapshot.outbox[-1]
    observed = runtime.perform(snapshot, action)
    current = snapshot.units[unit.id]
    if fault == "new-hold":
        current.hold = Hold("operator", "fresh stop", id="new-hold")
    runtime.apply(snapshot, action, observed)
    answers = current.evidence.get("owner_answers") or {}
    if fault == "none":
        [answer] = answers.values()
        assert answer["actor"] == "5" and answer["decision_id"] == decision
        assert current.evidence["decision_input_revision"] == answer["revision"]
        assert (
            proposals(snapshot, project.project, candidate(), now=10)[0].kind
            == "resolution-context"
        )
    else:
        assert not answers
    assert current.hold is not None
