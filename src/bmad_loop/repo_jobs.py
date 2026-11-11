"""Bounded single-writer scheduling over qualified disposable native sessions.

Quota retries retain all immutable attempts and require explicit resume. Unknown
launches are observed and never replaced. Stop drains already admitted readers.
"""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .campaign_capacity import observe
from .campaign_headless import (
    HeadlessRunner,
    input_text,
    provider_family,
    read_json,
    readonly_grants,
)
from .campaign_jobs import JobLimits, JobRequest, digest, request_from_dict, validate_result
from .campaign_model import CampaignError
from .campaign_scheduler import route_unavailable
from .platform_util import is_link_like
from .repo_communication import RepoCommunication
from .repo_store import RepoStopped, common_dir


class RepoCapacityWait(CampaignError):
    """Availability can be rechecked without widening authority."""


class RepoJobs:
    def __init__(self, store, policy, *, runner=None, capacity=observe, communication=None):
        self.store, self.policy = store, policy
        self.runner, self.capacity = runner or HeadlessRunner(), capacity
        self.injected_runner = runner is not None
        self.communication = communication or RepoCommunication(store, policy)

    def _prepare(self, state, task):
        name, role, value, prompt, schema = task
        identity = "repo-" + digest([name, value, state["policy_digest"]])[:32]
        row = state["jobs"].get(identity)
        if row and row["state"] == "preparation_stopped":
            row.setdefault("preparations", []).append(
                {key: row[key] for key in ("path", "launch_id")}
            )
            row.update(state="retry_preparing", preparation=row.get("preparation", 0) + 1)
        if row and row["state"] == "capacity_wait" and row.get("receipt"):
            if row.get("waiting_epoch") == state.get("resume_epoch", 0):
                return identity, row
            if row["attempt"] >= self.policy.max_attempts:
                row.update(state="failed", reason="Repository native attempt budget exhausted")
                self.store.save(state, "attempt-budget-exhausted", job=identity)
            else:
                row["history"].append(
                    {
                        key: row[key]
                        for key in ("attempt", "path", "launch_id", "receipt", "request_digest")
                    }
                )
                row.update(state="retry_preparing", attempt=row["attempt"] + 1)
        if row and row["state"] != "retry_preparing":
            return identity, row
        if row is None:
            if len(state["jobs"]) >= self.policy.max_jobs:
                raise CampaignError("Repository reasoning job budget exhausted")
            row = {"state": "retry_preparing", "attempt": 1, "history": [], "role": role}
            state["jobs"][identity] = row
        candidate, cap = getattr(self.policy, role), state["capabilities"][role]
        source = self.store.artifact(state["id"], "input", value)
        path = Path(source["path"])
        raw = path.read_bytes()
        limit = min(self.policy.input_bytes, cap["input_tokens"])
        sources = [
            {
                "source_id": "evidence:" + source["digest"],
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content": raw.decode("utf-8"),
            }
        ]
        if len(input_text(prompt, schema, sources).encode("utf-8")) + 2048 > limit:
            raise CampaignError("Complete repository evidence exceeds qualified input budget")
        coordinates = {
            "graph_revision": 1,
            "config_revision": 1,
            "head": source["digest"],
            "base": state["policy_digest"],
        }
        request = JobRequest(
            identity,
            "semantic_sync" if role == "proposer" else "report",
            role,
            coordinates,
            (
                {
                    "source_id": sources[0]["source_id"],
                    "path": str(path),
                    "sha256": sources[0]["sha256"],
                    "tokens": len(raw),
                },
            ),
            prompt,
            schema,
            JobLimits(
                limit,
                min(self.policy.output_tokens, cap["output_tokens"]),
                min(self.policy.max_turns, cap["max_turns"]),
                self.policy.timeout_s,
            ),
            candidates=(candidate,),
        )
        launch_id = identity + f"-attempt-{row['attempt']}"
        if row.get("preparation"):
            launch_id += f"-preparation-{row['preparation']}"
        directory = self.store.control_directory(state["id"]) / "jobs" / launch_id
        row.pop("receipt", None)
        row.pop("endpoint", None)
        row.update(
            state="prepared",
            path=str(directory / "request.json"),
            request=self.store.artifact(state["id"], "job-request", asdict(request)),
            request_digest=digest(asdict(request)),
            launch_id=launch_id,
            reason="",
            controller_quiescent=False,
            verified_quiescent=False,
        )
        self.store.save(state, "job-prepared", job=identity, attempt=row["attempt"])
        return identity, row

    def _materialize(self, state, identity, row):
        candidate, cap = getattr(self.policy, row["role"]), state["capabilities"][row["role"]]
        request = request_from_dict(self.store.read(row["request"]))
        read_workspace = (
            Path(self.policy.presentation_workspace)
            if self.policy.presentation_workspace
            else self.store.project
        )
        presentation = None
        if self.policy.presentation_workspace:
            if common_dir(read_workspace) != self.store.common:
                raise CampaignError("Repository presentation belongs to another repository")
            presentation = {
                "workspace": str(read_workspace),
                "workspace_id": self.policy.presentation_workspace_id,
                "navigation_prefix": "Repository audit · " + row["role"],
            }
            if not self.injected_runner:
                from .adapters.paradev_backend import ParadevMultiplexer

                self.runner = HeadlessRunner(
                    ParadevMultiplexer(
                        state_dir=self.store.control_directory(state["id"]) / "native-transport"
                    )
                )
                self.injected_runner = True
        grants = (
            readonly_grants(
                read_workspace,
                request.coordinates,
                cap,
                read_only_paths=tuple(source["path"] for source in request.inputs),
            )
            if cap["permissions"] == "read-only-tools"
            else None
        )
        endpoint = self.communication.prepare(state, identity)
        if endpoint:
            row["endpoint"] = endpoint
            self.store.save(state, "job-controller-prepared", job=identity)
        prepared = self.runner.prepare(
            Path(row["path"]).parent,
            request,
            candidate,
            cap,
            launch_id=row["launch_id"],
            generation=1,
            permissions=cap["permissions"],
            grants=grants,
            communication_room=self.policy.communication_room,
            presentation=presentation,
            managed_binding=endpoint,
        )
        if str(prepared) != row["path"]:
            raise CampaignError("Repository native request path changed")

    def _start(self, state, identity, row, reserved):
        candidate = getattr(self.policy, row["role"])

        @contextmanager
        def admission():
            if self.store.stopped(state["id"]):
                raise RepoCapacityWait("Repository audit stop requested")
            observed = self.capacity(candidate, self.store.project)
            reason = route_unavailable(observed, now=time.time())
            if reason:
                raise RepoCapacityWait("Repository route capacity: " + reason)
            if observed.concurrency_known and observed.active + reserved >= observed.slots:
                raise RepoCapacityWait("Repository route slots reserved by active audit jobs")
            with self.store.admission(state["id"]):
                yield

        try:
            # Capacity is checked before registering a controller, then again at
            # the irreversible native launch-intent boundary.
            if not Path(row["path"]).exists():
                with admission():
                    pass
                self._materialize(state, identity, row)
            observed = self.runner.launch(Path(row["path"]), admission=admission)
        except (RepoCapacityWait, RepoStopped) as exc:
            row.update(state="capacity_wait", reason=str(exc))
            self.store.save(state, "job-capacity-wait", job=identity, reason=str(exc))
            return
        row["state"] = observed["state"]
        self.store.save(state, "job-observed", job=identity, state=row["state"])
        if observed["state"] == "finished":
            self._finish(state, identity, row, observed["receipt"])

    def _finish(self, state, identity, row, receipt):
        row["receipt"] = self.store.artifact(state["id"], "native-receipt", receipt)
        identity_ok = (
            receipt.get("job_id") == identity
            and receipt.get("launch_id") == row["launch_id"]
            and receipt.get("generation") == 1
            and receipt.get("request_digest") == row["request_digest"]
            and receipt.get("quiescent") is True
        )
        row["verified_quiescent"] = identity_ok
        if not identity_ok:
            row.update(
                state="unknown", reason="Repository native receipt identity/quiescence is uncertain"
            )
            self.store.save(state, "job-uncertain", job=identity)
            return
        if identity_ok and receipt.get("outcome") == "quota":
            row.update(
                state="capacity_wait",
                reason="Native quota outcome; explicit resume may recheck capacity",
                waiting_epoch=state.get("resume_epoch", 0),
            )
        else:
            candidate = getattr(self.policy, row["role"])
            families = {provider_family(model) for model in receipt.get("observed_models", ())}
            primary = provider_family(receipt.get("observed_model", ""))
            families.add(primary)
            valid = (
                identity_ok
                and receipt.get("outcome") == "completed"
                and receipt.get("input_complete") is True
                and primary == candidate.provider_family
            )
            if valid:
                try:
                    schema = read_json(Path(row["path"]))["request"]["output_schema"]
                    validate_result(receipt["result"], schema)
                    row.update(
                        state="completed",
                        result=self.store.artifact(state["id"], "result", receipt["result"]),
                        families=sorted(families),
                    )
                except CampaignError as exc:
                    row.update(state="failed", reason=str(exc))
            else:
                row.update(
                    state="failed",
                    reason="Repository native receipt failed identity, completion or attribution validation",
                )
        self.store.save(state, "job-terminal", job=identity, state=row["state"])
        self.communication.sync(state, strict=False)

    def _settle_controllers(self, state):
        if not self.policy.communication_room:
            return
        for identity, row in state["jobs"].items():
            if (
                not row.get("endpoint")
                or row.get("receipt")
                or row["state"] not in {"prepared", "capacity_wait", "retry_preparing"}
            ):
                continue
            directory = Path(row["path"]).parent
            # Only positive prelaunch absence permits controller closure. Links
            # and any persisted native intent stay uncertain and unreplaced.
            if any(
                path.exists() or is_link_like(path)
                for path in (
                    directory / "launch-intent.json",
                    directory / "startup.json",
                    directory / "receipt.json",
                )
            ):
                continue
            row.update(state="preparation_stopped", controller_quiescent=True)
            self.store.save(state, "job-controller-prelaunch-closed", job=identity)
        self.communication.sync(state)

    def run_many(self, state, tasks, *, allow_failed=False):
        try:
            return self._run_many(state, tasks, allow_failed=allow_failed)
        finally:
            self._settle_controllers(state)

    def _run_many(self, state, tasks, *, allow_failed=False):
        selected = [self._prepare(state, task) for task in tasks]
        waiting_routes = set()
        next_communication = 0.0
        while True:
            if self.policy.communication_room and time.monotonic() >= next_communication:
                self.communication.sync(state, strict=False)
                next_communication = time.monotonic() + 5
            active = []
            for identity, row in state["jobs"].items():
                if row["state"] in {"running", "unknown", "uncertain"}:
                    observed = self.runner.observe(Path(row["path"]))
                    if observed["state"] == "finished":
                        self._finish(state, identity, row, observed["receipt"])
                    else:
                        row["state"] = observed["state"]
                        active.append((identity, row))
            uncertain = any(row["state"] != "running" for _, row in active)
            if not uncertain and not self.store.stopped(state["id"]):
                for identity, row in selected:
                    if len(active) >= self.policy.max_sessions:
                        break
                    if row["state"] == "prepared" or (
                        row["state"] == "capacity_wait" and not row.get("receipt")
                    ):
                        candidate = getattr(self.policy, row["role"])
                        wait_key = (
                            candidate.profile,
                            candidate.model,
                            candidate.effort,
                            tuple(key for key, _ in active),
                        )
                        if wait_key in waiting_routes:
                            continue
                        reserved = sum(
                            getattr(self.policy, other["role"]).provider_family
                            == candidate.provider_family
                            for _, other in active
                        )
                        self._start(state, identity, row, reserved)
                        if row["state"] == "capacity_wait":
                            waiting_routes.add(wait_key)
                        if row["state"] in {"running", "unknown", "uncertain"}:
                            active.append((identity, row))
            if active and any(row["state"] == "running" for _, row in active):
                time.sleep(0.2)
                continue
            if uncertain or any(row["state"] in {"unknown", "uncertain"} for _, row in active):
                raise CampaignError(
                    "Repository native job outcome uncertain; replacement forbidden"
                )
            failures = [row for _, row in selected if row["state"] == "failed"]
            if failures and not allow_failed:
                raise CampaignError(failures[0]["reason"])
            if all(
                row["state"] == "completed" or (allow_failed and row["state"] == "failed")
                for _, row in selected
            ):
                return [
                    self.store.read(row["result"]) if row["state"] == "completed" else None
                    for _, row in selected
                ]
            if self.store.stopped(state["id"]):
                raise RepoCapacityWait("Repository audit stop requested; active children drained")
            raise RepoCapacityWait(
                "Repository jobs await fresh route capacity; resume preserves attempts"
            )

    def run(self, state, name, role, value, prompt, schema):
        return self.run_many(state, [(name, role, value, prompt, schema)])[0]
