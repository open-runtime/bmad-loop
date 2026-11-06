"""Read-only migration inventory and owner-reviewed live qualification.

Inventory receipts are cached outside all checkouts. A manifest cites that exact
receipt; qualification rereads the real services and compares every item and
service revision before refreshing only observation timestamps. Manifest JSON
cannot provide an observer, service fence, command or replacement capability.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from pathlib import Path

from .campaign_github import GhTransport, GithubGateway
from .campaign_migration import (
    MigrationError,
    MigrationInventory,
    MigrationItem,
    ServiceInventory,
    manifest_from_dict,
    validate_manifest,
)
from .campaign_model import spec_to_dict
from .campaign_native_inventory import NativeServiceObserver, native_inventory
from .platform_util import atomic_write_text, is_link_like

MAX_BYTES = 32 * 1024 * 1024


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read(path: Path) -> dict:
    if is_link_like(path) or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise MigrationError("migration input must be a bounded regular file")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MigrationError("migration input must be an object")
    return data


def _inventory(raw: dict) -> MigrationInventory:
    value = dict(raw)
    value["items"] = tuple(
        MigrationItem(**{**item, "units": tuple(item.get("units", ()))}) for item in value["items"]
    )
    value["services"] = tuple(
        ServiceInventory(
            **{
                **service,
                "faults": tuple(service.get("faults", ())),
                "items": tuple(
                    MigrationItem(**{**item, "units": tuple(item.get("units", ()))})
                    for item in service["items"]
                ),
            }
        )
        for service in value.get("services", ())
    )
    value["unavailable"] = tuple(value.get("unavailable", ()))
    value["truncated"] = tuple(value.get("truncated", ()))
    result = MigrationInventory(**value)
    if result.schema_version != 1:
        raise MigrationError("unsupported migration inventory version")
    return result


def _facts(observed: MigrationInventory) -> dict:
    value = dataclasses.asdict(observed)
    value.pop("observed_at")
    value["items"] = sorted(value["items"], key=lambda item: item["id"])
    for service in value["services"]:
        service.pop("observed_at")
        service["items"] = sorted(service["items"], key=lambda item: item["id"])
    value["services"] = sorted(value["services"], key=lambda service: service["service"])
    return value


@dataclasses.dataclass(frozen=True)
class _RevalidatedInventory(MigrationInventory):
    reviewed_digest: str = ""

    @property
    def digest(self) -> str:
        return self.reviewed_digest


def _revalidated(
    reviewed: MigrationInventory, fresh: MigrationInventory, *, now: float
) -> MigrationInventory:
    if not reviewed.complete or not fresh.complete:
        raise MigrationError("migration inventory coverage is incomplete")
    if not 0 <= now - fresh.observed_at <= 120 or any(
        not service.complete or service.faults or not 0 <= now - service.observed_at <= 120
        for service in fresh.services
    ):
        raise MigrationError("fresh migration service observation is stale or incomplete")
    if _facts(reviewed) != _facts(fresh):
        raise MigrationError(
            "reviewed migration ownership/revisions changed; review a new inventory"
        )
    # Private wrapper is constructed only after authenticated equality above.
    # It preserves the owner's reviewed digest while proving current freshness.
    return _RevalidatedInventory(
        **{
            field.name: getattr(fresh, field.name)
            for field in dataclasses.fields(MigrationInventory)
        },
        reviewed_digest=reviewed.digest,
    )


def _context(project, spec, *, gateway=None, host=None):
    from .campaign_preflight import repository_name
    from .campaign_runtime import store_for
    from .campaign_workers import git

    project = Path(project).resolve()
    root = store_for(project).root
    gateway = gateway or GithubGateway(
        GhTransport(),
        spec.github.repository or repository_name(git(project, "remote", "get-url", spec.remote)),
    )
    observed = native_inventory(project, spec, gateway=gateway, campaign_root=root, host=host)
    return root, gateway, observed


def inventory_document(project, spec, *, gateway=None, host=None) -> dict:
    root, _, observed = _context(project, spec, gateway=gateway, host=host)
    receipt = {
        "schema_version": 1,
        "inventory_digest": observed.digest,
        "spec_digest": _digest(spec_to_dict(spec)),
        "inventory": dataclasses.asdict(observed),
    }
    encoded = json.dumps(receipt, sort_keys=True)
    if len(encoded.encode()) > MAX_BYTES:
        raise MigrationError("migration inventory exceeds receipt bound")
    path = root / "migration-inventory" / (observed.digest + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if is_link_like(path) or _read(path) != receipt:
            raise MigrationError("immutable migration inventory receipt changed")
    else:
        atomic_write_text(path, encoded, follow_symlinks=False)
    return {
        "ok": observed.complete,
        "inventory": dataclasses.asdict(observed),
        "inventory_digest": observed.digest,
        "receipt": str(path),
        "problems": [*observed.unavailable, *observed.truncated],
        "native_capabilities": NativeServiceObserver.capabilities(),
        "manifest_template": {
            "schema_version": 1,
            "common_dir": observed.common_dir,
            "inventory_digest": observed.digest,
            "authorized_by": "",
            "selections": [
                {
                    "id": item.id,
                    "revision": item.revision,
                    "disposition": "preserve",
                    "units": list(item.units),
                }
                for item in observed.items
            ],
            "disable_automations": [],
        },
    }


def qualify_document(
    project, spec, manifest_path: Path, *, gateway=None, host=None, preflight=None, clock=time.time
) -> dict:
    manifest = manifest_from_dict(_read(Path(manifest_path)))
    if not re.fullmatch(r"[0-9a-f]{64}", manifest.inventory_digest):
        raise MigrationError("migration manifest must cite an exact cached inventory digest")
    root, gateway, fresh = _context(project, spec, gateway=gateway, host=host)
    receipt = _read(root / "migration-inventory" / (manifest.inventory_digest + ".json"))
    reviewed = _inventory(receipt["inventory"])
    if (
        receipt.get("schema_version") != 1
        or receipt.get("inventory_digest") != reviewed.digest
        or reviewed.digest != manifest.inventory_digest
    ):
        raise MigrationError("cached migration inventory digest differs from the reviewed manifest")
    if receipt.get("spec_digest") != _digest(spec_to_dict(spec)):
        raise MigrationError("migration campaign specification changed since inventory review")
    current = _revalidated(reviewed, fresh, now=clock())
    if preflight is None:
        from .campaign_preflight import inspect

        replacement = inspect(Path(project).resolve(), spec, gateway=gateway)
    else:
        replacement = preflight(Path(project).resolve(), spec, gateway=gateway)
    qualification = validate_manifest(current, manifest, replacement, now=clock())
    return {
        "ok": qualification.qualified,
        "qualification": dataclasses.asdict(qualification),
        "inventory_digest": reviewed.digest,
        "fresh_inventory_digest": fresh.digest,
        "fresh_observed_at": fresh.observed_at,
        "problems": list(qualification.problems),
        "native_capabilities": NativeServiceObserver.capabilities(),
        "effects": "observation only; no automation, process, workspace or GitHub changes",
    }
