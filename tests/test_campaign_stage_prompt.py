"""Large stage context is preserved without exceeding process argument bounds."""

import pytest

from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_stage_prompt import MAX_PACKET_BYTES, instruction, materialize


def test_inventory_larger_than_cli_argv_is_readable_as_exact_packet(tmp_path):
    context = "Requirements and source evidence:\n" + "a" * (2 * 1024 * 1024)
    packet = materialize(tmp_path, context)
    assert packet.path.read_text() == context
    assert packet.size_bytes == len(context.encode())
    assert len(instruction(packet).encode()) < 2048
    assert str(packet.path) in instruction(packet) and packet.sha256 in instruction(packet)
    packet.verify()


def test_reentry_reuses_exact_packet_and_refuses_session_rewrite(tmp_path):
    packet = materialize(tmp_path, "original instruction")
    assert materialize(tmp_path, "original instruction").sha256 == packet.sha256
    packet.path.write_text("original instructioX")
    with pytest.raises(CampaignError, match="changed"):
        packet.verify()
    with pytest.raises(CampaignError, match="changed"):
        materialize(tmp_path, "original instruction")


def test_packet_redirect_and_unbounded_inputs_are_refused(tmp_path):
    other = tmp_path / "other"
    other.write_text("original instruction")
    try:
        (tmp_path / "stage-packet.md").symlink_to(other)
    except OSError:
        pytest.skip("filesystem cannot create symbolic links")
    with pytest.raises(CampaignError, match="safely"):
        materialize(tmp_path, "original instruction")
    with pytest.raises(CampaignError, match="byte bound"):
        materialize(tmp_path, "x" * (MAX_PACKET_BYTES + 1))


def test_stage_worker_delivers_large_packet_through_short_adapter_prompt(
    project, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from test_campaign_workers import envelope, worker_request

    from bmad_loop import campaign_workers as workers

    context = envelope(project.project, tmp_path / "execution")
    context.directory.mkdir()
    calls = []

    class Adapter:
        def run(self, spec):
            calls.append(spec)
            assert len(spec.prompt.encode()) < 2048
            assert str(context.directory / "stage-packet.md") in spec.prompt
            assert "complete inventory" in (context.directory / "stage-packet.md").read_text()
            return SimpleNamespace(
                status="completed",
                result_json={"status": "done"},
                session_id="scripted",
                transcript_path="",
            )

        def read_usage(self, result):
            return None

    monkeypatch.setattr(workers, "_adapters", lambda *args, **kwargs: {"triage": Adapter()})
    request = worker_request(
        project.project,
        context,
        kind="stage",
        role="triage",
        candidate={"profile": "claude"},
        profile_key="triage",
        prompt="complete inventory\n" + "x" * (2 * 1024 * 1024),
        read_only=True,
    )
    result = workers._stage(request, context)
    assert result["outcome"] == "stage_complete" and len(calls) == 1
    assert result["input_packet"]["size_bytes"] > 2 * 1024 * 1024
