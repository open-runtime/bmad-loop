"""Explicit native writer canary in a newly owned, disposable Git fixture.

The model edits and commits one scoped file. Independent readback checks the
commit and preserved protected bytes/refs; a deterministic descendant probe
exercises the same OS sandbox and proves refused writes. No inference runs in
normal admission or in repository tests.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import verify
from .campaign_jobs import JobLimits, JobRequest, digest, request_from_dict
from .campaign_model import CampaignError, RoleCandidate
from .platform_util import is_link_like

KIND = "workspace-tools-v1"
BRANCH = "bmad-qualification"
CONTENT = "native writer qualification: "
SCHEMA = {
    "type": "object",
    "properties": {
        "nonce": {"type": "string"},
        "markers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["nonce", "markers"],
    "additionalProperties": False,
}
PROMPT = (
    "This is an explicitly authorized disposable native writer qualification. "
    "Use your file and shell tools to replace src/owned.txt with the exact line "
    "given in the immutable source, including its final newline, then git add only "
    "src/owned.txt and git commit -m 'native writer qualification'. "
    "Do not change any other file or branch, run background jobs, or use network tools. "
    "Return JSON with the exact nonce and all CANARY values in numeric order from the source. Completion is verified independently."
)


def _git(directory, *args):
    result = verify.git_bytes(directory, *args, timeout_s=30)
    if result.returncode:
        raise CampaignError(
            "Writer qualification Git operation failed: " + result.stderr.decode("utf-8", "replace")
        )
    return result.stdout.decode().strip()


def launcher_text():
    """Exact managed-interpreter launcher; preserve virtualenv dependency resolution."""
    return (
        f"#!{sys.executable}\n"
        "from bmad_loop.campaign_execution_sandbox import main\n"
        "raise SystemExit(main())\n"
    )


def validate_proposal(capability, candidate):
    from . import campaign_execution_sandbox
    from .campaign_headless import _validate_capability, file_digest

    if capability.get("qualification", {}).get("status") == "passed":
        raise CampaignError("Writer probe proposal must not claim passed qualification")
    _validate_capability(
        capability, candidate, permissions="workspace-tools", qualification_required=False
    )
    argv = capability["argv"]
    wrapper = Path(campaign_execution_sandbox.__file__).resolve()
    launcher = Path(capability["binary"])
    prefix = [str(launcher), "--grants", "{grants_path}", "--"]
    if (
        argv[:4] != prefix
        or len(argv) <= 4
        or not Path(argv[4]).is_absolute()
        or launcher.read_text(encoding="utf-8") != launcher_text()
    ):
        raise CampaignError("Writer qualification requires the exact scoped sandbox wrapper")
    dependencies = {row["path"]: row["sha256"] for row in capability.get("dependencies", [])}
    for path in (wrapper, Path(argv[4]), Path(sys.executable).resolve()):
        if dependencies.get(str(path)) != file_digest(path):
            raise CampaignError(
                "Writer sandbox/native executable lacks an exact dependency binding"
            )


def build(
    directory, candidate, capability, *, input_bytes=16384, output_tokens=8192, timeout_s=300
):
    from .campaign_headless import file_digest, write_json

    directory = Path(directory).absolute()
    if is_link_like(directory) or (directory.exists() and any(directory.iterdir())):
        raise CampaignError("Writer qualification requires a new empty directory")
    if not 2048 <= input_bytes <= 1024 * 1024 or not 1 <= timeout_s <= 3600:
        raise CampaignError("Writer qualification limits are invalid")
    validate_proposal(capability, candidate)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    repo, workspace = directory / "repository", directory / "workspace"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "BMAD qualification")
    _git(repo, "config", "user.email", "qualification@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.hooksPath", str(directory / "empty-hooks"))
    (repo / "src").mkdir()
    (repo / "src/owned.txt").write_text("initial\n", encoding="utf-8")
    (repo / "src/sibling.txt").write_text("protected sibling\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "immutable qualification baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    _git(repo, "worktree", "add", "-b", BRANCH, str(workspace))
    outside = directory / "outside.txt"
    outside.write_text("protected outside\n", encoding="utf-8")
    nonce = secrets.token_hex(16)
    from .campaign_qualification import canary_source

    markers = [secrets.token_hex(16) for _ in range(min(64, max(8, input_bytes // 512)))]
    text = f"Exact src/owned.txt line: {CONTENT}{nonce}\n" + canary_source(
        nonce, markers, input_bytes
    )
    source = directory / "source.txt"
    source.write_text(text, encoding="utf-8")
    grants = {
        "schema_version": 1,
        "workspace": str(workspace),
        "scopes": ["src/owned.txt"],
        "config_digest": digest([KIND, nonce]),
        "git_branch": BRANCH,
        "protected_branches": ["main"],
        "runtime_write_paths": capability.get("runtime_write_paths", []),
    }
    probe = {
        "kind": KIND,
        "nonce": nonce,
        "markers": markers,
        "source_bytes": input_bytes,
        "root": str(directory),
        "baseline": baseline,
        "source_sha256": file_digest(source),
        "grants": grants,
        "protected": {
            str(path): file_digest(path)
            for path in (outside, repo / "src/sibling.txt", workspace / "src/sibling.txt")
        },
    }
    request = JobRequest(
        "writer-qualification-" + nonce,
        "report",
        "qualification",
        {"graph_revision": 1, "config_revision": 1, "head": baseline, "base": baseline},
        (
            {
                "source_id": "fixed-writer-canary",
                "path": str(source),
                "sha256": file_digest(source),
                "tokens": len(text.encode()),
            },
        ),
        PROMPT,
        SCHEMA,
        JobLimits(capability["input_tokens"], output_tokens, capability["max_turns"], timeout_s),
        candidates=(candidate,),
        required=False,
    )
    probe["request_digest"] = digest(asdict(request))
    write_json(directory / "fixture.json", probe)
    return request, probe, grants


def validate_request(request, probe):
    from .campaign_headless import file_digest

    root = Path(probe["root"])
    from .campaign_qualification import canary_source

    expected_text = f"Exact src/owned.txt line: {CONTENT}{probe['nonce']}\n" + canary_source(
        probe["nonce"], probe["markers"], probe["source_bytes"]
    )
    if (
        probe.get("kind") != KIND
        or not root.is_absolute()
        or is_link_like(root)
        or request.id != "writer-qualification-" + probe["nonce"]
        or request.prompt != PROMPT
        or request.output_schema != SCHEMA
        or request.role != "qualification"
        or request.purpose != "report"
        or request.required
        or request.unit
        or request.dependencies
        or len(request.candidates) != 1
        or digest(asdict(request)) != probe.get("request_digest")
        or len(request.inputs) != 1
        or request.inputs[0]["source_id"] != "fixed-writer-canary"
        or request.inputs[0]["path"] != str(root / "source.txt")
        or request.inputs[0]["sha256"] != file_digest(root / "source.txt")
        or probe["source_sha256"] != file_digest(root / "source.txt")
        or (root / "source.txt").read_text(encoding="utf-8") != expected_text
        or probe["grants"]["workspace"] != str(root / "workspace")
        or probe["grants"]["scopes"] != ["src/owned.txt"]
        or probe["grants"]["git_branch"] != BRANCH
        or probe["grants"].get("schema_version") != 1
        or probe["grants"].get("config_digest") != digest([KIND, probe["nonce"]])
        or probe["grants"].get("protected_branches") != ["main"]
        or probe["grants"].get("write_paths")
        or probe["grants"].get("read_only_paths")
    ):
        raise CampaignError("Writer canary request differs from its fixed fixture")


def validate_envelope(envelope):
    probe = envelope["probe"]
    if (
        digest(probe) != envelope.get("probe_digest")
        or envelope.get("permissions") != "workspace-tools"
        or envelope.get("grants") != probe["grants"]
        or envelope.get("environment")
        or probe["grants"].get("runtime_write_paths", [])
        != envelope["capability"].get("runtime_write_paths", [])
        or Path(envelope["directory"]) != Path(probe["root"]) / "job"
        or envelope["workspace"] != probe["grants"]["workspace"]
    ):
        raise CampaignError("Writer canary envelope differs from fixed grants")
    validate_request(request_from_dict(envelope["request"]), probe)
    validate_proposal(envelope["capability"], RoleCandidate(**envelope["selection"]))


def containment(envelope):
    """Prove exact sandbox grants deny protected writes, independently of prose."""
    from .campaign_execution_sandbox import command

    root = Path(envelope["probe"]["root"])
    # This deterministic descendant runs under the identical frozen grants and
    # wrapper policy used by the native CLI. The report is captured by its parent.
    code = """import json,sys
from pathlib import Path
checks={}
for name,path in [('sibling',Path('src/sibling.txt')),('outside',Path(sys.argv[1])),('target_ref',Path(sys.argv[2]))]:
 try: path.write_text('forbidden change')
 except PermissionError: checks[name]=True
 else: checks[name]=False
print(json.dumps(checks))
"""
    argv, cwd, env = command(
        root / "job/grants.json",
        [
            str(Path(sys.executable).resolve()),
            "-c",
            code,
            str(root / "outside.txt"),
            str(root / "repository/.git/refs/heads/main"),
        ],
    )
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise CampaignError("Writer containment probe did not complete")
    return json.loads(result.stdout)


def result(path, observation):
    from .campaign_headless import file_digest, read_json, write_json

    envelope = read_json(path)
    validate_envelope(envelope)
    probe, receipt = envelope["probe"], observation.get("receipt", {})
    root, workspace = Path(probe["root"]), Path(probe["grants"]["workspace"])
    finished = (
        observation.get("state") == "finished"
        and receipt.get("quiescent") is True
        and receipt.get("processes_quiescent") is True
    )
    checks = {}
    if finished:
        head = _git(workspace, "rev-parse", "HEAD")
        checks = {
            "native_completed": receipt.get("outcome") == "completed",
            "nonce": receipt.get("result")
            == {"nonce": probe["nonce"], "markers": probe["markers"]},
            "scoped_commit": head != probe["baseline"]
            and _git(workspace, "diff", "--name-only", probe["baseline"], head) == "src/owned.txt",
            "content": verify.file_bytes_at_revision(workspace, head, "src/owned.txt")
            == (CONTENT + probe["nonce"] + "\n").encode(),
            "clean": verify.worktree_clean(workspace),
            "branch": _git(workspace, "symbolic-ref", "--short", "HEAD") == BRANCH,
            "main": _git(root / "repository", "rev-parse", "main") == probe["baseline"],
            "protected": all(
                file_digest(Path(name)) == value for name, value in probe["protected"].items()
            ),
            "attribution": bool(receipt.get("session_id") and receipt.get("observed_model")),
            "input_complete": receipt.get("input_complete") is True,
            "usage": receipt.get("usage_known") is True
            and receipt.get("usage", {}).get("output_tokens", 0) > 0,
        }
        if all(checks.values()):
            checks.update(containment(envelope))
            checks["protected_after_probe"] = (
                all(file_digest(Path(name)) == value for name, value in probe["protected"].items())
                and _git(root / "repository", "rev-parse", "main") == probe["baseline"]
            )
    passed = bool(finished and checks and all(checks.values()))
    report = {
        "schema_version": 1,
        "kind": KIND,
        "ok": passed,
        "checks": checks,
        "receipt": receipt,
        "request_digest": envelope["request_digest"],
        "reason": (
            ""
            if passed
            else receipt.get("reason")
            or observation.get("reason")
            or "Writer lifecycle or fixture readback failed"
        ),
        "input_tokens": envelope["input"]["token_upper_bound"],
        "configured_output_tokens": envelope["request"]["limits"]["output_tokens"],
    }
    report_path = path.parent / ("writer-qualification-" + digest(report) + ".json")
    write_json(report_path, report)
    if passed:
        capability = {
            **envelope["capability"],
            "input_tokens": report["input_tokens"],
            "output_tokens": report["configured_output_tokens"],
            "qualification": {
                "status": "passed",
                "path": str(report_path),
                "sha256": file_digest(report_path),
                "input_tokens": report["input_tokens"],
                "output_tokens": report["configured_output_tokens"],
                "observed_output_tokens": receipt["usage"]["output_tokens"],
                "served_models": receipt["observed_models"],
                "native_output_limit_enforced": False,
                "output_enforcement": "native receipt rejection plus whole-job time/output-byte containment",
                "checks": [
                    "full_input",
                    "scoped_tools",
                    "window_completion",
                    "quiescence",
                    "model_attribution",
                    "structured_output",
                    "turn_bound",
                    "output_bound",
                ],
            },
        }
        write_json(path.parent / "capability.json", capability)
        report["capability_path"] = str(path.parent / "capability.json")
    return {**report, "evidence_path": str(report_path)}


def qualify(
    proposal_path, directory, *, input_bytes=16384, output_tokens=8192, timeout_s=300, runner=None
):
    from .campaign_headless import HeadlessRunner, read_json

    capability = read_json(proposal_path)
    candidate = RoleCandidate(capability["profile"], capability["model"], capability["efforts"][0])
    runner = runner or HeadlessRunner()
    directory = Path(directory).absolute()
    path = directory / "job/request.json"
    if path.exists():
        envelope = read_json(path)
        if envelope["capability"] != capability:
            raise CampaignError("Writer qualification directory belongs to another proposal")
        validate_envelope(envelope)
    else:
        request, probe, grants = build(
            directory,
            candidate,
            capability,
            input_bytes=input_bytes,
            output_tokens=output_tokens,
            timeout_s=timeout_s,
        )
        path = runner._prepare(
            directory / "job",
            request,
            candidate,
            capability,
            launch_id="writer-probe-" + probe["nonce"],
            generation=1,
            probe=probe,
            permissions="workspace-tools",
            grants=grants,
        )
    runner.launch(path)
    deadline = time.monotonic() + timeout_s + 15
    while True:
        observed = runner.observe(path)
        if observed.get("state") == "finished":
            break
        if time.monotonic() >= deadline:
            runner.terminate(path)
            observed = {
                "state": "unknown",
                "reason": "Writer qualification wait expired; resume for owned process readback",
            }
            break
        time.sleep(0.2)
    return result(path, observed)
