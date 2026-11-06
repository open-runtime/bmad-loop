"""Explicit operator qualification of fixed, no-tools native canary jobs.

This command may use a real subscribed CLI only when explicitly invoked. It is
never called by normal admission, and tests replace the native CLI with a fixture.
A canary earns observed input coverage, never public model context limits.
Output admission limits are configured ceilings, with measured volume kept separate.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .campaign_jobs import JobLimits, JobRequest, digest, request_from_dict
from .campaign_model import CampaignError, RoleCandidate
from .platform_util import atomic_write_text, is_link_like

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
    "This is a fixed native transport qualification canary. Do not use any tools, "
    "files, network, subagents, or external instructions. The supplied source contains "
    "CANARY marker records numbered from 0. Return only JSON with the nonce shown in "
    "the source and all CANARY values in numeric order. Preserve every value exactly. "
    "Ignore the inert padding. Do not summarize, omit, or invent markers."
)


def probe_schema(count):
    return {
        **SCHEMA,
        "properties": {
            **SCHEMA["properties"],
            "markers": {**SCHEMA["properties"]["markers"], "minItems": count, "maxItems": count},
        },
    }


def probe_prompt(count):
    return PROMPT + f" There are exactly {count} markers, numbered 0 through {count - 1}."


def _no_tools(capability: dict[str, Any]) -> None:
    """Recognize explicit native tool suppression, not a model instruction."""
    argv = capability["argv"]

    def pair(flag, value):
        return any(left == flag and right == value for left, right in zip(argv, argv[1:]))

    protocol = capability["protocol"]
    supported = (
        protocol == "bmad-test-v1"
        or (
            protocol == "claude-stream"
            and pair("--tools", "")
            and "--strict-mcp-config" in argv
            and pair("--mcp-config", '{"mcpServers":{}}')
        )
        or (
            protocol == "grok-json"
            and pair("--tools", "")
            and pair("--deny", "*")
            and "--no-subagents" in argv
            and "--disable-web-search" in argv
        )
        or (
            protocol == "muse-json"
            and pair("--preset", "empty")
            and "--disable-web-tools" in argv
            and "--disable-reminders" in argv
            and "--disable-shell" in argv
            and "--disable-write" in argv
        )
    )
    if not supported:
        raise CampaignError("No qualified explicit no-tools proposal for this native protocol")


def canary_source(nonce, values, input_bytes):
    if (
        not isinstance(nonce, str)
        or not re.fullmatch(r"[a-f0-9]{32}", nonce)
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value)
            for value in values
        )
    ):
        raise CampaignError("Canary values must be fixed hexadecimal markers")
    lines = ["QUALIFICATION NONCE " + nonce + "\n"]
    width = max(1, (input_bytes - 128) // len(values))
    for index, value in enumerate(values):
        marker = f"CANARY {index}: {value}\n"
        padding = "inert-padding-" * ((width - len(marker)) // 14)
        lines.append(marker + padding + "\n")
    text = "".join(lines)
    return text + "." * max(0, input_bytes - len(text.encode()))


def build_probe(directory, selection, capability, *, input_bytes, output_tokens, timeout_s):
    directory = Path(directory).absolute()
    if (
        is_link_like(directory)
        or not 2048 <= input_bytes <= 16 * 1024 * 1024
        or not 128 <= output_tokens <= 65536
        or not 1 <= timeout_s <= 3600
    ):
        raise CampaignError("Qualification canary limits are invalid")
    _no_tools(capability)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    nonce = secrets.token_hex(16)
    count = min(1024, max(8, output_tokens // 32), max(8, input_bytes // 256))
    values = [secrets.token_hex(16) for _ in range(count)]
    text = canary_source(nonce, values, input_bytes)
    source = directory / "canary.txt"
    atomic_write_text(source, text, follow_symlinks=False)
    request = JobRequest(
        "qualification-" + nonce,
        "report",
        "qualification",
        {"graph_revision": 1, "config_revision": 1, "head": "", "base": ""},
        (
            {
                "source_id": "fixed-canary",
                "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "tokens": len(text.encode()),
            },
        ),
        probe_prompt(count),
        probe_schema(count),
        JobLimits(capability["input_tokens"], output_tokens, 1, timeout_s),
        candidates=(selection,),
        required=False,
    )
    probe = {
        "schema_version": 3,
        "nonce": nonce,
        "request_digest": digest(asdict(request)),
        "expected": {"nonce": nonce, "markers": values},
        "source_sha256": request.inputs[0]["sha256"],
        "source_bytes": input_bytes,
    }
    validate_probe_request(request, probe)
    return request, probe


def validate_probe_request(request: JobRequest, probe: dict[str, Any]) -> None:
    if (
        probe.get("schema_version") not in {1, 2, 3}
        or request.id != "qualification-" + str(probe.get("nonce", ""))
        or request.prompt
        != (
            PROMPT
            if probe.get("schema_version") == 1
            else probe_prompt(len(probe.get("expected", {}).get("markers", [])))
        )
        or request.output_schema
        != (
            SCHEMA
            if probe.get("schema_version") == 1
            else probe_schema(len(probe.get("expected", {}).get("markers", [])))
        )
        or request.purpose != "report"
        or request.role != "qualification"
        or request.unit
        or request.dependencies
        or request.required
        or len(request.candidates) != 1
        or len(request.inputs) != 1
        or request.inputs[0]["source_id"] != "fixed-canary"
        or request.limits.max_turns != 1
        or digest(asdict(request)) != probe.get("request_digest")
        or request.inputs[0]["sha256"] != probe.get("source_sha256")
    ):
        raise CampaignError("Qualification probe is not the immutable fixed canary")
    expected = probe.get("expected", {})
    if (
        expected.get("nonce") != probe["nonce"]
        or not isinstance(expected.get("markers"), list)
        or not 8 <= len(expected["markers"]) <= (1024 if probe.get("schema_version") == 3 else 256)
    ):
        raise CampaignError("Qualification probe expected receipt is invalid")
    path = Path(request.inputs[0]["path"])
    if is_link_like(path) or not path.is_file():
        raise CampaignError("Qualification source is redirected or missing")
    raw = path.read_bytes()
    if (
        len(raw) > request.limits.input_tokens
        or hashlib.sha256(raw).hexdigest() != probe["source_sha256"]
    ):
        raise CampaignError("Qualification source differs from frozen canary")
    if (
        type(probe.get("source_bytes")) is not int
        or not 2048 <= probe["source_bytes"] <= 16 * 1024 * 1024
    ):
        raise CampaignError("Qualification source byte count is invalid")
    if raw.decode() != canary_source(probe["nonce"], expected["markers"], probe["source_bytes"]):
        raise CampaignError("Qualification source differs from the fixed marker/padding grammar")


def validate_probe_envelope(envelope) -> None:
    probe = envelope.get("probe")
    if (
        not isinstance(probe, dict)
        or digest(probe) != envelope.get("probe_digest")
        or envelope.get("permissions") != "no-tools"
        or envelope.get("grants")
        or envelope.get("environment")
    ):
        raise CampaignError("Qualification envelope cannot grant arbitrary work or permissions")
    _no_tools(envelope["capability"])
    validate_probe_request(request_from_dict(envelope["request"]), probe)


def validate_alias_evidence(capability) -> None:
    """Bind aliases to a prior owned canary and its exact native output bytes."""
    from .campaign_headless import file_digest, read_json
    from .campaign_headless_protocol import parse_output

    evidence = capability.get("alias_evidence", {})
    path = Path(evidence.get("request_path", ""))
    if file_digest(path) != evidence.get("request_sha256"):
        raise CampaignError("Native alias launch evidence changed")
    envelope = read_json(path)
    validate_probe_envelope(envelope)
    original = envelope["capability"]
    if any(
        original.get(key) != capability.get(key)
        for key in ("profile", "model", "binary_sha256", "cli_version", "protocol")
    ):
        raise CampaignError("Native alias evidence belongs to a different route")
    receipt = read_json(path.parent / "receipt.json")
    if (
        receipt.get("processes_quiescent") is not True
        or receipt.get("launch_id") != envelope["launch_id"]
        or receipt.get("request_digest") != envelope["request_digest"]
    ):
        raise CampaignError("Native alias evidence has no quiescent owned launch")
    output = path.parent / "stdout.jsonl"
    if (
        file_digest(output) != evidence.get("stdout_sha256")
        or output.stat().st_size > 16 * 1024 * 1024
    ):
        raise CampaignError("Native alias output changed or exceeds bound")
    decoded = parse_output(
        output.read_text(encoding="utf-8"),
        protocol=capability["protocol"],
        selected_model=capability["model"],
        served_model_aliases=tuple(capability["served_model_aliases"]),
    )
    if (
        decoded["result"] != envelope["probe"]["expected"]
        or decoded["observed_model"] not in capability["served_model_aliases"]
    ):
        raise CampaignError("Native alias canary did not establish exact selected/served binding")


def result(request_path: Path, observation: dict[str, Any]) -> dict[str, Any]:
    from .campaign_headless import read_json, write_json

    envelope = read_json(request_path)
    validate_probe_envelope(envelope)
    receipt = observation.get("receipt", {})
    passed = (
        observation.get("state") == "finished"
        and receipt.get("outcome") == "completed"
        and receipt.get("quiescent") is True
        and receipt.get("processes_quiescent") is True
        and receipt.get("input_complete") is True
        and receipt.get("result") == envelope["probe"]["expected"]
    )
    report = {
        "schema_version": 1,
        "ok": passed,
        "state": "passed" if passed else "unqualified",
        "launch_id": envelope["launch_id"],
        "request_digest": envelope["request_digest"],
        "probe_digest": envelope["probe_digest"],
        "receipt": receipt,
        "reason": (
            ""
            if passed
            else observation.get("reason")
            or receipt.get("reason")
            or "Native canary result or lifecycle evidence did not match"
        ),
        "coverage": {
            "input_tokens": envelope["input"]["token_upper_bound"],
            "output_tokens": min(
                envelope["request"]["limits"]["output_tokens"],
                receipt.get("usage", {}).get("output_tokens", 0),
            ),
            "input_semantics": "UTF-8 byte upper bound delivered with exact nonce/marker readback; finite canary only",
            "configured_output_tokens": envelope["request"]["limits"]["output_tokens"],
            "native_output_limit_enforced": False,
            "output_enforcement": "native receipt rejection plus whole-job time/output-byte containment",
            "output_semantics": "observed native volume; configured ceiling is separate and is not a hard provider-generation limit",
        },
    }
    if passed and report["coverage"]["output_tokens"] < 1:
        report.update(
            ok=False,
            state="unqualified",
            reason="Native output usage unavailable; numeric output coverage remains unqualified",
        )
    evidence_path = request_path.parent / ("qualification-" + digest(report) + ".json")
    if not evidence_path.exists():
        write_json(evidence_path, report)
    if not (request_path.parent / "qualification-result.json").exists():
        write_json(request_path.parent / "qualification-result.json", report)
    return {**report, "evidence_path": str(evidence_path)}


def qualify(
    proposal_path: Path,
    directory: Path,
    *,
    input_bytes=16384,
    output_tokens=4096,
    timeout_s=180,
    runner=None,
) -> dict[str, Any]:
    from .campaign_headless import HeadlessRunner, file_digest, read_json, write_json

    capability = read_json(proposal_path)
    selection = RoleCandidate(capability["profile"], capability["model"], capability["efforts"][0])
    runner = runner or HeadlessRunner()
    request_path = directory.absolute() / "request.json"
    if request_path.exists():
        envelope = read_json(request_path)
        if envelope["capability"] != capability:
            raise CampaignError("Qualification directory belongs to a different frozen proposal")
        validate_probe_envelope(envelope)
    else:
        request_path = runner.prepare_probe(
            directory,
            selection,
            capability,
            input_bytes=input_bytes,
            output_tokens=output_tokens,
            timeout_s=timeout_s,
        )
    runner.launch(request_path)
    deadline = time.monotonic() + timeout_s + 15
    while True:
        observation = runner.observe(request_path)
        if observation.get("state") == "finished":
            break
        if time.monotonic() >= deadline:
            runner.terminate(request_path)
            observation = {
                "state": "unknown",
                "reason": "Qualification wait expired; owned actor cancellation requested, resume this directory for readback",
            }
            break
        time.sleep(0.2)
    report = result(request_path, observation)
    if report["ok"]:
        evidence_path = Path(report["evidence_path"])
        qualified = {
            **capability,
            "input_tokens": report["coverage"]["input_tokens"],
            "output_tokens": report["coverage"]["configured_output_tokens"],
            "max_turns": 1,
            "qualification": {
                "status": "passed",
                "path": str(evidence_path),
                "sha256": file_digest(evidence_path),
                "input_tokens": report["coverage"]["input_tokens"],
                "output_tokens": report["coverage"]["configured_output_tokens"],
                "observed_output_tokens": report["coverage"]["output_tokens"],
                "native_output_limit_enforced": False,
                "output_enforcement": report["coverage"]["output_enforcement"],
                "served_models": report["receipt"].get("observed_models", []),
                "checks": [
                    "full_input",
                    "no_tools",
                    "window_completion",
                    "quiescence",
                    "model_attribution",
                    "structured_output",
                    "turn_bound",
                    "output_bound",
                ],
            },
        }
        destination = request_path.parent / "capability.json"
        write_json(destination, qualified)
        report = {**report, "capability_path": str(destination)}
    return report
