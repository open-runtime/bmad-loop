"""Native allowance probes share worker account inputs without actor attribution."""

import dataclasses
import sys

import pytest
from test_campaign_native_capacity import GROK, MUSE, NOW, grok, muse

from bmad_loop import campaign_native_capacity as native
from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_capacity_transport import ProbeError


def configured_reader(monkeypatch, candidate, *, overrides=None, binary=None, payload=None):
    profile = get_profile(candidate.profile)
    if overrides is not None:
        profile = dataclasses.replace(profile, env=overrides)
    if binary is not None:
        profile = dataclasses.replace(profile, binary=binary)
    monkeypatch.setattr(native, "get_profile", lambda name, project: profile)
    original_which = native.shutil.which
    monkeypatch.setattr(
        native.shutil,
        "which",
        lambda name, path=None: (
            sys.executable if name == candidate.profile else original_which(name, path=path)
        ),
    )
    observed = []

    def read(binary, project, environment, deadline):
        observed.append((binary, project, environment))
        return payload if payload is not None else grok() if candidate == GROK else muse()

    monkeypatch.setattr(native, "_read_" + candidate.profile, read)
    return observed


@pytest.mark.parametrize("candidate", [GROK, MUSE])
def test_profile_launcher_cannot_observe_a_different_account(tmp_path, monkeypatch, candidate):
    observed = configured_reader(monkeypatch, candidate, binary=sys.executable)
    result = native.observe(candidate, tmp_path, now=NOW)
    assert result is not None and not result.available and not result.known
    assert result.reason == "native capacity launcher overrides require a custom capacity command"
    assert observed == []


@pytest.mark.parametrize("candidate", [GROK, MUSE])
@pytest.mark.parametrize("key", ["HOME", "PATH", "XDG_CONFIG_HOME", "MUSE_CONFIG_DIR"])
def test_profile_environment_cannot_observe_a_different_account(
    tmp_path, monkeypatch, candidate, key
):
    observed = configured_reader(monkeypatch, candidate, overrides={key: str(tmp_path / "other")})
    result = native.observe(candidate, tmp_path, now=NOW)
    assert result is not None and not result.available and not result.known
    assert (
        result.reason == "native capacity environment overrides require a custom capacity command"
    )
    assert observed == []


@pytest.mark.parametrize("candidate", [GROK, MUSE])
def test_probe_preserves_inherited_account_inputs_and_removes_parent_attribution(
    tmp_path, monkeypatch, candidate
):
    inherited = {
        "HOME": str(tmp_path / "account"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "MUSE_CONFIG_DIR": str(tmp_path / "muse"),
        "GROK_CONFIG_HOME": str(tmp_path / "grok"),
        "BMAD_LOOP_STATE_DIR": str(tmp_path / "state"),
    }
    removed = {
        "HCOM_DIR": str(tmp_path / "parent-room"),
        "HCOM_PROCESS_ID": "parent-process",
        "HCOM_NAME": "parent-name",
        "SUPERSET_WORKSPACE_ID": "parent-workspace",
        "CODEX_THREAD_ID": "parent-thread",
        "CLAUDECODE": "1",
        "BMAD_LOOP_TASK_ID": "parent-task",
        "GH_TOKEN": "synthetic-github-value",
        "GITHUB_TOKEN": "synthetic-github-value",
    }
    for key, value in {**inherited, **removed}.items():
        monkeypatch.setenv(key, value)
    observed = configured_reader(monkeypatch, candidate)
    result = native.observe(candidate, tmp_path, now=NOW)
    assert result is not None and result.available and result.model_access
    assert len(observed) == 1
    binary, project, environment = observed[0]
    assert binary == sys.executable and project == tmp_path
    assert {key: environment.get(key) for key in inherited} == inherited
    assert not set(removed) & environment.keys()


@pytest.mark.parametrize("period_type", [[], {}])
def test_unhashable_billing_period_is_unknown_capacity(tmp_path, monkeypatch, period_type):
    data = grok()
    data["billing"]["config"]["currentPeriod"]["type"] = period_type
    with pytest.raises(ProbeError, match="billing period type is unsupported"):
        native.from_grok(data, GROK, observed_at=NOW, now=NOW)
    observed = configured_reader(monkeypatch, GROK, payload=data)
    result = native.observe(GROK, tmp_path, now=NOW)
    assert len(observed) == 1
    assert result is not None and not result.available and not result.known
    assert result.reason == "native Grok billing period type is unsupported"
