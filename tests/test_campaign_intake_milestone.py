"""A linked milestone's activity is independent of a mapped issue's disposition."""

import copy

import pytest
from test_campaign_intake_flow import ROLE, prepare
from test_campaign_intake_flow import snapshot as snapshot
from test_campaign_intake_flow import triage_raw

from bmad_loop import campaign_intake_flow as flow
from bmad_loop import campaign_runtime_intake as intake
from bmad_loop.campaign_store import CampaignStore


class Service:
    repository = "a/b"

    def __init__(self):
        self.row = {
            "id": 1003,
            "number": 3,
            "title": "Second",
            "body": "Owner confirmed the migration date",
            "updated_at": "2026-11-05T10:00:00Z",
            "user": {"id": 10, "login": "owner"},
            "milestone": {
                "id": 9001,
                "number": 1,
                "node_id": "M1",
                "title": "Delivery",
                "state": "open",
            },
        }

    def issues(self, **_):
        return [copy.deepcopy(self.row)]

    def comments(self, _):
        return []


def poll(snapshot, service):
    observed = intake.collect(snapshot, service, now=100)
    intake.apply_poll(snapshot, observed)
    return observed["batch"]["observations"]


@pytest.mark.parametrize("legacy", [False, True])
def test_joined_milestone_close_keeps_prior_disposition_across_restart(snapshot, tmp_path, legacy):
    service = Service()
    unit = next(iter(snapshot.units.values()))
    unit.bindings["issue"] = {"number": 3}
    poll(snapshot, service)
    (original,) = flow.untriaged(snapshot)
    attempt = prepare(snapshot, original)
    flow.accept_triage(
        snapshot,
        triage_raw(snapshot, [original], disposition="duplicate", unit_id=unit.id, issue=3),
        attempt_id=attempt,
        observed_role=ROLE,
    )
    previous = snapshot.bindings[flow.INBOX]["records"][original]
    if legacy:
        previous.pop("disposition_fingerprint")
    store = CampaignStore(tmp_path / "authority")
    snapshot = store.create(snapshot)
    disposition = copy.deepcopy(store.load(snapshot.id).bindings[flow.INBOX]["records"][original])
    service.row["milestone"].update(state="closed", closed_at="2026-11-08T12:00:00Z")
    (incoming,) = poll(snapshot, service)
    saved = store.save(snapshot, snapshot.revision)
    snapshot = store.load(saved.id)
    inbox = snapshot.bindings[flow.INBOX]
    assert not flow.untriaged(snapshot) and flow.next(snapshot) is None
    assert inbox["latest"]["issue:1003"] == original and inbox["records"][original] == disposition
    (latest,) = [row for version, row in inbox["records"].items() if version != original]
    assert latest["state"] == "observation_only" and latest["disposition_of"] == original
    assert latest["observation"] == incoming
    assert latest["observation"]["payload"]["milestone"]["state"] == "closed"
    assert disposition["observation"]["payload"]["milestone"]["state"] == "open"
    assert latest["observation"]["digest"] != disposition["observation"]["digest"]
    assert snapshot.units[unit.id].evidence["decision_input_revision"] == original
    assert snapshot.bindings["intake"]["cursor"] == service.row["updated_at"]
    assert not poll(snapshot, service)


@pytest.mark.parametrize(
    "change",
    [
        "body",
        "title",
        "removed",
        "new-id",
        "new-node",
        "number",
        "unknown-id",
        "new-owner",
        "milestone-title",
        "milestone-description",
    ],
)
def test_changed_issue_input_remains_actionable_with_a_milestone(snapshot, change):
    service = Service()
    next(iter(snapshot.units.values())).bindings["issue"] = {"number": 3}
    poll(snapshot, service)
    (first,) = flow.untriaged(snapshot)
    if change in {"body", "title"}:
        service.row[change] += " New owner request."
    elif change == "removed":
        service.row["milestone"] = None
    elif change == "new-id":
        service.row["milestone"]["id"] += 1
    elif change == "new-node":
        service.row["milestone"]["node_id"] = "M2"
    elif change == "number":
        service.row["milestone"]["number"] += 1
    elif change == "unknown-id":
        service.row["milestone"].pop("id")
    elif change.startswith("milestone-"):
        service.row["milestone"][change.removeprefix("milestone-")] = "Owner changed the scope"
    else:
        service.row["user"] = {"id": 11, "login": "other-owner"}
    poll(snapshot, service)
    (replacement,) = flow.untriaged(snapshot)
    assert first != replacement
    assert snapshot.bindings[flow.INBOX]["records"][first]["superseded_by"] == replacement
