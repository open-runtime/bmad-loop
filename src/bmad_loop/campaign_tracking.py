"""One-step GitHub hierarchy projection for a durable campaign supervisor.

The projection returns at most one outbound intent. Its caller atomically stores
that intent and the returned bindings, submits it through GithubGateway, and
supplies the saved receipt on the next step. No mutation is performed here.
Existing tracker bindings take precedence over markers and never get reparented.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from .campaign_completion_scope import retained_parent_reason
from .campaign_github import GithubError, GithubGateway, JsonObject, MutationIntent, managed_marker
from .campaign_model import CampaignSnapshot, CampaignUnit, UnitState
from .fences import fenced_spans


def _mirror_markers(body: str) -> set[str]:
    """Only standalone legacy identity comments, never quoted code examples."""
    spans = fenced_spans(body)
    return {
        match[1]
        for match in re.finditer(r"(?m)^<!-- github-mirror:issue:([^\r\n<>]+) -->[ \t]*\r?$", body)
        if not any(start <= match.start() < end for start, end in spans)
    }


def _settled_member(unit: CampaignUnit) -> bool:
    if unit.state == UnitState.DELIVERED:
        return True
    if unit.state == UnitState.CANCELED:
        from .campaign_runtime_intake import optional_canceled

        return optional_canceled(unit)
    return False


@dataclass(frozen=True)
class TrackingStep:
    action_key: str
    intent: MutationIntent | None
    bindings: JsonObject
    unit_bindings: JsonObject
    complete: bool = False
    reason: str = ""


class HierarchyCoordinator:
    def __init__(self, gateway: GithubGateway):
        self.gateway = gateway

    def step(
        self,
        snapshot: CampaignSnapshot,
        unit_id: str,
        *,
        source_links: tuple[str, ...] = (),
        source_metadata: JsonObject | None = None,
        receipts: Mapping[str, MutationIntent] | None = None,
    ) -> TrackingStep:
        """Ensure milestone, epic, parent stories, leaf and native links in order.

        ``receipts`` is keyed by the stable action_key returned by the previous
        call. No absent uncertain create is retried. A prepared returned intent
        is always the *same* action; supervisor outbox identity deduplicates it.
        """
        unit = snapshot.units[unit_id]
        bindings = copy.deepcopy(snapshot.bindings.get("github_hierarchy", {}))
        unit_bindings = copy.deepcopy(unit.bindings)
        saved = receipts or {}
        metadata = source_metadata or {}
        initiative = unit.spec.initiative or snapshot.spec.name
        namespace = f"{unit.spec.source}:{initiative}"
        milestone_key = "initiative:" + namespace
        epic_key = "epic:" + namespace + ":" + unit.spec.epic
        nodes: list[tuple[str, str, str, str | None, CampaignUnit | None]] = [
            (milestone_key, "milestone", initiative, None, None),
            (epic_key, "issue", unit.spec.epic, None, None),
        ]
        ancestry: list[CampaignUnit] = []
        current = unit
        seen: set[str] = set()
        while True:
            if current.id in seen:
                raise GithubError("Story parent cycle cannot be projected")
            seen.add(current.id)
            ancestry.append(current)
            if not current.spec.parent:
                break
            current = snapshot.units[current.spec.parent]
        if len(ancestry) > 7:
            raise GithubError("Native subissue hierarchy exceeds GitHub's supported depth")
        parent_key = epic_key
        for story in reversed(ancestry):
            key = "story:" + story.id
            nodes.append((key, "issue", story.spec.title, parent_key, story))
            parent_key = key
        imported = None
        if (
            "story:" + unit.id not in bindings
            and not unit.bindings.get("issue")
            and not unit.bindings.get("tracker_id")
            and not metadata.get(unit.id, {}).get("tracker_id")
        ):
            imported = self._legacy_mirror(snapshot, unit, metadata)
        if imported is not None:
            for key, kind, _, _, _ in nodes:
                prior_key = self._action(snapshot, key, "create")
                prior = saved.get(prior_key)
                if prior:
                    if prior.state == "confirmed" and prior.receipt:
                        confirmed = self._binding(prior.receipt, kind)
                        if key not in bindings:
                            bindings[key] = confirmed
                            return TrackingStep(prior_key, None, bindings, unit_bindings)
                        if any(
                            bindings[key].get(field) != confirmed[field]
                            for field in ("kind", "database_id", "number")
                        ):
                            raise GithubError(
                                "Legacy mirror binding conflicts with confirmed create receipt"
                            )
                        continue
                    return TrackingStep(
                        prior_key,
                        None,
                        bindings,
                        unit_bindings,
                        reason="Legacy mirror conflicts with prior hierarchy publication; reconcile the existing create first",
                    )
        self._seed_imported_lineage(
            unit, ancestry, metadata, bindings, milestone_key, epic_key, imported_issue=imported
        )
        leaf_key = "story:" + unit.id
        # parent_confirmed on an adopted leaf means native lineage was shorter than
        # the campaign split; keep collapsing on later steps after imported is unset.
        if (
            len(ancestry) > 1
            and bindings.get(leaf_key, {}).get("parent_confirmed")
            and any("story:" + story.id not in bindings for story in ancestry[1:])
        ):
            nodes = [
                (key, kind, title, epic_key if key == leaf_key else parent, story)
                for key, kind, title, parent, story in nodes
                if story is None or key == leaf_key
            ]
        for key, kind, title, _, story in nodes:
            if key in bindings:
                continue
            action_key = self._action(snapshot, key, "create")
            existing = saved.get(action_key)
            if existing:
                if existing.state == "confirmed" and existing.receipt:
                    bindings[key] = self._binding(existing.receipt, kind)
                    return TrackingStep(action_key, None, bindings, unit_bindings)
                return TrackingStep(
                    action_key,
                    None,
                    bindings,
                    unit_bindings,
                    reason=existing.error or "Outbound create requires reconciliation",
                )
            # Imported tracker identity wins. A number alone belongs to this repository;
            # a URL for another repository is a conflict, never silently recreated.
            tracker = ""
            if story:
                tracker = str(
                    story.bindings.get("tracker_id")
                    or metadata.get(story.id, {}).get("tracker_id")
                    or ""
                )
                bound = story.bindings.get("issue")
                if bound:
                    number = bound["number"] if isinstance(bound, dict) else bound
                    found = self.gateway.issue(int(number))
                    bindings[key] = self._binding(found, kind)
                    return TrackingStep(action_key, None, bindings, unit_bindings)
            if tracker:
                number = self._tracker_number(tracker)
                found = self.gateway.issue(number)
                bindings[key] = self._binding(found, kind)
                return TrackingStep(action_key, None, bindings, unit_bindings)
            marker = managed_marker(snapshot.id, key, kind)
            candidates = (
                self.gateway.pages(f"{self.gateway.root}/milestones?state=all")
                if kind == "milestone"
                else self.gateway.issues(state="all")
            )
            matches = [
                row
                for row in candidates
                if marker in str(row.get("body") or row.get("description") or "")
            ]
            if len(matches) > 1:
                raise GithubError("Conflicting GitHub hierarchy marker matches")
            if matches:
                bindings[key] = self._binding(matches[0], kind)
                return TrackingStep(action_key, None, bindings, unit_bindings)
            criteria = (
                "\n".join(f"- {criterion}" for criterion in story.spec.acceptance) if story else ""
            )
            body = (
                f"BMAD campaign: {snapshot.spec.name}\n\nSource: {story.spec.id if story else key}"
            )
            if criteria:
                body += "\n\nAcceptance criteria:\n" + criteria
            if source_links:
                body += "\n\n" + "\n".join(source_links)
            milestone = bindings.get(milestone_key, {}).get("number") if kind == "issue" else None
            intent = self.gateway.prepare_create(
                kind,
                action_id=action_key,
                campaign_id=snapshot.id,
                unit_id=key,
                title=title,
                body=body,
                milestone=milestone,
                generation=snapshot.generation,
            )
            return TrackingStep(action_key, intent, bindings, unit_bindings)
        # A delivered reviewed plan can extend closed scope. New owner edits
        # after that review remain a hold rather than being reopened by a bot.
        planning = snapshot.units.get(str(unit.bindings.get("planning_unit", "")))
        for key, kind, _, parent, story in nodes:
            if story is not None:
                continue
            binding = bindings[key]
            observed = (
                self.gateway.issue(binding["number"])
                if kind == "issue"
                else self.gateway.get(f"{self.gateway.root}/milestones/{binding['number']}")
            )
            if observed.get("state") not in {"open", "closed"}:
                raise GithubError("Parent scope state is unavailable")
            if observed.get("state") != "closed":
                binding.pop("closed_confirmed", None)
                binding["state"] = "open"
                continue
            flow = planning.evidence.get("planning_flow", {}) if planning else {}
            reviewed_at = flow.get("stages", {}).get("planning_review", {}).get("finished_at", 0)
            try:
                updated_at = datetime.fromisoformat(
                    observed.get("updated_at", "").replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, TypeError):
                updated_at = float("inf")
            source_revision = metadata.get(unit.id, {}).get("revision")
            if (
                not planning
                or planning.state != UnitState.DELIVERED
                or unit.id not in flow.get("activated_units", ())
                or not source_revision
                or not reviewed_at
                or updated_at > reviewed_at
            ):
                return TrackingStep(
                    "",
                    None,
                    bindings,
                    unit_bindings,
                    reason="Closed parent scope needs reviewed extension authority and fresh owner-state reconciliation",
                )
            action_key = self._action(snapshot, key, "reopen")
            if saved.get(action_key) and saved[action_key].state == "confirmed":
                return TrackingStep(
                    action_key,
                    None,
                    bindings,
                    unit_bindings,
                    reason="Parent was closed again after the reviewed scope reopening",
                )
            intent = self.gateway.reopen_intent(
                kind,
                binding["number"],
                action_id=action_key,
                generation=snapshot.generation,
                approved_scope={
                    "planning_pr": planning.bindings["pr"]["number"],
                    "source_revision": source_revision,
                    "graph_revision": snapshot.graph_revision,
                },
            )
            return TrackingStep(action_key, intent, bindings, unit_bindings)
        # Creation is complete. Native relationships are separate durable actions.
        milestone = bindings[milestone_key]["number"]
        for key, kind, _, parent, story in nodes:
            if kind != "issue":
                continue
            issue = bindings[key]
            if not issue.get("milestone_confirmed"):
                action_key = self._action(snapshot, key, "milestone")
                existing = saved.get(action_key)
                if existing and existing.state == "confirmed":
                    issue["milestone_confirmed"] = True
                    return TrackingStep(action_key, None, bindings, unit_bindings)
                if existing:
                    return TrackingStep(
                        action_key,
                        None,
                        bindings,
                        unit_bindings,
                        reason=existing.error or "Milestone assignment pending",
                    )
                observed = self.gateway.issue(issue["number"])
                membership = observed.get("milestone")
                if membership and membership.get("number") == milestone:
                    issue["milestone_confirmed"] = True
                    return TrackingStep(action_key, None, bindings, unit_bindings)
                intent = self.gateway.milestone_intent(
                    issue["number"], milestone, action_id=action_key, generation=snapshot.generation
                )
                return TrackingStep(action_key, intent, bindings, unit_bindings)
            if parent and not issue.get("parent_confirmed"):
                action_key = self._action(snapshot, key, "parent")
                existing = saved.get(action_key)
                if existing and existing.state == "confirmed":
                    issue["parent_confirmed"] = True
                    return TrackingStep(action_key, None, bindings, unit_bindings)
                if existing:
                    return TrackingStep(
                        action_key,
                        None,
                        bindings,
                        unit_bindings,
                        reason=existing.error or "Native parent relationship pending",
                    )
                intent = self.gateway.prepare_subissue(
                    bindings[parent]["number"],
                    issue["number"],
                    action_id=action_key,
                    child_database_id=issue["database_id"],
                    generation=snapshot.generation,
                )
                return TrackingStep(action_key, intent, bindings, unit_bindings)
        unit_bindings["issue"] = bindings["story:" + unit.id]
        unit_bindings["milestone"] = bindings[milestone_key]
        unit_bindings["epic_issue"] = bindings[epic_key]
        return TrackingStep("", None, bindings, unit_bindings, complete=True)

    def refresh(self, snapshot: CampaignSnapshot, unit_id: str) -> JsonObject:
        """Refresh active mappings before/after planning, review and merge."""
        unit = snapshot.units[unit_id]
        observed: JsonObject = {"issues": {}, "pulls": {}}
        for key in ("issue", "epic_issue"):
            binding = unit.bindings.get(key)
            if binding:
                number = binding["number"] if isinstance(binding, dict) else binding
                observed["issues"][key] = {
                    "issue": self.gateway.issue(int(number)),
                    "comments": self.gateway.comments(int(number)),
                }
        binding = unit.bindings.get("pr")
        if binding:
            number = binding["number"] if isinstance(binding, dict) else binding
            observed["pulls"][str(number)] = {
                "pr": self.gateway.pull(int(number)),
                "reviews": self.gateway.reviews(int(number)),
                "threads": self.gateway.review_threads(int(number)),
                "comments": self.gateway.comments(int(number)),
            }
        return observed

    def transition(
        self,
        snapshot: CampaignSnapshot,
        unit_id: str,
        *,
        event_id: str,
        message: str,
        head: str = "",
    ) -> MutationIntent:
        unit = snapshot.units[unit_id]
        binding = unit.bindings.get("pr") or unit.bindings.get("issue")
        if not binding:
            raise GithubError("Transition has no mapped issue/PR")
        number = binding["number"] if isinstance(binding, dict) else binding
        delivered = sum(member.state == UnitState.DELIVERED for member in snapshot.units.values())
        text = f"{message}\n\nUnit: `{unit_id}`\nHead: `{head or 'pending'}`\nDelivered units: {delivered}/{len(snapshot.units)}"
        return self.gateway.prepare_comment(
            int(number),
            action_id=event_id,
            campaign_id=snapshot.id,
            unit_id=unit_id,
            text=text,
            generation=snapshot.generation,
        )

    def closure(
        self, snapshot: CampaignSnapshot, unit_id: str, *, source_confirmed: bool
    ) -> MutationIntent:
        unit = snapshot.units[unit_id]
        if unit.state not in {UnitState.MERGED_PENDING_SYNC, UnitState.DELIVERED}:
            raise GithubError("Tracking closure requires a confirmed remote merge")
        if any(
            child.spec.parent == unit_id and child.state != UnitState.DELIVERED
            for child in snapshot.units.values()
        ):
            raise GithubError("Story closure requires all split child units delivered")
        binding = unit.bindings.get("issue")
        if not binding:
            raise GithubError("Unit has no mapped issue")
        number = binding["number"] if isinstance(binding, dict) else binding
        return self.gateway.close_issue_intent(
            int(number),
            action_id=self._action(snapshot, unit_id, "close"),
            generation=snapshot.generation,
            source_confirmed=source_confirmed,
        )

    def close_parents(
        self,
        snapshot: CampaignSnapshot,
        *,
        receipts: Mapping[str, MutationIntent] | None = None,
        only_key: str = "",
    ) -> TrackingStep:
        """Close drained parent issues and milestones after child delivery.

        Unmanaged open children/milestone members prevent automatic closure. This
        debt does not reserve a publication slot or block unrelated code merges.
        """
        bindings = copy.deepcopy(snapshot.bindings.get("github_hierarchy", {}))
        saved = receipts or {}
        managed_ids = {
            binding["database_id"]
            for binding in bindings.values()
            if binding.get("kind") == "issue"
        }
        for key, binding in bindings.items():
            if only_key and key != only_key:
                continue
            if not key.startswith("epic:") or binding.get("closed_confirmed"):
                continue
            members = [
                unit
                for unit in snapshot.units.values()
                if "epic:"
                + f"{unit.spec.source}:{unit.spec.initiative or snapshot.spec.name}:{unit.spec.epic}"
                == key
            ]
            if not members or any(not _settled_member(unit) for unit in members):
                continue
            reason = retained_parent_reason(snapshot, key)
            if reason:
                return TrackingStep("", None, bindings, {}, reason=reason)
            action_key = self._action(snapshot, key, "close")
            existing = saved.get(action_key)
            if existing:
                if existing.state == "confirmed":
                    binding["closed_confirmed"] = True
                    binding["state"] = "closed"
                    return TrackingStep(action_key, None, bindings, {})
                return TrackingStep(
                    action_key,
                    None,
                    bindings,
                    {},
                    reason=existing.error or "Parent closure pending",
                )
            children = self.gateway.pages(
                f"{self.gateway.root}/issues/{binding['number']}/sub_issues"
            )
            if any(row.get("state") != "closed" for row in children):
                return TrackingStep(
                    action_key,
                    None,
                    bindings,
                    {},
                    reason="Parent has open required or unmanaged native subissues",
                )
            intent = self.gateway.close_issue_intent(
                binding["number"],
                action_id=action_key,
                generation=snapshot.generation,
                source_confirmed=True,
            )
            return TrackingStep(action_key, intent, bindings, {})
        for key, binding in bindings.items():
            if only_key and key != only_key:
                continue
            if not key.startswith("initiative:") or binding.get("closed_confirmed"):
                continue
            namespace = key.removeprefix("initiative:")
            members = [
                unit
                for unit in snapshot.units.values()
                if f"{unit.spec.source}:{unit.spec.initiative or snapshot.spec.name}" == namespace
            ]
            if not members or any(not _settled_member(unit) for unit in members):
                continue
            reason = retained_parent_reason(snapshot, key)
            if reason:
                return TrackingStep("", None, bindings, {}, reason=reason)
            if any(
                k.startswith("epic:" + namespace + ":") and not b.get("closed_confirmed")
                for k, b in bindings.items()
            ):
                if only_key:
                    return TrackingStep(
                        "", None, bindings, {}, reason="Milestone retains open epic scope"
                    )
                continue
            action_key = self._action(snapshot, key, "close")
            existing = saved.get(action_key)
            if existing:
                if existing.state == "confirmed":
                    binding["closed_confirmed"] = True
                    binding["state"] = "closed"
                    return TrackingStep(action_key, None, bindings, {})
                return TrackingStep(
                    action_key,
                    None,
                    bindings,
                    {},
                    reason=existing.error or "Milestone closure pending",
                )
            open_members = self.gateway.pages(
                f"{self.gateway.root}/issues?state=open&milestone={binding['number']}"
            )
            if open_members:
                return TrackingStep(
                    action_key,
                    None,
                    bindings,
                    {},
                    reason="Milestone retains open scope outside this delivered selection",
                )
            intent = self.gateway.close_milestone_intent(
                binding["number"],
                action_id=action_key,
                generation=snapshot.generation,
                required_units_delivered=True,
                managed_issue_ids=managed_ids,
            )
            return TrackingStep(action_key, intent, bindings, {})
        return TrackingStep("", None, bindings, {}, complete=True)

    def _legacy_mirror(
        self, snapshot: CampaignSnapshot, unit: CampaignUnit, metadata: JsonObject
    ) -> JsonObject | None:
        """Reuse exact native ticket mirrors before publishing a new hierarchy.

        Intake runs concurrently with existing stories and cannot guarantee their
        tracker IDs are populated first. A repository-scoped, unique mirror
        comment is identity evidence; a matching title or selected issue order is
        not. The fresh issue and its existing parents remain the native authority.
        """
        if (
            unit.spec.kind != "story"
            or metadata.get(unit.id, {}).get("kind") != "tickets"
            or not unit.spec.initiative
            or not unit.spec.selector
        ):
            return None
        identity = unit.spec.initiative + "/" + unit.spec.selector
        matches = [
            row
            for row in self.gateway.issues(state="all")
            if identity in _mirror_markers(str(row.get("body") or ""))
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise GithubError("Conflicting legacy GitHub mirror matches")
        expected_campaign_marker = managed_marker(snapshot.id, "story:" + unit.id, "issue")

        def validate(row: JsonObject) -> None:
            number = row.get("number")
            body = str(row.get("body") or "")
            if (
                type(number) is not int
                or number <= 0
                or type(row.get("id")) is not int
                or row["id"] <= 0
                or "pull_request" in row
                or str(row.get("html_url", "")).lower()
                != f"https://github.com/{self.gateway.repository}/issues/{number}".lower()
                or (
                    row.get("repository_url") is not None
                    and str(row["repository_url"]).lower()
                    != f"https://api.github.com/repos/{self.gateway.repository}".lower()
                )
            ):
                raise GithubError("Legacy mirror lacks exact same-repository native issue identity")
            if row.get("state") != "open":
                raise GithubError("Legacy mirror issue is not open; owner reconciliation required")
            if _mirror_markers(body) != {identity}:
                raise GithubError("Legacy mirror identity changed or has conflicting markers")
            campaign_markers = re.findall(r"<!-- bmad-campaign:v1:[a-f0-9]{64} -->", body)
            if any(marker != expected_campaign_marker for marker in campaign_markers):
                raise GithubError("Legacy mirror has another campaign ownership marker")

        found = matches[0]
        validate(found)
        current = self.gateway.issue(found["number"])
        validate(current)
        if any(current.get(key) != found.get(key) for key in ("id", "number", "node_id")):
            raise GithubError("Legacy mirror native identity changed during observation")
        return current

    def _seed_imported_lineage(
        self,
        unit: CampaignUnit,
        ancestry: list[CampaignUnit],
        metadata: JsonObject,
        bindings: JsonObject,
        milestone_key: str,
        epic_key: str,
        *,
        imported_issue: JsonObject | None = None,
    ) -> None:
        leaf_key = "story:" + unit.id
        if leaf_key in bindings:
            return
        tracker = str(
            unit.bindings.get("tracker_id") or metadata.get(unit.id, {}).get("tracker_id") or ""
        )
        bound = unit.bindings.get("issue")
        if not tracker and not bound and imported_issue is None:
            return
        if imported_issue is not None and not tracker and not bound:
            issue = imported_issue
        else:
            number = (
                (bound["number"] if isinstance(bound, dict) else bound)
                if bound
                else self._tracker_number(tracker)
            )
            issue = self.gateway.issue(int(number))
        bindings[leaf_key] = self._binding(issue, "issue")
        milestone = issue.get("milestone")
        if milestone:
            existing = bindings.get(milestone_key)
            if existing and existing["database_id"] != milestone["id"]:
                raise GithubError(
                    "Imported issue belongs to another milestone; explicit migration required"
                )
            bindings[milestone_key] = {**(existing or {}), **self._binding(milestone, "milestone")}
        # Legacy mirrors may predate campaign split-story parents. Only reuse a
        # full native chain that already matches; otherwise keep the leaf's real
        # parent (or parentless state) and do not invent intermediate stories.
        if imported_issue is not None and not tracker and not bound and len(ancestry) > 1:
            child = issue
            mapped: list[tuple[CampaignUnit, JsonObject]] = []
            for parent_unit in ancestry[1:]:
                parent = self._parent(child["number"])
                if not parent:
                    break
                mapped.append((parent_unit, parent))
                child = parent
            else:
                parent = self._parent(child["number"])
                if parent:
                    for parent_unit, row in mapped:
                        key = "story:" + parent_unit.id
                        existing = bindings.get(key)
                        if existing and existing["database_id"] != row["id"]:
                            raise GithubError(
                                "Imported story parent conflicts with existing lineage"
                            )
                        bindings[key] = {**(existing or {}), **self._binding(row, "issue")}
                    existing = bindings.get(epic_key)
                    if existing and existing["database_id"] != parent["id"]:
                        raise GithubError("Imported epic parent conflicts with existing lineage")
                    bindings[epic_key] = {
                        **(existing or {}),
                        **self._binding(parent, "issue"),
                    }
                    return
            parent = self._parent(issue["number"])
            if parent:
                existing = bindings.get(epic_key)
                if existing and existing["database_id"] != parent["id"]:
                    raise GithubError("Imported epic parent conflicts with existing lineage")
                bindings[epic_key] = {**(existing or {}), **self._binding(parent, "issue")}
            bindings[leaf_key]["parent_confirmed"] = True
            return
        child = issue
        # A declared split-story parent maps to its actual parent before the epic.
        for parent_unit in ancestry[1:]:
            parent = self._parent(child["number"])
            if not parent:
                return
            key = "story:" + parent_unit.id
            existing = bindings.get(key)
            if existing and existing["database_id"] != parent["id"]:
                raise GithubError("Imported story parent conflicts with existing lineage")
            bindings[key] = {**(existing or {}), **self._binding(parent, "issue")}
            child = parent
        parent = self._parent(child["number"])
        if parent:
            existing = bindings.get(epic_key)
            if existing and existing["database_id"] != parent["id"]:
                raise GithubError("Imported epic parent conflicts with existing lineage")
            bindings[epic_key] = {**(existing or {}), **self._binding(parent, "issue")}

    def _parent(self, number: int) -> JsonObject | None:
        try:
            return self.gateway.get(f"{self.gateway.root}/issues/{number}/parent")
        except GithubError as exc:
            if exc.status != 404:
                raise
            return None

    @staticmethod
    def _binding(row: JsonObject, kind: str) -> JsonObject:
        if not row.get("id") or not row.get("number"):
            raise GithubError("Remote hierarchy object has no stable database ID/number")
        return {
            "kind": kind,
            "number": row["number"],
            "database_id": row["id"],
            "node_id": row.get("node_id", ""),
            "url": row.get("html_url", ""),
            "state": row.get("state", "open"),
        }

    def _tracker_number(self, tracker: str) -> int:
        if tracker.isdigit() and int(tracker) > 0:
            return int(tracker)
        if tracker.startswith("#") and tracker[1:].isdigit() and int(tracker[1:]) > 0:
            return int(tracker[1:])
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)", tracker)
        if match and match.group(1).lower() == self.gateway.repository.lower():
            return int(match.group(2))
        raise GithubError("Existing tracker binding does not identify an issue in this repository")

    @staticmethod
    def _action(snapshot: CampaignSnapshot, key: str, operation: str) -> str:
        if operation in {"close", "reopen"}:
            operation += f":graph-{snapshot.graph_revision}"
        return "github-" + hashlib.sha256(f"{snapshot.id}:{key}:{operation}".encode()).hexdigest()
