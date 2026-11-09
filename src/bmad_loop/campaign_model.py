"""Campaign specifications and durable records, independent of live integrations.

A campaign owns a graph of qualified units. Local engine completion and remote
publication are deliberately different states; only delivered dependencies release
work. Configuration is resolved once, serialized with the campaign and reused on
resume. Operational integrations must not fill unknown observations with success.
"""

from __future__ import annotations

import copy
import math
import re
import tomllib
from collections.abc import MutableSequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1


class CampaignError(ValueError):
    """Invalid campaign input or incompatible persisted state."""


class UnitState(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    DRAFT = "draft"
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    LOCALLY_COMPLETE = "locally_complete"
    SYNCING = "syncing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    READY_TO_PUBLISH = "ready_to_publish"
    MERGE_REQUESTED = "merge_requested"
    MERGED_PENDING_SYNC = "merged_pending_sync"
    DELIVERED = "delivered"
    CANCELED = "canceled"


@dataclass(frozen=True)
class SourceSelector:
    kind: str
    initiative: str = ""
    epics: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    path: str = ""
    independent: bool = False
    id: str = ""
    store_remote: str = ""
    store_branch: str = ""
    scheduler_config: str = ""
    completion_scope: str = "epic"


@dataclass(frozen=True)
class RoleCandidate:
    profile: str
    model: str = ""
    effort: str = ""
    provider_family: str = ""
    capacity_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoleSpec:
    pool: str = ""
    profile: str = ""
    model: str = ""
    effort: str = ""
    max_sessions: int = 1
    budget_usd: float | None = None
    timeout_s: int = 3600
    max_tokens: int | None = None
    capacity_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceSpec:
    verify_slots: int = 1
    max_sessions: int = 4
    budget_usd: float | None = None


@dataclass(frozen=True)
class GitHubSpec:
    enabled: bool = True
    repository: str = ""
    merge_mode: str = "direct-strict"
    merge_method: str = "merge"
    required_checks: tuple[str, ...] = ()
    hold_labels: tuple[str, ...] = ("bmad:hold",)
    require_independent_review: bool = True
    candidate_command: tuple[str, ...] = ()
    candidate_check_context: str = ""
    candidate_check_app_id: int = 0
    candidate_producer_probe: tuple[str, ...] = ()
    candidate_publish: str = "native"
    merge_command: tuple[str, ...] = ()
    merge_executor: tuple[str, ...] = ()
    review_users: tuple[str, ...] = ()
    review_teams: tuple[str, ...] = ()
    recent_days: int = 30
    inventory_metadata_bytes: int = 64 * 1024 * 1024
    inventory_pr_detail_bytes: int = 16 * 1024 * 1024
    max_base_refreshes: int = 8
    max_remote_head_refreshes: int = 8
    allow_publication_wait: bool = False
    allow_admin_merge: bool = False


@dataclass(frozen=True)
class ParadevSpec:
    reflect: bool = False
    project_id: str = ""
    group_by: str = "story"
    tasks: bool = False
    worktree_root: str = ""
    retention: int = 20


@dataclass(frozen=True)
class CheckpointSpec:
    push: bool = True
    target_interval_s: int = 600
    max_backup_failures: int = 5


@dataclass(frozen=True)
class DecisionSpec:
    mode: str = "evidence-first"
    sources: tuple[str, ...] = ()
    transcript_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolutionSpec:
    mode: str = "diagnose"
    max_attempts_per_unit: int = 2
    max_sessions: int = 2


@dataclass(frozen=True)
class IntakeSpec:
    issues: str = "selected"
    labels: tuple[str, ...] = ()
    issue_numbers: tuple[int, ...] = ()
    batch_size: int = 20
    allow_new_units: bool = False
    max_new_units: int = 20
    max_triage_attempts: int = 2
    allowed_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommunicationSpec:
    mode: str = "off"
    room: str = ""
    max_context_events: int = 50


@dataclass(frozen=True)
class PlanningSpec:
    brief: str = ""
    stages: tuple[str, ...] = ("requirements", "architecture", "decomposition", "planning_review")
    enabled: bool = False
    skills: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ReasoningSpec:
    enabled: bool = False
    capabilities: str = ""
    max_sessions: int = 4
    max_attempts: int = 2
    max_turns: int = 4
    timeout_s: int = 1800
    packet_token_limit: int = 160000
    purposes: tuple[str, ...] = (
        "routing",
        "triage",
        "semantic_sync",
        "priority",
        "graph_review",
        "report",
        "handoff_map",
        "handoff_reduce",
        "handoff_audit",
    )


@dataclass(frozen=True)
class UnitSpec:
    id: str
    source: str
    epic: str
    title: str
    selector: str = ""
    after: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    kind: str = "story"
    acceptance: tuple[str, ...] = ()
    order: int = 0
    parent: str = ""
    initiative: str = ""
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class CampaignSpec:
    name: str
    schema_version: int = SCHEMA_VERSION
    remote: str = "origin"
    target: str = "main"
    sources: tuple[SourceSelector, ...] = ()
    units: tuple[UnitSpec, ...] = ()
    max_workers: int = 1
    max_active_epics: int = 1
    max_in_flight_units: int = 4
    max_review_sessions: int = 1
    max_workers_per_epic: int | None = None
    resources: ResourceSpec = field(default_factory=ResourceSpec)
    github: GitHubSpec = field(default_factory=GitHubSpec)
    paradev: ParadevSpec = field(default_factory=ParadevSpec)
    checkpoints: CheckpointSpec = field(default_factory=CheckpointSpec)
    decisions: DecisionSpec = field(default_factory=DecisionSpec)
    resolution: ResolutionSpec = field(default_factory=ResolutionSpec)
    intake: IntakeSpec = field(default_factory=IntakeSpec)
    communication: CommunicationSpec = field(default_factory=CommunicationSpec)
    planning: PlanningSpec = field(default_factory=PlanningSpec)
    reasoning: ReasoningSpec = field(default_factory=ReasoningSpec)
    role_pools: tuple[tuple[str, tuple[RoleCandidate, ...]], ...] = ()
    roles: tuple[tuple[str, RoleSpec], ...] = ()
    verify_commands: tuple[tuple[str, ...], ...] = ()
    archive_roots: tuple[str, ...] = ()
    worktree_root: str = ""
    poll_interval_s: float = 1.0
    ownership: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def role(self, name: str) -> RoleSpec:
        return dict(self.roles).get(name, RoleSpec())

    def candidates(self, name: str) -> tuple[RoleCandidate, ...]:
        role = self.role(name)
        if role.pool:
            return dict(self.role_pools)[role.pool]
        return (
            RoleCandidate(
                role.profile, role.model, role.effort, capacity_command=role.capacity_command
            ),
        )


@dataclass(frozen=True)
class EvidenceCoordinates:
    repository: str
    unit: str
    head: str
    base: str
    graph_revision: int
    config_revision: int
    source_revision: str = ""
    provider_family: str = ""
    model: str = ""


@dataclass
class Hold:
    kind: str
    reason: str
    id: str = ""
    actor: str = ""
    created_at: float = 0
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkerClaim:
    unit: str
    attempt: int
    generation: int
    launch_id: str
    pid: int | None = None
    process_identity: str = ""
    run_id: str = ""
    run_dir: str = ""
    workspace: str = ""
    branch: str = ""
    session_ids: list[str] = field(default_factory=list)
    status: str = "prepared"
    role: str = "execution"
    resource: str = "execution"
    started_at: float = 0
    quiescent: bool = False
    writes: bool = False


@dataclass
class CampaignUnit:
    spec: UnitSpec
    state: UnitState = UnitState.PENDING
    attempt: int = 0
    hold: Hold | None = None
    claim: WorkerClaim | None = None
    bindings: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    resume_state: UnitState | None = None
    retry_at: float | None = None
    failure_attempts: int = 0
    resolution_attempts: int = 0
    review_attempts: int = 0
    legacy_outcome: str = ""

    @property
    def id(self) -> str:
        return self.spec.id


@dataclass
class OutboxAction:
    id: str
    kind: str
    unit: str
    payload: dict[str, Any]
    generation: int
    state: str = "prepared"
    receipt: dict[str, Any] = field(default_factory=dict)
    next_retry_at: float | None = None
    error: str = ""


@dataclass
class CampaignSnapshot:
    id: str
    repository: str
    spec: CampaignSpec
    units: dict[str, CampaignUnit] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    revision: int = 0
    generation: int = 0
    graph_revision: int = 1
    config_revision: int = 1
    phase: str = "active"
    outbox: list[OutboxAction] = field(default_factory=list)
    events: MutableSequence[dict[str, Any]] = field(default_factory=list)
    processed_commands: list[str] = field(default_factory=list)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    bindings: dict[str, Any] = field(default_factory=dict)
    last_epic: str = ""
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    common_dir: str = ""
    root: str = ""
    remote_url: str = ""
    created_at: float = 0
    updated_at: float = 0

    def clone(self) -> CampaignSnapshot:
        from .campaign_clone import clone

        return clone(self)


def _table(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise CampaignError(f"{name} must be a table")
    return value


def _fields(raw: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise CampaignError(f"{name}: unknown fields: {', '.join(sorted(unknown))}")


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) or not v for v in value):
        raise CampaignError(f"{name} must be an array of nonempty strings")
    return tuple(value)


def _positive(value: Any, name: str) -> int:
    if type(value) is not int or value < 1:
        raise CampaignError(f"{name} must be a positive integer")
    return value


def _construct(cls: Any, value: Any, name: str) -> Any:
    raw = _table(value, name)
    defaults = {f.name: f for f in cls.__dataclass_fields__.values()}
    _fields(raw, set(defaults), name)
    out = dict(raw)
    for key, item in raw.items():
        if key in {
            "allowed_scopes",
            "capacity_command",
            "required_checks",
            "hold_labels",
            "candidate_command",
            "candidate_producer_probe",
            "merge_command",
            "merge_executor",
            "review_users",
            "review_teams",
            "sources",
            "transcript_sources",
            "labels",
            "stages",
            "purposes",
        }:
            out[key] = _strings(item, f"{name}.{key}")
        elif key in {"inventory_metadata_bytes", "inventory_pr_detail_bytes"}:
            _positive(item, f"{name}.{key}")
        elif key == "candidate_check_app_id":
            if type(item) is not int or item < 0:
                raise CampaignError("github.candidate_check_app_id must be a nonnegative integer")
        elif key in {
            "verify_slots",
            "max_sessions",
            "target_interval_s",
            "max_attempts_per_unit",
            "batch_size",
            "retention",
            "max_context_events",
            "recent_days",
            "max_base_refreshes",
            "max_remote_head_refreshes",
            "max_backup_failures",
            "max_new_units",
            "max_triage_attempts",
            "timeout_s",
            "max_tokens",
            "max_turns",
            "max_attempts",
            "packet_token_limit",
        }:
            if item is not None:
                _positive(item, f"{name}.{key}")
        elif key in {
            "enabled",
            "reflect",
            "tasks",
            "push",
            "require_independent_review",
            "allow_new_units",
            "allow_publication_wait",
            "allow_admin_merge",
        }:
            if type(item) is not bool:
                raise CampaignError(f"{name}.{key} must be a boolean")
        elif key == "budget_usd":
            if item is not None and (
                type(item) not in (float, int) or not math.isfinite(item) or item <= 0
            ):
                raise CampaignError(f"{name}.{key} must be positive or absent")
        elif key == "issue_numbers":
            if not isinstance(item, (list, tuple)):
                raise CampaignError(f"{name}.{key} must be an array")
            out[key] = tuple(_positive(n, f"{name}.{key}") for n in item)
        elif key == "skills":
            skills = _table(item, f"{name}.skills") if isinstance(item, dict) else dict(item)
            if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in skills.items()):
                raise CampaignError(f"{name}.skills must map stages to skill names")
            out[key] = tuple(skills.items())
        elif not isinstance(item, str):
            raise CampaignError(f"{name}.{key} must be a string")
    return cls(**out)


def unit_from_dict(raw: Mapping[str, Any]) -> UnitSpec:
    _fields(raw, set(UnitSpec.__dataclass_fields__), "unit")
    out = dict(raw)
    for key in ("after", "scopes", "acceptance", "paths"):
        out[key] = _strings(raw.get(key, []), f"unit.{key}")
    for key in ("id", "source", "epic", "title"):
        if not isinstance(out.get(key), str) or not out[key]:
            raise CampaignError(f"unit.{key} must be a nonempty string")
    if type(out.get("order", 0)) is not int:
        raise CampaignError("unit.order must be an integer")
    for key in ("selector", "kind", "parent", "initiative"):
        if key in out and not isinstance(out[key], str):
            raise CampaignError(f"unit.{key} must be a string")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", out["id"]):
        raise CampaignError("unit.id must be a qualified identifier")
    for scope in out["scopes"]:
        normalize_scope(scope)
    unit = UnitSpec(**out)
    product_scopes(unit)
    return unit


def validate_graph(
    units: tuple[UnitSpec, ...] | list[UnitSpec], *, external_prefixes: tuple[str, ...] = ()
) -> None:
    indexed = {unit.id: unit for unit in units}
    if len(indexed) != len(units):
        raise CampaignError("duplicate qualified unit IDs")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise CampaignError(f"dependency cycle involving {key}")
        if key in visited:
            return
        if key not in indexed:
            if any(key.startswith(prefix) for prefix in external_prefixes):
                return
            raise CampaignError(f"unknown dependency {key}")
        visiting.add(key)
        for dependency in indexed[key].after:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in indexed:
        visit(key)
    for unit in units:
        product_scopes(unit)
        if (
            unit.parent
            and unit.parent not in indexed
            and not any(unit.parent.startswith(prefix) for prefix in external_prefixes)
        ):
            raise CampaignError(f"{unit.id}: unknown parent {unit.parent}")
        ancestors = {unit.id}
        parent = unit.parent
        while parent:
            if parent in ancestors:
                raise CampaignError(f"parent cycle involving {unit.id}")
            ancestors.add(parent)
            if parent not in indexed:
                break
            parent = indexed[parent].parent


def normalize_scope(scope: str) -> str:
    """Canonical resource/path scope; an empty set of scopes means unknown."""
    if scope.startswith("resource:"):
        if not scope.removeprefix("resource:"):
            raise CampaignError("empty resource scope")
        return scope
    scope = scope.replace("\\", "/").rstrip("/")
    if scope in {"*", "."}:
        return "*"
    if (
        not scope
        or scope.startswith("/")
        or ":" in scope
        or any(part in {"", ".."} for part in scope.split("/"))
    ):
        raise CampaignError(f"invalid ownership scope {scope!r}")
    normalized = "/".join(part for part in scope.split("/") if part != ".")
    return normalized or "*"


def product_scopes(unit: UnitSpec) -> tuple[str, ...]:
    """File authority bounded by admission; affected paths may only narrow it.

    Only a genuinely unknown footprint reserves the whole repository. A named
    resource grants no file authority. Validate direct specs and old snapshots at
    every writer boundary as well as when accepting a graph.
    """
    declared = tuple(normalize_scope(value) for value in unit.scopes)
    scopes = tuple(value for value in declared if not value.startswith("resource:"))
    paths = tuple(normalize_scope(value) for value in unit.paths)
    if any(value.startswith("resource:") for value in paths):
        raise CampaignError(f"{unit.id}: unit.paths must contain file paths")
    if declared and any(
        not any(scope == "*" or path == scope or path.startswith(scope + "/") for scope in scopes)
        for path in paths
    ):
        raise CampaignError(f"{unit.id}: unit.paths widens declared ownership scopes")
    return paths or (scopes if declared else ("*",))


def scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left or not right:
        return True
    for a in map(normalize_scope, left):
        for b in map(normalize_scope, right):
            if a == "*" or b == "*" or a == b:
                return True
            if (
                not a.startswith("resource:")
                and not b.startswith("resource:")
                and (a.startswith(b + "/") or b.startswith(a + "/"))
            ):
                return True
    return False


def spec_from_dict(value: Mapping[str, Any]) -> CampaignSpec:
    raw = dict(value)
    _fields(raw, set(CampaignSpec.__dataclass_fields__), "campaign")
    if raw.get("schema_version") != SCHEMA_VERSION or type(raw.get("schema_version")) is not int:
        raise CampaignError(f"campaign.schema_version must be {SCHEMA_VERSION}")
    if not isinstance(raw.get("name"), str) or not raw["name"].strip():
        raise CampaignError("campaign.name must be nonempty")
    for key in ("max_workers", "max_active_epics", "max_in_flight_units", "max_review_sessions"):
        if key in raw:
            _positive(raw[key], key)
    if raw.get("max_workers_per_epic") is not None:
        _positive(raw["max_workers_per_epic"], "max_workers_per_epic")
    for key in ("remote", "target", "worktree_root"):
        if key in raw and (
            not isinstance(raw[key], str) or (not raw[key] and key != "worktree_root")
        ):
            raise CampaignError(f"{key} must be a nonempty string")
    if raw.get("remote", "origin").startswith("-") or raw.get("target", "main").startswith("-"):
        raise CampaignError("remote and target must not be options")
    interval = raw.get("poll_interval_s", 1.0)
    if type(interval) not in (float, int) or not math.isfinite(interval) or interval <= 0:
        raise CampaignError("poll_interval_s must be positive")
    sections = {
        "resources": ResourceSpec,
        "github": GitHubSpec,
        "paradev": ParadevSpec,
        "checkpoints": CheckpointSpec,
        "decisions": DecisionSpec,
        "resolution": ResolutionSpec,
        "intake": IntakeSpec,
        "communication": CommunicationSpec,
        "planning": PlanningSpec,
        "reasoning": ReasoningSpec,
    }
    for name, cls in sections.items():
        raw[name] = _construct(cls, raw.get(name, {}), name)
    sources: list[SourceSelector] = []
    if not isinstance(raw.get("sources", []), (list, tuple)):
        raise CampaignError("sources must be an array")
    for i, value in enumerate(raw.get("sources", [])):
        source = _table(value, "source")
        _fields(source, set(SourceSelector.__dataclass_fields__), "source")
        if source.get("kind") not in {"tickets", "stories", "sprint", "sprint-status"}:
            raise CampaignError("source.kind must be tickets, stories or sprint-status")
        params = dict(source)
        if params["kind"] == "sprint":
            params["kind"] = "sprint-status"
        for key in ("epics", "units"):
            params[key] = _strings(params.get(key, []), f"source.{key}")
        for key in ("initiative", "path", "id", "store_remote", "store_branch", "scheduler_config"):
            if key in params and not isinstance(params[key], str):
                raise CampaignError(f"source.{key} must be a string")
        if params.get("scheduler_config") and params["kind"] != "tickets":
            raise CampaignError("source.scheduler_config applies only to tickets")
        if "independent" in params and type(params["independent"]) is not bool:
            raise CampaignError("source.independent must be boolean")
        if not isinstance(params.get("completion_scope", "epic"), str) or params.get(
            "completion_scope", "epic"
        ) not in {"epic", "selected-units"}:
            raise CampaignError("source.completion_scope must be epic or selected-units")
        if params.get("completion_scope") == "selected-units" and (
            params["kind"] != "tickets" or not params["units"]
        ):
            raise CampaignError("selected-units completion requires explicit ticket source.units")
        store_remote, store_branch = params.get("store_remote", ""), params.get("store_branch", "")
        if bool(store_remote) != bool(store_branch):
            raise CampaignError(
                "versioned source publication requires both store_remote and store_branch"
            )
        if any(
            value.startswith("-") or any(ord(ch) <= 32 or ord(ch) == 127 for ch in value)
            for value in (store_remote, store_branch)
            if value
        ):
            raise CampaignError(
                "source publication remote/branch must be explicit names, not options or whitespace"
            )
        params.setdefault("id", f"{params['kind']}-{i + 1}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", params["id"]):
            raise CampaignError("source.id must be a stable identifier")
        sources.append(SourceSelector(**params))
    if len({s.id for s in sources}) != len(sources):
        raise CampaignError("duplicate source IDs")
    raw["sources"] = tuple(sources)
    unit_values = raw.get("units", [])
    if not isinstance(unit_values, (list, tuple)):
        raise CampaignError("units must be an array")
    raw["units"] = tuple(unit_from_dict(_table(u, "unit")) for u in unit_values)
    pools = raw.get("role_pools", {})
    if not isinstance(pools, dict):
        pools = dict(pools)
    parsed_pools: list[tuple[str, tuple[RoleCandidate, ...]]] = []
    for name, pool in pools.items():
        if isinstance(pool, dict):
            _fields(pool, {"candidates"}, f"role_pools.{name}")
            candidates = pool.get("candidates", [])
        else:
            candidates = pool
        if not isinstance(candidates, (list, tuple)) or not candidates:
            raise CampaignError(f"role_pools.{name} must have candidates")
        parsed: list[RoleCandidate] = []
        for item in candidates:
            candidate = _construct(RoleCandidate, item, "candidate")
            if not candidate.profile or not candidate.model:
                raise CampaignError("pool candidate requires explicit profile and model")
            parsed.append(candidate)
        if len(set(parsed)) != len(parsed):
            raise CampaignError(f"role_pools.{name}: duplicate candidate")
        parsed_pools.append((name, tuple(parsed)))
    raw["role_pools"] = tuple(parsed_pools)
    roles = raw.get("roles", {})
    if not isinstance(roles, dict):
        roles = dict(roles)
    parsed_roles: list[tuple[str, RoleSpec]] = []
    allowed_roles = {
        "planning",
        "planning_review",
        "triage",
        "escalation",
        "decision_review",
        "conflict_repair",
        "publication_review",
        "native_review_pair",
        "execution",
        "execution_review",
        "reasoning",
        "routing",
        "summarization",
        "handoff_map",
        "handoff_reduce",
        "reporting",
        "handoff_audit",
        "graph_review",
    }
    for name, value in roles.items():
        if name not in allowed_roles:
            raise CampaignError(f"unknown campaign role {name}")
        role = _construct(RoleSpec, value, f"roles.{name}")
        if role.pool and (role.profile or role.model or role.effort or role.capacity_command):
            raise CampaignError(f"roles.{name}: choose pool or exact profile/model")
        if role.pool and role.pool not in pools:
            raise CampaignError(f"roles.{name}: unknown pool {role.pool}")
        if role.model and not role.profile:
            raise CampaignError(f"roles.{name}: exact model requires profile")
        parsed_roles.append((name, role))
    raw["roles"] = tuple(parsed_roles)
    commands = raw.get("verify_commands", [])
    if not isinstance(commands, (list, tuple)):
        raise CampaignError("verify_commands must be argv arrays")
    raw["verify_commands"] = tuple(_strings(command, "verify_commands") for command in commands)
    if any(not command for command in raw["verify_commands"]):
        raise CampaignError("verification command cannot be empty")
    raw["archive_roots"] = _strings(raw.get("archive_roots", []), "archive_roots")
    ownership = raw.get("ownership", {})
    if not isinstance(ownership, dict):
        ownership = dict(ownership)
    resolved_ownership = []
    for unit_id, scopes in ownership.items():
        if not isinstance(unit_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]*", unit_id
        ):
            raise CampaignError("ownership keys must be qualified unit IDs")
        parsed_scopes = _strings(scopes, f"ownership.{unit_id}")
        if not parsed_scopes:
            raise CampaignError("ownership overrides must name at least one scope")
        resolved_ownership.append(
            (unit_id, tuple(normalize_scope(scope) for scope in parsed_scopes))
        )
    raw["ownership"] = tuple(resolved_ownership)
    spec = CampaignSpec(**raw)
    if not spec.sources and not spec.units and not spec.planning.enabled:
        raise CampaignError("campaign requires sources, explicit units or planning")
    if spec.github.merge_mode not in {
        "direct-strict",
        "direct-authorized",
        "queue",
        "project-command",
    }:
        raise CampaignError(
            "github.merge_mode must be direct-strict, direct-authorized, queue or project-command"
        )
    if spec.github.merge_mode == "project-command" and not spec.github.merge_command:
        raise CampaignError("project-command requires a configured maintained publication gate")
    if spec.github.allow_admin_merge and (
        spec.github.merge_mode != "direct-authorized" or not spec.github.repository
    ):
        raise CampaignError(
            "allow_admin_merge requires an explicit repository and direct-authorized mode"
        )
    if spec.github.merge_mode == "direct-authorized" and not spec.github.allow_admin_merge:
        raise CampaignError("direct-authorized requires explicit allow_admin_merge authorization")
    if spec.github.merge_executor and spec.github.merge_mode != "project-command":
        raise CampaignError("github.merge_executor requires project-command mode")
    if set(spec.reasoning.purposes) - set(ReasoningSpec().purposes):
        raise CampaignError("unknown reasoning purpose")
    if spec.github.candidate_publish not in {"native", "external"}:
        raise CampaignError("github.candidate_publish must be native or external")
    if spec.github.merge_method != "merge":
        raise CampaignError(
            "github.merge_method must be merge: campaigns preserve all checkpoint commits and agent history; squash and rebase are prohibited"
        )
    if spec.paradev.group_by not in {"story", "epic"}:
        raise CampaignError("paradev.group_by must be story or epic")
    if spec.communication.mode not in {"off", "observe", "managed"}:
        raise CampaignError("communication mode requires off, observe or managed")
    if spec.communication.mode == "managed" and (
        not spec.communication.room or not Path(spec.communication.room).is_absolute()
    ):
        raise CampaignError("managed communication requires an explicit absolute room")
    if spec.decisions.mode not in {"off", "evidence-first"}:
        raise CampaignError("decisions.mode must be off or evidence-first")
    if spec.resolution.mode not in {"off", "diagnose", "bounded-auto"}:
        raise CampaignError("resolution.mode must be off, diagnose or bounded-auto")
    for scope in spec.intake.allowed_scopes:
        normalize_scope(scope)
    if spec.intake.allow_new_units and not spec.intake.allowed_scopes:
        raise CampaignError("automatic new intake units require explicit allowed_scopes")
    if spec.intake.issues not in {"off", "selected", "all-open-and-new"}:
        raise CampaignError("intake.issues must be off, selected or all-open-and-new")
    if spec.max_in_flight_units < spec.max_workers:
        raise CampaignError("max_in_flight_units must cover max_workers")
    validate_graph(
        list(spec.units),
        external_prefixes=tuple(
            f"{source.id}:{source.initiative or 'project'}:" for source in spec.sources
        ),
    )
    return spec


def parse_spec(path: Path | str) -> CampaignSpec:
    try:
        with Path(path).open("rb") as stream:
            return spec_from_dict(tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CampaignError(f"cannot read campaign specification: {exc}") from exc


def source_to_dict(source: SourceSelector) -> dict[str, Any]:
    """Preserve existing source identity hashes for the legacy default scope."""
    raw = asdict(source)
    if source.completion_scope == "epic":
        raw.pop("completion_scope")
    return raw


def spec_to_dict(spec: CampaignSpec) -> dict[str, Any]:
    raw = asdict(spec)
    raw["sources"] = tuple(source_to_dict(source) for source in spec.sources)
    raw["role_pools"] = {
        name: {"candidates": [asdict(c) for c in candidates]}
        for name, candidates in spec.role_pools
    }
    raw["roles"] = {name: asdict(role) for name, role in spec.roles}
    raw["planning"]["skills"] = dict(spec.planning.skills)
    raw["ownership"] = dict(spec.ownership)
    return raw


def snapshot_to_dict(
    snapshot: CampaignSnapshot, *, include_events: bool = True, include_actions: bool = True
) -> dict[str, Any]:
    # The persisted journal can be lazy. Never traverse it just to serialize hot
    # authority; explicit exports still include the complete event history.
    hot = copy.copy(snapshot)
    hot.events = []
    raw = asdict(hot)
    if include_actions:
        from .campaign_action_history import actions

        raw["outbox"] = [asdict(row) for row in actions(snapshot)]
    if include_events:
        raw["events"] = list(snapshot.events)
    raw["spec"] = spec_to_dict(snapshot.spec)
    return raw


def snapshot_from_dict(value: Mapping[str, Any]) -> CampaignSnapshot:
    raw = dict(value)
    if raw.get("schema_version") != SCHEMA_VERSION or type(raw.get("schema_version")) is not int:
        raise CampaignError("unsupported campaign snapshot schema")
    _fields(raw, set(CampaignSnapshot.__dataclass_fields__), "snapshot")
    raw["spec"] = spec_from_dict(_table(raw["spec"], "snapshot.spec"))
    units: dict[str, CampaignUnit] = {}
    for key, item in _table(raw.get("units", {}), "snapshot.units").items():
        unit = dict(item)
        unit["spec"] = unit_from_dict(_table(unit["spec"], "unit.spec"))
        if unit["spec"].id != key:
            raise CampaignError("snapshot unit key disagrees with qualified ID")
        unit["state"] = UnitState(unit["state"])
        if unit.get("resume_state") is not None:
            unit["resume_state"] = UnitState(unit["resume_state"])
        if unit.get("claim") is not None:
            unit["claim"] = WorkerClaim(**unit["claim"])
        if unit.get("hold") is not None:
            unit["hold"] = Hold(**unit["hold"])
        units[key] = CampaignUnit(**unit)
    raw["units"] = units
    raw["outbox"] = [OutboxAction(**action) for action in raw.get("outbox", [])]
    validate_graph([u.spec for u in units.values()])
    return CampaignSnapshot(**raw)
