"""Explicit handoff requests queue only independently qualified owner inputs."""

from pathlib import Path

from test_campaign_cli import runtime as runtime

from bmad_loop import campaign_adoption, cli


def test_adoption_cli_selects_installed_observer_and_queues_qualified_payload(
    runtime, tmp_path, monkeypatch, capsys
):
    from bmad_loop import campaign_runtime

    store, snapshot, _ = runtime
    gateway, calls = object(), []
    monkeypatch.setattr(
        campaign_runtime.CampaignRuntime, "gateway", lambda *args: gateway, raising=False
    )
    manifest = tmp_path / "adoption.json"
    payload = {
        "manifest": {"unit_id": "s:e:u"},
        "provider": {"name": "handoff"},
        "qualification": {"head": "H"},
    }

    def qualify(project, saved, unit, path, observer, *, gateway):
        calls.append((saved.id, unit, path, observer, gateway))
        return payload

    monkeypatch.setattr(campaign_adoption, "prepare_request", qualify)
    assert (
        cli.main(
            [
                "campaign",
                "adopt",
                snapshot.id,
                "--unit",
                "s:e:u",
                "--manifest",
                str(manifest),
                "--observer",
                "handoff",
                "--json",
            ]
        )
        == 0
    )
    import json

    output = capsys.readouterr()
    doc = json.loads(output.out)
    assert output.err == "" and doc["command"] == "campaign.adopt"
    request = store.commands(snapshot.id)[0]
    assert request["kind"] == "adopt" and request["payload"]["adoption"] == payload
    assert calls == [(snapshot.id, "s:e:u", Path(manifest), "handoff", gateway)]


def test_adoption_proposal_is_read_only_and_requires_owner_authorization(
    runtime, monkeypatch, capsys
):
    from bmad_loop import campaign_runtime

    store, snapshot, _ = runtime
    monkeypatch.setattr(
        campaign_runtime.CampaignRuntime, "gateway", lambda *args: object(), raising=False
    )
    monkeypatch.setattr(
        campaign_adoption,
        "propose",
        lambda *args, **kwargs: {
            "ready_for_owner_authorization": False,
            "manifest": {"authorized_by": ""},
            "problems": ["actual admission fence unavailable"],
            "instructions": "Review the exact owner handoff first",
        },
    )
    assert (
        cli.main(
            [
                "campaign",
                "adoption-proposal",
                snapshot.id,
                "--unit",
                "s:e:u",
                "--pr",
                "42",
                "--json",
            ]
        )
        == 1
    )
    import json

    output = capsys.readouterr()
    doc = json.loads(output.out)
    assert output.err == "" and doc["proposal"]["manifest"]["authorized_by"] == ""
    assert not store.commands(snapshot.id)


def test_refused_handoff_never_enters_command_inbox(runtime, monkeypatch, capsys):
    from bmad_loop import campaign_runtime

    store, snapshot, _ = runtime
    monkeypatch.setattr(
        campaign_runtime.CampaignRuntime, "gateway", lambda *args: object(), raising=False
    )

    def refuse(*args, **kwargs):
        raise campaign_adoption.AdoptionError("current actor still owns the branch")

    monkeypatch.setattr(campaign_adoption, "prepare_request", refuse)
    assert (
        cli.main(
            [
                "campaign",
                "adopt",
                snapshot.id,
                "--unit",
                "s:e:u",
                "--manifest",
                "owner.json",
                "--json",
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == "" and "current actor still owns" in output.err
    assert not store.commands(snapshot.id)
