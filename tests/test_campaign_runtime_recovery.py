"""A real child rejection recovers a published checkpoint and re-arms itself."""

import json
from dataclasses import replace
from pathlib import Path

from conftest import git
from test_campaign_runtime import diagnose
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_runtime, verify
from bmad_loop.campaign_model import UnitState

RETRY_SESSION = r"""
accepted_execution=execution
def execution(request, session):
    workspace=Path(request["workspace"])
    marker=MARKERS/"published-rejected-checkpoint.json"
    assert envelope_for_test.policy.scm.rollback_on_failure is True
    assert "Campaign backup contract" in session.prompt
    if not marker.exists():
        MARKERS.mkdir(exist_ok=True)
        baseline=verify.rev_parse_head(workspace)
        product=workspace/"src"/"first.txt"
        product.parent.mkdir(exist_ok=True)
        product.write_text("rejected incomplete attempt\n")
        verify.commit_paths(workspace,"checkpoint: rejected first attempt",[product])
        checkpoint=verify.rev_parse_head(workspace)
        deadline=time.monotonic()+20
        while True:
            remote=workers.git(workspace,"ls-remote","--heads","--",request["remote"],"refs/heads/"+request["branch"])
            if remote.partition("\t")[0]==checkpoint:
                break
            if time.monotonic()>deadline:
                raise RuntimeError("coordinator never published the live child checkpoint")
            threading.Event().wait(.01)
        marker.write_text(json.dumps({"head":checkpoint,"baseline":baseline,"run_id":request["run_id"]}))
        event("rejected-checkpoint-published",request)
        # Native source remains ready, so the real deterministic verifier
        # rejects this completed session as a non-fixable attempt.
        return SessionResult(status="completed",session_id="",result_json={"workflow":"auto-dev","story_key":"first","tasks_total":1,"tasks_done":1,"verification":[],"escalations":[]})
    rejected=json.loads(marker.read_text())
    current=verify.rev_parse_head(workspace)
    assert verify.is_ancestor(workspace,rejected["head"],current), "published history was rewound"
    assert not (workspace/"src"/"first.txt").exists(), "rejected work was not compensated before re-arm"
    remote=workers.git(workspace,"ls-remote","--heads","--",request["remote"],"refs/heads/"+request["branch"])
    assert remote.partition("\t")[0]==current, "recovery re-armed before its descendant push confirmed"
    (MARKERS/"rearmed-checkpoint.json").write_text(json.dumps({"head":current,"run_id":request["run_id"]}))
    event("rearmed-after-recovery",request)
    return accepted_execution(request,session)
"""


def configure_recovery_canary(case, *, guard_patch=""):
    policy = case.repo / ".bmad-loop/policy.toml"
    policy.write_text(
        policy.read_text(encoding="utf-8") + "\n[scm]\nrollback_on_failure=true\n", encoding="utf-8"
    )
    git(case.repo, "add", ".bmad-loop/policy.toml")
    git(case.repo, "commit", "-qm", "qualify inherited campaign rollback policy")
    git(case.repo, "push", "origin", "main")
    spec = replace(
        case.snapshot.spec,
        name="real-child-append-only-recovery",
        sources=(replace(case.snapshot.spec.sources[0], units=("first",)),),
        units=(),
        ownership=tuple(
            row for row in case.snapshot.spec.ownership if row[0] == "source:project:first"
        ),
        checkpoints=replace(case.snapshot.spec.checkpoints, target_interval_s=1),
    )
    _, case.snapshot = campaign_runtime.create(case.repo, spec)
    script = case.service.state_file.parent / "scripted-coding-cli.py"
    text = script.read_text(encoding="utf-8").replace(
        'if suffix in {"first","second"}:', "if False:"
    )
    text = text.replace(
        "def adapters(request,envelope,project,run_dir,policy,**kwargs):",
        RETRY_SESSION
        + "\ndef adapters(request,envelope,project,run_dir,policy,**kwargs):\n    global envelope_for_test\n    envelope_for_test=envelope",
    )
    text = text.replace("workers._adapters=adapters", guard_patch + "\nworkers._adapters=adapters")
    script.write_text(text, encoding="utf-8")


def test_real_child_retry_after_pushed_checkpoint_recovers_rearms_and_pushes_fast_forward(
    production_case,
):
    case = production_case
    configure_recovery_canary(case)
    result = run_case(case, ticks=2200)
    marker_root = case.events.parent / "parallel-markers"
    rejected = json.loads(
        (marker_root / "published-rejected-checkpoint.json").read_text(encoding="utf-8")
    )
    workspace = Path(result.units["source:project:first"].bindings["workspace"])
    assert verify.is_ancestor(
        workspace, rejected["head"], verify.rev_parse_head(workspace)
    ), "published checkpoint was rewound"
    assert result.phase == "delivered", diagnose(result)
    unit = result.units["source:project:first"]
    assert unit.state == UnitState.DELIVERED
    rearmed = json.loads((marker_root / "rearmed-checkpoint.json").read_text(encoding="utf-8"))
    history = unit.evidence["worker_history"]
    assert any(row["outcome"] == "recovery_required" for row in history)
    recovery = next(row for row in history if row["outcome"] == "recovered")
    assert recovery["recovery"]["state"] == "complete"
    assert recovery["recovery"]["head"] == rearmed["head"]
    assert rejected["run_id"] != rearmed["run_id"]
    canonical = git(case.remote, "rev-parse", "refs/heads/main")
    assert verify.is_ancestor(case.remote, rejected["head"], rearmed["head"])
    assert verify.is_ancestor(case.remote, rearmed["head"], unit.evidence["integrated"]["head"])
    assert verify.is_ancestor(case.remote, unit.evidence["integrated"]["head"], canonical)
    assert unit.evidence["dev_attempts_spent"] == 2
    assert git(case.remote, "show", canonical + ":src/first.txt") == "accepted first"
    assert len(case.service.pulls) == 1
