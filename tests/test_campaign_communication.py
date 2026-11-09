"""Peer awareness cannot become execution ownership or completion authority."""

import json
from dataclasses import replace
from subprocess import CompletedProcess

from bmad_loop.campaign_communication import HcomObserver, PeerBinding, sibling_context


def binding(room):
    return PeerBinding("campaign", "unit", 1, "nava", "process", "session", "terminal", str(room))


def observer(tmp_path, events=(), *, reused=False):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "list":
            value = json.dumps(
                [
                    {
                        "name": "nava",
                        "session_id": "wrong" if reused else "session",
                        "launch_context": {
                            "process_id": "process",
                            "env": {"SUPERSET_TERMINAL_ID": "terminal"},
                        },
                        "status": "listening",
                    }
                ]
            )
        else:
            value = "\n".join(json.dumps(event) for event in events)
        return CompletedProcess(argv, 0, value, "")

    return (
        HcomObserver(room=tmp_path, observer_name="tina", executable="/fake/hcom", runner=runner),
        calls,
    )


def test_observation_scoped_exact_binding_and_no_send(tmp_path):
    obs, calls = observer(tmp_path, [{"id": 2, "type": "status"}])
    result = obs.events(binding(tmp_path), cursor=1)
    assert result.available and result.cursor == 2
    assert all("--name" in call and call[-1] == "tina" for call in calls)
    assert calls[1][1:4] == ["events", "--agent", "nava"]
    assert not any(v in call for call in calls for v in ("send", "sub", "kill", "listen"))
    preflight = obs.preflight()
    assert not preflight["automatic_peer_delivery"]
    assert not preflight["incoming_hooks_muted"]


def test_reused_actor_name_never_adopted(tmp_path):
    obs, calls = observer(tmp_path, reused=True)
    result = obs.events(binding(tmp_path))
    assert not result.available
    assert len(calls) == 1


def test_room_mismatch_visible(tmp_path):
    obs, _ = observer(tmp_path)
    result = obs.roster((replace(binding(tmp_path), room=str(tmp_path / "foreign")),))
    assert not result.available and "another project room" in result.detail


def test_reset_does_not_advance_saved_cursor(tmp_path):
    obs, _ = observer(tmp_path, [{"id": 1, "type": "status"}])
    result = obs.events(binding(tmp_path), cursor=40)
    assert result.reset and not result.available
    assert result.cursor == 0


def test_truncated_event_stream_visible(tmp_path):
    obs, _ = observer(tmp_path, [{"truncated": True}])
    result = obs.events(binding(tmp_path), cursor=40)
    assert not result.available and result.cursor == 40


def test_sibling_context_has_links_and_no_delivery_authority():
    result = sibling_context(
        [{"id": "self"}, {"id": "other", "state": "locally_complete", "pr_url": "PR"}],
        own_unit="self",
        campaign_id="campaign",
    )
    assert result["advisory_only"]
    assert result["siblings"][0]["state"] == "locally_complete"
    assert result["siblings"][0]["pr_url"] == "PR"


def test_historical_actor_events_do_not_become_current_peer_observations(tmp_path):
    obs, _ = observer(
        tmp_path,
        [{"id": 1, "data": {"session": "old-session"}}, {"id": 2, "data": {"session": "session"}}],
    )
    result = obs.events(binding(tmp_path))
    assert result.available and result.cursor == 2
    assert [event["id"] for event in result.events] == [2]


def test_missing_native_event_attribution_is_visible(tmp_path):
    obs, _ = observer(tmp_path, [{"id": 1, "type": "status"}])
    result = obs.events(binding(tmp_path))
    assert result.available and result.events == ()
    assert "lack native session attribution" in result.detail
