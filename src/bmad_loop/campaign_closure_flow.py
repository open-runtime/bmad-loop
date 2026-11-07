"""Observe external closure prerequisites before reserving a broad writer."""

from pathlib import Path

from . import verify
from .campaign_closure import children_terminal
from .campaign_model import CampaignError, Hold, SourceSelector
from .campaign_source_publication import SourceBinding, SourcePublicationHold
from .campaign_store import add_event
from .campaign_supervisor import Observation


def observe(runtime, snapshot, action):
    unit = snapshot.units[action.unit]
    publisher = runtime._source_publisher(snapshot)
    try:
        binding = (
            SourceBinding.from_dict(unit.bindings["canonical_source"])
            if unit.bindings.get("canonical_source")
            else publisher.capture(runtime.source(snapshot, unit), unit.spec)
        )
        publisher = runtime._source_publisher(snapshot, project=Path(binding.project))
        head, _ = publisher._canonical(
            action.id + ":closure-prerequisite", verify.rev_parse_head(runtime.project)
        )
        publisher._closure_ready(binding, head)
        if unit.bindings.get("workspace"):
            children_terminal(
                Path(unit.bindings["workspace"]),
                SourceSelector(**unit.bindings["effective_source"]),
                unit.spec,
            )
    except (SourcePublicationHold, CampaignError) as exc:
        return Observation("confirmed", {"ready": False, "reason": str(exc)})
    return Observation("confirmed", {"ready": True, "head": head})


def apply(runtime, snapshot, unit, action, data):
    unit.evidence["closure_readiness"] = {
        **data,
        "observed_at": runtime.clock(),
        "graph_revision": snapshot.graph_revision,
    }
    if data["ready"]:
        if unit.hold and unit.hold.kind == "closure_prerequisite":
            unit.hold = None
            unit.retry_at = None
    else:
        runtime._set_hold(
            unit, Hold("closure_prerequisite", data["reason"], id=action.id, actor="bmad")
        )
        unit.retry_at = runtime.clock() + 30
    add_event(
        snapshot,
        "closure-prerequisite-observed",
        unit=unit.id,
        ready=data["ready"],
        reason=data.get("reason", ""),
    )
