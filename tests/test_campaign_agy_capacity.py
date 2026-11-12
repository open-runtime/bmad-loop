"""AGY status contract uses synthetic metadata and fake processes, never AGY."""

import copy
import dataclasses
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import pid_gone, write_script_launcher

from bmad_loop import campaign_agy_capacity as agy
from bmad_loop import campaign_capacity
from bmad_loop import campaign_native_capacity as native
from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_capacity_transport import ProbeError
from bmad_loop.campaign_model import RoleCandidate
from bmad_loop.campaign_scheduler import route_unavailable

NOW = 1_794_207_000.0
CANDIDATE = RoleCandidate("antigravity", "gemini-3.8-flash-high", "high")


def envelope(command, data):
    return {
        "conversation_id": "",
        "status": "SUCCESS",
        "response": "Synthetic status",
        "duration_seconds": 1,
        "num_turns": 0,
        "usage": {
            key: 0
            for key in (
                "input_tokens",
                "output_tokens",
                "thinking_tokens",
                "cache_read_tokens",
                "total_tokens",
            )
        },
        "command": {"name": command, "data": data},
    }


def payload():
    return {
        "usage": envelope(
            "usage",
            {
                "description": "Synthetic quota",
                "groups": [
                    {
                        "name": "Gemini Models",
                        "description": "Shared Gemini quota",
                        "buckets": [
                            {
                                "id": "fixture-gemini-weekly",
                                "name": "Weekly",
                                "description": "Synthetic window",
                                "window": "weekly",
                                "remaining_fraction": 0.5,
                                "reset_time": datetime.fromtimestamp(
                                    NOW + 90, timezone.utc
                                ).isoformat(),
                            }
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "description": "Separate quota",
                        "buckets": [
                            {
                                "id": "fixture-other-weekly",
                                "name": "Weekly",
                                "window": "weekly",
                                "remaining_fraction": 0,
                                "reset_time": datetime.fromtimestamp(
                                    NOW + 1000, timezone.utc
                                ).isoformat(),
                            }
                        ],
                    },
                ],
            },
        ),
        "models": envelope(
            "models",
            {
                "models": [
                    {"id": model, "label": label} for model, label in agy.FLASH_MODELS.items()
                ]
                + [{"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (Thinking)"}]
            },
        ),
    }


def parse(raw, candidate=CANDIDATE, observed=NOW - 1, now=NOW):
    return agy.from_agy(raw, candidate, observed_at=observed, now=now)


def groups(raw):
    return raw["usage"]["command"]["data"]["groups"]


@pytest.mark.parametrize("model", list(agy.FLASH_MODELS))
def test_exact_flash_catalog_joins_documented_group_with_expiry(model):
    result = parse(payload(), dataclasses.replace(CANDIDATE, model=model))
    assert result.known and result.available and result.model_access
    assert not result.concurrency_known and result.slots == 0
    assert result.budget_remaining is None
    assert result.observed_at == NOW - 1 and result.expires_at == NOW + 90
    assert not route_unavailable(result, now=NOW)
    assert route_unavailable(result, now=NOW + 91)
    # Effort does not alias native model IDs or earn a route qualification.
    assert parse(payload(), dataclasses.replace(CANDIDATE, model=model, effort="other")) == result


def test_exhaustion_is_known_but_missing_access_is_not_assumed():
    raw = payload()
    groups(raw)[0]["buckets"][0]["remaining_fraction"] = 0
    result = parse(raw)
    assert result.known and result.model_access and not result.available
    groups(raw)[0]["buckets"][0]["remaining_fraction"] = 0.5
    raw["models"]["command"]["data"]["models"] = raw["models"]["command"]["data"]["models"][1:]
    result = parse(raw)
    assert result.known and not result.model_access and not result.available
    assert "absent" in result.reason
    # Catalog absence cannot hide incomplete quota or a different native command.
    groups(raw)[0]["buckets"][0]["window"] = "unknown"
    with pytest.raises(ProbeError, match="window"):
        parse(raw)


@pytest.mark.parametrize(
    "model", ["gemini-3.8-flash", "gemini-4-argon-high", "claude-sonnet-4-6", ""]
)
def test_unknown_mapping_is_not_inferred_from_family_or_label(model):
    raw = payload()
    raw["models"]["command"]["data"]["models"] = [
        row for row in raw["models"]["command"]["data"]["models"] if row["id"] != model
    ]
    raw["models"]["command"]["data"]["models"].append(
        {"id": model or "unrelated", "label": "Gemini 3.8 Flash (High)"}
    )
    with pytest.raises(ProbeError, match="mapping"):
        parse(raw, dataclasses.replace(CANDIDATE, model=model))


@pytest.mark.parametrize(
    "change,reason",
    [
        ("wrong_label", "label"),
        ("duplicate_model", "catalog is ambiguous"),
        ("missing_group", "group is missing"),
        ("duplicate_group", "group is ambiguous"),
        ("unknown_group", "group is ambiguous"),
        ("empty_windows", "windows are missing"),
        ("duplicate_window", "window is ambiguous"),
        ("duplicate_bucket_id", "window is ambiguous"),
        ("unknown_window", "window is ambiguous"),
        ("extra_bucket_key", "schema"),
        ("extra_envelope_key", "schema"),
        ("missing_bucket_key", "schema"),
        ("empty_groups", "groups are missing"),
        ("invalid_models", "catalog is missing"),
    ],
)
def test_incomplete_or_ambiguous_metadata_is_not_capacity(change, reason):
    raw = payload()
    models = raw["models"]["command"]["data"]["models"]
    bucket = groups(raw)[0]["buckets"][0]
    if change == "wrong_label":
        models[0]["label"] = "Gemini 4 Argon"
    elif change == "duplicate_model":
        models.append(copy.deepcopy(models[0]))
    elif change == "missing_group":
        groups(raw).pop(0)
    elif change == "duplicate_group":
        duplicate = copy.deepcopy(groups(raw)[0])
        duplicate["buckets"][0]["id"] = "different-window-id-same-group"
        groups(raw).append(duplicate)
    elif change == "unknown_group":
        groups(raw)[1]["name"] = "Unknown allowance"
    elif change == "empty_windows":
        groups(raw)[0]["buckets"] = []
    elif change == "duplicate_window":
        groups(raw)[0]["buckets"].append({**bucket, "id": "second"})
    elif change == "duplicate_bucket_id":
        groups(raw)[1]["buckets"][0]["id"] = bucket["id"]
    elif change == "unknown_window":
        groups(raw)[0]["buckets"].append({**bucket, "id": "second", "window": "5h"})
    elif change == "extra_bucket_key":
        bucket["disabled"] = True
    elif change == "extra_envelope_key":
        raw["usage"]["error"] = None
    elif change == "missing_bucket_key":
        del bucket["remaining_fraction"]
    elif change == "empty_groups":
        raw["usage"]["command"]["data"]["groups"] = []
    elif change == "invalid_models":
        raw["models"]["command"]["data"]["models"] = {}
    with pytest.raises(ProbeError, match=reason):
        parse(raw)


@pytest.mark.parametrize(
    "value", [True, False, -0.1, 1.1, float("nan"), float("inf"), "0.5", None, 10**1000]
)
def test_fraction_requires_finite_bounded_number(value):
    raw = payload()
    groups(raw)[0]["buckets"][0]["remaining_fraction"] = value
    with pytest.raises(ProbeError, match="number|fraction"):
        parse(raw)


@pytest.mark.parametrize(
    "value",
    ["not-date", "2026-11-12T12:00:00", "2000-01-01T00:00:00Z", "2099-01-01T00:00:00+01:00", True],
)
def test_every_supplied_reset_requires_current_utc(value):
    raw = payload()
    groups(raw)[1]["buckets"][0]["reset_time"] = value
    with pytest.raises(ProbeError, match="reset|current|text"):
        parse(raw)


@pytest.mark.parametrize("offset", [-121, 1, float("nan")])
def test_local_observation_is_not_refreshed(offset):
    with pytest.raises(ProbeError, match="stale|number"):
        parse(payload(), observed=NOW + offset)


@pytest.mark.parametrize("part", ["usage", "models"])
@pytest.mark.parametrize(
    "key,value",
    [
        ("conversation_id", "private-session"),
        ("status", "ERROR"),
        ("num_turns", 1),
        ("num_turns", False),
        ("total_tokens", 1),
        ("total_tokens", False),
        ("command", "chat"),
    ],
)
def test_both_envelopes_must_be_exact_successful_zero_turn(part, key, value):
    raw = payload()
    if key == "total_tokens":
        raw[part]["usage"][key] = value
    elif key == "command":
        raw[part][key]["name"] = value
    else:
        raw[part][key] = value
    with pytest.raises(ProbeError, match="zero-turn|command mismatch"):
        parse(raw)


_FAKE = """import json, os, sys
from pathlib import Path
root = Path(os.environ["CAPACITY_FIXTURE"])
assert sys.stdin.read() == ""
with (root/"pids").open("a") as f: f.write(str(os.getpid())+"\\n")
with (root/"launches").open("a") as f: f.write(json.dumps({"argv":sys.argv[1:],"cwd":os.getcwd(),"room":os.environ.get("HCOM_DIR"),"home":os.environ.get("HOME"),"auth":os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),"update":os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE"),"borrowed":[k for k in os.environ if k.startswith(("PARADEV_","SUPERSET_","BMAD_LOOP_")) or k.startswith("HCOM_") and k!="HCOM_DIR"]})+"\\n")
raw=json.loads((root/"fixture.json").read_text())
print(json.dumps(raw["usage" if sys.argv[-1]=="/usage" else "models"]))
"""


def fixture(tmp_path, monkeypatch):
    binary = write_script_launcher(tmp_path, "agy", _FAKE)
    home = tmp_path / "account"
    home.mkdir()
    settings = home / ".gemini/antigravity-cli/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"disableSlashCommands":false}')
    (tmp_path / "fixture.json").write_text(json.dumps(payload()))
    monkeypatch.setenv("CAPACITY_FIXTURE", str(tmp_path))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGY_CLI_DISABLE_AUTO_UPDATE", "1")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(home / "auth-sentinel"))
    for name in (
        "HCOM_NAME",
        "HCOM_DIR",
        "PARADEV_WORKSPACE_ID",
        "SUPERSET_HOME_DIR",
        "BMAD_LOOP_STATE_DIR",
    ):
        monkeypatch.setenv(name, "borrowed")
    # Never use the real PATH lookup, even if this fixture's fake fails.
    monkeypatch.setattr(native.shutil, "which", lambda *args, **kwargs: str(binary))
    monkeypatch.setattr(
        campaign_capacity, "run_argv", lambda *a, **k: pytest.fail("unexpected TeamRouter probe")
    )
    return binary, settings


def test_default_dispatch_runs_fixed_commands_with_eof_and_private_account_context(
    tmp_path, monkeypatch
):
    assert native.TIMEOUT_S == 12.0
    # This positive test launches real fake interpreters and reaps them through
    # the host process census. Host/xdist load must not spend its success budget;
    # the deterministic deadline negative below tests production expiry instead.
    monkeypatch.setattr(native, "TIMEOUT_S", 60.0)
    _, settings = fixture(tmp_path, monkeypatch)
    original = dict(os.environ)
    deadlines = []
    original_process = agy.StatusProcess

    class RecordingProcess(original_process):
        def __init__(self, argv, project, env, deadline):
            if os.name != "nt":
                assert stat.S_IMODE(project.stat().st_mode) == 0o700
                assert stat.S_IMODE(Path(env["HCOM_DIR"]).stat().st_mode) == 0o700
            assert not (project / ".git").exists()
            deadlines.append(deadline)
            super().__init__(argv, project, env, deadline)

    monkeypatch.setattr(agy, "StatusProcess", RecordingProcess)
    before = time.monotonic()
    result = campaign_capacity.observe(CANDIDATE, tmp_path, now=NOW)
    assert result.known and result.available, result.reason
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert before < deadlines[0] <= before + native.TIMEOUT_S + 1
    launches = [json.loads(line) for line in (tmp_path / "launches").read_text().splitlines()]
    assert [row["argv"] for row in launches] == [
        ["--settings", agy._SETTINGS, "--output-format", "json", "-p", "/usage"],
        ["--settings", agy._SETTINGS, "--output-format", "json", "models"],
    ]
    assert all(
        row["home"] == original["HOME"]
        and row["auth"] == original["GOOGLE_APPLICATION_CREDENTIALS"]
        for row in launches
    )
    assert all(row["update"] == "true" and not row["borrowed"] for row in launches)
    assert len({row["cwd"] for row in launches}) == 1
    assert all(
        Path(row["room"]).parent == Path(row["cwd"]) and not Path(row["cwd"]).exists()
        for row in launches
    )
    assert dict(os.environ) == original
    assert settings.read_text() == '{"disableSlashCommands":false}'
    assert all(pid_gone(int(pid)) for pid in (tmp_path / "pids").read_text().splitlines())


@pytest.mark.parametrize(
    "change,reason",
    [
        ("binary", "launcher overrides"),
        ("args", "launch overrides"),
        ("env", "environment overrides"),
        ("model", "mapping"),
        ("provider_env", "environment overrides"),
        ("slash", "disabled"),
        ("settings", "cannot be verified"),
    ],
)
def test_prelaunch_boundary_refusals_never_execute(tmp_path, monkeypatch, change, reason):
    _, settings = fixture(tmp_path, monkeypatch)
    profile = get_profile("antigravity", tmp_path)
    candidate = CANDIDATE
    if change == "binary":
        profile = dataclasses.replace(profile, binary="wrapper")
    elif change == "args":
        profile = dataclasses.replace(profile, launch_args=("--settings", "other"))
    elif change == "env":
        profile = dataclasses.replace(profile, env={"AGY_CLI_DISABLE_AUTO_UPDATE": "1"})
    elif change == "model":
        candidate = dataclasses.replace(candidate, model="gemini-4-argon-high")
    elif change == "provider_env":
        monkeypatch.setenv("AGY_CONFIG_PATH", "/other-account")
    elif change == "slash":
        settings.write_text('{"disableSlashCommands":true}')
    elif change == "settings":
        settings.write_text("private invalid configuration")
    monkeypatch.setattr(native, "get_profile", lambda *a: profile)
    monkeypatch.setattr(agy, "StatusProcess", lambda *a: pytest.fail("unexpected native launch"))
    result = campaign_capacity.observe(candidate, tmp_path, now=NOW)
    assert not result.known and not result.available and reason in result.reason
    assert "private" not in result.reason


@pytest.mark.parametrize(
    "change", ["settings", "binary", "aggregate", "deadline", "cleanup", "bad_envelope"]
)
def test_observation_faults_are_unknown_without_followup(tmp_path, monkeypatch, change):
    binary, settings = fixture(tmp_path, monkeypatch)
    calls = []

    class FakeStatus:
        total = 140_000 if change == "aggregate" else 100

        def __init__(self, argv, cwd, env, deadline):
            calls.append(argv)

        def __enter__(self):
            return self

        def json_output(self):
            if change == "settings":
                settings.write_text('{"changed":true}')
            if change == "binary":
                binary.write_text("changed image")
            if change == "deadline":
                monkeypatch.setattr(agy.time, "monotonic", lambda: float("inf"))
            value = payload()["usage" if len(calls) == 1 else "models"]
            if change == "bad_envelope":
                value["num_turns"] = 1
            return value

        def __exit__(self, *args):
            if change == "cleanup":
                raise ProbeError("native capacity cleanup is unconfirmed")

    monkeypatch.setattr(agy, "StatusProcess", FakeStatus)
    result = campaign_capacity.observe(CANDIDATE, tmp_path, now=NOW)
    assert not result.known and not result.available
    reasons = {
        "settings": "changed",
        "binary": "changed",
        "aggregate": "aggregate",
        "deadline": "timed out",
        "cleanup": "cleanup is unconfirmed",
        "bad_envelope": "zero-turn",
    }
    assert reasons[change] in result.reason
    assert len(calls) == (2 if change == "aggregate" else 1)


def test_symlink_path_entry_pins_followed_target(tmp_path, monkeypatch):
    target, _ = fixture(tmp_path, monkeypatch)
    link = tmp_path / "bin" / "agy"
    link.parent.mkdir()
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this host: {exc}")
    monkeypatch.setattr(native.shutil, "which", lambda *a, **k: str(link))
    monkeypatch.setattr(native, "TIMEOUT_S", 60.0)
    result = campaign_capacity.observe(CANDIDATE, tmp_path, now=NOW)
    assert result.known and result.available, result.reason
    assert agy._image_pin(link) == agy._image_pin(target)


def test_account_home_without_HOME_uses_path_home(tmp_path, monkeypatch):
    binary, _ = fixture(tmp_path, monkeypatch)
    account = tmp_path / "account"
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(account))
    monkeypatch.setattr(Path, "home", lambda: account)
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "HOME" and not key.startswith(("GH_", "GITHUB_"))
    }
    raw = agy.read_agy(str(binary), env, time.monotonic() + 60.0)
    assert "usage" in raw and "models" in raw


def test_custom_capacity_command_keeps_precedence(tmp_path, monkeypatch):
    candidate = dataclasses.replace(CANDIDATE, capacity_command=("custom-status",))
    monkeypatch.setattr(
        native, "observe", lambda *a, **k: pytest.fail("native default bypassed custom command")
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            timed_out=False,
            interrupted=False,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "model": candidate.model,
                    "quota_known": True,
                    "available": True,
                    "model_access": True,
                    "concurrency_known": False,
                    "observed_at": NOW,
                    "expires_at": NOW + 90,
                }
            ),
        )

    monkeypatch.setattr(campaign_capacity, "run_argv", run)
    result = campaign_capacity.observe(candidate, tmp_path, now=NOW)
    assert result.known and result.available, result.reason
    assert calls == [("custom-status",)]
