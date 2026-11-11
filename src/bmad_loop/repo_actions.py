"""Typed organization actions and deterministic authorization at every write.

Models propose and independently review intent; they cannot select API paths,
Git operations, capabilities or policy. Originals and all Git history survive.
"""

from __future__ import annotations

import copy
import re

from .campaign_github import MutationIntent
from .campaign_jobs import digest
from .campaign_model import CampaignError
from .repo_inventory import intent_blocks
from .verify import git_bytes

KINDS = (
    "link",
    "label",
    "assign_milestone",
    "consolidate",
    "close_duplicate",
    "close_superseded",
    "close_completed_milestone",
)
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "source": {"type": "string"},
        "target": {"type": "string"},
        "labels": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["kind", "source", "target", "labels", "reason"],
    "additionalProperties": False,
}
PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {"type": "array", "items": ACTION_SCHEMA},
        "planning_required": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["actions", "planning_required"],
    "additionalProperties": False,
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "action_digest": {"type": "string"},
        "evidence_digest": {"type": "string"},
        "covered_blocks": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["accepted", "action_digest", "evidence_digest", "covered_blocks", "reason"],
    "additionalProperties": False,
}


def semantic_details(value, observations):
    """Only this audit's durable comments may advance its reviewed version."""
    value = copy.deepcopy(value)
    if observations:
        value["record"].pop("updated_at", None)
        value["record"].pop("comments", None)
        value["comments"] = [
            row
            for row in value.get("comments", ())
            if not any(
                row.get("id") == item.get("id") and row.get("body") == item.get("body")
                for item in observations
            )
        ]
    return value


def referencing_pulls(catalog, source, repository):
    if source["kind"] != "issue":
        return []
    number = source["record"]["number"]
    pattern = (
        rf"(?<![\w/])#{number}\b|https://github\.com/{re.escape(repository)}/issues/{number}\b"
    )
    return [
        item
        for item in catalog["objects"].values()
        if item["kind"] == "pr"
        and item["record"].get("state") == "open"
        and re.search(pattern, item["record"].get("body") or "")
    ]


def validate_action(action, evidence, policy, *, owners=None, project=None):
    kind = action["kind"]
    source = evidence["source"]
    target = evidence.get("target")
    record = source["record"]
    if kind not in KINDS or not action["reason"].strip():
        raise CampaignError("Unknown or unexplained repository action")
    if record.get("state") != "open":
        raise CampaignError("Organization source is no longer open")
    labels = {row["name"] if isinstance(row, dict) else row for row in record.get("labels", ())}
    if target:
        labels |= {
            row["name"] if isinstance(row, dict) else row
            for row in target["record"].get("labels", ())
        }
    if labels & set(policy.protected_labels):
        raise CampaignError("Owner protection prevents organization")
    if kind == "label":
        if (
            source["kind"] == "milestone"
            or not action["labels"]
            or not set(action["labels"]) <= set(policy.labels)
            or action["target"]
        ):
            raise CampaignError("Label proposal exceeds standing policy")
        return
    if action["labels"]:
        raise CampaignError("Non-label action cannot modify labels")
    if kind == "assign_milestone":
        if (
            source["kind"] != "issue"
            or not target
            or target["kind"] != "milestone"
            or target["record"].get("state") != "open"
            or record.get("milestone") is not None
        ):
            raise CampaignError(
                "Milestone assignment requires an unassigned issue and existing open milestone"
            )
        return
    if kind == "close_completed_milestone":
        if (
            source["kind"] != "milestone"
            or action["target"]
            or not source.get("members")
            or any(row.get("state") != "closed" for row in source["members"])
        ):
            raise CampaignError("Milestone requires complete nonempty closed membership")
        if len(source["members"]) != record.get("closed_issues") or record.get("open_issues") != 0:
            raise CampaignError("Milestone member counts are incomplete")
    elif (
        not target
        or action["target"] == action["source"]
        or source["kind"] == "milestone"
        or target["kind"] == "milestone"
        or target["record"].get("state") != "open"
    ):
        raise CampaignError("Organization requires a distinct open destination")
    if not kind.startswith("close_"):
        return
    if not evidence.get("catalog_complete"):
        raise CampaignError("Incomplete catalog cannot authorize closure")
    if source["kind"] == "pr":
        if not owners or not owners.get("complete") or not owners.get("provider"):
            raise CampaignError("PR closure requires authenticated writer release")
        rows = [
            row
            for service in owners.get("services", ())
            for row in service.get("items", ())
            if (row.get("details") or {}).get("pr_number") == record["number"]
        ]
        if not rows:
            raise CampaignError("No positive release proof for existing PR")
        for row in rows:
            details = row.get("details") or {}
            exact = {
                "repository": policy.repository,
                "pr_node_id": record.get("node_id"),
                "head": record.get("head", {}).get("sha"),
                "base": record.get("base", {}).get("sha"),
                "branch": record.get("head", {}).get("ref"),
            }
            if (
                row.get("liveness") != "dead"
                or row.get("ownership") != "unowned"
                or not row.get("revision")
                or any(not value or details.get(name) != value for name, value in exact.items())
                or not details.get("fence_revision")
                or not details.get("binding_revision")
                or details.get("admissions_open") is not False
                or type(details.get("active_workers")) is not int
                or details.get("active_workers") != 0
                or details.get("preserved_clean") is not True
                or details.get("resumable_binding_verified") is not True
            ):
                raise CampaignError("Existing writer has not released this exact PR head")
        for row in owners.get("items", ()):
            details = row.get("details") or {}
            if (
                row not in rows
                and (
                    details.get("branch") == record.get("head", {}).get("ref")
                    or details.get("campaign_conflict") is True
                )
                and (
                    row.get("liveness") != "dead"
                    or details.get("admissions_open") is not False
                    or details.get("active_workers") != 0
                    or not details.get("fence_revision")
                )
            ):
                raise CampaignError("Another live, paused or uncertain writer covers this PR")
        if project is None or target["kind"] != "pr":
            raise CampaignError("PR consolidation requires an exact successor PR")
        old, new = record["head"]["sha"], target["record"]["head"]["sha"]
        if (
            not re.fullmatch(r"[0-9a-f]{40,64}", old)
            or not re.fullmatch(r"[0-9a-f]{40,64}", new)
            or git_bytes(project, "merge-base", "--is-ancestor", old, new).returncode
        ):
            raise CampaignError("Successor ancestry does not preserve the original PR history")
    if source["kind"] == "issue":
        # Existing attached work must be reconciled before its acceptance is closed.
        number = record["number"]
        owners = owners or {}
        if any(
            str(problem).startswith("native-campaigns")
            for problem in (*owners.get("unavailable", ()), *owners.get("truncated", ()))
        ):
            raise CampaignError("Native campaign issue ownership coverage is incomplete")
        matching_units = set()
        for item in owners.get("items", ()):
            details = item.get("details") or {}
            if item.get("kind") != "native-unit" or details.get("repository") != policy.repository:
                continue
            bindings = (details.get("issue"), details.get("shared_issue"))
            linked = (
                any(
                    isinstance(binding, dict) and binding.get("number") == number
                    for binding in bindings
                )
                or details.get("issue_url")
                == f"https://github.com/{policy.repository}/issues/{number}"
            )
            if linked:
                matching_units.add((details.get("campaign_id"), details.get("unit_id")))
                claim = details.get("claim")
                if (
                    item.get("state") not in {"delivered", "canceled"}
                    or (claim and claim.get("quiescent") is not True)
                    or details.get("hold")
                ):
                    raise CampaignError("A live, paused or uncertain campaign owns this issue")
        for item in owners.get("items", ()):
            details = item.get("details") or {}
            if (
                item.get("kind") == "native-job"
                and (details.get("campaign_id"), details.get("unit_id")) in matching_units
                and (details.get("claim") or {}).get("quiescent") is not True
            ):
                raise CampaignError("A native reasoning owner still holds this issue")
        pattern = rf"(?<![\w/])#{number}\b|https://github\.com/{re.escape(policy.repository)}/issues/{number}\b"
        if any(
            row["record"].get("state") == "open"
            and re.search(pattern, row["record"].get("body") or "")
            for row in evidence.get("pulls", ())
        ):
            raise CampaignError("Open PR still references this issue")


def validate_review(action, evidence, review):
    blocks = set(intent_blocks(evidence["source"]))
    covered = review["covered_blocks"]
    if (
        review["accepted"] is not True
        or review["action_digest"] != digest(action)
        or review["evidence_digest"] != digest(evidence)
        or len(set(covered)) != len(covered)
        or set(covered) != blocks
    ):
        raise CampaignError("Independent review lacks exact, complete intent coverage")


def steps(action, evidence, audit_id, generation, gateway):
    identity = digest([audit_id, action])[:32]
    marker = f"<!-- bmad-repo:{audit_id}:{identity} -->"
    source, target = evidence["source"], evidence.get("target")
    number = source["record"]["number"]
    root = gateway.root
    out = []

    def add(kind, method, path, payload, suffix):
        endpoint = (
            "milestones"
            if source["kind"] == "milestone"
            else "pulls" if source["kind"] == "pr" else "issues"
        )
        coordinates = {"id": source["record"]["id"], "object_path": f"{root}/{endpoint}/{number}"}
        out.append(
            MutationIntent(
                identity + suffix,
                "repo_" + kind,
                method,
                path,
                payload,
                marker,
                generation=generation,
                coordinates=coordinates,
            ).to_dict()
        )

    if action["kind"] == "label":
        add(
            "label",
            "POST",
            f"{root}/issues/{number}/labels",
            {"labels": action["labels"]},
            "-label",
        )
    elif action["kind"] == "assign_milestone":
        add(
            "milestone",
            "PATCH",
            f"{root}/issues/{number}",
            {"milestone": target["record"]["number"]},
            "-milestone",
        )
    elif action["kind"] == "close_completed_milestone":
        add("close", "PATCH", f"{root}/milestones/{number}", {"state": "closed"}, "-close")
    else:
        text = f"Repository organization: {action['reason']}\n\nSource: {source['record']['html_url']}\nDestination: {target['record']['html_url']}"
        if action["kind"] in {"consolidate", "close_duplicate", "close_superseded"}:
            text += "\n\nPreserved source intent and discussion:\n" + "\n\n".join(
                f"[{key}]\n{body}" for key, body in intent_blocks(source).items()
            )
        text += "\n\n" + marker
        if len(text.encode("utf-8")) > 60000:
            raise CampaignError(
                "Lossless consolidation exceeds one managed comment; no closure authorized"
            )
        add(
            "comment",
            "POST",
            f"{root}/issues/{target['record']['number']}/comments",
            {"body": text},
            "-comment",
        )
        if action["kind"].startswith("close_"):
            endpoint = "pulls" if source["kind"] == "pr" else "issues"
            payload = {"state": "closed"}
            if source["kind"] == "issue":
                payload["state_reason"] = "not_planned"
            add("close", "PATCH", f"{root}/{endpoint}/{number}", payload, "-close")
    return out


def readback(intent, gateway):
    """Positive outcome only. Absence never proves that an uncertain POST was not sent."""
    coordinates = intent["coordinates"]
    if gateway.get(coordinates["object_path"]).get("id") != coordinates["id"]:
        raise CampaignError("Native object identity changed during effect readback")
    if intent["kind"] == "repo_comment":
        rows = gateway.pages(intent["path"])
        matches = [row for row in rows if intent["marker"] in (row.get("body") or "")]
        return (
            matches[0]
            if len(matches) == 1 and matches[0].get("body") == intent["payload"]["body"]
            else None
        )
    if intent["kind"] == "repo_label":
        rows = gateway.pages(intent["path"])
        return (
            {"labels": rows}
            if set(intent["payload"]["labels"]) <= {row["name"] for row in rows}
            else None
        )
    row = gateway.get(intent["path"])
    if intent["kind"] == "repo_milestone":
        return (
            row
            if (row.get("milestone") or {}).get("number") == intent["payload"]["milestone"]
            else None
        )
    return row if all(row.get(key) == value for key, value in intent["payload"].items()) else None
