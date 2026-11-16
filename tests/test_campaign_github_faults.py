"""Typed GitHub faults survive normal supervisor restart without mutation replay."""

import subprocess

import pytest
from test_campaign_github import FakeTransport
from test_campaign_supervisor import PipelineDriver
from test_campaign_supervisor import campaign_store as campaign_store

from bmad_loop.campaign_github import (
    GhTransport,
    GithubError,
    GithubGateway,
    MutationIntent,
    UncertainMutation,
)
from bmad_loop.campaign_github_faults import mutation_observation, retry_at
from bmad_loop.campaign_store import add_action
from bmad_loop.campaign_supervisor import CampaignSupervisor, Observation


@pytest.mark.parametrize(
    ("status", "headers", "category", "deadline"),
    [
        (401, "", "authentication", None),
        (403, "", "permission", None),
        (
            403,
            "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 900\nRetry-After: 60\n",
            "rate_limit",
            900,
        ),
        (429, "Retry-After: 120\n", "rate_limit", 220),
        (429, "Retry-After: nonsense\n", "rate_limit", 160),
        (503, "", "transient", None),
    ],
)
def test_transport_classifies_faults_and_retains_strongest_deadline(
    monkeypatch, status, headers, category, deadline
):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            1,
            f'HTTP/2.0 {status} Refused\n{headers}X-GitHub-Request-Id: request-7\n\n{{"message":"refused"}}',
            "",
        )

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(GithubError) as error:
        GhTransport(clock=lambda: 100).request("POST", "repos/a/b/issues", {})
    fault = error.value.to_dict()
    assert fault["category"] == category
    assert fault["retry_at"] == deadline
    assert fault["request_id"] == "request-7"
    assert fault["certainty"] == ("unknown" if status >= 500 else "refused")
    assert isinstance(error.value, UncertainMutation) == (status >= 500)


def test_graphql_query_transport_failure_is_observation_not_uncertain_mutation(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh", 45)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(GithubError) as error:
        GhTransport().request("POST", "graphql", {"query": "query { viewer { login } }"})
    assert type(error.value) is GithubError
    assert error.value.certainty == "observation_failed"
    assert len(error.value.request_id) == 64


def test_persisted_fault_does_not_charge_expired_retry_duration_again():
    fault = {"category": "rate_limit", "retry_at": 700, "retry_after_s": 600}
    assert retry_at(fault, now=100, wall_clock=lambda: 100) == 700
    assert retry_at(fault, now=701, wall_clock=lambda: 701) == 761
    assert retry_at(fault, now=1000, wall_clock=lambda: 701) == 1060


def test_definitive_mutation_refusal_projects_rejected_with_evidence():
    transport = FakeTransport()
    transport.responses["POST", "repos/a/b/issues"] = GithubError(
        "credentials expired", status=401, certainty="refused", request_id="req"
    )
    intent = MutationIntent("write", "issue", "POST", "repos/a/b/issues", {}, state="sent_unknown")
    result = GithubGateway(transport, "a/b").execute(intent, generation=0)
    observation = mutation_observation(MutationIntent.from_dict(result.to_dict()), now=100)
    assert observation.state == "rejected"
    assert observation.data["intent"]["fault"]["request_id"] == "req"
    assert len(transport.calls) == 1
    assert (
        MutationIntent.from_dict(
            {key: value for key, value in intent.to_dict().items() if key != "fault"}
        ).fault
        is None
    )


def test_rate_wait_restart_observes_after_deadline_without_replaying_write(campaign_store):
    store, campaign_id = campaign_store
    now = [100.0]

    class FaultDriver(PipelineDriver):
        def proposals(self, snapshot):
            return ()

        def perform(self, snapshot, action):
            self.performed.append(action.id)
            raise GithubError("rate limit", status=429, retry_after_s=600, request_id="rate-1")

        def reconcile_action(self, snapshot, action):
            self.reconciled.append(action.id)
            return Observation("confirmed", {"remote_id": 71})

        def apply(self, snapshot, action, observation):
            assert observation.data == {"remote_id": 71}

    snapshot = store.load(campaign_id)
    action = add_action(snapshot, "remote-create", "s:e:first", {}, action_id="create-rate")
    snapshot = store.save(snapshot, snapshot.revision)
    driver = FaultDriver(store)
    supervisor = CampaignSupervisor(store, driver, clock=lambda: now[0], progress=lambda _: None)
    with supervisor._executors() as executor:
        snapshot = supervisor._submit(snapshot, action, executor, reconcile=False)
        try:
            supervisor._active[action.id].result(timeout=3)
        except GithubError:
            pass
        snapshot = supervisor._harvest(snapshot)
    saved = store.load(campaign_id).outbox[0]
    assert saved.next_retry_at == 700
    assert saved.receipt["github_fault"]["category"] == "rate_limit"
    assert saved.receipt["github_fault_attempts"] == 1
    restarted = CampaignSupervisor(store, driver, clock=lambda: now[0], progress=lambda _: None)
    restarted.run(campaign_id, max_ticks=1)
    assert driver.reconciled == []
    now[0] = 701
    result = restarted.run(campaign_id, max_ticks=1)
    assert driver.reconciled == [action.id]
    assert driver.performed == [action.id]
    confirmed = next(row for row in result.outbox if row.id == action.id)
    assert confirmed.state == "confirmed"
    assert confirmed.receipt["remote_id"] == 71
