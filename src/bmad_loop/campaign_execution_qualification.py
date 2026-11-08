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
READ_ONLY_KIND = "read-only-tools-v1"
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
READ_ONLY_PROMPT = (
    "This is an explicitly authorized disposable native read-only qualification. "
    "Use your file or shell read tools to read src/owned.txt in the workspace. "
    "Return its exact content as workspace_readback, plus the exact nonce and all CANARY "
    "values in numeric order from the immutable source. Do not write any product or Git "
    "file, use network tools or run background jobs. Return only the requested JSON."
)
READ_ONLY_SCHEMA = {
    **SCHEMA,
    "properties": {**SCHEMA["properties"], "workspace_readback": {"type": "string"}},
    "required": [*SCHEMA["required"], "workspace_readback"],
}


def _readonly(probe):
    return probe.get("kind") == READ_ONLY_KIND


def expected_result(probe):
    result = {"nonce": probe["nonce"], "markers": probe["markers"]}
    if _readonly(probe):
        result["workspace_readback"] = probe["workspace_readback"]
    return result


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
    permission = capability.get("permissions")
    if permission not in {"workspace-tools", "read-only-tools"}:
        raise CampaignError("Tools qualification requires exact execution permissions")
    _validate_capability(
        capability, candidate, permissions=permission, qualification_required=False
    )
    if capability.get("confinement") in {"codex-native-read-only-v1", "codex-scoped-tools-v1"}:
        from .campaign_codex_appserver import validate_recipe

        validate_recipe(capability)
        return
    if capability.get("confinement") == "claude-scoped-mcp-v1":
        from .campaign_claude_scoped import validate_recipe as validate_claude_recipe

        validate_claude_recipe(capability)
        return
    if capability.get("confinement") == "agy-scoped-command-v1":
        from .campaign_agy_scoped import validate_recipe as validate_agy_recipe

        validate_agy_recipe(capability)
        return
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
    if capability["protocol"] in {"codex-app-server", "codex-native-review"}:
        from .campaign_codex_appserver import validate_recipe

        validate_recipe(capability)


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
    readonly = capability["permissions"] == "read-only-tools"
    kind = READ_ONLY_KIND if readonly else KIND
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    repo, workspace = directory / "repository", directory / "workspace"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "BMAD qualification")
    _git(repo, "config", "user.email", "qualification@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.hooksPath", str(directory / "empty-hooks"))
    (repo / "src").mkdir()
    readback = "private workspace canary: " + secrets.token_hex(24) + "\n"
    (repo / "src/owned.txt").write_text(readback if readonly else "initial\n", encoding="utf-8")
    (repo / "src/sibling.txt").write_text("protected sibling\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "immutable qualification baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    # Packed history must be protected too: a forged loose file can otherwise
    # shadow a valid packed object without changing a protected branch ref.
    _git(repo, "repack", "-ad")
    _git(repo, "prune-packed")
    _git(repo, "worktree", "add", "-b", BRANCH, str(workspace))
    outside = directory / "outside.txt"
    outside.write_text("protected outside\n", encoding="utf-8")
    nonce = secrets.token_hex(16)
    from .campaign_qualification import canary_source

    markers = [secrets.token_hex(16) for _ in range(min(64, max(8, input_bytes // 512)))]
    text = ("" if readonly else f"Exact src/owned.txt line: {CONTENT}{nonce}\n") + canary_source(
        nonce, markers, input_bytes
    )
    source = directory / "source.txt"
    source.write_text(text, encoding="utf-8")
    grants = {
        "schema_version": 1,
        "workspace": str(workspace),
        "scopes": [] if readonly else ["src/owned.txt"],
        "config_digest": digest([kind, nonce]),
        "protected_branches": ["main"],
        "runtime_write_paths": capability.get("runtime_write_paths", []),
    }
    grants.update({"permissions": "read-only-tools"} if readonly else {"git_branch": BRANCH})
    if "--review-pair" in capability.get("argv", ()):
        grants["native_review_target"] = {"head": baseline, "base": baseline}
    probe = {
        "kind": kind,
        "nonce": nonce,
        "markers": markers,
        "source_bytes": input_bytes,
        "root": str(directory),
        "baseline": baseline,
        "source_sha256": file_digest(source),
        "grants": grants,
        "protected": {
            str(path): file_digest(path)
            for path in (
                outside,
                repo / "src/sibling.txt",
                workspace / "src/sibling.txt",
                *sorted((repo / ".git/objects/pack").glob("*.pack")),
            )
        },
    }
    if readonly:
        probe["workspace_readback"] = readback
    if capability.get("confinement") in {"codex-native-read-only-v1", "codex-scoped-tools-v1"}:
        grants["native_probe"] = {
            "root": str(directory),
            "workspace_readback": readback if readonly else "initial\n",
        }
    request = JobRequest(
        ("readonly-qualification-" if readonly else "writer-qualification-") + nonce,
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
        READ_ONLY_PROMPT if readonly else PROMPT,
        READ_ONLY_SCHEMA if readonly else SCHEMA,
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
    readonly = _readonly(probe)
    from .campaign_qualification import canary_source

    expected_text = (
        "" if readonly else f"Exact src/owned.txt line: {CONTENT}{probe['nonce']}\n"
    ) + canary_source(probe["nonce"], probe["markers"], probe["source_bytes"])
    if (
        probe.get("kind") not in {KIND, READ_ONLY_KIND}
        or not root.is_absolute()
        or is_link_like(root)
        or request.id
        != ("readonly-qualification-" if readonly else "writer-qualification-") + probe["nonce"]
        or request.prompt != (READ_ONLY_PROMPT if readonly else PROMPT)
        or request.output_schema != (READ_ONLY_SCHEMA if readonly else SCHEMA)
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
        or probe["grants"]["scopes"] != ([] if readonly else ["src/owned.txt"])
        or probe["grants"].get("git_branch") != (None if readonly else BRANCH)
        or (readonly and probe["grants"].get("permissions") != "read-only-tools")
        or probe["grants"].get("schema_version") != 1
        or probe["grants"].get("config_digest") != digest([probe["kind"], probe["nonce"]])
        or probe["grants"].get("protected_branches") != ["main"]
        or probe["grants"].get("write_paths")
        or probe["grants"].get("read_only_paths")
    ):
        raise CampaignError("Writer canary request differs from its fixed fixture")
    if (
        readonly
        and verify.file_bytes_at_revision(root / "repository", probe["baseline"], "src/owned.txt")
        != probe.get("workspace_readback", "").encode()
    ):
        raise CampaignError("Read-only canary workspace identity changed")
    if "native_probe" in probe["grants"] and (
        probe["grants"]["native_probe"]
        != {
            "root": str(root),
            "workspace_readback": probe["workspace_readback"] if readonly else "initial\n",
        }
    ):
        raise CampaignError("Native read-only containment probe differs from the fixture")
    if "native_review_target" in probe["grants"] and probe["grants"]["native_review_target"] != {
        "head": probe["baseline"],
        "base": probe["baseline"],
    }:
        raise CampaignError("Native review pair target differs from its fixed fixture")


def validate_envelope(envelope):
    probe = envelope["probe"]
    if (
        digest(probe) != envelope.get("probe_digest")
        or envelope.get("permissions") != probe["kind"].removesuffix("-v1")
        or envelope.get("grants") != probe["grants"]
        or envelope.get("environment")
        or probe["grants"].get("runtime_write_paths", [])
        != envelope["capability"].get("runtime_write_paths", [])
        or Path(envelope["directory"]) != Path(probe["root"]) / "job"
        or envelope["workspace"] != probe["grants"]["workspace"]
        or ("native_probe" in probe["grants"])
        != (
            envelope["capability"].get("confinement")
            in {"codex-native-read-only-v1", "codex-scoped-tools-v1"}
        )
    ):
        raise CampaignError("Writer canary envelope differs from fixed grants")
    validate_request(request_from_dict(envelope["request"]), probe)
    validate_proposal(envelope["capability"], RoleCandidate(**envelope["selection"]))


def containment(envelope):
    """Prove exact sandbox grants deny protected writes, independently of prose."""
    from .campaign_execution_sandbox import command

    root = Path(envelope["probe"]["root"])
    if _readonly(envelope["probe"]):
        from .campaign_readonly_qualification import containment as readonly_containment

        return readonly_containment(envelope)
    # This deterministic descendant runs under the identical frozen grants and
    # wrapper policy used by the native CLI. The report is captured by its parent.
    code = """import json,os,sys,zlib
from pathlib import Path
checks={}
for name,path in [('sibling',Path('src/sibling.txt')),('outside',Path(sys.argv[1])),('target_ref',Path(sys.argv[2]))]:
 try: path.write_text('forbidden change')
 except PermissionError: checks[name]=True
 else: checks[name]=False
protected=Path(sys.argv[3]); alias=Path(sys.argv[4]); shadow=Path(sys.argv[5])
for name,action in [('object_unlink',lambda:protected.unlink()),('object_overwrite',lambda:protected.write_bytes(b'forbidden')),('object_hardlink',lambda:os.link(protected,alias))]:
 try: action()
 except PermissionError: checks[name]=True
 else: checks[name]=False
try:
 shadow.parent.mkdir(parents=True,exist_ok=True)
 shadow.write_bytes(b'forged canonical loose object')
except PermissionError: checks['packed_object_shadow']=True
else: checks['packed_object_shadow']=False
# A valid zlib object under a forged existing hash exercises real Git lookup,
# not merely the permission to create loose files in canonical storage.
oid=sys.argv[6]; fake=Path(os.environ['GIT_OBJECT_DIRECTORY'])/oid[:2]/oid[2:]
fake.parent.mkdir(parents=True,exist_ok=True)
poison=zlib.compress(b'blob 5\\0fake\\n')
if fake.exists(): assert fake.read_bytes()==poison
else: fake.write_bytes(poison)
print(json.dumps(checks))
"""
    baseline = envelope["probe"]["baseline"]
    protected_blob = _git(root / "repository", "rev-parse", baseline + ":src/sibling.txt")
    expected_blob = _git(root / "repository", "cat-file", "blob", protected_blob)
    probe_argv = [
        str(Path(sys.executable).resolve()),
        "-c",
        code,
        str(root / "outside.txt"),
        str(root / "repository/.git/refs/heads/main"),
        str(next((root / "repository/.git/objects/pack").glob("*.pack"))),
        str(root / "job/sandbox-runtime/forbidden-hardlink"),
        str(root / "repository/.git/objects" / baseline[:2] / baseline[2:]),
        protected_blob,
    ]
    if envelope["capability"].get("confinement") in {
        "codex-scoped-tools-v1",
        "claude-scoped-mcp-v1",
        "agy-scoped-command-v1",
    }:
        from .campaign_scoped_tools import ScopedTools

        worker = ScopedTools(root / "job/grants.json", root / "containment-tools")
        response = worker.execute("fixed-containment", {"command": probe_argv})
        if response.get("success") is not True:
            raise CampaignError("Scoped writer containment probe did not complete")
        checks = json.loads(response["contentItems"][0]["text"])
    else:
        argv, cwd, env = command(root / "job/grants.json", probe_argv)
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise CampaignError("Writer containment probe did not complete")
        checks = json.loads(result.stdout)
    checks["bank_shadow_readback"] = (
        _git(root / "repository", "cat-file", "blob", protected_blob) == expected_blob
    )
    return checks


def scoped_tool_checks(envelope):
    """Require actual native writing calls and durable independent quiescence."""
    from .campaign_headless import file_digest, read_json

    job = Path(envelope["directory"])
    directory = job / "scoped-tools"
    budget = read_json(directory / "budget.json")
    expected_binding = {
        "schema_version": 1,
        "grants_path": str(job / "grants.json"),
        "grants_digest": file_digest(job / "grants.json"),
        "max_calls": 32,
        "timeout_s": 60,
        "max_output_bytes": 65536,
    }
    calls = budget.get("calls", [])
    if budget.get("binding") != expected_binding or not 1 <= len(calls) <= 32:
        raise CampaignError("Scoped native tool budget lacks its frozen lifecycle binding")
    if len(set(calls)) != len(calls):
        raise CampaignError("Scoped native tool budget repeats a call identity")
    records = [read_json(directory / (key + ".json")) for key in calls]
    if envelope["capability"].get("confinement") == "claude-scoped-mcp-v1":
        from .campaign_claude_scoped import native_tool_checks

        return native_tool_checks(job, records, calls)
    native_calls = {}
    if envelope["capability"].get("confinement") == "agy-scoped-command-v1":
        native_calls = read_json(job / "agy-native/calls.json")
    for line in (
        () if native_calls else (job / "stdout.jsonl").read_text(encoding="utf-8").splitlines()
    ):
        frame = json.loads(line)
        if frame.get("method") == "item/tool/call":
            params = frame.get("params", {})
            if params.get("tool") != "bmad_shell" or params.get("namespace") is not None:
                raise CampaignError("Native writer invoked an unqualified dynamic tool")
            key = digest(params.get("callId"))
            if key in native_calls and native_calls[key] != params:
                raise CampaignError("Native writer reused its dynamic tool identity")
            native_calls[key] = params
    if any(
        digest(record.get("call_id")) != key
        or native_calls.get(key, {}).get("arguments") != record.get("arguments")
        or record.get("state") != "finished"
        or record.get("receipt", {}).get("processes_quiescent") is not True
        for key, record in zip(calls, records, strict=True)
    ):
        raise CampaignError(
            "Scoped native tool calls have unfinished processes or changed identities"
        )
    return {
        "scoped_native_calls": any(
            record.get("result", {}).get("success") is True for record in records
        ),
        "scoped_native_quiescence": True,
    }


def result(path, observation):
    from .campaign_capability_evidence import request_binding
    from .campaign_headless import file_digest, read_json, write_json

    envelope = read_json(path)
    validate_envelope(envelope)
    probe, receipt = envelope["probe"], observation.get("receipt", {})
    readonly = _readonly(probe)
    root, workspace = Path(probe["root"]), Path(probe["grants"]["workspace"])
    finished = (
        observation.get("state") == "finished"
        and receipt.get("quiescent") is True
        and receipt.get("processes_quiescent") is True
    )
    checks = {}
    if finished:
        from .campaign_native_git import publish

        head = publish(workspace, probe["baseline"], directory=path.parent, final=True)
        from .campaign_object_promotion import promote

        sealed = promote(workspace, head, probe["baseline"])
        checks = {
            "canonical_objects": sealed["state"] in {"sealed", "not_required"},
            "native_completed": receipt.get("outcome") == "completed",
            "nonce": receipt.get("result") == expected_result(probe),
            "scoped_commit": (
                head == probe["baseline"]
                if readonly
                else head != probe["baseline"]
                and _git(workspace, "diff", "--name-only", probe["baseline"], head)
                == "src/owned.txt"
            ),
            "content": verify.file_bytes_at_revision(workspace, head, "src/owned.txt")
            == (
                probe["workspace_readback"] if readonly else CONTENT + probe["nonce"] + "\n"
            ).encode(),
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
        if (
            envelope["capability"]["protocol"] == "codex-native-review"
            and checks["native_completed"]
        ):
            from .campaign_codex_review_pair import validate_receipt

            validate_receipt(receipt, envelope["capability"], envelope["request"]["coordinates"])
            checks["native_review"] = True
        if all(checks.values()):
            confinement = envelope["capability"].get("confinement")
            if confinement in {"codex-native-read-only-v1", "codex-scoped-tools-v1"}:
                from .campaign_readonly_qualification import native_checks

                native_denial = native_checks(receipt.get("native_confinement"))
                if confinement == "codex-native-read-only-v1":
                    checks.update(native_denial)
                else:
                    checks["native_builtin_confinement"] = all(native_denial.values())
                    checks.update(scoped_tool_checks(envelope))
                    checks.update(containment(envelope))
            elif confinement in {"claude-scoped-mcp-v1", "agy-scoped-command-v1"}:
                checks.update(scoped_tool_checks(envelope))
                checks.update(containment(envelope))
            else:
                checks.update(containment(envelope))
            checks["protected_after_probe"] = (
                all(file_digest(Path(name)) == value for name, value in probe["protected"].items())
                and _git(root / "repository", "rev-parse", "main") == probe["baseline"]
            )
    passed = bool(finished and checks and all(checks.values()))
    report = {
        "schema_version": 2,
        "request_binding": request_binding(path),
        "kind": probe["kind"],
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
            "efforts": [envelope["selection"]["effort"]],
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
                    "read_only_tools" if readonly else "scoped_tools",
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
            permissions=capability["permissions"],
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
