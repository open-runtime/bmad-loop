"""Trusted execution envelope for a single campaign child.

The envelope lives outside the checkout.  It is deliberately separate from the
legacy execution-surface digest: a campaign freezes operational policy as well.
Children hand recovery to their coordinator before any destructive legacy path.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from .adapters.profile import CLIProfile
from .platform_util import atomic_write_text, is_link_like
from .plugins.grants import GrantSet
from .policy import Policy


class CampaignExecutionError(ValueError):
    """A missing or inconsistent trusted child envelope."""


def _restore(annotation: Any, raw: Any) -> Any:
    """Decode only types declared by the package's frozen dataclasses."""
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation is Any:
        return raw
    if origin in (Union, types.UnionType):
        if raw is None and type(None) in args:
            return None
        for choice in args:
            if choice is type(None):
                continue
            try:
                return _restore(choice, raw)
            except (TypeError, ValueError):
                continue
        raise CampaignExecutionError("snapshot value does not match its declared type")
    if dataclasses.is_dataclass(annotation):
        if not isinstance(raw, dict):
            raise CampaignExecutionError("snapshot dataclass must be an object")
        declared = {f.name for f in dataclasses.fields(annotation)}
        if set(raw) - declared:
            raise CampaignExecutionError("unknown snapshot fields")
        hints = get_type_hints(annotation)
        constructor: Any = annotation
        return constructor(**{key: _restore(hints[key], value) for key, value in raw.items()})
    if origin in (tuple, list):
        if not isinstance(raw, (tuple, list)):
            raise CampaignExecutionError("snapshot sequence expected")
        values = [_restore(args[0], value) for value in raw]
        return tuple(values) if origin is tuple else values
    if origin is dict:
        if not isinstance(raw, dict):
            raise CampaignExecutionError("snapshot mapping expected")
        return {_restore(args[0], k): _restore(args[1], v) for k, v in raw.items()}
    if annotation is Path:
        if not isinstance(raw, str):
            raise CampaignExecutionError("snapshot path expected")
        return Path(raw)
    if annotation in (str, bool, int, float):
        if type(raw) is not annotation and not (annotation is float and type(raw) is int):
            raise CampaignExecutionError(f"snapshot {annotation.__name__} expected")
        return annotation(raw)
    if annotation is type(None) and raw is None:
        return None
    raise CampaignExecutionError(f"unsupported snapshot type: {annotation}")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


@dataclasses.dataclass(frozen=True)
class CampaignExecution:
    campaign_id: str
    unit_id: str
    attempt: int
    generation: int
    config_revision: int
    directory: Path
    policy: Policy
    profiles: dict[str, CLIProfile]
    grants: GrantSet = dataclasses.field(default_factory=GrantSet.empty)
    checkpoint_interval_s: float = 600.0

    def save(self) -> tuple[Path, str]:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "execution.json"
        payload = dataclasses.asdict(self)
        payload["schema_version"] = 1
        text = _json(payload)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if path.exists():
            if path.read_text(encoding="utf-8") != text:
                raise CampaignExecutionError("cannot replace a frozen worker execution envelope")
        else:
            atomic_write_text(path, text)
        return path, digest

    @classmethod
    def load(cls, path: Path, digest: str) -> CampaignExecution:
        if is_link_like(path) or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise CampaignExecutionError("execution snapshot must be a bounded regular file")
        text = path.read_text(encoding="utf-8")
        if hashlib.sha256(text.encode()).hexdigest() != digest:
            raise CampaignExecutionError("execution snapshot digest mismatch")
        raw = json.loads(text)
        if not isinstance(raw, dict) or raw.pop("schema_version", None) != 1:
            raise CampaignExecutionError("unsupported execution snapshot")
        result = _restore(cls, raw)
        if result.directory.resolve() != path.parent.resolve():
            raise CampaignExecutionError("execution directory binding mismatch")
        return result

    def recovery_request(self, task: Any, head: str, *, cause: str) -> dict[str, Any]:
        request = {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "unit_id": self.unit_id,
            "attempt": self.attempt,
            "generation": self.generation,
            "config_revision": self.config_revision,
            "cause": cause,
            "head": head,
            "task": task.to_dict(),
        }
        path = self.directory / "recovery-request.json"
        if path.exists():
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior != request:
                raise CampaignExecutionError("unsettled campaign recovery request already exists")
        else:
            atomic_write_text(path, _json(request))
        return request


def worker_policy(policy: Policy) -> Policy:
    """Remove child publication/closure ownership without rewriting project files."""
    return dataclasses.replace(
        policy,
        recovery=dataclasses.replace(policy.recovery, reload_policy="off"),
        scm=dataclasses.replace(policy.scm, isolation="none"),
        gates=dataclasses.replace(policy.gates, retrospective="never"),
        sweep=dataclasses.replace(policy.sweep, auto="never"),
        cleanup=dataclasses.replace(policy.cleanup, auto_clean_on_finish=False),
    )


def legacy_control_refusal(run_dir: Path) -> str | None:
    from . import runs

    marker = run_dir / "campaign-worker.json"
    if marker.exists() or is_link_like(marker):
        return (
            "campaign child: use bmad-loop campaign resume/reconcile; legacy recovery is disabled"
        )
    external = (
        runs.state_dir_for(runs._project_of_run_dir(run_dir), run_dir.name) / "campaign-worker.json"
    )
    if external.exists() or is_link_like(external):
        return (
            "campaign child: use bmad-loop campaign resume/reconcile; legacy recovery is disabled"
        )
    return None


def bind_run(project: Path, run_dir: Path, context: CampaignExecution) -> dict[str, Any]:
    """Publish campaign ownership before resumable legacy state becomes visible."""
    from . import runs

    path, digest = context.save()
    binding = {
        "campaign_id": context.campaign_id,
        "unit_id": context.unit_id,
        "config_revision": context.config_revision,
        "execution": str(path),
        "digest": digest,
    }
    external = runs.state_dir_for(project, run_dir.name) / "campaign-worker.json"
    external.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(external, _json(binding), follow_symlinks=False)
    atomic_write_text(run_dir / "campaign-worker.json", _json(binding), follow_symlinks=False)
    return binding
