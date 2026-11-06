"""Normal supervisor delivery exercises actual recruitment and merge executors."""

import json
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_campaign_merge_adapter import source
from test_campaign_runtime import production_case as production_case
from test_campaign_runtime import run_case

from bmad_loop import campaign_runtime
from bmad_loop.campaign_github import UncertainMutation


def configure(case, github):
    _, snapshot = campaign_runtime.create(
        case.repo,
        replace(case.snapshot.spec, name="publication-extension", units=(), github=github),
    )
    unit = snapshot.units["source:project:dependent"]
    unit.spec = replace(unit.spec, after=("source:project:first",))
    snapshot.graph_revision += 1
    case.snapshot = case.store.save(snapshot, snapshot.revision)


def test_runtime_recruits_configured_users_and_teams_once_after_lost_ack(
    production_case, monkeypatch
):
    case = production_case
    configure(
        case,
        replace(
            case.snapshot.spec.github, review_users=("additional",), review_teams=("security",)
        ),
    )
    original = case.service.request
    requests = {}
    observed = set()

    def request(method, path, payload=None):
        route = urlsplit(path).path.removeprefix("repos/a/b/")
        parts = route.split("/")
        if len(parts) == 3 and parts[0] == "pulls":
            number = int(parts[1])
            pr = case.service.pulls[number]
            key = (number, case.service._pull(number)["head"]["sha"])
            if parts[2] == "requested_reviewers" and method == "POST":
                assert key not in requests
                requests[key] = payload
                pr["requested_reviewers"] = [{"login": "additional"}]
                pr["requested_teams"] = [{"slug": "security"}]
                raise UncertainMutation("lost recruitment acknowledgement")
            if parts[2] == "requested_reviewers" and method == "GET" and key in requests:
                result = original(method, path, payload)
                observed.add(key)
                return result
            if parts[2] == "reviews" and method == "GET" and key in observed:
                pr["requested_reviewers"] = []
                pr["requested_teams"] = []
        return original(method, path, payload)

    monkeypatch.setattr(case.service, "request", request)
    result = run_case(case)
    assert result.phase == "delivered"
    assert {number for number, _ in requests} == {
        unit.bindings["pr"]["number"] for unit in result.units.values()
    }
    for unit in result.units.values():
        assert unit.evidence["review_recruitment"]
        assert unit.bindings["recruited_head"] == unit.evidence["integrated"]["head"]


def test_runtime_project_executor_is_actual_merger_and_never_native_fallback(
    production_case, tmp_path, monkeypatch
):
    case = production_case
    operations = tmp_path / "executor-operations.jsonl"
    delivered = tmp_path / "executor-merges"
    delivered.mkdir()
    script = tmp_path / "actual-merger.py"
    script.write_text(
        """import argparse,json,subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--bmad-campaign-request');p.add_argument('--bmad-campaign-result');a=p.parse_args()
r=json.loads(Path(a.bmad_campaign_request).read_text());c=r['candidate']
def git(*args):return subprocess.check_output(['git','-C',REMOTE,*args],text=True).strip()
with Path(LOG).open('a') as f:f.write(json.dumps({'operation':r['operation'],'request_id':r['request_id']})+'\\n')
record=Path(DELIVERED)/(r['request_id']+'.json')
if r['operation']=='merge-submit':
 assert not record.exists()
 assert git('rev-parse','main')==c['base']
 tree=git('merge-tree','--write-tree',c['base'],c['head']).splitlines()[0]
 sha=git('-c','user.name=Project merger','-c','user.email=fixture@example.test','commit-tree',tree,'-p',c['base'],'-p',c['head'],'-m','Maintained project merge')
 git('update-ref','refs/heads/main',sha,c['base'])
 record.write_text(json.dumps({'number':c['pr_number'],'sha':sha,'base':c['base']}))
# Deliberately lose the submit acknowledgement. Reconciliation never re-submits.
if r['operation']=='merge-reconcile':Path(a.bmad_campaign_result).write_text(json.dumps({**r,'ready':True,'submission':'pending'}))
""".replace("REMOTE", repr(str(case.remote)))
        .replace("LOG", repr(str(operations)))
        .replace("DELIVERED", repr(str(delivered)))
    )
    gate = tmp_path / "gate.py"
    gate.write_text(source())
    configure(
        case,
        replace(
            case.snapshot.spec.github,
            merge_mode="project-command",
            merge_command=(sys.executable, str(gate)),
            merge_executor=(sys.executable, str(script)),
        ),
    )
    original = case.service.request

    def request(method, path, payload=None):
        assert not (
            method == "PUT" and path.endswith("/merge-async")
        ), "configured executor must be the only submitter"
        for record in delivered.glob("*.json"):
            row = json.loads(record.read_text())
            pr = case.service.pulls[row["number"]]
            pr.update(merged=True, state="closed", merge_commit_sha=row["sha"])
            case.service.merge_bases[row["number"]] = row["base"]
        return original(method, path, payload)

    monkeypatch.setattr(case.service, "request", request)
    result = run_case(case)
    assert result.phase == "delivered"
    calls = [json.loads(line) for line in operations.read_text().splitlines()]
    submits = [row for row in calls if row["operation"] == "merge-submit"]
    assert len(submits) == 3 and len({row["request_id"] for row in submits}) == 3
    assert all(
        unit.bindings["project_merge_intent"]["kind"] == "project_merge"
        for unit in result.units.values()
    )


@pytest.mark.parametrize("malformed", [False, True])
def test_normal_review_cannot_close_an_unrun_required_criterion(
    production_case, monkeypatch, malformed
):
    case = production_case
    script = Path(case.events.parent) / "scripted-coding-cli.py"
    text = script.read_text()
    text = (
        text.replace(
            '"command_indexes":list(range(len(proof["commands"])))', '"command_indexes":[99]'
        )
        if malformed
        else text.replace(
            '"status":"passed","verification_launch_id"',
            '"status":"not_run","verification_launch_id"',
        )
    )
    script.write_text(text)
    original = case.runtime.proposals

    class ExpectedAcceptanceHold(Exception):
        pass

    def proposals(snapshot):
        if any(
            unit.hold and unit.hold.kind in {"review_findings", "invalid_stage_result"}
            for unit in snapshot.units.values()
        ):
            raise ExpectedAcceptanceHold
        return original(snapshot)

    monkeypatch.setattr(case.runtime, "proposals", proposals)
    with pytest.raises(ExpectedAcceptanceHold):
        run_case(case)
    snapshot = case.store.load(case.snapshot.id)
    held = [unit for unit in snapshot.units.values() if unit.hold]
    assert held
    if not malformed:
        assert all(not unit.evidence["acceptance"]["current"]["complete"] for unit in held)
    assert not case.service.merge_requests
