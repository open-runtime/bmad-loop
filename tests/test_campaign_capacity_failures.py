"""Failed capacity probe launches leave healthy pool alternatives admissible."""

import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest
from test_campaign_runtime_admission import configured

from bmad_loop import campaign_capacity
from bmad_loop.campaign_model import RoleCandidate, RoleSpec
from bmad_loop.campaign_preflight import ROLES
from bmad_loop.campaign_scheduler import route_unavailable


@pytest.fixture
def paid_status():
    return {
        "availability": {
            "gpt-astra": {
                "model": "gpt-astra",
                "provider": "codex",
                "observedAt": 1_000_000,
                "accounts": [
                    {
                        "id": "paid",
                        "eligible": False,
                        "quotaKnown": False,
                        "blockers": [{"code": "quota", "bucket": "unified7d", "until": 2_000_000}],
                    }
                ],
            }
        },
        "extraUsage": {
            "codex": {
                "on": True,
                "waiting": None,
                "gate": {"open": True, "shutBy": None, "held": 0},
            }
        },
        "accounts": [
            {
                "id": "paid",
                "provider": "codex",
                "type": "oauth",
                "status": "active",
                "disabled": False,
                "allowExtraUsage": True,
                "onExtraUsage": True,
                "canonicalSource": {"serving": True},
                "quota": {
                    "unified7d": 1.0,
                    "purchasedCredits": {
                        "hasCredits": True,
                        "balance": None,
                        "unlimited": False,
                        "seenAt": 990_000,
                    },
                },
            }
        ],
    }


def paid_observation(raw):
    return campaign_capacity.from_teamrouter(raw, RoleCandidate("codex", "gpt-astra"), now=1000)


def test_authorized_credits_admit_exact_model_without_inventing_slots(paid_status):
    result = paid_observation(paid_status)
    assert result.available and result.known and result.model_access
    assert not result.concurrency_known and result.slots == 0
    assert result.observed_at == 1000 and result.expires_at == 1110
    assert result.budget_remaining is None
    assert "authorized Codex credits" in result.reason
    assert route_unavailable(result, now=1000) == ""


@pytest.mark.parametrize("availability", [None, [], "malformed"])
def test_malformed_router_availability_is_an_explicit_unknown(paid_status, availability):
    paid_status["availability"] = availability
    result = paid_observation(paid_status)
    assert not result.available and not result.known
    assert result.reason == "model absent from router availability"


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowExtraUsage", False),
        ("allowExtraUsage", None),
        ("disabled", True),
        ("status", "error"),
        ("type", "api-key"),
        ("provider", "anthropic"),
        ("canonicalSource", {"serving": False}),
        ("canonicalSource", {"serving": True, "errno": "ENFILE"}),
        ("errorReason", {"code": "source_refresh_failed"}),
    ],
)
def test_credits_require_same_active_authorized_serving_account(paid_status, field, value):
    paid_status["accounts"][0][field] = value
    assert not paid_observation(paid_status).available


def test_authorized_cold_start_does_not_require_an_existing_paid_session(paid_status):
    paid_status["extraUsage"]["codex"]["on"] = False
    paid_status["accounts"][0]["onExtraUsage"] = False
    assert paid_observation(paid_status).available
    paid_status["accounts"][0]["allowExtraUsage"] = False
    assert not paid_observation(paid_status).available


@pytest.mark.parametrize(
    "blocker",
    [
        "auth",
        "route",
        "model-blocked",
        "model-ineligible",
        "capped",
        "spend-capped",
        "disabled",
        "entitlement",
        "credential",
        "throttled",
        "renewal",
        "source-transient",
        "unknown",
    ],
)
def test_credit_funding_never_overrides_exact_model_hard_blocker(paid_status, blocker):
    paid_status["availability"]["gpt-astra"]["accounts"][0]["blockers"].append({"code": blocker})
    assert not paid_observation(paid_status).available


@pytest.mark.parametrize("bucket", ["codex:unobserved", "tokens", "unknown", ["unified7d"]])
def test_shared_exhaustion_does_not_override_an_unproved_second_quota(paid_status, bucket):
    paid_status["availability"]["gpt-astra"]["accounts"][0]["blockers"].append(
        {"code": "quota", "bucket": bucket, "until": 2_000_000}
    )
    assert not paid_observation(paid_status).available


@pytest.mark.parametrize(
    "patch",
    [
        {"hasCredits": None},
        {"hasCredits": False},
        {"balance": 0},
        {"balance": -1},
        {"balance": "10"},
        {"balance": True},
        {"balance": float("nan")},
        {"seenAt": 879_000},
        {"seenAt": 1_001_000},
        {"seenAt": None},
        {"seenAt": float("inf")},
        {"seenAt": True},
        {"seenAt": 10**1000},
        {"balance": 10**1000},
    ],
)
def test_unknown_exhausted_or_stale_credit_evidence_does_not_admit(paid_status, patch):
    paid_status["accounts"][0]["quota"]["purchasedCredits"].update(patch)
    assert not paid_observation(paid_status).available


@pytest.mark.parametrize("field", ["observedAt", "unified7d", "until"])
def test_overflowing_router_numbers_are_unavailable_not_crashes(paid_status, field):
    entry = paid_status["availability"]["gpt-astra"]
    if field == "observedAt":
        entry[field] = 10**1000
    elif field == "unified7d":
        paid_status["accounts"][0]["quota"][field] = 10**1000
    else:
        entry["accounts"][0]["blockers"][0][field] = 10**1000
    assert not paid_observation(paid_status).available


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-join",
        "duplicate-account",
        "duplicate-verdict",
        "wrong-model",
        "wrong-provider",
        "closed-gate",
        "stale-model",
        "model-blocked",
        "malformed-blocker",
    ],
)
def test_credit_admission_rejects_ambiguous_identity_and_model_evidence(paid_status, mutation):
    entry = paid_status["availability"]["gpt-astra"]
    if mutation == "missing-join":
        paid_status["accounts"][0]["id"] = "different"
    elif mutation == "duplicate-account":
        paid_status["accounts"].append(copy.deepcopy(paid_status["accounts"][0]))
    elif mutation == "duplicate-verdict":
        entry["accounts"].append(copy.deepcopy(entry["accounts"][0]))
    elif mutation == "wrong-model":
        entry["model"] = "other-model"
    elif mutation == "wrong-provider":
        entry["provider"] = "anthropic"
    elif mutation == "closed-gate":
        paid_status["extraUsage"]["codex"]["gate"]["open"] = False
    elif mutation == "stale-model":
        entry["observedAt"] = 879_000
    elif mutation == "model-blocked":
        paid_status["blockedModels"] = ["gpt-astra"]
    else:
        entry["accounts"][0]["blockers"] = [{"code": ["quota"]}]
    assert not paid_observation(paid_status).available


@pytest.mark.parametrize(
    "kind", ["free-band", "unknown-free", "temporary-hold", "host-pressure", "renewal-retry"]
)
def test_provider_credit_gate_does_not_override_exact_model_peer_ambiguity(paid_status, kind):
    peer = copy.deepcopy(paid_status["accounts"][0])
    peer.update(id="peer", allowExtraUsage=False, onExtraUsage=False)
    verdict = copy.deepcopy(paid_status["availability"]["gpt-astra"]["accounts"][0])
    verdict["id"] = "peer"
    peer["quota"]["unified7d"] = 0.98
    if kind == "unknown-free":
        verdict.update(eligible=True, quotaKnown=False, blockers=[])
    elif kind == "temporary-hold":
        verdict["blockers"].append({"code": "renewal"})
    elif kind in {"host-pressure", "renewal-retry"}:
        verdict["blockers"].append({"code": "auth"})
        if kind == "host-pressure":
            peer["canonicalSource"]["errno"] = "ENFILE"
        else:
            peer["errorReason"] = {"code": "source_refresh_failed"}
    paid_status["accounts"].append(peer)
    paid_status["availability"]["gpt-astra"]["accounts"].append(verdict)
    assert not paid_observation(paid_status).available


def test_permanently_unavailable_peer_does_not_hide_an_authorized_credit_route(paid_status):
    peer = {
        "id": "peer",
        "provider": "codex",
        "status": "error",
        "canonicalSource": {"serving": False, "code": "source_unavailable"},
    }
    paid_status["accounts"].append(peer)
    paid_status["availability"]["gpt-astra"]["accounts"].append(
        {"id": "peer", "eligible": False, "quotaKnown": False, "blockers": [{"code": "auth"}]}
    )
    assert paid_observation(paid_status).available


def test_ordinary_known_quota_does_not_require_credit_metadata(paid_status):
    row = paid_status["availability"]["gpt-astra"]["accounts"][0]
    row.update(eligible=True, quotaKnown=True, blockers=[])
    paid_status.pop("accounts")
    paid_status.pop("extraUsage")
    observed = paid_observation(paid_status)
    assert observed.available and observed.expires_at == 1120


@pytest.mark.parametrize(
    "change,admitted",
    [
        ({}, True),
        ({"quota_known": False}, False),
        ({"model": "different"}, False),
        ({"model_access": False}, False),
        ({"observed_at": 800, "expires_at": 920}, False),
        ({"concurrency_known": "false"}, False),
        ({"concurrency_known": True}, False),
        ({"slots": -1}, False),
        ({"slots": None}, True),
    ],
)
def test_custom_unknown_concurrency_retains_exact_model_quota_and_freshness_gates(
    tmp_path, monkeypatch, change, admitted
):
    payload = {
        "schema_version": 1,
        "model": "gpt-astra",
        "quota_known": True,
        "model_access": True,
        "available": True,
        "concurrency_known": False,
        "observed_at": 1000,
        "expires_at": 1120,
        **change,
    }
    monkeypatch.setattr(
        campaign_capacity,
        "run_argv",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, timed_out=False, interrupted=False, stdout=json.dumps(payload)
        ),
    )
    result = campaign_capacity.observe(
        RoleCandidate("codex", "gpt-astra", capacity_command=("observer",)), tmp_path, now=1000
    )
    assert (not route_unavailable(result, now=1000)) is admitted
    if admitted:
        assert not result.concurrency_known and result.slots == 0


def test_missing_probe_is_an_explicit_unknown_capacity_observation(tmp_path):
    candidate = RoleCandidate(
        "codex", "gpt-test", capacity_command=(str(tmp_path / "missing-command"),)
    )
    observed = campaign_capacity.observe(candidate, tmp_path)
    assert not observed.available and not observed.known
    assert "FileNotFoundError" in observed.reason
    assert not observed.observed_at and not observed.expires_at


def test_custom_probe_can_timestamp_its_observation_during_execution(tmp_path, monkeypatch):
    clock = iter([1000.0, 1001.0])
    monkeypatch.setattr(campaign_capacity, "time", SimpleNamespace(time=lambda: next(clock)))
    payload = {
        "schema_version": 1,
        "model": "gpt-astra",
        "quota_known": True,
        "available": True,
        "model_access": True,
        "concurrency_known": False,
        "observed_at": 1000.5,
        "expires_at": 1120.5,
    }
    monkeypatch.setattr(
        campaign_capacity,
        "run_argv",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, timed_out=False, interrupted=False, stdout=json.dumps(payload)
        ),
    )
    result = campaign_capacity.observe(
        RoleCandidate("codex", "gpt-astra", capacity_command=("observer",)), tmp_path
    )
    assert result.available and result.expires_at == 1120.5


@pytest.mark.parametrize("failure", [PermissionError, OSError])
def test_probe_spawn_error_preserves_failure_type_without_exposing_command(
    tmp_path, monkeypatch, failure
):
    def unavailable(*args, **kwargs):
        raise failure("credential-bearing command detail")

    monkeypatch.setattr(campaign_capacity, "run_argv", unavailable)
    observed = campaign_capacity.observe(RoleCandidate("codex", "gpt-test"), tmp_path)
    assert not observed.available and not observed.known
    assert failure.__name__ in observed.reason
    assert "credential-bearing" not in observed.reason


def test_runtime_capacity_refresh_retains_fallback_when_preferred_probe_cannot_start(
    project, tmp_path, monkeypatch
):
    runtime, snapshot, astra, fable = configured(project, tmp_path)
    astra = dataclasses.replace(astra, capacity_command=("unavailable-probe", astra.model))
    fable = dataclasses.replace(fable, capacity_command=("working-probe", fable.model))
    snapshot.spec = dataclasses.replace(
        snapshot.spec,
        role_pools=(("planners", (astra, fable)),),
        roles=tuple((role, RoleSpec(pool="planners")) for role in (*ROLES, "execution")),
    )

    def probe(argv, **kwargs):
        if argv[0] == "unavailable-probe":
            raise FileNotFoundError(argv[0])
        assert argv == ("working-probe", fable.model)
        return SimpleNamespace(
            returncode=0,
            timed_out=False,
            interrupted=False,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "model": fable.model,
                    "quota_known": True,
                    "available": True,
                    "model_access": True,
                    "slots": 4,
                }
            ),
        )

    monkeypatch.setattr(campaign_capacity, "run_argv", probe)
    runtime.capacity_observer = lambda candidate, workspace: campaign_capacity.observe(
        candidate, workspace, now=runtime.clock()
    )
    snapshot.bindings["capacity"] = runtime._perform_capacity(snapshot, None).data
    assert runtime._role(snapshot, "planning") == fable
    failed = next(
        row for row in snapshot.bindings["capacity"]["observations"] if row["model"] == astra.model
    )
    assert not failed["capacity"]["known"]
    assert "FileNotFoundError" in failed["capacity"]["reason"]
