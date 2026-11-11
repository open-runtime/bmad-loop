"""Bounded repository catalog and exact per-object evidence, observation only.

Catalog absence is never release proof for a paused or external writer. Remote
content is data; only the frozen local policy selects an installed observer.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict

from .campaign_adoption import load_provider
from .campaign_jobs import digest
from .campaign_migration import inventory
from .campaign_model import CampaignError
from .verify import git_bytes


def size(value):
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def key(kind, row):
    if (
        kind not in {"issue", "pr", "milestone"}
        or type(row.get("number")) is not int
        or row["number"] < 1
        or not row.get("id")
    ):
        raise CampaignError("Repository object lacks native identity")
    return f"{kind}:{row['number']}"


class RepoInventory:
    def __init__(self, project, gateway, policy):
        self.project, self.gateway, self.policy = project, gateway, policy

    def _git(self, *args):
        result = git_bytes(self.project, *args)
        if result.returncode:
            raise CampaignError("Repository Git observation failed")
        return result.stdout.decode("utf-8")

    def owners(self):
        from .campaign_runtime import store_for

        observers = ()
        identity = None
        if self.policy.ownership_observer:
            provider, observer = load_provider(self.policy.ownership_observer)
            identity = asdict(provider)

            def named_observer(project, common_dir):
                return observer(project, common_dir)

            observers = (named_observer,)
        value = inventory(
            self.project,
            gateway=self.gateway,
            campaign_root=store_for(self.project).root,
            observers=observers,
            max_items=self.policy.max_objects,
            max_bytes=self.policy.metadata_bytes,
        )
        return {**asdict(value), "complete": value.complete, "provider": identity}

    def repository_identity(self):
        # Verify the configured endpoint independently of arbitrary policy text.
        remote = self._git("remote", "get-url", self.policy.remote).strip()
        suffix = re.sub(r"\.git$", "", remote).rstrip("/")
        if suffix not in {
            f"https://github.com/{self.policy.repository}",
            f"git@github.com:{self.policy.repository}",
            f"ssh://git@github.com/{self.policy.repository}",
        }:
            raise CampaignError("Policy repository does not match the Git remote")
        root = self.gateway.get(self.gateway.root)
        if root.get(
            "full_name", ""
        ).casefold() != self.policy.repository.casefold() or not root.get("id"):
            raise CampaignError("GitHub repository native identity changed")
        return root

    def collect(self):
        root = self.repository_identity()
        objects, faults, used = {}, [], 0
        for kind, endpoint in (("issue", "issues"), ("pr", "pulls"), ("milestone", "milestones")):
            for page in self.gateway.iter_pages(f"{self.gateway.root}/{endpoint}?state=all"):
                for row in page:
                    if kind == "issue" and "pull_request" in row:
                        continue
                    identifier = key(kind, row)
                    if identifier in objects:
                        if objects[identifier]["record"] != row:
                            raise CampaignError("Repository catalog moved during pagination")
                        continue
                    used += size(row)
                    if used > self.policy.metadata_bytes or len(objects) >= self.policy.max_objects:
                        faults.append("catalog_bound")
                        break
                    objects[identifier] = {"kind": kind, "record": row}
                if faults:
                    break
            if faults:
                break
        refs = []
        for line in self._git(
            "for-each-ref",
            f"--count={self.policy.max_objects + 1}",
            "--format=%(refname) %(objectname)",
            "refs/heads",
            "refs/remotes",
        ).splitlines():
            ref, sha = line.split(" ", 1)
            if len(refs) >= self.policy.max_objects:
                faults.append("ref_bound")
                break
            refs.append({"ref": ref, "sha": sha})
        remote_refs = self._git("ls-remote", "--heads", self.policy.remote)
        if len(remote_refs.encode()) > self.policy.metadata_bytes:
            faults.append("remote_ref_bound")
            remote_refs = ""
        history = self._git(
            "log", "--all", f"--max-count={self.policy.max_objects + 1}", "--format=%H %P"
        ).splitlines()
        if len(history) > self.policy.max_objects:
            faults.append("history_bound")
            history = history[: self.policy.max_objects]
        owners = self.owners()
        if (
            used + size(refs) + size(history) + size(remote_refs) + size(owners)
            > self.policy.metadata_bytes
        ):
            faults.append("combined_inventory_bound")
        return {
            "schema_version": 1,
            "repository": root,
            "objects": objects,
            "refs": refs,
            "remote_refs": remote_refs,
            "history": history,
            "owners": owners,
            "complete": not faults,
            "unavailable": faults,
            "metadata_bytes": used,
            "coverage": {
                "objects": "all_states",
                "history": "bounded_all_refs",
                "owners_complete": owners["complete"],
            },
        }

    def details(self, identifier):
        kind, raw = identifier.split(":", 1)
        number = int(raw)
        endpoint = {"issue": "issues", "pr": "pulls", "milestone": "milestones"}[kind]
        path = f"{self.gateway.root}/{endpoint}/{number}"
        row = self.gateway.get(path)
        if key(kind, row) != identifier:
            raise CampaignError("Repository detail identity changed")
        expected = f"https://github.com/{self.policy.repository}/{'pull' if kind == 'pr' else 'milestone' if kind == 'milestone' else 'issues'}/{number}"
        if (
            not isinstance(row.get("html_url"), str)
            or row["html_url"].casefold() != expected.casefold()
        ):
            raise CampaignError("Repository detail URL belongs to a different native object")
        result = {"kind": kind, "record": row}
        if kind == "milestone":
            result["members"] = self.gateway.pages(
                f"{self.gateway.root}/issues?state=all&milestone={number}"
            )
        else:
            result["comments"] = self.gateway.comments(number)
            result["events"] = self.gateway.pages(f"{self.gateway.root}/issues/{number}/events")
            if kind == "pr":
                result.update(
                    files=self.gateway.files(number),
                    reviews=self.gateway.reviews(number),
                    threads=self.gateway.review_threads(number),
                )
                if len(result["files"]) != row.get("changed_files") or len(result["files"]) >= 3000:
                    raise CampaignError("PR file coverage is incomplete")
                head = row.get("head", {}).get("sha", "")
                result["checks"] = self.gateway.pages(
                    f"{self.gateway.root}/commits/{head}/check-runs", collection="check_runs"
                )
                result["statuses"] = self.gateway.pages(
                    f"{self.gateway.root}/commits/{head}/statuses"
                )
        if digest(self.gateway.get(path)) != digest(row):
            raise CampaignError("Repository object moved during detail observation")
        if size(result) > self.policy.detail_bytes:
            raise CampaignError("Required object evidence exceeds detail budget")
        return result


def intent_blocks(details):
    """Lossless requirement/discussion units; models cannot omit a nonempty block."""
    result = {}
    row = details["record"]
    for field in ("title", "body", "description"):
        if row.get(field):
            result[f"object/{field}"] = row[field]
    for group in ("comments", "reviews"):
        for item in details.get(group, ()):
            if item.get("body"):
                result[f"{group}/{item['id']}"] = item["body"]
    for thread in details.get("threads", ()):
        for item in thread.get("comments", {}).get("nodes", ()):
            if item.get("body"):
                result[f"threads/{item['id']}"] = item["body"]
    return result
