"""Project commands add exact-candidate requirements and never become merge authority."""

import sys
from dataclasses import replace

import pytest
from conftest import git
from test_campaign_publication import publishable as publishable

from bmad_loop.campaign_merge_adapter import (
    MergeAdapterError,
    check_project_gate,
    verify_queue_candidate,
)
from bmad_loop.campaign_publication import Candidate


def script(project, source):
    path = project.project / "publication-gate.py"
    path.write_text(source)
    return (sys.executable, str(path))


GENERIC = """import argparse,json
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--bmad-campaign-request')
p.add_argument('--bmad-campaign-result')
a=p.parse_args()
request=json.loads(Path(a.bmad_campaign_request).read_text())
result={**request,'ready':True,'verification':{'commands':[['qualified-test','all']], 'returncodes':[0]}}
{mutation}
Path(a.bmad_campaign_result).write_text(json.dumps(result))
"""


def source(mutation=""):
    return GENERIC.replace("{mutation}", mutation)


def test_real_script_gate_receipt_is_additional_and_native_gates_remain_fresh(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, observation = publishable
    seen = []
    monkeypatch.setattr(controller, "inspect", lambda number: seen.append(number) or observation)
    receipt = check_project_gate(
        script(project, source()),
        project.project,
        tmp_path / "state",
        candidate,
        evidence,
        controller,
        target="main",
        clock=lambda: 100,
    )
    assert receipt.state == "ready" and receipt.candidate == candidate
    assert len(seen) == 2
    assert receipt.rules_fingerprint == observation.rules.fingerprint
    assert receipt.request_digest and receipt.command_digest
    assert not controller.gateway.transport.calls


@pytest.mark.parametrize(
    "mutation",
    [
        "result['candidate']['head']='other'",
        "result['bypass_rules']=True",
        "result['merge_method']='squash'",
        "result['rules_fingerprint']='old'",
    ],
)
def test_generic_command_cannot_bless_stale_candidate_or_waive_policy(
    project, tmp_path, publishable, monkeypatch, mutation
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    with pytest.raises(MergeAdapterError, match="stale coordinates|bypass"):
        check_project_gate(
            script(project, source(mutation)),
            project.project,
            tmp_path / "state",
            candidate,
            evidence,
            controller,
            target="main",
            clock=lambda: 100,
        )
    assert not controller.gateway.transport.calls


def test_exit_zero_without_machine_receipt_does_not_authorize_merge(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    with pytest.raises(MergeAdapterError, match="result missing"):
        check_project_gate(
            script(project, "print('all good, merge now')\n"),
            project.project,
            tmp_path / "state",
            candidate,
            evidence,
            controller,
            target="main",
            clock=lambda: 100,
        )


def test_late_base_and_rule_changes_hold_after_a_successful_project_command(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, observation = publishable
    observations = iter([observation, replace(observation, base_head="new-base")])
    monkeypatch.setattr(controller, "inspect", lambda number: next(observations))
    receipt = check_project_gate(
        script(project, source()),
        project.project,
        tmp_path / "state",
        candidate,
        evidence,
        controller,
        target="main",
        clock=lambda: 100,
    )
    assert receipt.state == "held" and "Canonical target moved" in receipt.reason


def test_missing_native_rule_cannot_be_repaired_by_project_command(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(
        controller,
        "inspect",
        lambda number: replace(observation, rules=replace(observation.rules, strict=False)),
    )
    command = script(project, "raise AssertionError('native gate must refuse before command')\n")
    receipt = check_project_gate(
        command,
        project.project,
        tmp_path / "state",
        candidate,
        evidence,
        controller,
        target="main",
        clock=lambda: 100,
    )
    assert receipt.state == "held" and not receipt.artifact


def test_rem_adapter_forces_dry_run_with_zero_wait_and_reads_machine_record(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    path = project.project / "merge_pr.py"
    path.write_text("""import argparse,json
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('pr',type=int)
for name in ['repo','base','head','wait-min','poll-s','log']:p.add_argument('--'+name)
p.add_argument('--dry-run',action='store_true')
a=p.parse_args()
assert a.dry_run and a.wait_min=='0' and a.poll_s=='0' and a.base=='main'
Path(a.log).with_name('merges.jsonl').write_text(json.dumps({'pr':a.pr,'repo':a.repo,'head':a.head,'dry_run':True,'rc':0,'outcome':'dry-run'})+'\\n')
""")
    receipt = check_project_gate(
        (sys.executable, str(path)),
        project.project,
        tmp_path / "state",
        candidate,
        evidence,
        controller,
        target="main",
        clock=lambda: 100,
    )
    assert receipt.state == "ready"
    assert not controller.gateway.transport.calls


@pytest.mark.parametrize("flag", ["--admin", "--bypass-rules=true", "--squash", "--rebase"])
def test_unsafe_command_flags_are_refused_even_with_native_rules(
    project, tmp_path, publishable, monkeypatch, flag
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    command = (*script(project, source()), flag)
    with pytest.raises(MergeAdapterError, match="bypass/history"):
        check_project_gate(
            command,
            project.project,
            tmp_path / "state",
            candidate,
            evidence,
            controller,
            target="main",
            clock=lambda: 100,
        )


def queue(project):
    base = git(project.project, "rev-parse", "HEAD").strip()
    (project.project / "candidate.py").write_text("accepted\n")
    git(project.project, "add", "candidate.py")
    git(project.project, "commit", "-qm", "head candidate")
    head = git(project.project, "rev-parse", "HEAD").strip()
    (project.project / "combined.py").write_text("queue combined candidate\n")
    git(project.project, "add", "combined.py")
    git(project.project, "commit", "-qm", "combined group")
    group = git(project.project, "rev-parse", "HEAD").strip()
    return Candidate("a/b", "unit", 1, head, base, "source", 1, "config"), group


def test_real_queue_command_is_bound_to_combined_g_and_needs_hosted_check(project, tmp_path):
    candidate, group = queue(project)
    receipt = verify_queue_candidate(
        script(project, source()),
        project.project,
        tmp_path / "state",
        candidate,
        group_head=group,
        group_base=candidate.base,
        context="bmad-combined",
        rules_fingerprint="rules",
    )
    assert receipt.state == "ready" and receipt.group_head == group
    assert receipt.hosted_check_pending is True
    assert receipt.context == "bmad-combined"


def test_head_only_or_wrong_group_receipt_never_proves_combined_candidate(project, tmp_path):
    candidate, group = queue(project)
    with pytest.raises(MergeAdapterError, match="exact combined"):
        verify_queue_candidate(
            script(project, source()),
            project.project,
            tmp_path / "state",
            candidate,
            group_head=candidate.head,
            group_base=candidate.base,
            context="combined",
            rules_fingerprint="rules",
        )
    with pytest.raises(MergeAdapterError, match="exact G/B"):
        verify_queue_candidate(
            script(project, source("result['group']['head']=request['candidate']['head']")),
            project.project,
            tmp_path / "state",
            candidate,
            group_head=group,
            group_base=candidate.base,
            context="combined",
            rules_fingerprint="rules",
        )


def test_merge_gate_cannot_modify_checkout_and_still_authorize_publication(
    project, tmp_path, publishable, monkeypatch
):
    controller, candidate, evidence, observation = publishable
    monkeypatch.setattr(controller, "inspect", lambda number: observation)
    with pytest.raises(MergeAdapterError, match="changed the candidate"):
        check_project_gate(
            script(project, source("Path('new-code.py').write_text('unexpected')")),
            project.project,
            tmp_path / "state",
            candidate,
            evidence,
            controller,
            target="main",
            clock=lambda: 100,
        )
