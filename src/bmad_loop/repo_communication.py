"""Explicit per-repository controller bridges over the existing managed ledger.

This projection borrows no campaign authority. Messages are retained advisory
mail, never policy or session-completion evidence; endpoints label their replies
as controller facts. No ambient room or existing native actor is impersonated.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict
from types import SimpleNamespace

from .campaign_managed_communication import ManagedCommunication
from .campaign_model import CampaignError


class RepoCommunication:
    def __init__(self, store, policy, *, factory=ManagedCommunication):
        self.store, self.policy, self.factory = store, policy, factory

    def manager(self, state):
        return self.factory(self.store.control_directory(state["id"]))

    def view(self, state):
        jobs = {}
        for identity, row in state["jobs"].items():
            if not row.get("launch_id"):
                continue
            receipt = self.store.read(row["receipt"]) if row.get("receipt") else {}
            jobs[identity] = {
                "request": {"unit": "", "role": row["role"]},
                "state": row["state"],
                "selection": asdict(getattr(self.policy, row["role"])),
                "receipt": receipt,
                "claim": {
                    "launch_id": row["launch_id"],
                    "generation": 1,
                    "quiescent": row.get("verified_quiescent") is True
                    or row.get("controller_quiescent") is True,
                    "status": row["state"],
                    "session_ids": [receipt["session_id"]] if receipt.get("session_id") else [],
                },
            }
        return SimpleNamespace(
            id=state["id"],
            spec=SimpleNamespace(
                communication=SimpleNamespace(mode="managed", room=self.policy.communication_room)
            ),
            jobs=jobs,
            units={},
            outbox=[],
        )

    def prepare(self, state, identity):
        if not self.policy.communication_room:
            return None
        row = state["jobs"][identity]
        target = {
            "campaign_id": state["id"],
            "unit_id": "",
            "job_id": identity,
            "launch_id": row["launch_id"],
            "generation": 1,
        }
        try:
            endpoint = self.manager(state).prepare(
                self.view(state),
                target,
                {
                    "role": row["role"],
                    "repository": self.policy.repository,
                    "audit_id": state["id"],
                    "status_source": "controller",
                    "model_reply": False,
                    "stage": "prepared",
                },
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            raise CampaignError(
                "Repository controller bridge preparation failed: " + str(exc)
            ) from exc
        return {key: endpoint[key] for key in ("kind", "name", "room", "target")}

    def sync(self, state, *, strict=True):
        if not self.policy.communication_room:
            return
        try:
            manager = self.manager(state)
            report = manager.observe(self.view(state))
            state["communication"] = report
            self.store.save(state, "communication-observed", available=report["available"])
            if not report["available"] and strict:
                raise CampaignError("Repository controller communication is unavailable")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            if not strict:
                state["communication"] = {
                    "available": False,
                    "errors": [str(exc)],
                    "mode": "managed",
                }
                self.store.save(state, "communication-unavailable")
                return
            raise CampaignError(
                "Repository controller bridge observation failed: " + str(exc)
            ) from exc
