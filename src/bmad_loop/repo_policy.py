"""Frozen standing authority for repository organization, separate from campaigns.

Remote text cannot supply policy, commands, credentials or executable observers.
The default policy preserves every branch, worktree and original discussion.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .campaign_headless import capability_for, load_capabilities, validate_capability
from .campaign_jobs import digest
from .campaign_model import CampaignError, RoleCandidate
from .platform_util import is_link_like


@dataclass(frozen=True)
class RepoPolicy:
    repository: str
    capabilities: str
    proposer: RoleCandidate
    reviewer: RoleCandidate
    authorized_by: str
    target: str = "main"
    remote: str = "origin"
    labels: tuple[str, ...] = ()
    protected_labels: tuple[str, ...] = ("bmad:hold",)
    ownership_observer: str = ""
    max_mutations: int = 100
    max_closures: int = 20
    metadata_bytes: int = 64 * 1024 * 1024
    detail_bytes: int = 1_000_000
    max_objects: int = 10000
    max_pages: int = 1000
    max_jobs: int = 128
    max_sessions: int = 3
    max_attempts: int = 2
    input_bytes: int = 131072
    output_tokens: int = 8192
    max_turns: int = 4
    timeout_s: int = 1800
    schema_version: int = 1

    def validate(self):
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise CampaignError("Unsupported repository policy schema")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repository):
            raise CampaignError("Repository policy requires owner/name")
        if not self.authorized_by.strip() or not Path(self.capabilities).is_absolute():
            raise CampaignError("Policy needs owner authority and absolute capability path")
        for name in ("target", "remote"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or value.startswith("-")
                or any(c.isspace() for c in value)
            ):
                raise CampaignError("Invalid repository ref/remote")
        for name in (
            "max_mutations",
            "max_closures",
            "metadata_bytes",
            "detail_bytes",
            "max_objects",
            "max_pages",
            "max_jobs",
            "max_sessions",
            "max_attempts",
            "input_bytes",
            "output_tokens",
            "max_turns",
            "timeout_s",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise CampaignError(f"{name} must be a positive integer")
        if self.max_closures > self.max_mutations:
            raise CampaignError("Closure limit exceeds mutation limit")
        for values in (self.labels, self.protected_labels):
            if len(set(values)) != len(values) or any(
                not isinstance(v, str) or not v.strip() or len(v) > 50 for v in values
            ):
                raise CampaignError("Labels must be unique bounded strings")
        if set(self.labels) & set(self.protected_labels):
            raise CampaignError("Managed labels cannot clear or supply protection")
        if (
            not self.proposer.provider_family
            or not self.reviewer.provider_family
            or self.proposer.provider_family == self.reviewer.provider_family
        ):
            raise CampaignError("Repository review requires two distinct known provider families")


def policy_from_dict(raw):
    try:
        value = dict(raw)
        for name in ("proposer", "reviewer"):
            item = value[name]
            value[name] = RoleCandidate(
                **{**item, "capacity_command": tuple(item.get("capacity_command", ()))}
            )
        for name in ("labels", "protected_labels"):
            if name in value:
                value[name] = tuple(value[name])
        policy = RepoPolicy(**value)
        policy.validate()
        return policy
    except (TypeError, KeyError, ValueError, AttributeError) as exc:
        raise CampaignError(f"Invalid repository policy: {exc}") from exc


def load_policy(path: Path):
    if is_link_like(path) or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise CampaignError("Policy must be a bounded regular file")
    raw = path.read_text(encoding="utf-8")
    policy = policy_from_dict(json.loads(raw) if path.suffix == ".json" else tomllib.loads(raw))
    routes = load_capabilities(Path(policy.capabilities))
    selected = {}
    for name in ("proposer", "reviewer"):
        candidate = getattr(policy, name)
        cap = capability_for(
            routes, candidate.profile, candidate.model, "reasoning", effort=candidate.effort
        )
        validate_capability(cap, candidate, permissions=cap["permissions"])
        selected[name] = cap
    document = json.loads(json.dumps(asdict(policy)))
    return document, selected, digest(document)
