"""Qualified coding-CLI jobs in owned terminal windows.

The coordinator only launches, observes and requests cancellation. A separate
Python child delivers immutable input to a qualified native CLI. Window death
and process-tree readback establish completion; JSON is only a result artifact.
"""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from typing import Any

from .adapters.multiplexer import MultiplexerError, get_multiplexer
from .adapters.tmux_base import native_window_command
from .campaign_admission_files import fresh_reads
from .campaign_jobs import JobRequest, digest
from .campaign_model import CampaignError, RoleCandidate
from .platform_util import atomic_write_text, file_lock, is_link_like
from .process_host import get_process_host

PROTOCOLS = frozenset(
    {
        "claude-stream",
        "codex-jsonl",
        "codex-app-server",
        "codex-native-review",
        "agy-json",
        "grok-json",
        "muse-json",
        "bmad-test-v1",
    }
)
MAX_RECORD_BYTES = 16 * 1024 * 1024
_ADMISSION_VALIDATIONS: ContextVar[dict[str, Exception | None] | None] = ContextVar(
    "campaign_admission_validations", default=None
)


@contextmanager
def admission_validation_pass():
    """Share exact route checks only within this synchronous proposal pass."""
    if _ADMISSION_VALIDATIONS.get() is not None:
        yield
        return
    token = _ADMISSION_VALIDATIONS.set({})
    try:
        yield
    finally:
        _ADMISSION_VALIDATIONS.reset(token)


def capability_admission(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with admission_validation_pass():
            return function(*args, **kwargs)

    return wrapped


def read_json(path: Path, *, limit: int = MAX_RECORD_BYTES) -> dict[str, Any]:
    from .campaign_admission_files import read_json as read

    return read(path, limit=limit)


def write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, sort_keys=True), follow_symlinks=False)


def file_digest(path: Path) -> str:
    from .campaign_admission_files import file_digest as read

    return read(path)


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


def _validate_capability(
    raw: dict[str, Any],
    candidate: RoleCandidate,
    *,
    verify_files: bool = True,
    permissions: str = "no-tools",
    qualification_required: bool = True,
) -> None:
    """Validate a frozen route qualification, never infer capability from PATH.

    The qualification evidence is operator-owned, versioned test output. Its
    measured input coverage bounds this manifest; a tiny sentinel run cannot
    qualify a million-token route. Configured output ceilings and their enforcement
    mechanism remain distinct from the observed canary output volume.
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
    if permissions not in {"no-tools", "read-only-tools", "workspace-tools"}:
        raise CampaignError("Unsupported headless tool grant")
    runtime_paths = raw.get("runtime_write_paths", [])
    if not isinstance(runtime_paths, list) or any(
        not isinstance(value, str)
        or not value
        or not Path(value).is_absolute()
        or Path(value) in {Path(Path(value).anchor), Path.home()}
        or Path(value).resolve() != Path(value)
        or is_link_like(Path(value))
        for value in runtime_paths
    ):
        raise CampaignError("Headless runtime write paths must be explicit non-root native paths")
    if permissions == "no-tools" and runtime_paths:
        raise CampaignError("No-tools routes cannot carry execution write grants")
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
    if permissions != "no-tools" and not any("{grants_path}" in arg for arg in raw["argv"]):
        raise CampaignError("Execution route must consume the frozen grants file")
    if permissions == "workspace-tools":
        from . import campaign_native_git, campaign_object_promotion

        dependencies = {row["path"]: row["sha256"] for row in raw.get("dependencies", [])}
        for module in (campaign_native_git, campaign_object_promotion):
            if module.__file__ is None:
                raise CampaignError("Writer private Git module has no source binding")
            path = Path(module.__file__).resolve()
            if str(path) not in dependencies or (
                verify_files and dependencies[str(path)] != file_digest(path)
            ):
                raise CampaignError(
                    "Writer private Git publication lacks an exact dependency binding; requalification required"
                )
    binary = Path(raw.get("binary", ""))
    if not binary.is_absolute() or raw["argv"][0] != str(binary):
        raise CampaignError("Qualified argv must name its exact absolute executable")
    aliases = raw.get("served_model_aliases", [])
    if (
        not isinstance(aliases, list)
        or len(set(aliases)) != len(aliases)
        or any(not isinstance(alias, str) or not alias for alias in aliases)
    ):
        raise CampaignError("Native served model aliases must be explicit unique identities")
    if aliases:
        from .campaign_qualification import validate_alias_evidence

        validate_alias_evidence(raw)
    evidence = raw.get("qualification", {})
    if qualification_required:
        if not isinstance(evidence, dict) or evidence.get("status") != "passed":
            raise CampaignError("Native headless qualification has not passed")
        if not set(aliases) <= set(evidence.get("served_models", [])):
            raise CampaignError("Served model alias lacks earned qualification evidence")
        required = {
            "full_input",
            {
                "no-tools": "no_tools",
                "workspace-tools": "scoped_tools",
                "read-only-tools": "read_only_tools",
            }[permissions],
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
        if qualification_required:
            from .campaign_capability_evidence import validate

            validate(raw, candidate)


def validate_capability(raw, candidate, *, verify_files=True, permissions="no-tools") -> None:
    """Normal campaign launches always require earned native qualification."""
    memo = _ADMISSION_VALIDATIONS.get()
    key = digest([raw, asdict(candidate), verify_files, permissions]) if memo is not None else ""
    if memo is not None and key in memo:
        failure = memo[key]
        if failure is not None:
            raise failure
        return
    try:
        _validate_capability(raw, candidate, verify_files=verify_files, permissions=permissions)
    except (CampaignError, OSError) as exc:
        if memo is not None:
            memo[key] = exc
        raise
    if memo is not None:
        memo[key] = None


def validate_probe_capability(raw, candidate) -> None:
    """Operator canaries validate exact binaries but cannot claim qualification."""
    if raw.get("permissions") in {"workspace-tools", "read-only-tools"}:
        from .campaign_execution_qualification import validate_proposal

        validate_proposal(raw, candidate)
        return
    if raw.get("qualification", {}).get("status") == "passed":
        raise CampaignError("Probe proposals must not claim already-passed qualification")
    _validate_capability(raw, candidate, permissions="no-tools", qualification_required=False)


def load_capabilities(path: Path) -> dict[str, Any]:
    document = read_json(path)
    if document.get("schema_version") != 1 or not isinstance(document.get("routes"), dict):
        raise CampaignError("Invalid headless capability file")
    for key, value in document["routes"].items():
        if not isinstance(value, dict):
            raise CampaignError("Headless route must be a capability or permission mapping")
        variants = {None: value}
        if "by_effort" in value:
            if (
                set(value) != {"by_effort"}
                or not isinstance(value["by_effort"], dict)
                or not value["by_effort"]
            ):
                raise CampaignError(
                    "Headless effort variants require an exclusive nonempty mapping"
                )
            variants = value["by_effort"]
        for effort, variant in variants.items():
            if not isinstance(variant, dict):
                raise CampaignError(
                    "Headless effort variant must contain a capability or permission mapping"
                )
            modes = (
                variant
                if set(variant) & {"reasoning", "review", "execution"}
                else {"flat": variant}
            )
            if modes is variant and set(modes) - {"reasoning", "review", "execution"}:
                raise CampaignError("Headless permission mapping contains ambiguous fields")
            for mode, cap in modes.items():
                if (
                    not isinstance(cap, dict)
                    or not isinstance(cap.get("profile"), str)
                    or not isinstance(cap.get("model"), str)
                ):
                    raise CampaignError("Headless capability lacks a profile/model route")
                if effort is not None and cap.get("efforts") != [effort]:
                    raise CampaignError(
                        "Headless effort variant differs from its earned thinking level"
                    )
                candidate = RoleCandidate(
                    cap["profile"], cap["model"], (cap.get("efforts") or [""])[0]
                )
                if key != candidate.profile + ":" + candidate.model:
                    raise CampaignError("Headless capability key differs from its route")
                permission = {
                    "reasoning": cap.get("permissions"),
                    "execution": "workspace-tools",
                }.get(mode, cap.get("permissions", "no-tools"))
                if mode == "reasoning" and permission not in {"no-tools", "read-only-tools"}:
                    raise CampaignError("Reasoning routes cannot grant product writes")
                if mode != "review" and cap.get("protocol") == "codex-native-review":
                    raise CampaignError(
                        "Native review requires its separate review capability slot"
                    )
                if mode == "review" and permission != "read-only-tools":
                    raise CampaignError("Native review requires read-only tools")
                if not isinstance(permission, str):
                    raise CampaignError("Headless capability permission must be explicit")
                validate_capability(cap, candidate, permissions=permission)
    return document["routes"]


def capability_for(
    routes: dict[str, Any],
    profile: str,
    model: str,
    permissions: str = "no-tools",
    *,
    effort: str | None = None,
) -> dict[str, Any]:
    """Select the exact earned effort without borrowing another route's proof."""
    value = routes.get(profile + ":" + model, {})
    if not isinstance(value, dict):
        raise CampaignError("Headless route must be a capability or permission mapping")
    by_effort = "by_effort" in value
    if "by_effort" in value:
        variants = value["by_effort"]
        if set(value) != {"by_effort"} or not isinstance(variants, dict):
            raise CampaignError("Headless effort variants require an exclusive mapping")
        if effort is None or effort not in variants:
            raise CampaignError(
                f"No qualified exact thinking level {effort!r} for {profile}/{model}"
            )
        value = variants[effort]
    if not isinstance(value, dict):
        raise CampaignError(
            "Headless effort variant must contain a capability or permission mapping"
        )
    if permissions == "review" and "review" in value:
        value = value["review"]
    elif set(value) & {"reasoning", "review", "execution"}:
        value = value.get(
            (
                "reasoning"
                if permissions in {"reasoning", "review", "no-tools", "read-only-tools"}
                else "execution"
            ),
            {},
        )
    allowed = (
        {"no-tools", "read-only-tools"} if permissions in {"reasoning", "review"} else {permissions}
    )
    if not isinstance(value, dict) or value.get("permissions") not in allowed:
        raise CampaignError(f"No qualified {permissions} route for {profile}/{model}")
    if value.get("protocol") == "codex-native-review" and permissions != "review":
        raise CampaignError("Native review route cannot be borrowed for general reasoning")
    if by_effort and value.get("efforts") != [effort]:
        raise CampaignError("Headless effort variant differs from its earned thinking level")
    if effort is not None and effort not in value.get("efforts", []):
        raise CampaignError(
            f"Selected thinking level {effort!r} is not qualified for {profile}/{model}"
        )
    return value


def readonly_grants(workspace, coordinates, capability, *, generation=1, read_only_paths=()):
    """Construct a minimum read grant without inheriting any worker write authority."""
    if capability.get("permissions") != "read-only-tools":
        raise CampaignError("Read-only grant requires an explicitly selected read-only route")
    return {
        "schema_version": 1,
        "permissions": "read-only-tools",
        "workspace": str(Path(workspace).resolve()),
        "scopes": [],
        "config_digest": digest(coordinates),
        "generation": generation,
        "read_only_paths": list(read_only_paths),
        "runtime_write_paths": list(capability.get("runtime_write_paths", [])),
    }


def render_input(
    request: JobRequest, capability: dict[str, Any], *, steering=()
) -> tuple[bytes, dict[str, Any]]:
    """Deliver source bytes inline, with explicit identities and no tool reads."""
    pieces = [request.prompt, "\nReturn only the requested JSON result.\n"]
    if steering:
        from .campaign_messages import context

        pieces.append(context(steering))
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


def validate_control_directory(directory: Path, permissions: str, grants: dict[str, Any]):
    """A tool-capable process must not be able to rewrite its launch authority."""
    if permissions not in {"workspace-tools", "read-only-tools"}:
        return
    from .campaign_execution_sandbox import _path

    control = _path(str(directory.absolute()))
    workspace = _path(grants.get("workspace"))
    if control.is_relative_to(workspace):
        raise CampaignError("Native control records must be outside the product workspace")
    for raw in grants.get("runtime_write_paths", []):
        writable = _path(raw)
        if writable.is_relative_to(control) or control.is_relative_to(writable):
            raise CampaignError("Native runtime write grant overlaps control records")


class HeadlessRunner:
    def __init__(self, mux=None):
        self.mux = mux or get_multiplexer()

    def prepare(
        self,
        directory,
        request,
        selection,
        capability,
        *,
        launch_id,
        generation,
        permissions="no-tools",
        grants=None,
        environment=None,
        communication_room="",
        presentation=None,
        managed_binding=None,
        steering=None,
    ) -> Path:
        return self._prepare(
            directory,
            request,
            selection,
            capability,
            launch_id=launch_id,
            generation=generation,
            permissions=permissions,
            grants=grants,
            environment=environment,
            communication_room=communication_room,
            presentation=presentation,
            managed_binding=managed_binding,
            steering=steering,
        )

    def prepare_probe(
        self,
        directory,
        selection,
        capability,
        *,
        input_bytes=16384,
        output_tokens=4096,
        timeout_s=180,
        presentation=None,
    ) -> Path:
        """Prepare only the fixed operator qualification canary, never arbitrary work."""
        from .campaign_qualification import build_probe

        validate_probe_capability(capability, selection)
        request, probe = build_probe(
            directory,
            selection,
            capability,
            input_bytes=input_bytes,
            output_tokens=output_tokens,
            timeout_s=timeout_s,
        )
        return self._prepare(
            directory,
            request,
            selection,
            capability,
            launch_id="probe-" + probe["nonce"],
            generation=1,
            probe=probe,
            presentation=presentation,
        )

    @fresh_reads()
    def _prepare(
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
        communication_room: str = "",
        probe: dict[str, Any] | None = None,
        presentation: dict[str, str] | None = None,
        managed_binding: dict[str, Any] | None = None,
        steering: list[dict[str, Any]] | None = None,
    ) -> Path:
        request.validate()
        if not isinstance(communication_room, str) or (
            communication_room
            and (not Path(communication_room).is_absolute() or "\0" in communication_room)
        ):
            raise CampaignError("Headless communication room must be an absolute path")
        if communication_room:
            communication_room = str(Path(communication_room).resolve())
        if environment and any(
            not key.startswith("BMAD_LOOP_") or not isinstance(value, str)
            for key, value in environment.items()
        ):
            raise CampaignError("Headless environment contains an unsupported override")
        if selection not in request.candidates:
            raise CampaignError("Selected headless route is not authorized by the request")
        if probe is None:
            _validate_capability(capability, selection, permissions=permissions)
        else:
            from .campaign_qualification import validate_probe_request

            writer_probe = probe.get("kind") in {"workspace-tools-v1", "read-only-tools-v1"}
            if (
                environment
                or communication_room
                or (
                    writer_probe
                    and (
                        permissions != probe.get("kind", "").removesuffix("-v1")
                        or grants != probe.get("grants")
                    )
                )
                or (not writer_probe and (permissions != "no-tools" or grants))
            ):
                raise CampaignError(
                    "Qualification canaries cannot grant tools or environment overrides"
                )
            validate_probe_capability(capability, selection)
            validate_probe_request(request, probe)
        if permissions in {"workspace-tools", "read-only-tools"} and (
            not grants
            or grants.get("schema_version") != 1
            or not grants.get("workspace")
            or (permissions == "workspace-tools" and not grants.get("scopes"))
            or not grants.get("config_digest")
        ):
            raise CampaignError("Workspace tools require frozen, scoped execution grants")
        if permissions == "read-only-tools" and (
            not grants
            or grants.get("permissions") != permissions
            or any(grants.get(key) for key in ("scopes", "write_paths", "git_branch"))
        ):
            raise CampaignError("Read-only tools cannot acquire product or Git writes")
        if (
            request.limits.output_tokens > capability["output_tokens"]
            or request.limits.max_turns > capability["max_turns"]
        ):
            raise CampaignError("Requested limits exceed qualified route bounds")
        if not launch_id or type(generation) is not int or generation < 1:
            raise CampaignError("Headless launch ownership is invalid")
        directory = directory.absolute()
        validate_control_directory(directory, permissions, grants or {})
        if presentation:
            if (
                not {"workspace", "workspace_id"}.issubset(presentation)
                or set(presentation) - {"workspace", "workspace_id", "navigation_prefix"}
                or not all(isinstance(value, str) and value for value in presentation.values())
                or not Path(presentation["workspace"]).is_absolute()
                or is_link_like(Path(presentation["workspace"]))
            ):
                raise CampaignError("Headless terminal requires an exact workspace binding")
            if (
                permissions != "no-tools"
                and grants
                and Path(presentation["workspace"]).resolve() != Path(grants["workspace"]).resolve()
            ):
                raise CampaignError(
                    "Headless terminal and write grant belong to different workspaces"
                )
        if is_link_like(directory):
            raise CampaignError("Headless directory is redirected")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if steering and probe is not None:
            raise CampaignError("Qualification canaries cannot receive steering")
        payload, delivery = render_input(request, capability, steering=steering or ())
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
                if permissions != "no-tools" and grants
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
        if communication_room:
            # Optional for compatibility with already prepared envelopes. The
            # selected room is configuration, never inherited actor identity.
            envelope["communication_room"] = communication_room
            envelope["communication_room_digest"] = digest(communication_room)
        if managed_binding:
            if not isinstance(managed_binding, dict) or probe is not None:
                raise CampaignError("Managed communication requires a frozen campaign binding")
            envelope["managed_binding"] = managed_binding
            envelope["managed_binding_digest"] = digest(managed_binding)
        if steering:
            envelope["steering"] = steering
            envelope["steering_digest"] = digest(steering)
        if probe is not None:
            envelope.update(probe=probe, probe_digest=digest(probe))
        if presentation:
            # Presentation owns the outer terminal's worktree, not the native
            # no-tools process cwd or its write permissions. Both are frozen.
            envelope["presentation"] = presentation
        # Dataclass tuple fields (notably capacity_command) deserialize as JSON
        # lists. Compare the persisted wire form when reconciling a prepared job.
        envelope = json.loads(json.dumps(envelope))
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

    @fresh_reads()
    def launch(self, request_path: Path) -> dict[str, Any]:
        envelope = read_json(request_path)
        directory = request_path.parent
        validate_control_directory(directory, envelope["permissions"], envelope.get("grants", {}))
        candidate = RoleCandidate(**envelope["selection"])
        if envelope.get("probe") is not None:
            from .campaign_qualification import validate_probe_envelope

            validate_probe_envelope(envelope)
            validate_probe_capability(envelope["capability"], candidate)
        else:
            _validate_capability(
                envelope["capability"], candidate, permissions=envelope["permissions"]
            )
        with file_lock(directory / "launch.lock"):
            intent = directory / "launch-intent.json"
            if intent.exists() and read_json(intent).get("phase") != "before-window":
                return self.observe(request_path)
            write_json(
                intent,
                {
                    "launch_id": envelope["launch_id"],
                    "generation": envelope["generation"],
                    "phase": "before-window",
                },
            )
            try:
                presentation = envelope.get("presentation", {})
                terminal_cwd = Path(presentation.get("workspace", str(directory)))
                # Jobs share a session, but execute in independent windows. Only
                # the check/create mutation needs a repository-local mutex.
                with file_lock(directory.parent / "mux-session.lock"):
                    if not self.mux.has_session(envelope["session"]):
                        self.mux.new_session(envelope["session"], terminal_cwd)
            except (OSError, MultiplexerError) as exc:
                write_json(
                    intent,
                    {
                        **read_json(intent),
                        "pre_window_error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                )
                raise
            argv = [sys.executable, "-m", "bmad_loop.campaign_headless_worker", str(request_path)]
            # From this point, a lost acknowledgement is uncertain and must be
            # reconciled by window/startup identity rather than blindly replayed.
            write_json(intent, {**read_json(intent), "phase": "window-pending"})
            native_id = self.mux.new_window(
                envelope["session"],
                envelope["window_name"],
                terminal_cwd,
                (
                    {"BMAD_LOOP_PARADEV_WORKSPACE_ID": presentation["workspace_id"]}
                    if presentation
                    else {}
                ),
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
        if presentation:
            self.refresh_presentation(request_path, presentation.get("navigation_prefix", ""))
        return {
            "state": "running",
            "run_dir": str(directory),
            "native_id": native_id,
            "presentation_status": self._presentation_status(directory),
        }

    def refresh_presentation(self, request_path: Path, navigation_prefix: str = "") -> dict | None:
        """Refresh display-only links without changing frozen input or borrowing actor names."""
        from .campaign_titles import navigation_title

        envelope = read_json(request_path)
        if not envelope.get("presentation"):
            return None
        directory = request_path.parent
        window = read_json(directory / "window.json")
        if window.get("launch_id") != envelope["launch_id"]:
            raise CampaignError("Headless title target differs from owned launch")
        prefix = navigation_prefix or navigation_title(
            epics=("unbound",), issues=("none",), prs=("none",)
        )
        request = envelope["request"]
        detail = " · ".join(
            str(value) for value in (request["id"], request["role"], envelope["selection"]["model"])
        )
        title = prefix + " · " + " ".join(detail.split())[: max(0, 177 - len(prefix))]
        status = {
            "state": "confirmed",
            "title": title,
            "requested_title": title,
            "terminal_id": window["native_id"],
        }
        try:
            displayed = getattr(self.mux, "set_display_title")(
                window["native_id"], title, navigation_prefix=prefix
            )
            if isinstance(displayed, str):
                status["title"] = displayed
        except (AttributeError, OSError, MultiplexerError) as exc:
            status.update(state="unavailable", reason=str(exc))
        write_json(directory / "presentation.json", status)
        return status

    @staticmethod
    def _presentation_status(directory):
        path = directory / "presentation.json"
        return read_json(path) if path.exists() else None

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
            return {
                "state": "running",
                "native_id": window["native_id"],
                "presentation_status": self._presentation_status(directory),
            }
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
            quiescent=True,
            completion_evidence="owned-window-death-and-process-readback",
            presentation_status=self._presentation_status(directory),
        )
        return {"state": "finished", "receipt": receipt, "run_dir": str(directory)}

    def terminate(self, request_path: Path) -> dict[str, Any]:
        envelope = read_json(request_path)
        write_json(
            request_path.parent / "stop.json",
            {"launch_id": envelope["launch_id"], "generation": envelope["generation"]},
        )
        return {"state": "stop_requested", "launch_id": envelope["launch_id"]}
