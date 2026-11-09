"""A candidate-bound main/custom native review pair followed by schema mapping.

The two reviews are real review/start turns. The third turn only maps their
results; every finding and both delegate lineages remain authoritative.
"""

import re

from .campaign_model import CampaignError

FLAG = "--review-pair"


def selected(capability):
    return capability.get("protocol") == "codex-native-review" and FLAG in capability.get(
        "argv", ()
    )


def target(coordinates):
    if not isinstance(coordinates, dict) or any(
        not isinstance(coordinates.get(key), str)
        or re.fullmatch(r"[0-9a-f]{40}", coordinates[key]) is None
        for key in ("head", "base")
    ):
        raise CampaignError("Native review pair requires exact candidate and accepted base SHAs")
    return {"head": coordinates["head"], "base": coordinates["base"]}


def assert_target(workspace, value):
    from . import verify

    value = target(value)
    if verify.rev_parse_head(workspace) != value["head"] or not verify.worktree_clean(workspace):
        raise CampaignError("Native review pair candidate changed or has uncommitted work")
    result = verify.git_bytes(
        workspace, "merge-base", "--is-ancestor", value["base"], value["head"]
    )
    if result.returncode:
        raise CampaignError("Native review pair base is not integrated into the candidate")
    return value


def combine(reviews):
    from .campaign_codex_review import validate_review

    if len(reviews) != 2:
        raise CampaignError("Native main/custom pair requires both actual review artifacts")
    reviews = [validate_review(row) for row in reviews]
    return {
        "findings": [finding for row in reviews for finding in row["findings"]],
        "overall_correctness": (
            "patch is correct"
            if all(row["overall_correctness"] == "patch is correct" for row in reviews)
            else "patch is incorrect"
        ),
        "overall_explanation": "\n\n".join(
            kind + " native review: " + row["overall_explanation"]
            for kind, row in zip(("main", "custom"), reviews, strict=True)
        ),
        "overall_confidence_score": min(row["overall_confidence_score"] for row in reviews),
    }


def evidence(binding):
    from .campaign_codex_review_evidence import evidence as single_evidence

    if (
        not isinstance(binding, dict)
        or set(binding) != {"schema_version", "target", "reviews"}
        or binding["schema_version"] != 2
        or not isinstance(binding["reviews"], list)
        or any(
            not isinstance(row, dict) or set(row) != {"kind", "binding"}
            for row in binding["reviews"]
        )
        or [row.get("kind") for row in binding["reviews"]] != ["main", "custom"]
        or binding["target"] != target(binding["target"])
    ):
        raise CampaignError("Native review pair evidence is missing or incomplete")
    rows = binding["reviews"]
    observed = [single_evidence(row["binding"]) for row in rows]
    bindings = [row["binding"] for row in rows]
    if (
        len({row["thread"] for row in bindings}) != 1
        or any(
            len({row[key] for row in bindings}) != 2
            for key in ("root_turn", "child_turn", "item_id")
        )
        or len({row["session_id"] for row in observed}) != 2
        or len({row["model"] for row in observed}) != 1
        or len({row["effort"] for row in observed}) != 1
    ):
        raise CampaignError("Native review pair delegates are duplicated or attribution changed")
    return {
        "review": combine([row["review"] for row in observed]),
        "model": observed[0]["model"],
        "effort": observed[0]["effort"],
        "target": binding["target"],
        "usage": {
            key: sum(row["usage"].get(key, 0) for row in observed)
            for key in set().union(*(row["usage"] for row in observed))
        },
    }


def validate_receipt(receipt, capability, coordinates=None):
    from .campaign_codex_review import validate_review
    from .campaign_codex_review_evidence import evidence as single_evidence

    validate_review(receipt.get("native_review"))
    paired = selected(capability)
    if receipt.get("turns") != (3 if paired else 2):
        raise CampaignError("Native review evidence lacks its required bounded turns")
    if capability.get("confinement") == "codex-native-read-only-v1":
        observed = (evidence if paired else single_evidence)(receipt.get("native_review_evidence"))
        if (
            observed["review"] != receipt["native_review"]
            or observed["model"] != capability["model"]
            or observed["effort"] not in capability["efforts"]
            or (paired and coordinates is not None and observed["target"] != target(coordinates))
        ):
            raise CampaignError("Native review receipt differs from its frozen delegate evidence")


def stage_evidence(stage_receipt, capability, coordinates, *, launch):
    """Bind a stage's native proof to the owned request, never its model prose."""
    from pathlib import Path

    from .campaign_headless import file_digest, read_json
    from .campaign_jobs import digest
    from .platform_util import session_task_id

    if not selected(capability):
        raise CampaignError("Native review pair stage requires a qualified pair route")
    binding = stage_receipt.get("native_binding")
    if not isinstance(binding, dict):
        raise CampaignError("Native review pair stage lacks its native request binding")
    authority = Path(launch["execution"]).parent / "native" / launch["run_id"] / "headless"
    if (
        not authority.is_absolute()
        or any(
            stage_receipt.get(key) != launch.get(key)
            for key in ("campaign_id", "unit_id", "attempt", "generation", "launch_id")
        )
        or coordinates.get("campaign") != launch.get("campaign_id")
        or coordinates.get("unit") != launch.get("unit_id")
        or coordinates.get("stage") != "native_review_pair"
    ):
        raise CampaignError("Native review pair stage belongs to another worker launch")
    values = {}
    for name, path_key, digest_key in (
        ("request", "headless_request", "headless_request_sha256"),
        ("receipt", "reasoning_receipt", "headless_receipt_sha256"),
    ):
        path = Path(binding.get(path_key, ""))
        if (
            not path.is_absolute()
            or not path.is_relative_to(authority)
            or not path.resolve().is_relative_to(authority.resolve())
            or file_digest(path) != binding.get(digest_key)
        ):
            raise CampaignError("Native review pair stage request or receipt changed")
        values[name] = read_json(path)
    envelope, receipt = values["request"], values["receipt"]
    requested = envelope.get("request", {})
    if (
        envelope.get("capability") != capability
        or envelope.get("request_digest") != digest(requested)
        or receipt.get("request_digest") != envelope.get("request_digest")
        or receipt.get("launch_id") != envelope.get("launch_id")
        or receipt.get("generation") != envelope.get("generation")
        or receipt.get("outcome") != "completed"
        or envelope.get("generation") != launch["generation"]
        or envelope.get("launch_id")
        != session_task_id("campaign", launch["role"], launch["attempt"], launch["generation"])
        or any(
            requested.get("coordinates", {}).get(key) != coordinates.get(key)
            for key in (
                "campaign",
                "unit",
                "stage",
                "head",
                "base",
                "graph_revision",
                "config_revision",
                "inventory_digest",
                "source_revision",
            )
        )
        or stage_receipt.get("artifact", {}).get("native_review") != receipt.get("native_review")
    ):
        raise CampaignError(
            "Native review pair stage differs from its frozen candidate and request"
        )
    validate_receipt(receipt, capability, coordinates)
    return {
        "target": target(coordinates),
        "graph_revision": coordinates["graph_revision"],
        "config_revision": coordinates["config_revision"],
        "native_review": receipt["native_review"],
        "native_review_evidence": receipt["native_review_evidence"],
        "native_binding": dict(binding),
    }


def envelopes(frames, *, partial=False):
    from .campaign_codex_appserver import envelopes as one_turn
    from .campaign_codex_review import review_segment

    plans = [row["params"] for row in frames if row.get("method") == "bmad/nativeReviewPair"]
    if len(plans) != 1 or plans[0] != target(plans[0]):
        raise CampaignError("Native review pair target is missing or ambiguous")
    init = [row for row in frames if row.get("id") == 2 and "result" in row]
    boundaries = {
        identifier: [
            i for i, row in enumerate(frames) if row.get("id") == identifier and "result" in row
        ]
        for identifier in (3, 6, 4)
    }
    if any(len(rows) > 1 for rows in boundaries.values()):
        raise CampaignError("Native review pair repeats a native turn response")
    bindings, reviews, prior_usage = [], [], {}
    for index, identifier in enumerate((3, 6)):
        start = boundaries[identifier]
        following = boundaries[6 if identifier == 3 else 4]
        if not start:
            if partial:
                return one_turn(init, partial=True)
            raise CampaignError("Native review pair is missing a required review turn")
        end = following[0] if following else len(frames)
        if end <= start[0]:
            raise CampaignError("Native review pair native turns are out of order")
        segment = init + [
            {**row, "id": 3} if row.get("id") == identifier else row
            for row in frames[start[0] : end]
        ]
        normalized = one_turn(segment, partial=partial, review=True)
        if len(normalized) < 2:
            return normalized
        if normalized[-1].get("is_error"):
            if following:
                raise CampaignError("Native review pair continued after a failed native review")
            normalized[-1]["num_turns"] = index + 1
            return normalized
        if any(normalized[-1]["usage"].get(key, -1) < value for key, value in prior_usage.items()):
            raise CampaignError("Native review pair cumulative usage decreased")
        prior_usage = normalized[-1]["usage"]
        review, binding, delegate = review_segment(segment, normalized)
        if binding is None or delegate is None:
            raise CampaignError("Native review pair requires both retained delegate snapshots")
        bindings.append({"kind": ("main", "custom")[index], "binding": binding})
        reviews.append(review)
    if not boundaries[4]:
        if partial:
            return one_turn(init, partial=True)
        raise CampaignError("Native review pair mapping turn is missing")
    result = one_turn(
        init
        + [{**row, "id": 3} if row.get("id") == 4 else row for row in frames[boundaries[4][0] :]],
        partial=partial,
    )
    if len(result) > 1:
        totals = result[-1]["usage"]
        if any(totals.get(key, -1) < value for key, value in prior_usage.items()):
            raise CampaignError("Native review pair mapping usage decreased")
        binding = {"schema_version": 2, "target": plans[0], "reviews": bindings}
        observed = evidence(binding)
        result[-1].update(
            num_turns=3,
            native_review=combine(reviews),
            native_review_evidence=binding,
            usage={
                key: totals.get(key, 0) + observed["usage"].get(key, 0)
                for key in set(totals) | set(observed["usage"])
            },
        )
    return result
