"""Qualified coding-CLI jobs in owned terminal windows.

The coordinator only launches, observes and requests cancellation. A separate
Python child delivers immutable input to a qualified native CLI. Window death
and process-tree readback establish completion; JSON is only a result artifact.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters.multiplexer import get_multiplexer
from .adapters.tmux_base import native_window_command
from .campaign_jobs import JobRequest, digest
from .campaign_model import CampaignError, RoleCandidate
from .platform_util import atomic_write_text, file_lock, is_link_like
from .process_host import get_process_host

PROTOCOLS = frozenset(
    {"claude-stream", "codex-jsonl", "agy-json", "grok-json", "muse-json", "bmad-test-v1"}
)
MAX_RECORD_BYTES = 16 * 1024 * 1024


def read_json(path: Path, *, limit: int = MAX_RECORD_BYTES) -> dict[str, Any]:
    if is_link_like(path) or not path.is_file() or path.stat().st_size > limit:
        raise CampaignError(f"Headless record is redirected, missing or oversized: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CampaignError("Headless record must be an object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, sort_keys=True), follow_symlinks=False)


def file_digest(path: Path) -> str:
    if is_link_like(path) or not path.is_file():
        raise CampaignError("Qualified executable/input must be a regular non-symlink file")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def provider_family(model: str) -> str:
    name = model.lower()
    if name.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if name.startswith("claude-"):
        return "anthropic"
    if name.startswith(("gemini-", "models/gemini-")):
        return "google"
    if name.startswith("grok-"):
        return "xai"
    if name.startswith(("muse-", "meta/", "llama-")):
        return "meta"
    return ""


def validate_capability(
    raw: dict[str, Any],
    candidate: RoleCandidate,
    *,
    verify_files: bool = True,
    permissions: str = "no-tools",
) -> None:
    """Validate a frozen route qualification, never infer capability from PATH.

    The qualification evidence is operator-owned, versioned test output. Its
    measured input/output ceiling bounds this manifest; a tiny sentinel run
    cannot qualify a million-token route.
    """
    if raw.get("platform") != sys.platform:
        raise CampaignError("Headless native route is not qualified on this platform")
    if raw.get("schema_version") != 1 or raw.get("protocol") not in PROTOCOLS:
        raise CampaignError("Unsupported headless capability protocol")
    if raw.get("profile") != candidate.profile or raw.get("model") != candidate.model:
        raise CampaignError("Headless qualification does not match the selected route")
    if candidate.effort not in raw.get("efforts", []):
        raise CampaignError("Selected thinking level is not qualified")
    if not raw.get("cli_version") or raw.get("permissions") != permissions:
        raise CampaignError("Headless route requires the exact versioned permission qualification")
    if permissions not in {"no-tools", "workspace-tools"}:
        raise CampaignError("Unsupported headless tool grant")
    if raw.get("process_containment") != "foreground-contained":
        raise CampaignError("Reasoning requires qualified foreground process containment")
    if raw.get("input_mode") not in {"text-stdin", "claude-stream-stdin", "prompt-file"}:
        raise CampaignError("Headless input transport is unqualified")
    if raw.get("tokenizer") != "utf8-byte-upper-bound":
        raise CampaignError("Unsupported qualified tokenizer")
    for name in ("context_tokens", "input_tokens", "output_tokens", "reserved_tokens", "max_turns"):
        if type(raw.get(name)) is not int or raw[name] < 1:
            raise CampaignError("Headless capability limits must be positive integers")
    if (
        not isinstance(raw.get("argv"), list)
        or not raw["argv"]
        or any(not isinstance(arg, str) for arg in raw["argv"])
    ):
        raise CampaignError("Qualified argv must be a nonempty string list")
    if permissions == "workspace-tools" and not any("{grants_path}" in arg for arg in raw["argv"]):
        raise CampaignError("Execution route must consume the frozen grants file")
    binary = Path(raw.get("binary", ""))
    if not binary.is_absolute() or raw["argv"][0] != str(binary):
        raise CampaignError("Qualified argv must name its exact absolute executable")
    evidence = raw.get("qualification", {})
    if not isinstance(evidence, dict) or evidence.get("status") != "passed":
        raise CampaignError("Native headless qualification has not passed")
    required = {
        "full_input",
        "no_tools" if permissions == "no-tools" else "scoped_tools",
        "window_completion",
        "quiescence",
        "model_attribution",
        "structured_output",
        "turn_bound",
        "output_bound",
    }
    if not required <= set(evidence.get("checks", [])):
        raise CampaignError("Native headless qualification omits required checks")
    if (
        evidence.get("input_tokens", 0) < raw["input_tokens"]
        or evidence.get("output_tokens", 0) < raw["output_tokens"]
    ):
        raise CampaignError("Advertised limits exceed qualified coverage")
    if verify_files:
        if file_digest(binary) != raw.get("binary_sha256"):
            raise CampaignError("Qualified executable changed")
        for item in raw.get("dependencies", []):
            if file_digest(Path(item["path"])) != item["sha256"]:
                raise CampaignError("Qualified wrapper/native dependency changed")
        if file_digest(Path(evidence.get("path", ""))) != evidence.get("sha256"):
            raise CampaignError("Native qualification evidence changed")


def load_capabilities(path: Path) -> dict[str, Any]:
    document = read_json(path)
    if document.get("schema_version") != 1 or not isinstance(document.get("routes"), dict):
        raise CampaignError("Invalid headless capability file")
    for key, value in document["routes"].items():
        modes = value if "reasoning" in value or "execution" in value else {"flat": value}
        for mode, cap in modes.items():
            candidate = RoleCandidate(cap["profile"], cap["model"], (cap.get("efforts") or [""])[0])
            if key != candidate.profile + ":" + candidate.model:
                raise CampaignError("Headless capability key differs from its route")
            permission = (
                "workspace-tools" if mode == "execution" else cap.get("permissions", "no-tools")
            )
            validate_capability(cap, candidate, permissions=permission)
    return document["routes"]


def capability_for(
    routes: dict[str, Any], profile: str, model: str, permissions: str = "no-tools"
) -> dict[str, Any]:
    value = routes.get(profile + ":" + model, {})
    if "reasoning" in value or "execution" in value:
        value = value.get("reasoning" if permissions == "no-tools" else "execution", {})
    if value.get("permissions") != permissions:
        raise CampaignError(f"No qualified {permissions} route for {profile}/{model}")
    return value


def render_input(request: JobRequest, capability: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """Deliver source bytes inline, with explicit identities and no tool reads."""
    pieces = [request.prompt, "\nReturn only the requested JSON result.\n"]
    identities = []
    for source in request.inputs:
        path = Path(source["path"])
        if (
            is_link_like(path)
            or not path.is_file()
            or path.stat().st_size > capability["input_tokens"] * 4
        ):
            raise CampaignError(
                "Reasoning source is redirected, missing or exceeds the input bound"
            )
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise CampaignError("Reasoning source changed after request creation")
        content = raw.decode("utf-8", errors="strict")
        pieces.append(
            json.dumps(
                {"source_id": source["source_id"], "sha256": source["sha256"], "content": content},
                ensure_ascii=False,
            )
        )
        identities.append(
            {"source_id": source["source_id"], "sha256": source["sha256"], "bytes": len(raw)}
        )
    pieces.append("\nOUTPUT SCHEMA:\n" + json.dumps(request.output_schema, ensure_ascii=False))
    text = "\n".join(pieces)
    canonical = text.encode("utf-8")
    # Every UTF-8 byte is conservatively charged as a token. This is an upper
    # bound, not an invented chars/4 tokenizer or provider usage measurement.
    bound = len(canonical)
    if bound > min(request.limits.input_tokens, capability["input_tokens"]):
        raise CampaignError("Rendered prompt/source/schema exceeds its input token bound")
    if (
        bound + request.limits.output_tokens + capability["reserved_tokens"]
        > capability["context_tokens"]
    ):
        raise CampaignError("Rendered input leaves insufficient output/thinking context")
    if capability["input_mode"] == "claude-stream-stdin":
        payload = (
            json.dumps(
                {"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False
            )
            + "\n"
        ).encode()
    else:
        payload = canonical
    return payload, {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "token_upper_bound": bound,
        "sources": identities,
        "transport": capability["input_mode"],
    }


class HeadlessRunner:
    def __init__(self, mux=None):
        self.mux = mux or get_multiplexer()

    def prepare(
        self,
        directory: Path,
        request: JobRequest,
        selection: RoleCandidate,
        capability: dict[str, Any],
        *,
        launch_id: str,
        generation: int,
        permissions: str = "no-tools",
        grants: dict[str, Any] | None = None,
        environment: dict[str, str] | None = None,
    ) -> Path:
        request.validate()
        if environment and any(
            not key.startswith("BMAD_LOOP_") or not isinstance(value, str)
            for key, value in environment.items()
        ):
            raise CampaignError("Headless environment contains an unsupported override")
        if selection not in request.candidates:
            raise CampaignError("Selected headless route is not authorized by the request")
        validate_capability(capability, selection, permissions=permissions)
        if permissions == "workspace-tools" and (
            not grants
            or grants.get("schema_version") != 1
            or not grants.get("workspace")
            or not grants.get("scopes")
            or not grants.get("config_digest")
        ):
            raise CampaignError("Workspace tools require frozen, scoped execution grants")
        if (
            request.limits.output_tokens > capability["output_tokens"]
            or request.limits.max_turns > capability["max_turns"]
        ):
            raise CampaignError("Requested limits exceed qualified route bounds")
        if not launch_id or type(generation) is not int or generation < 1:
            raise CampaignError("Headless launch ownership is invalid")
        directory = directory.absolute()
        if is_link_like(directory):
            raise CampaignError("Headless directory is redirected")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload, delivery = render_input(request, capability)
        raw_request = json.loads(json.dumps(asdict(request)))
        envelope = {
            "schema_version": 1,
            "request": raw_request,
            "request_digest": digest(raw_request),
            "selection": asdict(selection),
            "capability": capability,
            "capability_digest": digest(capability),
            "permissions": permissions,
            "grants": grants or {},
            "grants_digest": digest(grants or {}),
            "environment": environment or {},
            "environment_digest": digest(environment or {}),
            "workspace": (
                str(Path(grants["workspace"]).resolve())
                if permissions == "workspace-tools" and grants
                else str(directory)
            ),
            "launch_id": launch_id,
            "generation": generation,
            "directory": str(directory),
            "input": delivery,
            "session": "bmad-reasoning-"
            + hashlib.sha256(str(directory.parent).encode()).hexdigest()[:16],
            "window_name": "job-" + hashlib.sha256(launch_id.encode()).hexdigest()[:24],
        }
        path = directory / "request.json"
        with file_lock(directory / "prepare.lock"):
            if path.exists():
                if read_json(path) != envelope:
                    raise CampaignError("Cannot replace an immutable headless launch")
                return path
            atomic_write_text(
                directory / "input.txt", payload.decode("utf-8"), follow_symlinks=False
            )
            write_json(directory / "schema.json", request.output_schema)
            write_json(directory / "grants.json", grants or {})
            write_json(path, envelope)
        return path

    def launch(self, request_path: Path) -> dict[str, Any]:
        envelope = read_json(request_path)
        directory = request_path.parent
        candidate = RoleCandidate(**envelope["selection"])
        validate_capability(envelope["capability"], candidate, permissions=envelope["permissions"])
        with file_lock(directory / "launch.lock"):
            intent = directory / "launch-intent.json"
            if intent.exists():
                return self.observe(request_path)
            write_json(
                intent, {"launch_id": envelope["launch_id"], "generation": envelope["generation"]}
            )
            if not self.mux.has_session(envelope["session"]):
                self.mux.new_session(envelope["session"], directory)
            argv = [sys.executable, "-m", "bmad_loop.campaign_headless_worker", str(request_path)]
            native_id = self.mux.new_window(
                envelope["session"],
                envelope["window_name"],
                directory,
                {},
                native_window_command(argv),
            )
            write_json(
                directory / "window.json",
                {"launch_id": envelope["launch_id"], "native_id": native_id},
            )
            write_json(
                directory / "go.json",
                {"launch_id": envelope["launch_id"], "generation": envelope["generation"]},
            )
        return {"state": "running", "run_dir": str(directory), "native_id": native_id}

    def observe(self, request_path: Path) -> dict[str, Any]:
        envelope = read_json(request_path)
        directory = request_path.parent
        window_path = directory / "window.json"
        startup_path = directory / "startup.json"
        if not window_path.exists():
            matches = [
                row[0]
                for row in self.mux.list_windows(envelope["session"], ["window_id", "window_name"])
                if row[1] == envelope["window_name"]
            ]
            if len(matches) > 1:
                raise CampaignError("Multiple windows claim the same reasoning launch")
            if matches:
                write_json(
                    window_path, {"launch_id": envelope["launch_id"], "native_id": matches[0]}
                )
                write_json(
                    directory / "go.json",
                    {"launch_id": envelope["launch_id"], "generation": envelope["generation"]},
                )
            elif not startup_path.exists():
                return {
                    "state": "unknown",
                    "reason": "No owned startup/window binding; replacement forbidden",
                }
        window = read_json(window_path) if window_path.exists() else None
        if window and window.get("launch_id") != envelope["launch_id"]:
            raise CampaignError("Headless window belongs to a different launch")
        if window and self.mux.window_alive(envelope["session"], window["native_id"]):
            return {"state": "running", "native_id": window["native_id"]}
        if not startup_path.exists():
            return {"state": "unknown", "reason": "Window exited without owned startup identity"}
        startup = read_json(startup_path)
        for name in ("launch_id", "generation", "request_digest"):
            if startup.get(name) != envelope[name]:
                raise CampaignError("Headless startup identity changed")
        host = get_process_host()
        actors = (
            [startup, *read_json(directory / "processes.json").get("processes", [])]
            if (directory / "processes.json").exists()
            else [startup]
        )
        if any(host.liveness_of(row["pid"], row.get("identity")) != "dead" for row in actors):
            return {
                "state": "unknown",
                "reason": "Window exited but process quiescence is unproven",
            }
        path = directory / "receipt.json"
        if not path.exists():
            return {
                "state": "unknown",
                "reason": "Quiescent worker has no containment/result receipt",
            }
        receipt = read_json(path)
        if any(
            receipt.get(name) != envelope[name]
            for name in ("launch_id", "generation", "request_digest")
        ):
            raise CampaignError("Headless receipt identity changed")
        if receipt.get("processes_quiescent") is not True:
            return {"state": "unknown", "reason": "Headless descendant containment is unproven"}
        receipt.update(
            quiescent=True, completion_evidence="owned-window-death-and-process-readback"
        )
        return {"state": "finished", "receipt": receipt, "run_dir": str(directory)}

    def terminate(self, request_path: Path) -> dict[str, Any]:
        envelope = read_json(request_path)
        write_json(
            request_path.parent / "stop.json",
            {"launch_id": envelope["launch_id"], "generation": envelope["generation"]},
        )
        return {"state": "stop_requested", "launch_id": envelope["launch_id"]}
