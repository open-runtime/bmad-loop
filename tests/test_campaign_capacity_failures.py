"""Failed capacity probe launches leave healthy pool alternatives admissible."""

import dataclasses
import json
from types import SimpleNamespace

import pytest
from test_campaign_runtime_admission import configured

from bmad_loop import campaign_capacity
from bmad_loop.campaign_model import RoleCandidate, RoleSpec
from bmad_loop.campaign_preflight import ROLES


def test_missing_probe_is_an_explicit_unknown_capacity_observation(tmp_path):
    candidate = RoleCandidate(
        "codex", "gpt-test", capacity_command=(str(tmp_path / "missing-command"),)
    )
    observed = campaign_capacity.observe(candidate, tmp_path)
    assert not observed.available and not observed.known
    assert "FileNotFoundError" in observed.reason
    assert not observed.observed_at and not observed.expires_at


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
