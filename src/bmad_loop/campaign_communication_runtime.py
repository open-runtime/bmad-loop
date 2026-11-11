"""Bind public controller mailboxes to immutable native launch generations."""

import errno

from .campaign_managed_communication import ManagedCommunication, _key
from .campaign_model import CampaignError
from .platform_util import LockUnavailableError


class RegistrationBusy(CampaignError):
    """This exact bridge is busy before the caller's worker request is sealed."""


def manager(runtime, snapshot):
    factory = getattr(runtime, "communication_manager", ManagedCommunication)
    return factory(runtime.store.directory(snapshot.id))


def prepare(runtime, snapshot, *, unit=None, job=None):
    if snapshot.spec.communication.mode != "managed":
        return None
    if job is not None:
        claim = job.get("claim") or {}
        unit_id, job_id = job["request"].get("unit", ""), job["id"]
        role, selection = job["request"]["role"], job.get("selection", {})
    elif unit is not None and unit.claim is not None:
        claim = vars(unit.claim)
        unit_id, job_id = unit.id, ""
        role, selection = unit.claim.role, unit.bindings.get("role_selection", {})
    else:
        raise CampaignError("Managed launch requires its owned unit or reasoning job")
    target = {
        "campaign_id": snapshot.id,
        "unit_id": unit_id,
        "job_id": job_id,
        "launch_id": claim["launch_id"],
        "generation": claim["generation"],
    }
    control = manager(runtime, snapshot)
    try:
        endpoint = control.prepare(
            snapshot,
            target,
            {
                "role": role,
                "selection": {
                    key: selection[key]
                    for key in ("profile", "model", "effort")
                    if key in selection
                },
                "stage": "prepared",
                "status_source": "controller",
                "steering": "next-safe-stage-boundary",
            },
        )
    except LockUnavailableError as exc:
        expected = control.directory / "actors" / _key(target) / ".registration.lock"
        if exc.errno not in (errno.EAGAIN, errno.EACCES) or exc.filename != str(expected):
            raise
        raise RegistrationBusy("Managed bridge registration is busy: " + str(expected)) from exc
    # Registration, cursors and facts evolve. The sealed native request carries
    # only the identity; retrying prepare must reproduce identical bytes.
    return {key: endpoint[key] for key in ("kind", "name", "room", "target")}


def apply_projection(snapshot, data):
    for endpoint in data.get("endpoints", ()):
        target = endpoint.get("target", {})
        if target.get("campaign_id") != snapshot.id:
            continue
        job = snapshot.jobs.get(target.get("job_id"))
        unit = snapshot.units.get(target.get("unit_id"))
        claim = (
            (job or {}).get("claim")
            if target.get("job_id")
            else (vars(unit.claim) if unit is not None and unit.claim is not None else None)
        )
        if not claim or (claim.get("launch_id"), claim.get("generation")) != (
            target.get("launch_id"),
            target.get("generation"),
        ):
            continue
        if job is not None:
            claim["managed_communication"] = endpoint
        elif unit is not None:
            unit.bindings["managed_communication"] = endpoint
