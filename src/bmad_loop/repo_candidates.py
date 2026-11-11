"""Deterministic related-candidate retrieval over the complete local catalog.

Every source is delivered once. Candidate recall is explicitly partial: exact
links, shared milestones and discriminating title tokens rank useful targets,
without pretending that bounded model prompts perform an exhaustive pair scan.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from .campaign_model import CampaignError


def wire_size(value):
    """Account for the JSON source being escaped again in native input."""
    return len(json.dumps(json.dumps(value, ensure_ascii=True), ensure_ascii=True).encode())


def batches(objects, input_bytes, repository):
    source_budget, candidate_budget = input_bytes // 2, input_bytes // 5
    index = {
        key: {
            "kind": item["kind"],
            "title": item["record"].get("title"),
            "state": item["record"].get("state"),
            "number": item["record"]["number"],
        }
        for key, item in objects.items()
    }
    words, numbers, milestones, tokens, references = (
        defaultdict(set),
        defaultdict(set),
        defaultdict(set),
        {},
        {},
    )
    for key, item in objects.items():
        row = item["record"]
        tokens[key] = set(re.findall(r"[a-z0-9]{4,}", str(row.get("title", "")).lower()))
        for token in tokens[key]:
            words[token].add(key)
        if item["kind"] != "milestone":
            numbers[row["number"]].add(key)
        milestone = row.get("milestone") or {}
        if milestone.get("id"):
            milestones[milestone["id"]].add(key)
        pattern = rf"(?<![\w/])#([1-9][0-9]*)\b|https://github\.com/{re.escape(repository)}/(?:issues|pull)/([1-9][0-9]*)\b"
        references[key] = {
            int(part)
            for pair in re.findall(pattern, row.get("body") or "")
            for part in pair
            if part
        }
    reverse = defaultdict(set)
    for key, values in references.items():
        for number in values:
            for target in numbers[number]:
                reverse[target].add(key)
    open_milestones = [
        key
        for key, item in objects.items()
        if item["kind"] == "milestone" and item["record"].get("state") == "open"
    ]
    sources, current = [], {}
    for key, item in sorted(objects.items()):
        if wire_size({key: item}) > source_budget:
            raise CampaignError("One complete repository object exceeds a bounded source shard")
        if current and wire_size({**current, key: item}) > source_budget:
            sources.append(current)
            current = {}
        current[key] = item
    if current:
        sources.append(current)
    output = []
    for source in sources:
        scores, per_source = {}, {}
        for key, item in source.items():
            ranking = defaultdict(int)
            for number in references[key]:
                for target in numbers[number]:
                    ranking[target] += 1000
            for target in reverse[key]:
                ranking[target] += 1000
            milestone = item["record"].get("milestone") or {}
            for target in milestones.get(milestone.get("id"), ()):
                ranking[target] += 100
            for token in tokens[key]:
                # Common tokens provide little identity evidence and must not
                # turn an index posting into an unbounded pairwise model scan.
                if len(words[token]) <= max(10, len(objects) // 10):
                    for target in words[token]:
                        ranking[target] += 10
            for target in open_milestones:
                ranking[target] += 1
            ranked = sorted(
                (
                    target
                    for target in ranking
                    if target != key and index[target]["state"] == "open"
                ),
                key=lambda target: (-ranking[target], target),
            )
            chosen = ranked[:16]
            per_source[key] = {
                "retrieved": len(ranked),
                "retained_before_byte_bound": len(chosen),
                "omitted": len(ranked) - len(chosen),
            }
            for target in chosen:
                scores[target] = max(scores.get(target, 0), ranking[target])
        candidates = {}
        for target in sorted(scores, key=lambda target: (-scores[target], target)):
            if wire_size({**candidates, target: index[target]}) <= candidate_budget:
                candidates[target] = index[target]
        output.append(
            {
                "shard": source,
                "index": candidates,
                "candidate_selection": {
                    "version": 1,
                    "mode": "related_subset_not_exhaustive",
                    "catalog_objects": len(objects),
                    "selected": len(candidates),
                    "omitted_at_byte_bound": len(scores) - len(candidates),
                    "sources": per_source,
                },
            }
        )
    return output
