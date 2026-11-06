"""Bounded reasoning requests, lifecycle accounting and result validation.

Models run in disposable child sessions. These functions only validate immutable
requests and receipts and record deterministic state; they never invoke a model.
Job identities are independent of unit write leases and may share a parent unit.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .campaign_model import CampaignError, CampaignSnapshot, RoleCandidate

PURPOSES = frozenset(
    {
        "routing",
        "triage",
        "semantic_sync",
        "priority",
        "graph_review",
        "report",
        "handoff_map",
        "handoff_reduce",
        "handoff_audit",
    }
)
TERMINAL = frozenset({"applied", "stale", "canceled"})
STATES = TERMINAL | {
    "proposed",
    "prepared",
    "running",
    "uncertain",
    "completed",
    "validated",
    "failed",
    "capacity_wait",
}
EXECUTION_OUTCOMES = frozenset(
    {"completed", "quota", "authentication", "transport", "canceled", "limit", "failed"}
)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class JobLimits:
    input_tokens: int
    output_tokens: int
    max_turns: int = 4
    timeout_s: int = 1800

    def validate(self) -> None:
        if any(type(value) is not int or value < 1 for value in asdict(self).values()):
            raise CampaignError("reasoning limits must be positive integers")


@dataclass(frozen=True)
class JobRequest:
    id: str
    purpose: str
    role: str
    coordinates: dict[str, Any]
    inputs: tuple[dict[str, Any], ...]
    prompt: str
    output_schema: dict[str, Any]
    limits: JobLimits
    unit: str = ""
    dependencies: tuple[str, ...] = ()
    candidates: tuple[RoleCandidate, ...] = ()
    required: bool = True
    created_at: float = 0
    schema_version: int = 1

    def validate(self) -> None:
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise CampaignError("unsupported reasoning request schema")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.id):
            raise CampaignError("reasoning job ID must be a safe path segment")
        if self.purpose not in PURPOSES or not self.role or not self.prompt:
            raise CampaignError("reasoning request purpose, role and prompt are required")
        if type(self.required) is not bool or not math.isfinite(self.created_at):
            raise CampaignError("invalid reasoning request authority")
        self.limits.validate()
        for name in ("graph_revision", "config_revision"):
            if type(self.coordinates.get(name)) is not int or self.coordinates[name] < 1:
                raise CampaignError("reasoning coordinates require graph/config revisions")
        for name in ("head", "base"):
            if not isinstance(self.coordinates.get(name), str):
                raise CampaignError("reasoning coordinates require head/base bindings")
        identifiers = set()
        for source in self.inputs:
            if (
                not isinstance(source, dict)
                or not isinstance(source.get("source_id"), str)
                or not source["source_id"]
                or source["source_id"] in identifiers
                or not isinstance(source.get("path"), str)
                or not source["path"]
                or not re.fullmatch(r"[a-f0-9]{64}", str(source.get("sha256", "")))
                or type(source.get("tokens")) is not int
                or source["tokens"] < 0
            ):
                raise CampaignError(
                    "reasoning input requires unique identity/hash/path/token bound"
                )
            identifiers.add(source["source_id"])
        if sum(source["tokens"] for source in self.inputs) > self.limits.input_tokens:
            raise CampaignError("reasoning input exceeds its qualified token budget")
        if self.id in self.dependencies or len(set(self.dependencies)) != len(self.dependencies):
            raise CampaignError("reasoning dependencies contain a self edge or duplicate")
        if any(not isinstance(item, str) or not item for item in self.dependencies):
            raise CampaignError("reasoning dependencies require job IDs")
        validate_schema(self.output_schema)


def request_from_dict(raw: Mapping[str, Any]) -> JobRequest:
    value = dict(raw)
    if set(value) - set(JobRequest.__dataclass_fields__):
        raise CampaignError("unknown reasoning request fields")
    try:
        value["limits"] = JobLimits(**value["limits"])
        value["inputs"] = tuple(dict(source) for source in value["inputs"])
        value["dependencies"] = tuple(value.get("dependencies", ()))
        value["candidates"] = tuple(
            RoleCandidate(**{**item, "capacity_command": tuple(item.get("capacity_command", ()))})
            for item in value.get("candidates", ())
        )
        request = JobRequest(**value)
        request.validate()
        return request
    except (TypeError, KeyError, AttributeError) as exc:
        raise CampaignError(f"invalid reasoning request: {exc}") from exc


def prepare(snapshot: CampaignSnapshot, request: JobRequest) -> dict[str, Any]:
    """Queue an immutable logical job; caller commits alongside parent state."""
    request.validate()
    if not snapshot.spec.reasoning.enabled:
        raise CampaignError("reasoning jobs are disabled")
    if request.purpose not in snapshot.spec.reasoning.purposes:
        raise CampaignError("reasoning purpose is not authorized")
    if request.unit and request.unit not in snapshot.units:
        raise CampaignError("reasoning unit is not in this campaign")
    raw = json.loads(json.dumps(asdict(request)))
    prior = snapshot.jobs.get(request.id)
    if prior is not None:
        if prior["request"] != raw:
            raise CampaignError("reasoning job ID was reused for different input")
        return prior
    row = {
        "id": request.id,
        "request": raw,
        "request_digest": digest(raw),
        "state": "proposed",
        "attempt": 0,
        "claim": None,
        "selection": None,
        "receipt": None,
        "result": None,
        "validation": "pending",
        "application": "pending",
        "reason": "",
        "retry_at": None,
        "usage": {},
        "updated_at": request.created_at,
    }
    snapshot.jobs[request.id] = row
    return row


def validate_record(row: Mapping[str, Any]) -> None:
    request = request_from_dict(row["request"])
    if row.get("id") != request.id or row.get("request_digest") != digest(row["request"]):
        raise CampaignError("reasoning request identity changed")
    if row.get("state") not in STATES or type(row.get("attempt")) is not int or row["attempt"] < 0:
        raise CampaignError("invalid reasoning job state")
    claim = row.get("claim")
    if claim is not None and (
        not isinstance(claim, dict)
        or not claim.get("launch_id")
        or type(claim.get("generation")) is not int
        or type(claim.get("quiescent")) is not bool
    ):
        raise CampaignError("reasoning claim has invalid ownership")
    if row["state"] in TERMINAL and claim and not claim["quiescent"]:
        raise CampaignError("terminal reasoning job retains an uncertain/live session")


def live(row: Mapping[str, Any]) -> bool:
    claim = row.get("claim")
    return bool(claim and not claim.get("quiescent"))


def current(
    snapshot: CampaignSnapshot,
    request: JobRequest,
    *,
    semantic_fingerprints: dict[str, str] | None = None,
) -> bool:
    coordinates = request.coordinates
    if coordinates["config_revision"] != snapshot.config_revision:
        return False
    if request.purpose in {"handoff_map", "handoff_reduce"} and coordinates.get("handoff_id"):
        unit = snapshot.units.get(request.unit)
        handoff = unit.evidence.get("handoff", {}) if unit else {}
        # Native transcript prefixes and reducer inputs are immutable artifacts.
        # An unrelated priority/graph edit does not stale their extraction. The
        # subsequent current-constraint audit still binds the current graph.
        return handoff.get("id") == coordinates["handoff_id"]
    if coordinates["graph_revision"] != snapshot.graph_revision:
        return False
    if coordinates.get("context_digest"):
        from .campaign_reasoning_plan import ROLES, fingerprint

        if request.purpose in ROLES:
            cache = {} if semantic_fingerprints is None else semantic_fingerprints
            if request.purpose not in cache:
                cache[request.purpose] = fingerprint(snapshot, request.purpose)
            if coordinates["context_digest"] != cache[request.purpose]:
                return False
    if request.unit:
        unit = snapshot.units[request.unit]
        observed = unit.evidence.get("integrated") or unit.evidence.get("local") or {}
        if any(
            coordinates[name] and coordinates[name] != observed.get(name)
            for name in ("head", "base")
        ):
            return False
    return True


def validate_schema(schema: Any) -> None:
    """The supported JSON Schema subset is explicit, never silently partial."""
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "description",
        "title",
        "$schema",
    }
    if not isinstance(schema, dict) or set(schema) - allowed:
        raise CampaignError("unsupported reasoning output schema keyword")
    if schema.get("type") not in {
        "object",
        "array",
        "string",
        "integer",
        "number",
        "boolean",
        "null",
    }:
        raise CampaignError("reasoning output schema requires an explicit supported type")
    if schema["type"] == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or any(not isinstance(key, str) or key not in properties for key in required)
            or type(schema.get("additionalProperties", False)) is not bool
        ):
            raise CampaignError("invalid reasoning object schema")
        for child in properties.values():
            validate_schema(child)
    if schema["type"] == "array":
        validate_schema(schema.get("items"))


def validate_result(value: Any, schema: Mapping[str, Any], *, path: str = "result") -> None:
    kind = schema["type"]
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if kind == "number":
        valid = type(value) in (int, float) and math.isfinite(value)
    else:
        valid = type(value) is types[kind]
    if not valid:
        raise CampaignError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise CampaignError(f"{path}: value is outside the permitted enumeration")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(schema.get("required", ())) - set(value):
            raise CampaignError(f"{path}: required output fields are missing")
        if not schema.get("additionalProperties", False) and set(value) - set(properties):
            raise CampaignError(f"{path}: unknown output fields")
        for key, item in value.items():
            if key in properties:
                validate_result(item, properties[key], path=f"{path}.{key}")
    if kind == "array":
        for index, item in enumerate(value):
            validate_result(item, schema["items"], path=f"{path}[{index}]")
    for lower, upper, observed in (
        ("minItems", "maxItems", len(value) if kind == "array" else None),
        ("minLength", "maxLength", len(value) if kind == "string" else None),
        ("minimum", "maximum", value if kind in {"integer", "number"} else None),
    ):
        if observed is not None and (
            (lower in schema and observed < schema[lower])
            or (upper in schema and observed > schema[upper])
        ):
            raise CampaignError(f"{path}: output exceeds schema bounds")


def accept_receipt(
    snapshot: CampaignSnapshot, job_id: str, receipt: dict[str, Any], *, now: float | None = None
) -> None:
    """Process a proven-quiescent child outcome, separate from result application."""
    row = snapshot.jobs[job_id]
    request = request_from_dict(row["request"])
    claim = row.get("claim") or {}
    if (
        receipt.get("job_id") != job_id
        or receipt.get("request_digest") != row["request_digest"]
        or receipt.get("launch_id") != claim.get("launch_id")
        or receipt.get("generation") != claim.get("generation")
        or receipt.get("quiescent") is not True
    ):
        raise CampaignError("reasoning receipt does not prove its owned session is quiescent")
    outcome = receipt.get("outcome")
    if outcome not in EXECUTION_OUTCOMES:
        raise CampaignError("reasoning receipt has no typed execution outcome")
    if row.get("receipt") == receipt:
        return
    if row.get("receipt") is not None:
        raise CampaignError("reasoning attempt already has a different terminal receipt")
    usage = receipt.get("usage") or {}
    if not isinstance(usage, dict) or any(
        type(v) not in (int, float) or v < 0 or not math.isfinite(v) for v in usage.values()
    ):
        raise CampaignError("reasoning usage counters are invalid")
    row["receipt"] = receipt
    row["claim"]["quiescent"] = True
    row["updated_at"] = time.time() if now is None else now
    for key, value in usage.items():
        row["usage"][key] = row["usage"].get(key, 0) + value
    if outcome != "completed":
        row["state"] = "capacity_wait" if outcome == "quota" else "failed"
        row["reason"] = receipt.get("reason") or f"reasoning execution {outcome}"
        return
    row["state"] = "completed"
    try:
        if receipt.get("input_complete") is not True:
            raise CampaignError("reasoning input delivery is incomplete or unqualified")
        validate_result(receipt.get("result"), request.output_schema)
        if not current(snapshot, request):
            row["state"], row["validation"], row["reason"] = (
                "stale",
                "stale",
                "reasoning inputs changed",
            )
            return
    except CampaignError as exc:
        row["validation"], row["reason"], row["state"] = "invalid", str(exc), "failed"
        return
    row["result"] = receipt["result"]
    row["validation"], row["state"] = "valid", "validated"
