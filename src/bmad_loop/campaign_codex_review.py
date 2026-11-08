"""Native Codex review artifacts and a separate bounded contract-mapping turn.

The mapping turn supplies campaign-specific evidence; it cannot discard or
approve away native findings. Raw native review JSON remains in the receipt.
"""

import copy
import json
import math

from .campaign_jobs import digest
from .campaign_model import CampaignError


def validate_review(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise CampaignError("Native Codex review is not structured JSON") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("overall_correctness")
        not in {
            "patch is correct",
            "patch is incorrect",
        }
        or not isinstance(raw.get("overall_explanation"), str)
    ):
        raise CampaignError("Native Codex review verdict is missing")
    findings = raw.get("findings")
    if not isinstance(findings, list):
        raise CampaignError("Native Codex review findings are missing")
    for item in [raw, *findings]:
        confidence = (
            item.get("overall_confidence_score" if item is raw else "confidence_score")
            if isinstance(item, dict)
            else None
        )
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise CampaignError("Native Codex review confidence is invalid")
    for finding in findings:
        location = finding.get("code_location", {})
        lines = location.get("line_range", {})
        if (
            any(
                not isinstance(finding.get(key), str) or not finding[key]
                for key in ("title", "body")
            )
            or type(finding.get("priority")) is not int
            or not 0 <= finding["priority"] <= 3
            or not isinstance(location.get("absolute_file_path"), str)
            or not location["absolute_file_path"]
            or type(lines.get("start")) is not int
            or type(lines.get("end")) is not int
            or not 1 <= lines["start"] <= lines["end"]
        ):
            raise CampaignError("Native Codex finding lacks a valid location or priority")
    return raw


def envelopes(frames, *, partial=False):
    from .campaign_codex_appserver import envelopes as one_turn

    boundary = next(
        (index for index, frame in enumerate(frames) if frame.get("id") == 4 and "result" in frame),
        None,
    )
    first = frames if boundary is None else frames[:boundary]
    normalized = one_turn(first, partial=partial, review=True)
    if len(normalized) < 2:
        return normalized
    if normalized[-1].get("is_error"):
        if boundary is not None:
            raise CampaignError("Native Codex review mapping ran after a failed review")
        return normalized
    if boundary is None:
        if partial:
            return normalized[:1]
        raise CampaignError("Native Codex review mapping turn is missing")
    artifacts = []
    for frame in first:
        params = frame.get("params", {})
        items = (
            [params.get("item", {})]
            if frame.get("method") == "item/completed"
            else (
                params.get("turn", {}).get("items", [])
                if frame.get("method") == "turn/completed"
                else []
            )
        )
        for item in items:
            if item.get("type") == "exitedReviewMode":
                artifacts.append(item)
    distinct = {item["id"]: item.get("review") for item in artifacts if item.get("id")}
    if len(distinct) != 1:
        raise CampaignError("Native Codex review artifact is missing or ambiguous")
    bindings = [
        frame["params"] for frame in first if frame.get("method") == "bmad/nativeReviewEvidence"
    ]
    delegate = None
    if bindings:
        from .campaign_codex_review_evidence import evidence

        if len(bindings) != 1:
            raise CampaignError("Native review evidence is ambiguous")
        binding = bindings[0]
        root = next(
            frame["result"]["turn"]["id"]
            for frame in first
            if frame.get("id") == 3 and "result" in frame
        )
        if (
            binding["thread"] != normalized[0]["session_id"]
            or binding["root_turn"] != root
            or binding["item_id"] != next(iter(distinct))
            or not any(
                frame.get("method") == "turn/started"
                and frame["params"].get("turn", {}).get("id") == binding["child_turn"]
                and frame["params"]["turn"].get("rootTurnId") == root
                for frame in first
            )
        ):
            raise CampaignError("Native review snapshot differs from the observed RPC lineage")
        delegate = evidence(binding)
        if delegate["model"] != normalized[0]["model"]:
            raise CampaignError(
                "Native review delegate model differs from the configured native thread"
            )
        review = delegate["review"]
    else:
        review = validate_review(next(iter(distinct.values())))
    second = [frame for frame in first if frame.get("id") == 2 and "result" in frame]
    second += [{**frame, "id": 3} if frame.get("id") == 4 else frame for frame in frames[boundary:]]
    result = one_turn(second, partial=partial)
    if len(result) > 1:
        totals = result[-1].get("usage", {})
        if any(totals.get(key, -1) < value for key, value in normalized[-1]["usage"].items()):
            raise CampaignError("Native Codex review mapping usage decreased")
        result[-1]["num_turns"] = 2
        result[-1]["native_review"] = review
        if delegate:
            # Native review is a separate child session; parent token totals do
            # not contain its spend. Preserve and add the two real counters.
            result[-1]["usage"] = {
                key: totals.get(key, 0) + delegate["usage"].get(key, 0)
                for key in set(totals) | set(delegate["usage"])
            }
            result[-1]["native_review_evidence"] = binding
    return result


def fold_result(result, review):
    """Retain every native finding regardless of the mapping model's verdict."""
    review = validate_review(review)
    result = copy.deepcopy(result)
    if not isinstance(result, dict) or not isinstance(result.get("findings", []), list):
        raise CampaignError("Native review mapping did not produce campaign findings")
    preserved = []
    for index, finding in enumerate(review["findings"]):
        preserved.append(
            {
                **finding,
                "id": "native-codex:" + digest([review, index]),
                "required": True,
                "resolved": False,
                "source": "codex-review-start",
            }
        )
    result["findings"] = [*result.get("findings", []), *preserved]
    result["native_review"] = copy.deepcopy(review)
    if preserved:
        result["verdict"] = "changes_requested"
    elif (
        review["overall_correctness"] != "patch is correct"
        or review["overall_confidence_score"] == 0
    ):
        result["verdict"] = "hold"
    return result
