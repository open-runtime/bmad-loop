"""Explicit, fenced handoff of an existing PR to a campaign unit.

An owner selects an installed service observer on the CLI; manifest text cannot
select code or assert quiescence. Every effect rechecks the exact native PR,
remote branch, selected source and authenticated service fence. Existing code,
feedback and private sources are retained. No legacy run state becomes engine
authority and no archive PID is interpreted as an ownership release.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.metadata
import inspect
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from . import runs, verify
from .campaign_github import GithubGateway, MutationIntent, managed_marker
from .campaign_migration import MigrationItem, ServiceInventory
from .campaign_model import CampaignError, CampaignSnapshot, SourceSelector, UnitState
from .campaign_source_publication import SourcePublication
from .campaign_store import add_event
from .platform_util import atomic_write_text, is_link_like, safe_segment

GROUP = "bmad_loop.migration_observers"


class AdoptionError(CampaignError):
    """Ownership, preservation or exact acceptance cannot currently be proved."""


class AdoptionUncertain(AdoptionError):
    """An already-issued mutation must reconcile before another attempt."""


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclasses.dataclass(frozen=True)
class ProviderIdentity:
    name: str
    entry_point: str
    distribution: str
    version: str
    module_sha256: str


@dataclasses.dataclass(frozen=True)
class AdoptionManifest:
    schema_version: int
    campaign_id: str
    unit_id: str
    repository: str
    common_dir: str
    pr_number: int
    pr_node_id: str
    branch: str
    head: str
    base: str
    target: str
    source_identity: str
    actor_id: str
    actor_revision: str
    binding_revision: str
    fence_revision: str
    authorized_by: str
    mode: str = "continue"
    workspace: str = ""
    workspace_id: str = ""


@dataclasses.dataclass(frozen=True)
class Qualification:
    schema_version: int
    manifest_digest: str
    provider: ProviderIdentity
    service_revision: str
    observed_at: float
    head: str
    base: str
    source: dict[str, Any]
    actor: dict[str, Any]
    pr: dict[str, Any]
    provisional: dict[str, Any] | None = None


def _git(project: Path, *args: str) -> str:
    result = verify.git_bytes(
        project,
        *args,
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    )
    if result.returncode:
        raise AdoptionError("adoption Git observation failed: " + args[0])
    return result.stdout.decode("utf-8", errors="strict").strip()


def common(project: Path) -> Path:
    return Path(_git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def provisional(project: Path, snapshot: CampaignSnapshot, unit_id: str) -> dict[str, Any]:
    unit = snapshot.units[unit_id]
    if (
        unit.state not in {UnitState.DRAFT, UnitState.READY}
        or (unit.claim and not unit.claim.quiescent)
        or not unit.bindings.get("pr")
        or not unit.bindings.get("spec_path")
        or unit.legacy_outcome
        or unit.evidence.get("source_artifacts")
        or unit.evidence.get("accepted_resolution")
    ):
        raise AdoptionError(
            "unit already owns implementation or uncertain work; it cannot retire a provisional draft"
        )
    workspace = Path(unit.bindings["workspace"]).resolve()
    head, base = unit.bindings["head"], unit.bindings["base"]
    if (
        common(workspace) != common(project)
        or verify.rev_parse_head(workspace) != head
        or _git(workspace, "symbolic-ref", "--short", "HEAD") != unit.bindings["branch"]
        or not verify.worktree_clean(workspace)
        or not verify.is_ancestor(workspace, base, head)
    ):
        raise AdoptionError(
            "provisional workspace identity, preservation or actor ownership changed"
        )
    changed = _git(workspace, "diff", "--name-only", base, head).splitlines()
    if changed != [unit.bindings["spec_path"]]:
        raise AdoptionError(
            "provisional branch contains actual work beyond its generated specification"
        )
    return {
        "bindings": {key: value for key, value in unit.bindings.items() if key != "adoption"},
        "claim": dataclasses.asdict(unit.claim) if unit.claim else None,
        "head": head,
        "branch": unit.bindings["branch"],
        "pr_number": unit.bindings["pr"]["number"],
        "hold_id": unit.hold.id if unit.hold else "",
    }


def manifest_from_dict(raw: Mapping[str, Any]) -> AdoptionManifest:
    if set(raw) - set(AdoptionManifest.__dataclass_fields__):
        raise AdoptionError("adoption manifest contains unsupported fields")
    try:
        value = AdoptionManifest(**raw)
    except TypeError as exc:
        raise AdoptionError(
            "adoption manifest requires all exact PR/source/handoff coordinates"
        ) from exc
    if (
        type(value.schema_version) is not int
        or value.schema_version != 1
        or type(value.pr_number) is not int
        or value.pr_number < 1
    ):
        raise AdoptionError("adoption manifest version/native PR number is invalid")
    for field in dataclasses.fields(value):
        item = getattr(value, field.name)
        if field.name not in {"schema_version", "pr_number"} and (
            not isinstance(item, str)
            or (field.name not in {"workspace", "workspace_id"} and not item.strip())
        ):
            raise AdoptionError("adoption manifest has an empty or non-string coordinate")
    if (
        value.mode not in {"continue", "verify-existing"}
        or not Path(value.common_dir).is_absolute()
        or (value.workspace and not Path(value.workspace).is_absolute())
    ):
        raise AdoptionError("adoption mode/common-directory/workspace binding is invalid")
    if value.branch == value.target or any(
        item.startswith("-") or any(ord(char) <= 32 for char in item)
        for item in (value.branch, value.target)
    ):
        raise AdoptionError("adoption cannot claim the target branch or option-like ref")
    if any(
        not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", item)
        for item in (value.head, value.base, value.source_identity)
    ):
        raise AdoptionError("adoption requires full immutable commit/source digests")
    return value


def read_manifest(path: Path) -> AdoptionManifest:
    if is_link_like(path) or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise AdoptionError("adoption manifest is redirected, missing or unbounded")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AdoptionError("adoption manifest is not an object")
    return manifest_from_dict(raw)


def load_provider(
    name: str, *, snapshot: CampaignSnapshot | None = None
) -> tuple[ProviderIdentity, Callable[[Path, Path], ServiceInventory]]:
    """Resolve only an explicitly selected installed extension, never a URL/argv."""
    if name == "native":
        if snapshot is None or not snapshot.spec.paradev.project_id:
            raise AdoptionError("native observer needs this campaign's exact Paradev project ID")
        from . import __version__
        from .campaign_native_inventory import NativeServiceObserver

        provider = NativeServiceObserver(snapshot.spec.paradev.project_id)
        path = Path(inspect.getfile(NativeServiceObserver))
        return (
            ProviderIdentity(
                name,
                "bmad_loop.campaign_native_inventory:NativeServiceObserver",
                "bmad-loop",
                __version__,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            ),
            provider,
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        raise AdoptionError("observer must name an installed migration observer entry point")
    entries = [
        entry for entry in importlib.metadata.entry_points(group=GROUP) if entry.name == name
    ]
    if len(entries) != 1:
        raise AdoptionError("installed migration observer is missing or ambiguous: " + name)
    entry = entries[0]
    provider = entry.load()
    if not callable(provider) or entry.dist is None:
        raise AdoptionError(
            "migration observer lacks a callable and installed distribution identity"
        )
    try:
        path = Path(inspect.getfile(provider))
    except TypeError as exc:
        raise AdoptionError("migration observer code has no verifiable installed module") from exc
    if is_link_like(path) or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise AdoptionError("migration observer module is redirected, missing or unbounded")
    return ProviderIdentity(
        name,
        entry.value,
        entry.dist.metadata["Name"],
        entry.dist.version,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    ), cast(Callable[[Path, Path], ServiceInventory], provider)


def source_binding(
    project: Path, snapshot: CampaignSnapshot, unit_id: str
) -> tuple[SourceSelector, dict[str, Any], str]:
    unit = snapshot.units[unit_id]
    source = next(
        (source for source in snapshot.spec.sources if source.id == unit.spec.source), None
    )
    if source is None or unit.spec.kind != "story":
        raise AdoptionError("adoption requires one actual configured BMAD story source")
    root = (
        runs.state_root()
        / "adoption-observations"
        / hashlib.sha256(str(snapshot.common_dir).encode()).hexdigest()
    )
    binding = SourcePublication(project, root).capture(source, unit.spec).to_dict()
    identity = digest(
        {
            "selector": dataclasses.asdict(source),
            "unit": dataclasses.asdict(unit.spec),
            "binding": binding,
        }
    )
    return source, binding, identity


def _remote_head(project: Path, remote: str, branch: str) -> str:
    observed = _git(
        project, "ls-remote", "--exit-code", remote, "refs/heads/" + branch
    ).splitlines()
    if len(observed) != 1 or observed[0].split()[1] != "refs/heads/" + branch:
        raise AdoptionError("adoption remote branch identity is absent or ambiguous")
    return observed[0].split()[0]


def qualify(
    project: Path,
    snapshot: CampaignSnapshot,
    manifest: AdoptionManifest,
    provider_identity: ProviderIdentity,
    *,
    resolver=load_provider,
    now: float | None = None,
    allow_owned: bool = False,
) -> Qualification:
    """Fresh read-only gates repeated before every adoption mutation/claim."""
    explicit_now = now
    now = time.time() if now is None else now
    project = project.resolve()
    if (
        manifest.campaign_id != snapshot.id
        or manifest.repository != snapshot.repository
        or manifest.unit_id not in snapshot.units
        or common(project) != Path(snapshot.common_dir).resolve()
        or Path(manifest.common_dir).resolve() != common(project)
    ):
        raise AdoptionError("adoption campaign/repository/common-directory identity differs")
    unit = snapshot.units[manifest.unit_id]
    if _git(project, "remote", "get-url", snapshot.spec.remote) != snapshot.remote_url:
        raise AdoptionError(
            "adoption selected remote URL changed from the campaign repository binding"
        )
    if allow_owned and (
        unit.bindings.get("adoption", {}).get("state") != "owned"
        or unit.bindings["adoption"].get("manifest_digest") != digest(dataclasses.asdict(manifest))
    ):
        raise AdoptionError("ongoing fence observation has no persisted owned adoption")
    if not allow_owned and (
        unit.state
        in {
            UnitState.MERGE_REQUESTED,
            UnitState.MERGED_PENDING_SYNC,
            UnitState.DELIVERED,
            UnitState.CANCELED,
        }
        or unit.claim
        and not unit.claim.quiescent
    ):
        raise AdoptionError("adoption cannot replace a live/uncertain actor or landed unit")
    if unit.bindings.get("workspace") and unit.bindings.get("adoption", {}).get(
        "manifest_digest"
    ) != digest(dataclasses.asdict(manifest)):
        provisional(project, snapshot, unit.id)
    if not allow_owned and any(
        action.unit == unit.id
        and action.state in {"prepared", "sent_unknown"}
        and not action.kind.startswith("adopt")
        and not (action.kind == "prepare-unit" and action.payload.get("adoption"))
        and not (
            action.kind == "github"
            and action.payload.get("purpose") in {"adoption-draft", "adoption-retire"}
        )
        for action in snapshot.outbox
    ):
        raise AdoptionError("adoption waits for existing unit effects to drain")
    if manifest.target != snapshot.spec.target:
        raise AdoptionError("adoption target differs from frozen publication policy")
    source, binding, identity = source_binding(project, snapshot, unit.id)
    if manifest.source_identity != identity:
        raise AdoptionError("adoption selected source/acceptance contract changed")
    loaded, observer = resolver(provider_identity.name, snapshot=snapshot)
    if loaded != provider_identity:
        raise AdoptionError("adoption observer installation changed since owner selection")
    observation = observer(project, common(project))
    now = time.time() if explicit_now is None else explicit_now
    if (
        not isinstance(observation, ServiceInventory)
        or Path(observation.common_dir).resolve() != common(project)
        or not observation.complete
        or observation.faults
        or not observation.revision
        or not 0 <= now - observation.observed_at <= 120
    ):
        raise AdoptionError("adoption service fence coverage is unavailable, stale or foreign")
    matches = [item for item in observation.items if item.id == manifest.actor_id]
    if len(matches) != 1:
        raise AdoptionError("adoption handoff actor identity is absent or ambiguous")
    actor: MigrationItem = matches[0]
    details = actor.details or {}
    exact = {
        "repository": snapshot.repository,
        "pr_number": manifest.pr_number,
        "pr_node_id": manifest.pr_node_id,
        "head": manifest.head,
        "base": manifest.base,
        "branch": manifest.branch,
        "source_unit": unit.id,
        "source_identity": identity,
        "binding_revision": manifest.binding_revision,
        "fence_revision": manifest.fence_revision,
        "workspace": manifest.workspace,
    }
    if (
        actor.revision != manifest.actor_revision
        or unit.id not in actor.units
        or actor.liveness != "dead"
        or actor.ownership != "unowned"
        or details.get("preserved_clean") is not True
        or details.get("resumable_binding_verified") is not True
        or details.get("admissions_open") is not False
        or type(details.get("active_workers")) is not int
        or details["active_workers"] != 0
        or any(details.get(key) != value for key, value in exact.items())
    ):
        raise AdoptionError(
            "fresh external handoff does not prove exact exclusive preserved PR/source ownership"
        )
    for other in observation.items:
        if other.id == actor.id:
            continue
        data = other.details or {}
        conflict = (
            unit.id in other.units
            or data.get("branch") == manifest.branch
            or data.get("campaign_conflict") is True
        )
        if conflict and (
            other.liveness != "dead"
            or data.get("admissions_open") is not False
            or data.get("active_workers") != 0
            or not data.get("fence_revision")
        ):
            raise AdoptionError(
                "another active, human or uncertain actor still covers the adopted scope"
            )
    return Qualification(
        1,
        digest(dataclasses.asdict(manifest)),
        loaded,
        observation.revision,
        now,
        manifest.head,
        manifest.base,
        {"selector": dataclasses.asdict(source), "binding": binding, "identity": identity},
        dataclasses.asdict(actor),
        {},
    )


def qualify_pr(
    project: Path,
    snapshot: CampaignSnapshot,
    manifest: AdoptionManifest,
    provider_identity: ProviderIdentity,
    gateway: GithubGateway,
    *,
    resolver=load_provider,
    now: float | None = None,
) -> Qualification:
    qualified = qualify(project, snapshot, manifest, provider_identity, resolver=resolver, now=now)
    if gateway.repository != snapshot.repository:
        raise AdoptionError("adoption GitHub gateway names another repository")
    pr = gateway.pull(manifest.pr_number)
    if (
        pr.get("number") != manifest.pr_number
        or pr.get("node_id") != manifest.pr_node_id
        or pr.get("merged") is not False
        or pr.get("state") != "open"
        or pr.get("head", {}).get("ref") != manifest.branch
        or pr.get("head", {}).get("sha") != manifest.head
        or (pr.get("head", {}).get("repo") or {}).get("full_name") != snapshot.repository
        or pr.get("base", {}).get("sha") != manifest.base
        or pr.get("base", {}).get("ref") != manifest.target
        or (pr.get("base", {}).get("repo") or {}).get("full_name") != snapshot.repository
    ):
        raise AdoptionError("adoption native PR/head/base/repository changed or is merged/closed")
    if (
        _remote_head(project, snapshot.spec.remote, manifest.branch) != manifest.head
        or _remote_head(project, snapshot.spec.remote, manifest.target) != manifest.base
    ):
        raise AdoptionError("adoption remote branch or main moved since owner qualification")
    if manifest.workspace:
        workspace = Path(manifest.workspace).resolve()
        for component in (Path(manifest.workspace), *Path(manifest.workspace).parents):
            if is_link_like(component):
                raise AdoptionError("adoption workspace traverses redirected ownership")
        allowed = Path(
            snapshot.spec.paradev.worktree_root
            if snapshot.spec.paradev.reflect
            else snapshot.spec.worktree_root
        ).resolve()
        if (
            is_link_like(Path(manifest.workspace))
            or not workspace.is_relative_to(allowed)
            or workspace == project
            or common(workspace) != common(project)
            or _git(workspace, "symbolic-ref", "--short", "HEAD") != manifest.branch
            or verify.rev_parse_head(workspace) != manifest.head
            or not verify.worktree_clean(workspace)
        ):
            raise AdoptionError(
                "adoption worktree is foreign, dirty, moved or outside its project root"
            )
    previous = None
    unit = snapshot.units[manifest.unit_id]
    if unit.bindings.get("workspace") and unit.bindings.get("adoption", {}).get("state") != "owned":
        previous = provisional(project, snapshot, unit.id)
        old = gateway.pull(previous["pr_number"])
        if (
            old.get("merged") is not False
            or old.get("state") != "open"
            or old.get("draft") is not True
            or old.get("head", {}).get("sha") != previous["head"]
            or old.get("head", {}).get("ref") != previous["branch"]
            or managed_marker(snapshot.id, unit.id, "pr") not in str(old.get("body", ""))
        ):
            raise AdoptionError(
                "only the exact clean unused owned draft can be retired for adoption"
            )
        previous = {**previous, "pr": old}
    return dataclasses.replace(qualified, pr=pr, provisional=previous)


def prepare_request(
    project: Path,
    snapshot: CampaignSnapshot,
    unit_id: str,
    manifest_path: Path,
    observer_name: str = "native",
    *,
    gateway: GithubGateway,
    resolver=load_provider,
    now: float | None = None,
) -> dict[str, Any]:
    manifest = read_manifest(manifest_path)
    if unit_id != manifest.unit_id:
        raise AdoptionError("adoption --unit differs from owner manifest selection")
    identity, _ = resolver(observer_name, snapshot=snapshot)
    qualified = qualify_pr(
        project, snapshot, manifest, identity, gateway, resolver=resolver, now=now
    )
    return {
        "manifest": dataclasses.asdict(manifest),
        "provider": dataclasses.asdict(identity),
        "qualification": dataclasses.asdict(qualified),
    }


def draft_intent(
    snapshot: CampaignSnapshot,
    manifest: AdoptionManifest,
    qualification: Qualification,
    gateway: GithubGateway,
    *,
    action_id: str,
) -> MutationIntent | None:
    if qualification.manifest_digest != digest(dataclasses.asdict(manifest)):
        raise AdoptionError("draft conversion no longer owns the qualified adoption")
    pr = qualification.pr
    if pr.get("draft") is True:
        return None
    return gateway.prepare_ready(
        manifest.pr_node_id,
        action_id=action_id,
        generation=snapshot.generation,
        draft=True,
        expected_head=manifest.head,
        expected_updated_at=pr["updated_at"],
        expected_draft=False,
    )


def propose(
    project: Path,
    snapshot: CampaignSnapshot,
    unit_id: str,
    pr_number: int,
    observer_name: str = "native",
    *,
    gateway: GithubGateway,
    resolver=load_provider,
    now: float | None = None,
) -> dict[str, Any]:
    """Return fresh concrete coordinates for owner review; grant no authority."""
    if unit_id not in snapshot.units:
        raise AdoptionError("unknown campaign unit selected for adoption proposal")
    explicit_now = now
    now = time.time() if now is None else now
    provider, observer = resolver(observer_name, snapshot=snapshot)
    observed = observer(project, common(project))
    now = time.time() if explicit_now is None else explicit_now
    if (
        not isinstance(observed, ServiceInventory)
        or Path(observed.common_dir).resolve() != common(project)
        or not observed.complete
        or observed.faults
        or not 0 <= now - observed.observed_at <= 120
    ):
        raise AdoptionError("adoption proposal service coverage is unavailable or stale")
    pr = gateway.pull(pr_number)
    _, _, source_identity = source_binding(project, snapshot, unit_id)
    candidates = [
        item
        for item in observed.items
        if unit_id in item.units and (item.details or {}).get("pr_number") == pr_number
    ]
    actor = candidates[0] if len(candidates) == 1 else None
    details = (actor.details or {}) if actor else {}
    value = {
        "schema_version": 1,
        "campaign_id": snapshot.id,
        "unit_id": unit_id,
        "repository": snapshot.repository,
        "common_dir": str(common(project)),
        "pr_number": pr_number,
        "pr_node_id": pr.get("node_id", ""),
        "branch": pr.get("head", {}).get("ref", ""),
        "head": pr.get("head", {}).get("sha", ""),
        "base": pr.get("base", {}).get("sha", ""),
        "target": pr.get("base", {}).get("ref", ""),
        "source_identity": source_identity,
        "actor_id": actor.id if actor else "",
        "actor_revision": actor.revision if actor else "",
        "binding_revision": details.get("binding_revision", ""),
        "fence_revision": details.get("fence_revision", ""),
        "authorized_by": "",
        "mode": "continue",
        "workspace": details.get("workspace", ""),
        "workspace_id": details.get("workspace_id", ""),
    }
    problems = []
    if actor is None:
        problems.append(
            "No uniquely authenticated fenced handoff binds this unit and PR; native daemon/PID observations do not establish adoption authority"
        )
    else:
        try:
            checked = manifest_from_dict({**value, "authorized_by": "proposal-check-only"})
            qualify_pr(project, snapshot, checked, provider, gateway, resolver=resolver, now=now)
        except AdoptionError as exc:
            problems.append(str(exc))
    return {
        "schema_version": 1,
        "ready_for_owner_authorization": not problems,
        "manifest": value,
        "provider": dataclasses.asdict(provider),
        "problems": problems,
        "instructions": "Review these exact coordinates, select continue or verify-existing and set authorized_by to your explicit handoff decision before campaign adopt. No process is stopped and no ownership is granted by this proposal.",
    }


def begin(
    snapshot: CampaignSnapshot, unit_id: str, payload: Mapping[str, Any], *, command_id: str
) -> None:
    """Persist an immutable owner-selected transaction before any side effect."""
    manifest = manifest_from_dict(payload["manifest"])
    provider = ProviderIdentity(**payload["provider"])
    qualification = payload["qualification"]
    if (
        manifest.unit_id != unit_id
        or manifest.campaign_id != snapshot.id
        or qualification.get("manifest_digest") != digest(dataclasses.asdict(manifest))
        or qualification.get("provider") != dataclasses.asdict(provider)
    ):
        raise AdoptionError("queued adoption coordinates differ from the qualified owner selection")
    unit = snapshot.units[unit_id]
    previous = unit.bindings.get("adoption")
    if previous:
        if previous["manifest"] != dataclasses.asdict(manifest) or previous[
            "provider"
        ] != dataclasses.asdict(provider):
            raise AdoptionError("unit already has a different pending adoption transaction")
        return
    if (
        unit.claim
        and not unit.claim.quiescent
        or unit.state not in {UnitState.PENDING, UnitState.DRAFT, UnitState.READY}
        or (unit.bindings.get("workspace") and not qualification.get("provisional"))
    ):
        raise AdoptionError("adoption requires an unclaimed unit without another editing workspace")
    unit.bindings["adoption"] = {
        "schema_version": 1,
        "state": "retire_pending" if qualification.get("provisional") else "draft_pending",
        "manifest": dataclasses.asdict(manifest),
        "manifest_digest": qualification["manifest_digest"],
        "provider": dataclasses.asdict(provider),
        "owner_command": command_id,
        "provisional": qualification.get("provisional"),
    }
    add_event(
        snapshot,
        "adoption-requested",
        unit=unit_id,
        command=command_id,
        pr=manifest.pr_number,
        manifest=qualification["manifest_digest"],
        observer=provider.name,
    )


def transaction(
    snapshot: CampaignSnapshot, unit_id: str
) -> tuple[AdoptionManifest, ProviderIdentity, dict[str, Any]]:
    value = snapshot.units[unit_id].bindings.get("adoption")
    if not value or value.get("schema_version") != 1:
        raise AdoptionError("unit has no explicit adoption transaction")
    manifest = manifest_from_dict(value["manifest"])
    if value.get("manifest_digest") != digest(dataclasses.asdict(manifest)):
        raise AdoptionError("adoption transaction manifest was modified")
    return manifest, ProviderIdentity(**value["provider"]), value


def perform_draft(runtime, snapshot, action) -> dict[str, Any]:
    manifest, provider, _ = transaction(snapshot, action.unit)
    qualified = qualify_pr(
        runtime.project,
        snapshot,
        manifest,
        provider,
        runtime.gateway(snapshot),
        resolver=runtime.adoption_resolver,
    )
    intent = draft_intent(
        snapshot, manifest, qualified, runtime.gateway(snapshot), action_id=action.id + "-draft"
    )
    return (
        {"intent": intent.to_dict(), "purpose": "adoption-draft"}
        if intent
        else {"draft_confirmed": True, "qualification": dataclasses.asdict(qualified)}
    )


def perform_retire(runtime, snapshot, action) -> dict[str, Any]:
    manifest, provider, value = transaction(snapshot, action.unit)
    qualified = qualify_pr(
        runtime.project,
        snapshot,
        manifest,
        provider,
        runtime.gateway(snapshot),
        resolver=runtime.adoption_resolver,
    )
    previous = qualified.provisional
    if (
        value["state"] != "retire_pending"
        or not previous
        or previous["head"] != value["provisional"]["head"]
        or previous["pr_number"] != value["provisional"]["pr_number"]
    ):
        raise AdoptionError("provisional retirement differs from the explicit preserved handoff")
    pr = previous["pr"]
    intent = MutationIntent(
        action.id + "-retire",
        "retire_provisional_pr",
        "PATCH",
        f"{runtime.gateway(snapshot).root}/pulls/{pr['number']}",
        {"state": "closed"},
        generation=snapshot.generation,
        coordinates={
            "id": pr["id"],
            "updated_at": pr["updated_at"],
            "head": previous["head"],
            "marker": managed_marker(snapshot.id, action.unit, "pr"),
            "adoption_manifest": value["manifest_digest"],
        },
    )
    return {"intent": intent.to_dict(), "purpose": "adoption-retire"}


def apply_retirement(snapshot, unit, intent) -> None:
    _, _, value = transaction(snapshot, unit.id)
    previous = value["provisional"]
    receipt = intent.get("receipt") or {}
    if (
        value["state"] != "retire_pending"
        or intent.get("state") != "confirmed"
        or receipt.get("number") != previous["pr_number"]
        or receipt.get("state") != "closed"
        or receipt.get("merged") is not False
        or receipt.get("head", {}).get("sha") != previous["head"]
        or unit.claim
        and not unit.claim.quiescent
    ):
        raise AdoptionError(
            "provisional retirement is not natively confirmed for the preserved draft"
        )
    unit.evidence.setdefault("adoption_predecessors", []).append(previous)
    retained = {
        key: value
        for key, value in unit.bindings.items()
        if key in {"issue", "epic_issue", "milestone", "tracker_id", "hierarchy_ready"}
    }
    value["state"] = "draft_pending"
    unit.bindings = {**retained, "adoption": value}
    unit.claim = None
    if (
        unit.hold
        and unit.hold.id == previous["hold_id"]
        and (
            unit.hold.kind in {"migration", "overlap", "admission"}
            or (
                unit.hold.kind in {"invalid_stage_result", "failed", "effect_refused"}
                and (previous.get("claim") or {}).get("role") == "triage"
                and unit.evidence.get("worker_history", [{}])[-1].get("stage") == "admission_review"
            )
        )
    ):
        unit.hold = None
    for key in ("admission", "failure", "resolution_context", "resolution_next_step"):
        unit.evidence.pop(key, None)
    add_event(
        snapshot,
        "adoption-provisional-retired",
        unit=unit.id,
        pr=previous["pr_number"],
        head=previous["head"],
        workspace=previous["bindings"]["workspace"],
    )


def guard_draft_submission(runtime, snapshot, unit_id: str, intent: MutationIntent) -> None:
    manifest, provider, value = transaction(snapshot, unit_id)
    if (
        value["state"] != "draft_pending"
        or intent.kind != "draft"
        or (intent.coordinates or {}).get("node_id") != manifest.pr_node_id
    ):
        raise AdoptionError("adoption draft mutation no longer owns the explicit transaction")
    qualify_pr(
        runtime.project,
        snapshot,
        manifest,
        provider,
        runtime.gateway(snapshot),
        resolver=runtime.adoption_resolver,
    )


def guard_retire_submission(runtime, snapshot, unit_id: str, intent: MutationIntent) -> None:
    manifest, provider, value = transaction(snapshot, unit_id)
    qualified = qualify_pr(
        runtime.project,
        snapshot,
        manifest,
        provider,
        runtime.gateway(snapshot),
        resolver=runtime.adoption_resolver,
    )
    if (
        value["state"] != "retire_pending"
        or not qualified.provisional
        or intent.kind != "retire_provisional_pr"
        or (intent.coordinates or {}).get("id") != qualified.provisional["pr"]["id"]
        or (intent.coordinates or {}).get("head") != qualified.provisional["head"]
    ):
        raise AdoptionError("provisional retirement no longer owns the preserved native draft")


def prepare(runtime, snapshot, action) -> dict[str, Any]:
    """Create an owned private source/spec checkpoint on the same existing PR.

    The action journal precedes allocation and commits. A retry recognizes only
    its exact documentation commit and stored preparation digest. It never
    resets, force pushes, checks out another active branch or drops user files.
    """
    from .campaign_planning import execution_spec
    from .campaign_sources import prepare_source_view

    unit = snapshot.units[action.unit]
    manifest, provider, value = transaction(snapshot, unit.id)
    if value["state"] != "draft_confirmed":
        raise AdoptionError(
            "adoption must confirm native draft state before preparing any source/spec"
        )
    directory = runtime.store.directory(snapshot.id) / "adoptions" / action.id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "prepare.json"
    root = Path(
        snapshot.spec.paradev.worktree_root
        if snapshot.spec.paradev.reflect
        else snapshot.spec.worktree_root
    ).resolve()
    if not root.is_absolute():
        raise AdoptionError("adoption needs an explicit absolute project worktree root")
    workspace = (
        Path(manifest.workspace).resolve()
        if manifest.workspace
        else root / safe_segment("adopt-" + unit.id + "-" + action.id[:8])
    )
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if state and state.get("manifest_digest") != value["manifest_digest"]:
        raise AdoptionError("adoption preparation action was reused for different owner intent")
    if not state:
        qualified = qualify_pr(
            runtime.project,
            snapshot,
            manifest,
            provider,
            runtime.gateway(snapshot),
            resolver=runtime.adoption_resolver,
        )
        if qualified.pr.get("draft") is not True:
            raise AdoptionError(
                "native adopted PR is no longer draft before source/spec preparation"
            )
        state = {
            "schema_version": 1,
            "manifest_digest": value["manifest_digest"],
            "workspace": str(workspace),
            "stage": "prepared",
            "original_head": manifest.head,
        }
        atomic_write_text(path, json.dumps(state, sort_keys=True), follow_symlinks=False)
    else:
        qualify(runtime.project, snapshot, manifest, provider, resolver=runtime.adoption_resolver)
        pr = runtime.gateway(snapshot).pull(manifest.pr_number)
        allowed_head = (state.get("result") or {}).get("head", manifest.head)
        if (
            pr.get("node_id") != manifest.pr_node_id
            or pr.get("head", {}).get("ref") != manifest.branch
            or pr.get("head", {}).get("sha") not in {manifest.head, allowed_head}
            or pr.get("draft") is not True
            or pr.get("merged") is not False
            or pr.get("state") != "open"
        ):
            raise AdoptionError(
                "adoption lost-ACK recovery cannot adopt moved/merged/ready PR state"
            )
        if state.get("result"):
            result = state["result"]
            if (
                common(workspace) != Path(snapshot.common_dir).resolve()
                or _git(workspace, "symbolic-ref", "--short", "HEAD") != manifest.branch
                or verify.rev_parse_head(workspace) != result["head"]
                or not verify.worktree_clean(workspace)
            ):
                raise AdoptionError("adopted candidate changed before checkpoint acknowledgement")
            pushed = runtime._pusher(
                snapshot,
                dataclasses.replace(
                    unit,
                    bindings={
                        **unit.bindings,
                        "workspace": str(workspace),
                        "branch": manifest.branch,
                    },
                ),
            ).push(action.id + "-initial", result["head"])
            if pushed.state != "confirmed":
                raise AdoptionUncertain(
                    "adoption initial push is still awaiting exact remote acknowledgement"
                )
            return result
    observation_ref = f"refs/bmad-campaign/{snapshot.id}/adoption/{action.id}"
    _git(
        runtime.project,
        "fetch",
        "--no-tags",
        snapshot.spec.remote,
        "refs/heads/" + manifest.branch + ":" + observation_ref,
    )
    if verify.ref_revision(runtime.project, observation_ref) != manifest.head:
        raise AdoptionError("remote branch moved before adoption allocation")
    preserved_ref = observation_ref + "-preserved"
    if not verify.ref_exists_checked(runtime.project, preserved_ref):
        verify.create_ref(runtime.project, preserved_ref, manifest.head)
    if verify.ref_revision(runtime.project, preserved_ref) != manifest.head:
        raise AdoptionError("adoption preservation ref changed")
    from .campaign_workers import worktree_metadata_lock

    with worktree_metadata_lock(runtime.project):
        if not workspace.exists():
            if snapshot.spec.paradev.reflect:
                raise AdoptionError(
                    "native reflection requires the explicitly registered owner-qualified workspace UUID/path"
                )
            worktrees = _git(runtime.project, "worktree", "list", "--porcelain")
            if "branch refs/heads/" + manifest.branch in worktrees.splitlines():
                raise AdoptionError("adopted branch is checked out in an unselected workspace")
            root.mkdir(parents=True, exist_ok=True)
            local = (
                verify.rev_parse_revision(runtime.project, "refs/heads/" + manifest.branch)
                if verify.ref_exists_checked(runtime.project, "refs/heads/" + manifest.branch)
                else None
            )
            if local is not None and local != manifest.head:
                raise AdoptionError(
                    "local adopted branch has unpublished history; explicit preservation required"
                )
            if local is None:
                _git(runtime.project, "branch", manifest.branch, manifest.head)
            _git(runtime.project, "worktree", "add", str(workspace), manifest.branch)
        if (
            common(workspace) != Path(snapshot.common_dir).resolve()
            or _git(workspace, "symbolic-ref", "--short", "HEAD") != manifest.branch
            or not verify.worktree_clean(workspace)
        ):
            raise AdoptionError("selected adopted workspace identity or clean preservation changed")
    native = {}
    if snapshot.spec.paradev.reflect:
        from .campaign_paradev import ParadevWorkspaceProvider

        selected = ParadevWorkspaceProvider(
            project_id=snapshot.spec.paradev.project_id,
            allowed_root=root,
            repository=runtime.project,
            state_dir=runtime.store.directory(snapshot.id) / "reflection",
        )
        native = dataclasses.asdict(
            selected.adopt(
                unit.id,
                manifest.branch,
                manifest.workspace_id,
                str(workspace),
                action.id,
                group=(
                    unit.spec.epic
                    if snapshot.spec.paradev.group_by == "epic"
                    else unit.spec.parent or unit.id
                ),
            )
        )
    spec_path = workspace / "docs" / "bmad-campaign" / (safe_segment(unit.id) + ".md")
    marker = "BMAD-Adoption-Action: " + action.id
    current = verify.rev_parse_head(workspace)
    if current != manifest.head:
        message = _git(workspace, "show", "-s", "--format=%B", current)
        changed = _git(workspace, "diff", "--name-only", manifest.head, current).splitlines()
        if (
            marker not in message.splitlines()
            or changed != [spec_path.relative_to(workspace).as_posix()]
            or _git(workspace, "rev-parse", current + "^") != manifest.head
        ):
            raise AdoptionError(
                "adoption found foreign or unpublished commits beyond its preserved head"
            )
    runtime._seed_checkout(workspace)
    selector = runtime.source(snapshot, unit)
    if not state.get("source"):
        prepared = prepare_source_view(
            runtime.project, workspace, selector, authority_dir=directory / "source-views"
        )
        binding = runtime._source_publisher(snapshot, project=workspace).capture(
            prepared.selector, unit.spec
        )
        if manifest.mode == "continue" and binding.status not in {
            "",
            "draft",
            "ready-for-dev",
            "backlog",
        }:
            raise AdoptionError(
                "continue mode needs executable native source; choose explicit verify-existing for built/done candidates"
            )
        if manifest.mode == "verify-existing" and binding.status not in {"built", "done"}:
            raise AdoptionError(
                "verify-existing requires an actual built/done native source candidate"
            )
        canonical = (
            binding
            if binding.versioning == "code-repo"
            else runtime._source_publisher(snapshot).capture(selector, unit.spec)
        )
        state["source"] = {
            "effective_source": dataclasses.asdict(prepared.selector),
            "canonical_source": canonical.to_dict(),
            "initial_source": (
                {
                    "path": Path(binding.path).relative_to(workspace).as_posix(),
                    "sha256": binding.revision,
                }
                if binding.path
                else None
            ),
            "private_paths": [str(item) for item in prepared.private_paths],
        }
        atomic_write_text(path, json.dumps(state, sort_keys=True), follow_symlinks=False)
    from .campaign_product_input import collect as selected_product_input

    specification = execution_spec(
        unit.spec,
        campaign_id=snapshot.id,
        graph_revision=snapshot.graph_revision,
        source=snapshot.bindings.get("source_observations", {}).get(unit.id, {}),
        native_input=selected_product_input(
            workspace, SourceSelector(**state["source"]["effective_source"]), unit.spec
        ),
    )
    specification += (
        "\n\nExplicit adopted PR: "
        + str(manifest.pr_number)
        + "\nPreserved commit: "
        + manifest.head
        + "\nOwner handoff: "
        + manifest.authorized_by
        + "\n"
    )
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(spec_path, specification, follow_symlinks=False)
    verify.commit_paths(workspace, "docs: bind adopted campaign unit\n\n" + marker, [spec_path])
    head = verify.rev_parse_head(workspace)
    if not verify.is_ancestor(workspace, manifest.head, head):
        raise AdoptionError("adoption checkpoint did not preserve original PR history")
    result = {
        "workspace": str(workspace),
        "branch": manifest.branch,
        "base": manifest.base,
        "head": head,
        "native": native,
        "pr": {
            "number": manifest.pr_number,
            "node_id": manifest.pr_node_id,
            "url": f"https://github.com/{snapshot.repository}/pull/{manifest.pr_number}",
        },
        "spec_path": spec_path.relative_to(workspace).as_posix(),
        "canonical_selector": dataclasses.asdict(selector),
        "source_authority_root": str(runtime.store.directory(snapshot.id) / "source-publication"),
        "preserved_adoption_ref": preserved_ref,
        **state["source"],
    }
    result = json.loads(json.dumps(result))
    state.update(stage="checkpointed", result=result)
    atomic_write_text(path, json.dumps(state, sort_keys=True), follow_symlinks=False)
    owned = dataclasses.replace(unit, bindings={**unit.bindings, **result})
    pushed = runtime._pusher(snapshot, owned).push(action.id + "-initial", head)
    if pushed.state != "confirmed":
        raise AdoptionUncertain(
            "adoption initial push is uncertain; retain the existing checkpoint for reconciliation"
        )
    return result


def guard_owned(runtime, snapshot, unit_id: str) -> None:
    """Keep the original external admissions fence live for every later actor."""
    unit = snapshot.units[unit_id]
    if unit.bindings.get("adoption", {}).get("state") != "owned":
        return
    manifest, provider, _ = transaction(snapshot, unit_id)
    qualify(
        runtime.project,
        snapshot,
        manifest,
        provider,
        resolver=runtime.adoption_resolver,
        allow_owned=True,
    )
    pr = runtime.gateway(snapshot).pull(manifest.pr_number)
    if (
        pr.get("node_id") != manifest.pr_node_id
        or pr.get("head", {}).get("ref") != manifest.branch
        or (pr.get("head", {}).get("repo") or {}).get("full_name") != snapshot.repository
        or pr.get("base", {}).get("ref") != manifest.target
        or pr.get("merged") is not False
        or pr.get("state") != "open"
    ):
        raise AdoptionError("owned adopted PR changed native branch/repository identity")
    remote_head = _remote_head(runtime.project, snapshot.spec.remote, manifest.branch)
    local_head = verify.rev_parse_head(Path(unit.bindings["workspace"]))
    if (
        pr.get("head", {}).get("sha") != remote_head
        or not verify.is_ancestor(runtime.project, manifest.head, local_head)
        or not verify.is_ancestor(runtime.project, remote_head, local_head)
    ):
        raise AdoptionError("adopted remote branch gained unowned work or lost preserved ancestry")
