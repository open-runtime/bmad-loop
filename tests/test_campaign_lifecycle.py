"""Only owned ended views retire; no terminal, worktree or history is removed."""

import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest
from test_campaign_reflection_flow import flow as flow

from bmad_loop.campaign_lifecycle import CampaignLifecycle, LifecycleError, LifecyclePolicy
from bmad_loop.campaign_model import Hold, UnitState


@pytest.fixture
def lifecycle(flow, tmp_path):
    snapshot, unit, claim, binding, run_dir, mux = flow
    claim.quiescent = True
    claim.role = "publication_review"
    unit.state = UnitState.DELIVERED
    log = run_dir / "logs" / "task.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"Retained worker output\n")
    mux.root.mkdir(parents=True, exist_ok=True)
    (mux.root / "journal.jsonl").write_text(
        json.dumps({"event": "pane-attach-requested", "id": "terminal", "pane": "pane"}) + "\n"
    )
    mux.status = lambda **_: {
        "windows": [
            {
                "id": "terminal",
                "workspace": "workspace",
                "session": "real-session",
                "state": "disposed",
                "paneId": "pane",
                "native": {"agentSessionId": "native-session"},
            }
        ]
    }
    mux._daemon = lambda: {}
    mux._rpc = lambda *args: {"agentSessionId": "native-session", "endedAt": 123}

    class Host:
        def __init__(self):
            self.layout = {
                "tabs": [
                    {
                        "id": "tab",
                        "panes": [{"id": "pane", "terminalId": "terminal", "type": "terminal"}],
                    }
                ]
            }
            self.closes = []
            self.unknown = False

        def cli(self, args):
            if args[:2] == ["panes", "list"]:
                return copy.deepcopy(self.layout)
            assert args[:2] == ["panes", "close"] and args[-1] == "--keep-terminal"
            self.closes.append(args)
            if self.unknown:
                raise TimeoutError
            self.layout["tabs"][0]["panes"] = []
            return {}

    host = Host()
    controller = SimpleNamespace(
        available=lambda: True,
        observe=lambda _: {"complete": True, "protected": False, "revision": "controller-revision"},
    )
    provider = SimpleNamespace(
        host=host, _rows=lambda _: [{"id": "workspace"}], _verify=lambda *args: binding
    )
    bridge = CampaignLifecycle(
        tmp_path / "lifecycle", mux=mux, provider=provider, controller=controller
    )
    bridge.observe(snapshot, unit, run_directories=(run_dir,))
    return bridge, snapshot, unit, mux, host, controller, log


def test_completed_owned_view_retirement_preserves_native_history_and_checkout(lifecycle):
    bridge, snapshot, unit, _, host, _, log = lifecycle
    receipt = bridge.retire(snapshot, unit, "retire", policy=LifecyclePolicy(retention=0))
    assert receipt["state"] == "confirmed" and len(host.closes) == 1
    assert receipt["git_data_retained"] and receipt["worktree_retained"]
    assert receipt["workspace_retained"] and log.read_bytes() == b"Retained worker output\n"
    assert receipt["latest_completed_phase"] == "publication_review"
    bridge.retire(snapshot, unit, "retire", policy=LifecyclePolicy(retention=0))
    assert len(host.closes) == 1


def test_default_retention_keeps_recent_tabs(lifecycle):
    bridge, snapshot, unit, _, host, _, _ = lifecycle
    receipt = bridge.retire(snapshot, unit, "retire", policy=LifecyclePolicy())
    assert receipt["state"] == "confirmed" and receipt["retained_stages"] == 1
    assert host.closes == []


@pytest.mark.parametrize("obstacle", ["held", "live", "residual-shell", "controller", "protected"])
def test_retirement_never_closes_held_live_unknown_or_operator_sessions(lifecycle, obstacle):
    bridge, snapshot, unit, mux, host, controller, _ = lifecycle
    policy = LifecyclePolicy(retention=0)
    if obstacle == "held":
        unit.hold = Hold("owner", "Inspect this work")
    elif obstacle == "live":
        unit.claim.quiescent = False
    elif obstacle == "residual-shell":
        mux._daemon = lambda: {"terminal": {"pid": 99}}
    elif obstacle == "controller":
        controller.observe = lambda _: {"complete": False, "protected": True}
    else:
        policy = dataclasses.replace(policy, scope="protected")
    with pytest.raises(LifecycleError):
        bridge.retire(snapshot, unit, "retire", policy=policy)
    assert host.closes == []


def test_human_sidebar_move_is_preserved(lifecycle):
    bridge, snapshot, unit, _, host, _, _ = lifecycle
    host.layout["sidebar"] = {"tabs": host.layout["tabs"]}
    host.layout["tabs"] = []
    with pytest.raises(LifecycleError, match="Operator changed"):
        bridge.retire(snapshot, unit, "retire", policy=LifecyclePolicy(retention=0))
    assert host.closes == []


def test_unknown_close_is_observed_without_repeating_dispatch(lifecycle):
    bridge, snapshot, unit, _, host, _, _ = lifecycle
    host.unknown = True
    receipt = bridge.retire(snapshot, unit, "retire", policy=LifecyclePolicy(retention=0))
    assert receipt["state"] == "unknown" and len(host.closes) == 1
    again = bridge.retire(snapshot, unit, "retire", policy=LifecyclePolicy(retention=0))
    assert again["state"] == "unknown" and len(host.closes) == 1
    host.layout["tabs"][0]["panes"] = []
    assert (
        bridge.retire(snapshot, unit, "retire", policy=LifecyclePolicy(retention=0))["state"]
        == "confirmed"
    )
    assert len(host.closes) == 1


def test_custom_native_registry_cannot_fall_back_to_another_transport(lifecycle, tmp_path):
    bridge, snapshot, unit, mux, host, _, _ = lifecycle
    assert bridge.preflight(LifecyclePolicy())["native_state_root"] == str(mux.root)
    mux.root = tmp_path / "different-native-registry"
    with pytest.raises(LifecycleError, match="state root changed"):
        bridge.retire(snapshot, unit, "retire", policy=LifecyclePolicy(retention=0))
    assert host.closes == []


def test_observe_mode_missing_hcom_reader_is_visible(lifecycle):
    from bmad_loop.campaign_model import CommunicationSpec

    bridge, snapshot, unit, _, _, _, log = lifecycle
    snapshot.spec = dataclasses.replace(
        snapshot.spec, communication=CommunicationSpec(mode="observe")
    )
    observed = bridge.observe(snapshot, unit, run_directories=(log.parent.parent,))
    assert not observed["communication"]["available"]
    assert observed["communication"]["detail"] == "hcom observer not configured"
