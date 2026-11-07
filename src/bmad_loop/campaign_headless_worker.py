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

from .campaign_headless import read_json, validate_capability, validate_probe_capability, write_json
from .campaign_headless_protocol import parse_output
from .campaign_jobs import digest, request_from_dict
from .campaign_model import CampaignError, CommunicationSpec, RoleCandidate
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
    limit_kind = ""
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
        if envelope.get("probe") is not None:
            from .campaign_qualification import validate_probe_envelope

            validate_probe_envelope(envelope)
            validate_probe_capability(capability, candidate)
        else:
            validate_capability(capability, candidate, permissions=envelope["permissions"])
        if (
            digest(envelope.get("grants")) != envelope["grants_digest"]
            or digest(read_json(directory / "grants.json")) != envelope["grants_digest"]
        ):
            raise CampaignError("Headless execution grants changed")
        if digest(envelope.get("environment", {})) != envelope["environment_digest"]:
            raise CampaignError("Headless environment changed")
        communication_room = envelope.get("communication_room", "")
        if ("communication_room" in envelope or "communication_room_digest" in envelope) and (
            not isinstance(communication_room, str)
            or not communication_room
            or not Path(communication_room).is_absolute()
            or "\0" in communication_room
            or digest(communication_room) != envelope.get("communication_room_digest")
        ):
            raise CampaignError("Headless communication room changed or is invalid")
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
        from .campaign_reflection_flow import sanitized_actor_env

        environment = sanitized_actor_env(
            {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("GH_", "GITHUB_"))
            },
            communication=CommunicationSpec(room=communication_room),
        )
        environment.update(envelope.get("environment", {}))
        from .campaign_headless_context import restore

        environment, native_context = restore(environment, os.environ, envelope.get("presentation"))
        if native_context:
            receipt["native_terminal"] = native_context
        if capability["protocol"] == "muse-json":
            # The launcher can otherwise replace the pinned native executable
            # after qualification and before exec. Account routing is unchanged.
            environment["MUSE_NO_AUTO_UPDATE"] = "1"
        # MSP emits input echoes and a separate native envelope for every output
        # delta. Bound those wire bytes separately from model output tokens.
        wire_limit = request.limits.output_tokens * 16 + 65536
        if capability["protocol"] == "muse-json":
            wire_limit = envelope["input"]["bytes"] * 3 + request.limits.output_tokens * 128 + 65536
        receipt["wire_output_limit_bytes"] = wire_limit
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
                    limit_kind = "wall-time"
                elif (directory / "stdout.jsonl").stat().st_size > wire_limit:
                    reason = "limit"
                    limit_kind = "stdout-wire-bytes"
                elif (directory / "stderr.log").stat().st_size > 1048576:
                    reason = "limit"
                    limit_kind = "stderr-bytes"
                if reason:
                    kill_tree(process, known=known)
                    break
                time.sleep(0.1)
            # A successful parent exit does not authorize leaving background
            # processes behind. The qualified no-tools route has no owned daemon.
            kill_tree(process, known=known)
            record_tree()
        # Exit may occur between polls, after emitting a complete oversized
        # frame. Recheck immutable final streams and the whole-job deadline
        # after descendants drain; a fast exit cannot bypass these bounds.
        if not reason:
            if stopped():
                reason = "canceled"
            elif time.monotonic() - started > request.limits.timeout_s:
                reason, limit_kind = "limit", "wall-time"
            elif (directory / "stdout.jsonl").stat().st_size > wire_limit:
                reason, limit_kind = "limit", "stdout-wire-bytes"
            elif (directory / "stderr.log").stat().st_size > 1048576:
                reason, limit_kind = "limit", "stderr-bytes"
        if reason:
            try:
                partial = parse_output(
                    (directory / "stdout.jsonl").read_text(encoding="utf-8"),
                    protocol=capability["protocol"],
                    selected_model=candidate.model,
                    served_model_aliases=tuple(capability.get("served_model_aliases", ())),
                    partial=True,
                )
                for key in (
                    "usage",
                    "usage_known",
                    "session_id",
                    "selected_model",
                    "observed_model",
                    "observed_models",
                    "provider_family",
                    "turns",
                ):
                    if key in partial:
                        receipt[key] = partial[key]
            except (OSError, ValueError, TypeError, CampaignError) as exc:
                receipt["telemetry_error"] = f"{type(exc).__name__}: {exc}"
            receipt.update(
                outcome=reason,
                reason=(
                    "Native job canceled"
                    if reason == "canceled"
                    else "Native job exceeded whole-job " + limit_kind + " bound"
                ),
                limit_kind=limit_kind,
            )
        else:
            native = parse_output(
                (directory / "stdout.jsonl").read_text(encoding="utf-8"),
                protocol=capability["protocol"],
                selected_model=candidate.model,
                served_model_aliases=tuple(capability.get("served_model_aliases", ())),
            )
            receipt.update(native)
            receipt["native_outcome"] = native["outcome"]
            if process.returncode and receipt["outcome"] == "completed":
                receipt.update(outcome="failed", reason="Native CLI exited unsuccessfully")
            if (
                native["turns"] > request.limits.max_turns
                or native["usage"].get("output_tokens", 0) > request.limits.output_tokens
            ):
                receipt.update(
                    outcome="limit",
                    reason="Native turn/output receipt exceeds the job bound",
                    limit_kind=(
                        "native-turns"
                        if native["turns"] > request.limits.max_turns
                        else "native-output-tokens"
                    ),
                )
            receipt["input_complete"] = (
                native["outcome"] == "completed" or native.get("native_terminal_completed") is True
            ) and not native["truncated"]
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
