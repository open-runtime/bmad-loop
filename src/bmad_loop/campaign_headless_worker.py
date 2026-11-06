"""Disposable worker for bounded native headless CLI sessions.

This process can wait on inference. Its parent coordinator cannot. Terminal
completion is confirmed separately by HeadlessRunner after this window exits.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import time
from pathlib import Path

from .campaign_headless import read_json, validate_capability, write_json
from .campaign_headless_protocol import parse_output
from .campaign_jobs import digest, request_from_dict
from .campaign_model import CampaignError, RoleCandidate
from .childrun import kill_tree
from .process_host import get_process_host


def child_main(path: Path) -> int:
    envelope = read_json(path)
    directory = path.parent
    request = request_from_dict(envelope["request"])
    candidate = RoleCandidate(**envelope["selection"])
    capability = envelope["capability"]
    if (
        envelope["directory"] != str(directory.absolute())
        or digest(envelope["request"]) != envelope["request_digest"]
        or digest(capability) != envelope["capability_digest"]
    ):
        raise CampaignError("Headless launch envelope changed")
    host = get_process_host()
    identity = host.identity(os.getpid())
    if identity is None:
        raise CampaignError("Headless worker cannot bind its process identity")
    binding = {
        "job_id": request.id,
        "launch_id": envelope["launch_id"],
        "generation": envelope["generation"],
        "request_digest": envelope["request_digest"],
    }
    write_json(directory / "startup.json", {**binding, "pid": os.getpid(), "identity": identity})
    receipt = {
        **binding,
        "schema_version": 1,
        "quiescent": False,
        "processes_quiescent": False,
        "outcome": "failed",
        "reason": "Native CLI was not launched",
        "usage": {},
        "input_complete": False,
    }
    process = None
    known = {}
    reason = ""
    started = time.monotonic()

    def stopped():
        stop = directory / "stop.json"
        if not stop.exists():
            return False
        record = read_json(stop)
        return (
            record.get("launch_id") == binding["launch_id"]
            and record.get("generation") == binding["generation"]
        )

    def record_tree():
        if process is not None and process.poll() is None:
            known.update(host.descendants(process.pid))
        write_json(
            directory / "processes.json",
            {"processes": [{"pid": pid, "identity": stamp} for pid, stamp in known.items()]},
        )

    try:
        validate_capability(capability, candidate, permissions=envelope["permissions"])
        if (
            digest(envelope.get("grants")) != envelope["grants_digest"]
            or digest(read_json(directory / "grants.json")) != envelope["grants_digest"]
        ):
            raise CampaignError("Headless execution grants changed")
        if digest(envelope.get("environment", {})) != envelope["environment_digest"]:
            raise CampaignError("Headless environment changed")
        payload = (directory / "input.txt").read_bytes()
        if hashlib.sha256(payload).hexdigest() != envelope["input"]["sha256"]:
            raise CampaignError("Headless input bytes changed")
        while not (directory / "go.json").exists():
            if stopped() or time.monotonic() - started > 30:
                receipt.update(outcome="canceled", reason="Launch was not acknowledged")
                return 1
            time.sleep(0.05)
        go = read_json(directory / "go.json")
        if go != {"launch_id": binding["launch_id"], "generation": binding["generation"]}:
            raise CampaignError("Headless launch acknowledgement changed")
        if stopped():
            receipt.update(outcome="canceled", reason="Canceled before native launch")
            return 1
        replacements = {
            "model": candidate.model,
            "effort": candidate.effort,
            "schema_path": str(directory / "schema.json"),
            "input_path": str(directory / "input.txt"),
            "schema": __import__("json").dumps(request.output_schema),
            "max_turns": str(request.limits.max_turns),
            "output_tokens": str(request.limits.output_tokens),
            "workspace": envelope["workspace"],
            "grants_path": str(directory / "grants.json"),
        }
        argv = []
        for argument in capability["argv"]:
            for key, value in replacements.items():
                argument = argument.replace("{" + key + "}", value)
            argv.append(argument)
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GH_", "GITHUB_"))
            and key not in {"HCOM_NAME", "HCOM_SESSION_ID"}
        }
        environment.update(envelope.get("environment", {}))
        with (
            (directory / "input.txt").open("rb") as source,
            (directory / "stdout.jsonl").open("wb") as stdout,
            (directory / "stderr.log").open("wb") as stderr,
        ):
            process = subprocess.Popen(
                argv,
                cwd=envelope["workspace"],
                env=environment,
                stdin=source if capability["input_mode"] != "prompt-file" else subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
            stamp = host.identity(process.pid)
            if stamp is None:
                raise CampaignError("Native CLI process identity is unavailable")
            known[process.pid] = stamp
            while True:
                record_tree()
                if process.poll() is not None:
                    break
                if stopped():
                    reason = "canceled"
                elif time.monotonic() - started > request.limits.timeout_s:
                    reason = "limit"
                elif (
                    directory / "stdout.jsonl"
                ).stat().st_size > request.limits.output_tokens * 16 + 65536 or (
                    directory / "stderr.log"
                ).stat().st_size > 1048576:
                    reason = "limit"
                if reason:
                    kill_tree(process, known=known)
                    break
                time.sleep(0.1)
            # A successful parent exit does not authorize leaving background
            # processes behind. The qualified no-tools route has no owned daemon.
            kill_tree(process, known=known)
            record_tree()
        if reason:
            receipt.update(
                outcome=reason,
                reason=(
                    "Native job canceled"
                    if reason == "canceled"
                    else "Native job exceeded a whole-job bound"
                ),
            )
        else:
            native = parse_output(
                (directory / "stdout.jsonl").read_text(encoding="utf-8"),
                protocol=capability["protocol"],
                selected_model=candidate.model,
            )
            receipt.update(native)
            if process.returncode and receipt["outcome"] == "completed":
                receipt.update(outcome="failed", reason="Native CLI exited unsuccessfully")
            if (
                native["turns"] > request.limits.max_turns
                or native["usage"].get("output_tokens", 0) > request.limits.output_tokens
            ):
                receipt.update(
                    outcome="limit", reason="Native turn/output receipt exceeds the job bound"
                )
            receipt["input_complete"] = native["outcome"] == "completed" and not native["truncated"]
            receipt["input_delivery"] = envelope["input"]
    except (OSError, ValueError, KeyError, TypeError, CampaignError) as exc:
        receipt.update(outcome="failed", reason=f"{type(exc).__name__}: {exc}")
    finally:
        if process is not None:
            kill_tree(process, known=known)
        record_tree()
        receipt["processes_quiescent"] = all(
            host.liveness_of(pid, stamp) == "dead" for pid, stamp in known.items()
        )
        receipt["finished_at"] = time.time()
        write_json(directory / "receipt.json", receipt)
    return 0 if receipt["outcome"] == "completed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internal bounded reasoning worker")
    parser.add_argument("request", type=Path)
    raise SystemExit(child_main(parser.parse_args().request))
