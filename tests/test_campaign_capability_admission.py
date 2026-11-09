"""Proposal-local route checks cannot become a persistent executable cache."""

import copy
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from test_campaign_headless import fixture_capability
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_headless as headless
from bmad_loop.campaign_model import CampaignError


def test_repeated_route_success_and_failure_are_shared_only_inside_one_pass(prepared, monkeypatch):
    _, _, _, candidate, capability = prepared
    original = headless._validate_capability
    observed = []

    def checked(raw, route, **kwargs):
        observed.append((copy.deepcopy(raw), route, kwargs))
        return original(raw, route, **kwargs)

    monkeypatch.setattr(headless, "_validate_capability", checked)
    with headless.admission_validation_pass():
        for _ in range(24):
            headless.validate_capability(copy.deepcopy(capability), candidate)
        assert len(observed) == 1
        with headless.admission_validation_pass():
            headless.validate_capability(capability, candidate)
        for _ in range(24):
            with pytest.raises(CampaignError, match="thinking level"):
                headless.validate_capability(capability, replace(candidate, effort="unqualified"))
        assert len(observed) == 2
        altered = {**capability, "reserved_tokens": capability["reserved_tokens"] + 1}
        with pytest.raises(CampaignError):
            headless.validate_capability(altered, candidate)
        with pytest.raises(CampaignError, match="permission"):
            headless.validate_capability(capability, candidate, permissions="workspace-tools")
        assert len(observed) == 4
    headless.validate_capability(capability, candidate)
    assert len(observed) == 5


def test_actual_preparation_rehashes_binary_even_inside_a_cached_admission_pass(prepared, tmp_path):
    runner, path, request, candidate, capability = prepared
    binary = tmp_path / "private-python-copy"
    shutil.copy2(capability["binary"], binary)
    capability = fixture_capability(
        tmp_path,
        {
            **capability,
            "binary": str(binary),
            "argv": [str(binary), *capability["argv"][1:]],
        },
        candidate,
    )
    assert binary.is_relative_to(tmp_path) and binary != Path(capability["dependencies"][0]["path"])
    with headless.admission_validation_pass():
        headless.validate_capability(capability, candidate)
        binary.write_bytes(binary.read_bytes() + b"\n# changed after proposal\n")
        headless.validate_capability(capability, candidate)
        with pytest.raises(CampaignError, match="executable|changed|digest"):
            runner.prepare(
                path.parent / "later",
                request,
                candidate,
                capability,
                launch_id="later",
                generation=1,
            )
    with pytest.raises(CampaignError, match="executable|changed|digest"):
        headless.validate_capability(capability, candidate)


def test_admission_decorator_discards_cache_on_failure(prepared, monkeypatch):
    _, _, _, candidate, capability = prepared
    original = headless._validate_capability
    calls = []

    def checked(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(headless, "_validate_capability", checked)

    @headless.capability_admission
    def propose():
        headless.validate_capability(capability, candidate)
        raise CampaignError("proposal rejected")

    for _ in range(2):
        with pytest.raises(CampaignError, match="proposal rejected"):
            propose()
    assert len(calls) == 2
