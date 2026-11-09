"""Restore only the new PTY's verified native hook routing after account isolation."""

from .adapters.paradev_runtime import CONTEXT_FILE, verify_context
from .campaign_model import CampaignError


def restore(environment, source, presentation):
    if not source.get(CONTEXT_FILE):
        return environment, None
    verdict = verify_context(source)
    context = verdict.get("context", {})
    if not verdict.get("verified") or (
        presentation and context.get("SUPERSET_WORKSPACE_ID") != presentation["workspace_id"]
    ):
        raise CampaignError("Headless native terminal context is not its frozen workspace")
    return {**environment, **context}, {
        "verified": True,
        "checks": verdict["checks"],
        "terminal_id": context["SUPERSET_TERMINAL_ID"],
        "workspace_id": context["SUPERSET_WORKSPACE_ID"],
    }
