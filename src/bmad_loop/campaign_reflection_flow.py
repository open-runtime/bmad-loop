"""Campaign-to-Paradev lifecycle projection without another scheduler.

The supervisor is the sole campaign writer. These helpers return observations
and apply explicitly requested view metadata to the existing owned PTYs; no
helper creates a coding session, decides completion or changes execution claims.
Terminal matching uses recorded run/task environment plus project/worktree
identity, never a title, timing guess or the workspace's last active agent.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Mapping

from . import envvars, runs
from .adapters.base import CodingCLIAdapter, SessionHandle, SessionSpec
from .adapters.paradev_backend import ParadevMultiplexer, ParadevTransportError
from .adapters.paradev_runtime import read_private_json
from .campaign_communication import CommunicationError, HcomObserver, PeerBinding, sibling_context
from .campaign_model import CampaignSnapshot, CampaignUnit, CommunicationSpec, WorkerClaim
from .campaign_paradev import ParadevWorkspaceProvider, ReflectionError, WorkspaceBinding
from .platform_util import atomic_write_text, is_link_like, safe_segment
from .policy import Policy
from .signals import SessionAttribution, _parse_event, is_session_event

# Account homes and account/router configuration intentionally stay intact.
# Only identities inherited from a different actor are removed.
_NATIVE_ACTOR_KEYS = frozenset(
    {
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "GEMINI_SESSION_ID",
        "OPENCODE_SESSION_ID",
        "BMAD_FACTORY_PARADEV_VERIFIED",
    }
)
_HCOM_CONFIGURATION = frozenset({"HCOM_DIR", "HCOM_BIN", "HCOM_CONFIG", "HCOM_DEV_ROOT"})


def reflection_policy(policy: Policy, enabled: bool) -> Policy:
    """Select the bundled native mux in the frozen child policy when opted in."""
    return (
        dataclasses.replace(policy, mux=dataclasses.replace(policy.mux, backend="paradev"))
        if enabled
        else policy
    )


def workspace_binding(unit: CampaignUnit) -> WorkspaceBinding:
    raw = unit.bindings.get("native")
    if not isinstance(raw, dict) or not raw:
        raise ReflectionError("Unit has no persisted Paradev workspace binding")
    try:
        binding = WorkspaceBinding(**raw)
    except (TypeError, ValueError):
        raise ReflectionError("Persisted native workspace binding is invalid") from None
    if (
        binding.unit_id != unit.id
        or binding.path != unit.bindings.get("workspace")
        or binding.branch != unit.bindings.get("branch")
    ):
        raise ReflectionError("Native workspace binding differs from campaign editing ownership")
    return binding


def sanitized_worker_env(
    source: Mapping[str, str],
    *,
    binding: WorkspaceBinding,
    state_dir: Path,
    communication: CommunicationSpec,
) -> dict[str, str]:
    """Remove parent attribution before spawning the engine or standalone stage.

    The native host supplies the new PTY's SUPERSET context later; account homes
    and room configuration are preserved. This does not enroll an actor or mute
    delivery hooks installed by the selected account wrapper.
    """
    if not state_dir.is_absolute():
        raise ReflectionError("Native transport registry must be an absolute external path")
    result = {}
    for key, value in source.items():
        if key.startswith("SUPERSET_") or key in _NATIVE_ACTOR_KEYS:
            continue
        if key.startswith("HCOM_") and key not in _HCOM_CONFIGURATION:
            continue
        if key.startswith("BMAD_LOOP_") and key != envvars.STATE_DIR:
            continue
        result[key] = value
    result.update(binding.worker_env())
    result[envvars.PARADEV_STATE_DIR] = str(state_dir.resolve())
    result[envvars.MUX_BACKEND] = "paradev"
    if communication.room:
        room = Path(communication.room)
        if not room.is_absolute():
            raise ReflectionError("Campaign hcom room must be absolute")
        result["HCOM_DIR"] = str(room.resolve())
    return result


def session_env(
    snapshot: CampaignSnapshot,
    unit: CampaignUnit,
    claim: WorkerClaim,
    *,
    run_dir: Path,
    task_id: str,
    native_state_dir: Path | None = None,
) -> dict[str, str]:
    """Exact hook routing for a standalone role session as well as execution."""
    if not run_dir.is_absolute() or not task_id or "/" in task_id or "\\" in task_id:
        raise ReflectionError("Explicit run/task binding required for a campaign session")
    env = {
        "BMAD_LOOP_MODE": "1",
        "BMAD_LOOP_RUN_DIR": str(run_dir),
        "BMAD_LOOP_TASK_ID": task_id,
        "BMAD_LOOP_EVENTS_DIR": str(runs.events_dir_for(Path(claim.workspace), run_dir.name)),
        "BMAD_LOOP_STORY_KEY": unit.spec.selector or unit.id,
    }
    if snapshot.spec.paradev.reflect:
        if native_state_dir is None:
            raise ReflectionError("Native session requires its persisted transport state root")
        env.update(
            sanitized_worker_env(
                {},
                binding=workspace_binding(unit),
                state_dir=native_state_dir,
                communication=snapshot.spec.communication,
            )
        )
    return env


def observe_adapter(
    adapter: CodingCLIAdapter,
    *,
    directory: Path,
    run_dir: Path,
    claim: WorkerClaim,
    campaign_id: str,
    environment: Mapping[str, str],
    context: str = "",
) -> None:
    """Record real handles while retaining the adapter's completion/finalizer.

    Install once on the owned child adapter before dispatch. Recording failure
    cannot strand an actor by escaping between start_session and run's finally;
    it is visibly retained in child stderr and the adapter's reflection_errors.
    """
    if getattr(adapter, "_campaign_reflection_observed", False):
        raise ReflectionError("Campaign adapter observer is already installed")
    original = adapter.start_session
    errors: list[str] = []
    setattr(adapter, "reflection_errors", errors)

    def start(spec: SessionSpec) -> SessionHandle:
        protocol = {
            "BMAD_LOOP_MODE": "1",
            "BMAD_LOOP_RUN_DIR": str(run_dir),
            "BMAD_LOOP_TASK_ID": spec.task_id,
            "BMAD_LOOP_EVENTS_DIR": str(runs.events_dir_for(Path(claim.workspace), run_dir.name)),
        }
        applied = dataclasses.replace(
            spec, env={**spec.env, **environment, **protocol}, prompt=spec.prompt + context
        )
        handle = original(applied)
        try:
            if safe_segment(handle.task_id) != handle.task_id:
                raise ReflectionError("Adapter returned an unsafe native task id")
            target = directory / "native-sessions" / (handle.task_id + ".json")
            payload = {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "unit_id": claim.unit,
                "attempt": claim.attempt,
                "generation": claim.generation,
                "launch_id": claim.launch_id,
                "run_id": claim.run_id,
                "run_dir": str(run_dir),
                "workspace": claim.workspace,
                "branch": claim.branch,
                "role": spec.role,
                "task_id": handle.task_id,
                "native_id": handle.native_id,
                "launched_ns": handle.launched_ns,
                "pinned_session_id": handle.pinned_session_id,
                "pane_target": handle.pane_target,
            }
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, json.dumps(payload, sort_keys=True), follow_symlinks=False)
        except (OSError, ValueError, ReflectionError) as exc:
            note = "Native handle observation unavailable: " + type(exc).__name__
            errors.append(note)
            try:
                print(note, file=sys.stderr)
            except (OSError, ValueError):
                errors.append("Native handle observation stderr unavailable")
        return handle

    adapter.start_session = start
    setattr(adapter, "_campaign_reflection_observed", True)


def _hook_identity(launch: dict, handle: dict, task_id: str) -> dict:
    raw_directory = (launch.get("env") or {}).get("BMAD_LOOP_EVENTS_DIR")
    if not raw_directory:
        return {"state": "unknown", "detail": "Hook event channel was not recorded"}
    directory = Path(raw_directory)
    if not directory.is_absolute():
        raise ReflectionError("Hook event channel is not absolute")
    if not directory.exists():
        return {"state": "pending", "detail": "No hook events observed"}
    files = sorted(directory.glob("*.json"))
    if len(files) > 2000:
        return {"state": "unknown", "detail": "Hook attribution coverage exceeds observation bound"}
    events = []
    for path in files:
        _read_document(path)  # Bound/regular-file check before the legacy parser.
        event = _parse_event(path)
        if event is not None and is_session_event(event, task_id, handle.get("launched_ns", 0)):
            events.append(event)
    attribution = SessionAttribution(pinned_id=handle.get("pinned_session_id"))
    accepted = []
    for event in sorted(events, key=lambda event: event.ts):
        if attribution.admit(event):
            accepted.append(event)
    own = [event for event in accepted if event.session_id == attribution.bound_id]
    latest = own[-1] if own else None

    def field(name):
        return next((getattr(event, name) for event in reversed(own) if getattr(event, name)), None)

    return {
        "state": "observed" if latest else "pending",
        "cli_session_id": attribution.bound_id,
        "transcript_path": field("transcript_path"),
        "hcom_name": field("hcom_name"),
        "hcom_room": field("hcom_dir"),
        "observed_model": field("model"),
        "detail": "" if latest else "Parent CLI hook attribution not observed",
    }


def _read_document(path: Path) -> dict:
    if is_link_like(path) or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ReflectionError("Worker binding artifact must be a bounded regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReflectionError("Worker binding artifact has an unsupported schema")
    return value


def _run_directories(
    snapshot: CampaignSnapshot, unit: CampaignUnit, claim: WorkerClaim, supplied: tuple[Path, ...]
) -> tuple[Path, ...]:
    directories = [Path(p).resolve() for p in supplied]
    if claim.run_dir:
        request_path = Path(claim.run_dir) / "request.json"
        if request_path.exists():
            request = _read_document(request_path)
            expected = {
                "campaign_id": snapshot.id,
                "unit_id": unit.id,
                "attempt": claim.attempt,
                "generation": claim.generation,
                "launch_id": claim.launch_id,
                "workspace": claim.workspace,
                "branch": claim.branch,
            }
            if any(request.get(k) != v for k, v in expected.items()):
                raise ReflectionError("Worker request differs from native reflection claim")
            if request.get("run_id") != claim.run_id:
                raise ReflectionError("Worker run identity differs from native reflection claim")
            if request.get("kind") == "stage":
                # This is the explicit worker entry-point convention, not a
                # guessed title. Each claim run id is globally unique.
                directories.append((request_path.parent / claim.run_id).resolve())
            for key in ("session_dir", "engine_run_dir"):
                if request.get(key):
                    directories.append(Path(request[key]).resolve())
    if claim.run_id:
        directories.append(runs.run_dir_for(Path(claim.workspace), claim.run_id).resolve())
    return tuple(dict.fromkeys(directories))


def discover_peer_binding(
    snapshot: CampaignSnapshot,
    unit: CampaignUnit,
    claim: WorkerClaim,
    window: dict,
    *,
    observer: HcomObserver,
) -> PeerBinding | None:
    """Join hook-reported hcom identity to its current process/native terminal.

    Never enrolls, sends or adopts a reused name. A title is not an actor id.
    """
    if snapshot.spec.communication.mode == "off":
        return None
    hook = window.get("hook") or {}
    name, room, session = hook.get("hcom_name"), hook.get("hcom_room"), hook.get("cli_session_id")
    if not name or not room or not session:
        return None
    if Path(room).resolve() != observer.room:
        raise ReflectionError("Hook hcom room differs from configured campaign observer")
    rows = observer._read(["list", "--json"])
    if not isinstance(rows, list):
        raise ReflectionError("hcom roster schema unavailable")
    matches = [row for row in rows if row.get("name") == name]
    if len(matches) != 1:
        raise ReflectionError("Hook hcom actor is missing or ambiguous")
    row, context = matches[0], matches[0].get("launch_context") or {}
    if (
        row.get("session_id") != session
        or context.get("env", {}).get("SUPERSET_TERMINAL_ID") != window["terminal_id"]
        or not isinstance(context.get("process_id"), str)
        or not context["process_id"]
    ):
        raise ReflectionError("Hook hcom actor has a different native session/process/terminal")
    binding = PeerBinding(
        snapshot.id,
        unit.id,
        claim.attempt,
        name,
        context["process_id"],
        session,
        window["terminal_id"],
        room,
    )
    observed = observer.roster((binding,))
    if not observed.available:
        raise ReflectionError(observed.detail)
    return binding


def stage_binding(
    snapshot: CampaignSnapshot,
    unit: CampaignUnit,
    claim: WorkerClaim,
    *,
    mux: ParadevMultiplexer,
    run_directories: tuple[Path, ...] = (),
    observer: HcomObserver | None = None,
) -> dict:
    """Observe exact worker terminals without creating a view or guessing a PID.

    Missing bindings are visible. They never prove death or authorize another
    launch. Callers persist the returned projection through their state writer.
    """
    binding = workspace_binding(unit)
    if (
        claim.unit != unit.id
        or claim.workspace != binding.path
        or claim.branch != binding.branch
        or unit.claim is None
        or unit.claim.launch_id != claim.launch_id
    ):
        raise ReflectionError("Reflection claim differs from current unit ownership")
    directories = _run_directories(snapshot, unit, claim, run_directories)
    result = {
        "schema_version": 1,
        "campaign_id": snapshot.id,
        "unit_id": unit.id,
        "launch_id": claim.launch_id,
        "attempt": claim.attempt,
        "generation": claim.generation,
        "workspace_id": binding.workspace_id,
        "windows": [],
        "detail": "",
        "state": "pending",
        "actors_quiescent": False,
    }
    try:
        handles = {}
        if claim.run_dir:
            for path in sorted((Path(claim.run_dir) / "native-sessions").glob("*.json")):
                handle = _read_document(path)
                expected = {
                    "campaign_id": snapshot.id,
                    "unit_id": unit.id,
                    "attempt": claim.attempt,
                    "generation": claim.generation,
                    "launch_id": claim.launch_id,
                    "workspace": claim.workspace,
                    "branch": claim.branch,
                }
                if any(handle.get(key) != value for key, value in expected.items()):
                    raise ReflectionError("Recorded session handle differs from the claimed actor")
                handles[handle["native_id"]] = handle
        records = mux.status(native=False)["windows"]
        for record in records:
            if record.get("workspace") != binding.workspace_id:
                continue
            launch = read_private_json(mux.root / "launches" / record["id"] / "launch.json")
            env = launch.get("env") or {}
            run_dir = env.get("BMAD_LOOP_RUN_DIR")
            if not isinstance(run_dir, str) or Path(run_dir).resolve() not in directories:
                continue
            task_id = env.get("BMAD_LOOP_TASK_ID")
            if (
                not isinstance(task_id, str)
                or not task_id
                or "/" in task_id
                or "\\" in task_id
                or Path(launch.get("cwd", "")).resolve() != Path(binding.path).resolve()
                or launch.get("workspaceId") != binding.workspace_id
            ):
                raise ReflectionError("Native terminal launch binding is malformed or foreign")
            handle = handles.get(record["id"], {})
            if handle and (
                handle.get("task_id") != task_id
                or Path(handle["run_dir"]).resolve() != Path(run_dir).resolve()
            ):
                raise ReflectionError(
                    "Recorded adapter handle differs from the actual terminal launch"
                )
            hook = _hook_identity(launch, handle, task_id)
            row = {
                "phase": handle.get("role", "unknown"),
                "hook": hook,
                "terminal_id": record["id"],
                "session_name": record["session"],
                "task_id": task_id,
                "run_dir": run_dir,
                "state": record["state"],
                "pane_id": record.get("paneId"),
                "native": record.get("native"),
                "heartbeat": None,
                "log_path": str(Path(run_dir) / "logs" / (task_id + ".log")),
            }
            heartbeat = Path(run_dir) / "tasks" / task_id / "heartbeat.json"
            if heartbeat.exists():
                row["heartbeat"] = _read_document(heartbeat)
            if record["state"] in ("active", "launching"):
                row["native"] = mux.native_identity(record["id"])
            row["attribution"] = (
                "confirmed" if row["native"] and row["native"].get("agentSessionId") else "pending"
            )
            if observer is not None and snapshot.spec.communication.mode != "off":
                peer = discover_peer_binding(snapshot, unit, claim, row, observer=observer)
                row["hcom_binding"] = dataclasses.asdict(peer) if peer is not None else None
            result["windows"].append(row)
        result["state"] = "observed" if result["windows"] else "pending"
        if result["windows"]:
            daemon = mux._daemon()
            result["actors_quiescent"] = all(
                window["state"] in ("disposed", "gone", "void")
                and window["terminal_id"] not in daemon
                for window in result["windows"]
            )
        if not result["windows"]:
            result["detail"] = "Worker terminal binding not observed; ownership remains reserved"
    except (
        ParadevTransportError,
        CommunicationError,
        ReflectionError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
    ) as exc:
        result.update(
            state="unknown", detail="Native observation unavailable: " + type(exc).__name__
        )
    return result


def _pr_url(unit: CampaignUnit) -> str:
    raw = unit.bindings.get("pr") or {}
    return raw.get("url", raw.get("html_url", "")) if isinstance(raw, dict) else ""


def reflect(
    snapshot: CampaignSnapshot,
    unit: CampaignUnit,
    claim: WorkerClaim | None,
    *,
    provider: ParadevWorkspaceProvider,
    mux: ParadevMultiplexer,
    run_directories: tuple[Path, ...] = (),
    show_history: bool = False,
) -> dict:
    """Reconcile views of existing actors; never launch a terminal to show logs."""
    binding = workspace_binding(unit)
    metadata = provider.reflect(
        binding,
        campaign_id=snapshot.id,
        epic=unit.spec.epic,
        phase=unit.state.value,
        attempt=unit.attempt,
        pr_url=_pr_url(unit),
        held=unit.hold is not None,
    )
    projection = (
        stage_binding(snapshot, unit, claim, mux=mux, run_directories=run_directories)
        if claim
        else {"state": "pending", "windows": []}
    )
    for window in projection["windows"]:
        if window["state"] in ("active", "launching"):
            observed = provider.bind_terminal(
                binding,
                mux,
                window["terminal_id"],
                phase=window.get("phase", "unknown"),
                attempt=claim.attempt if claim else unit.attempt,
                agent=(window.get("native") or {}).get("definitionId") or "pending",
            )
            window.update(observed)
        elif show_history and claim and claim.quiescent and Path(window["log_path"]).is_file():
            provider.history_view(binding, Path(window["log_path"]))
    return {"schema_version": 1, "workspace": metadata, "stage": projection}


def compose_context(snapshot: CampaignSnapshot, unit: CampaignUnit) -> str:
    """Inject scoped siblings and PR/interface links even without hcom."""
    rows = [
        {
            "id": key,
            "epic": peer.spec.epic,
            "state": peer.state.value,
            "role": peer.claim.role if peer.claim else "",
            "branch": peer.bindings.get("branch", ""),
            "pr_url": _pr_url(peer),
            "ownership": list(peer.spec.scopes),
            "interfaces": list(peer.spec.acceptance),
            "hcom_binding": peer.bindings.get("hcom"),
        }
        for key, peer in snapshot.units.items()
    ]
    context = sibling_context(rows, own_unit=unit.id, campaign_id=snapshot.id)
    context["communication_mode"] = snapshot.spec.communication.mode
    context["automatic_peer_delivery"] = False
    context["incoming_hooks_muted"] = False
    return "\n\nCampaign sibling context (advisory):\n" + json.dumps(context, sort_keys=True)


def observe_communication(snapshot: CampaignSnapshot, *, observer: HcomObserver) -> dict:
    """Optional peer observation retains gaps; it never sends or schedules work."""
    if snapshot.spec.communication.mode == "off":
        return {"mode": "off", "available": True, "peers": [], "detail": ""}
    bindings = []
    for unit in snapshot.units.values():
        raw = unit.bindings.get("hcom")
        if not raw:
            continue
        try:
            binding = PeerBinding(**raw)
        except TypeError:
            raise ReflectionError("Peer communication binding is malformed") from None
        if binding.campaign_id != snapshot.id or binding.unit_id != unit.id:
            raise ReflectionError("Peer communication binding belongs to another campaign unit")
        bindings.append(binding)
    observation = observer.roster(tuple(bindings))
    return {
        "mode": "observe",
        **dataclasses.asdict(observation),
        "automatic_peer_delivery": False,
        "incoming_hooks_muted": False,
        "missing_bindings": [
            unit.id
            for unit in snapshot.units.values()
            if unit.claim and not unit.claim.quiescent and not unit.bindings.get("hcom")
        ],
    }
