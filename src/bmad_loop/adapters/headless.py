"""Qualified headless coding sessions backed by the campaign window runner.

Reasoning and execution permissions are different frozen grants. This adapter
never loads a capability file from the worker checkout or upgrades a read-only
route into a writer because the prompt asks for edits.
"""

from __future__ import annotations

import copy
import hashlib
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..campaign_headless import HeadlessRunner, file_digest, provider_family
from ..campaign_jobs import JobLimits, JobRequest
from ..campaign_model import CampaignError, RoleCandidate
from ..childrun import hard_stop_pending
from ..model import TokenUsage
from .base import CodingCLIAdapter, SessionHandle, SessionResult, SessionSpec


class HeadlessAdapter(CodingCLIAdapter):
    name = "bounded-headless"
    injection = "stdin"
    observation = "window-death"
    state = "native-json"
    requires_inline_context = True

    def __init__(self, *, run_dir, profile, policy, mux=None, paths=None, extra_args=(), **kwargs):
        del kwargs
        if extra_args:
            raise CampaignError(
                "Headless extra arguments must be included in the qualified capability"
            )
        self.run_dir = Path(run_dir)
        self.native_job_root = self.run_dir
        self.profile, self.policy, self.paths = profile, policy, paths
        self.runner = HeadlessRunner(mux)
        self.capability = None
        self.grants = None
        self.coordinates = None
        self.handoff_packet = None
        self.managed_binding = None
        self.requests: dict[str, Path] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.baselines: dict[str, str] = {}
        self.name = profile.name

    def set_capability(self, capability, grants, coordinates, handoff_packet=None):
        self.capability = copy.deepcopy(capability)
        self.grants = copy.deepcopy(grants)
        if capability.get("permissions") == "read-only-tools" and self.grants:
            from ..campaign_headless import readonly_grants

            self.grants = readonly_grants(
                self.grants["workspace"],
                coordinates,
                capability,
                generation=self.grants.get("generation", 1),
                read_only_paths=self.grants.get("read_only_paths", ()),
            )
        if capability.get("permissions") in {"workspace-tools", "read-only-tools"} and self.grants:
            from ..campaign_execution_sandbox import _path

            workspace = Path(self.grants["workspace"]).resolve()
            approved = []
            for raw in capability.get("runtime_write_paths", []):
                path = _path(raw)
                if path.is_relative_to(workspace) or workspace.is_relative_to(path):
                    raise CampaignError("Native runtime write grant overlaps product ownership")
                approved.append(str(path))
            if approved:
                self.grants["runtime_write_paths"] = list(
                    dict.fromkeys([*self.grants.get("runtime_write_paths", []), *approved])
                )
        self.coordinates = copy.deepcopy(coordinates)
        self.handoff_packet = copy.deepcopy(handoff_packet)

    def set_managed_communication(self, binding):
        self.managed_binding = copy.deepcopy(binding)

    def set_native_job_root(self, directory: Path):
        """Keep native authority records outside the engine's writable checkout."""
        self.native_job_root = Path(directory)

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        from ..campaign_hold import held
        from .multiplexer import MultiplexerError

        if held():
            raise MultiplexerError("campaign operator hold suppresses automatic session launch")
        if self.capability is None or self.coordinates is None:
            raise CampaignError(
                "Headless session requires frozen native capability and coordinates"
            )
        permission = self.capability["permissions"]
        if permission in {"workspace-tools", "read-only-tools"} and (
            not self.grants or Path(self.grants["workspace"]).resolve() != spec.cwd.resolve()
        ):
            raise CampaignError("Headless execution grant belongs to another workspace")
        candidate = RoleCandidate(
            self.profile.name, spec.model, spec.effort, provider_family(spec.model)
        )
        identifier = hashlib.sha256(spec.task_id.encode()).hexdigest()[:24]
        limits = JobLimits(
            self.capability["input_tokens"],
            self.capability["output_tokens"],
            self.capability["max_turns"],
            max(1, int(spec.timeout_s)),
        )
        if (
            spec.token_budget is not None
            and spec.token_budget_mode == "enforce"  # nosec B105: policy mode, not a credential.
            and limits.input_tokens + limits.output_tokens > spec.token_budget
        ):
            raise CampaignError(
                "Qualified headless input/output envelope exceeds the session token budget"
            )
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        prompt = spec.prompt
        if self.paths is not None:
            prompt += "\nWhen finished, return a JSON object with status and spec_file (the actual BMAD spec path). Keep writing the native BMAD spec and Auto Run Result as instructed."
        request = JobRequest(
            identifier,
            "triage",
            spec.role,
            self.coordinates,
            (
                (dict(self.handoff_packet, source_id="campaign-handoff"),)
                if self.handoff_packet
                else ()
            ),
            prompt,
            schema,
            limits,
            candidates=(candidate,),
        )
        path = self.runner.prepare(
            self.native_job_root / "headless" / identifier,
            request,
            candidate,
            self.capability,
            launch_id=spec.task_id,
            generation=self.grants.get("generation", 1) if self.grants else 1,
            permissions=permission,
            grants=self.grants,
            environment={
                key: value for key, value in spec.env.items() if key.startswith("BMAD_LOOP_")
            },
            communication_room=spec.env.get("HCOM_DIR", ""),
            managed_binding=self.managed_binding,
            presentation=(
                {
                    "workspace": str(spec.cwd.resolve()),
                    "workspace_id": spec.env["BMAD_LOOP_PARADEV_WORKSPACE_ID"],
                }
                if spec.env.get("BMAD_LOOP_PARADEV_WORKSPACE_ID")
                else None
            ),
        )
        self.requests[spec.task_id] = path
        if permission == "workspace-tools":
            from ..verify import rev_parse_head

            self.baselines[spec.task_id] = rev_parse_head(spec.cwd)
        launched_ns = time.time_ns()
        if held():
            raise MultiplexerError("campaign operator hold suppresses automatic session launch")
        launched = self.runner.launch(path)
        return SessionHandle(
            spec.task_id, launched.get("native_id", identifier), launched_ns=launched_ns
        )

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        path = self.requests[handle.task_id]
        deadline = time.monotonic() + spec.timeout_s + 30
        stopped = False
        while True:
            observed = self.runner.observe(path)
            if observed["state"] == "finished":
                receipt = observed["receipt"]
                break
            if hard_stop_pending() or time.monotonic() > deadline:
                self.runner.terminate(path)
                stopped = True
            if time.monotonic() > deadline + 30:
                raise CampaignError(
                    "Headless termination quiescence remains unknown; replacement forbidden"
                )
            time.sleep(0.1)
        if handle.task_id in self.baselines:
            from ..campaign_native_git import publish
            from ..campaign_object_promotion import promote

            # Native completion precedes all legacy verification/commit reads.
            # Only sealed canonical bytes may enter the child engine's proofs.
            head = publish(
                spec.cwd, self.baselines[handle.task_id], directory=path.parent, final=True
            )
            promote(spec.cwd, head, self.baselines[handle.task_id])
        self.receipts[handle.task_id] = receipt
        outcome = receipt["outcome"]
        result_json = receipt.get("result")
        if (
            outcome == "completed"
            and (self.capability or {}).get("protocol") == "codex-native-review"
        ):
            from ..campaign_codex_review import fold_result

            result_json = fold_result(result_json, receipt.get("native_review"))
        if self.journal is not None and receipt.get("observed_model"):
            self.journal.append(
                "session-model",
                task_id=handle.task_id,
                model=receipt["observed_model"],
                cli=self.profile.name,
            )
        return SessionResult(
            (
                "completed"
                if outcome == "completed"
                else (
                    "aborted"
                    if stopped or outcome == "canceled"
                    else "over_budget" if outcome == "limit" else "crashed"
                )
            ),
            result_json=result_json,
            session_id=receipt.get("session_id"),
            transcript_path=str(path.parent / "stdout.jsonl"),
            usage_limited=receipt.get("reason") if outcome == "quota" else None,
            transport_fault={
                "quota": "usage",
                "authentication": "environment",
                "transport": "transport",
            }.get(outcome),
            transport_evidence=(
                receipt.get("reason")
                if outcome in {"quota", "authentication", "transport"}
                else None
            ),
            native_binding={
                "profile": self.profile.name,
                "family": receipt.get("provider_family", ""),
                "native_id": handle.native_id,
                "session_id": receipt.get("session_id"),
                "observed_model": receipt.get("observed_model", ""),
                "observed_models": receipt.get("observed_models", []),
                "reasoning_receipt": str(path.parent / "receipt.json"),
                "headless_request": str(path),
                "headless_request_sha256": file_digest(path),
                "headless_receipt_sha256": file_digest(path.parent / "receipt.json"),
                "outcome": outcome,
                "quiescent": True,
                "input_delivery": receipt.get("input_delivery", {}),
            },
        )

    def kill(self, handle: SessionHandle) -> None:
        if handle.task_id in self.requests and handle.task_id not in self.receipts:
            self.runner.terminate(self.requests[handle.task_id])

    def matches_selected_model(self, spec: SessionSpec, observed: str) -> bool:
        """Accept an earned served alias only for this completed native task."""
        cap = self.capability or {}
        receipt = self.receipts.get(spec.task_id, {})
        return (
            cap.get("profile") == self.profile.name
            and cap.get("model") == spec.model
            and spec.effort in cap.get("efforts", [])
            and receipt.get("selected_model") == spec.model
            and receipt.get("observed_model") == observed
            and receipt.get("outcome") == "completed"
            and receipt.get("quiescent") is True
            and receipt.get("input_complete") is True
            and observed in {spec.model, *cap.get("served_model_aliases", [])}
        )

    def read_usage(self, result: SessionResult) -> TokenUsage | None:
        request_path = (result.native_binding or {}).get("headless_request")
        receipt = next(
            (
                self.receipts.get(task_id)
                for task_id, path in self.requests.items()
                if str(path) == request_path
            ),
            None,
        )
        if receipt is None or not receipt.get("usage_known"):
            return None
        counts = receipt.get("usage", {})
        return TokenUsage(
            **{
                key: counts.get(key, 0)
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                )
            },
            models=tuple(receipt.get("observed_models", ())),
        )


class HeadlessDevAdapter(HeadlessAdapter):
    """Read the exact session-written BMAD spec after native window completion."""

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        result = super().wait_for_completion(handle, spec)
        if result.status != "completed":
            return result
        from ..devcontract import synthesize_result

        named = spec.expected_spec or (result.result_json or {}).get("spec_file")
        if not isinstance(named, str) or not named:
            return replace(
                result,
                result_json=None,
                resultless_detail="Headless development did not identify its BMAD spec",
            )
        path = Path(named)
        if not path.is_absolute():
            path = spec.cwd / path
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(spec.cwd.resolve())
            or not path.is_file()
            or path.stat().st_mtime_ns < handle.launched_ns
        ):
            raise CampaignError(
                "Headless BMAD spec is missing, redirected or not written by this session"
            )
        if (
            spec.spec_snapshot
            and hashlib.sha256(path.read_bytes()).hexdigest() == spec.spec_snapshot.sha256
        ):
            raise CampaignError("Headless review left the accepted spec unchanged")
        synthesized = synthesize_result(
            path,
            story_key=spec.env.get("BMAD_LOOP_STORY_KEY"),
            success_statuses=spec.success_statuses,
            plan_halt=spec.env.get("BMAD_LOOP_PLAN_HALT") == "1",
        )
        return replace(result, result_json=synthesized.result_json)
