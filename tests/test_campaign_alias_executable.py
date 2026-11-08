"""Nested native alias proof never transfers a writer permission grant."""

from pathlib import Path

import pytest

from bmad_loop import campaign_execution_sandbox
from bmad_loop.campaign_execution_qualification import launcher_text
from bmad_loop.campaign_headless import file_digest
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_qualification import validate_alias_executable


def binding(tmp_path):
    native = tmp_path / "native"
    native.write_text("exact-native-version")
    launcher = tmp_path / "wrapper"
    launcher.write_text(launcher_text())
    wrapper = Path(campaign_execution_sandbox.__file__).resolve()
    original = {"binary": str(native), "binary_sha256": file_digest(native)}
    capability = {
        "binary": str(launcher),
        "binary_sha256": file_digest(launcher),
        "permissions": "workspace-tools",
        "argv": [
            str(launcher),
            "--grants",
            "{grants_path}",
            "--",
            str(native),
            "--model",
            "{model}",
        ],
        "dependencies": [
            {"path": str(path), "sha256": file_digest(path)} for path in (native, wrapper)
        ],
    }
    return original, capability


def test_exact_nested_binary_can_reuse_alias_identity_without_qualification(tmp_path):
    original, capability = binding(tmp_path)
    validate_alias_executable(original, capability)
    assert "qualification" not in capability


@pytest.mark.parametrize(
    "change", ["native", "wrapper", "missing-dependency", "other-command", "no-tools"]
)
def test_nested_alias_refuses_any_unbound_executable_or_permission_route(tmp_path, change):
    original, capability = binding(tmp_path)
    if change == "native":
        Path(original["binary"]).write_text("different-native-version")
    elif change == "wrapper":
        Path(capability["binary"]).write_text("unsupported-wrapper")
    elif change == "missing-dependency":
        capability["dependencies"] = []
    elif change == "other-command":
        capability["argv"][4] = "/unbound/native"
    else:
        capability["permissions"] = "no-tools"
    with pytest.raises(CampaignError, match="exact nested executable"):
        validate_alias_executable(original, capability)
