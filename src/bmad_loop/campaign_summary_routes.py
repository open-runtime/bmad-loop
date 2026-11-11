"""Frozen, purpose-specific summary ladders; no model names imply qualification.

Role limits still account for summarization and auditing as shared resources.
Only an explicit stage map changes candidate authority or ordered selection.
The temporary selection view never changes the durable campaign specification.
"""

from __future__ import annotations

from dataclasses import replace

from .campaign_model import CampaignError

STAGES = {
    "leaf": ("summarization", "handoff_map", ""),
    "intermediate": ("summarization", "handoff_reduce", ""),
    "final": ("summarization", "handoff_final", ""),
    "constraint": ("summarization", "constraint_reduce", "constraints-v1"),
    "recovery": ("summarization", "recovery_extract", "recovery-v1"),
    "audit": ("handoff_audit", "handoff_final_audit", ""),
    "constraint_audit": ("handoff_audit", "constraint_audit", "constraints-v1"),
    "recovery_audit": ("handoff_audit", "recovery_audit", "recovery-v1"),
}


def enabled(spec):
    return bool(spec.reasoning.summary_routes)


def stage_for(purpose, contract=""):
    for stage, (_, expected, expected_contract) in STAGES.items():
        if (purpose, contract) == (expected, expected_contract):
            return stage
    raise CampaignError("Unknown summary purpose or contract")


def candidates(spec, role, stage=""):
    """Stage authority, or the complete route union for read-only discovery."""
    mapping = dict(spec.reasoning.summary_routes)
    if not mapping:
        return spec.candidates(role)
    if stage:
        if stage not in STAGES or STAGES[stage][0] != role or stage not in mapping:
            raise CampaignError("Summary stage does not belong to its frozen role")
        return dict(spec.role_pools)[mapping[stage]]
    result = list(spec.candidates(role))
    for name, pool in mapping.items():
        if STAGES[name][0] == role:
            result.extend(dict(spec.role_pools)[pool])
    return tuple(dict.fromkeys(result))


def selection_spec(spec, role, stage):
    if not stage or not enabled(spec):
        return spec
    options = candidates(spec, role, stage)
    pool = "__summary_stage_" + stage
    pools = {**dict(spec.role_pools), pool: options}
    roles = dict(spec.roles)
    roles[role] = replace(
        spec.role(role), pool=pool, profile="", model="", effort="", capacity_command=()
    )
    return replace(spec, role_pools=tuple(pools.items()), roles=tuple(roles.items()))


def qualified_candidates(spec, routes, stage):
    """Return exact earned stage options and visible exclusions, without probing."""
    from .campaign_fidelity import validate
    from .campaign_handoff_obligations import AUDIT_SCHEMA
    from .campaign_headless import capability_for, validate_capability

    role, purpose, contract = STAGES[stage]
    qualified, reasons = [], []
    for candidate in candidates(spec, role, stage):
        try:
            cap = capability_for(
                routes, candidate.profile, candidate.model, "reasoning", effort=candidate.effort
            )
            validate_capability(cap, candidate, permissions=cap["permissions"])
            validate(cap, AUDIT_SCHEMA, candidate=candidate, purpose=purpose, contract=contract)
            qualified.append(candidate)
        except (CampaignError, OSError, ValueError) as exc:
            reasons.append(f"{candidate.profile}/{candidate.model}/{candidate.effort}: {exc}")
    return tuple(qualified), reasons


def request_stage(spec, request):
    """Check durable coordinates before a new native launch acquires authority."""
    stage = request.coordinates.get("summary_stage", "")
    if not stage:
        if enabled(spec) and request.purpose in {"handoff_map", "handoff_reduce", "handoff_audit"}:
            raise CampaignError("Summary request is missing its frozen stage authority")
        return ""
    if not enabled(spec) or stage not in STAGES:
        raise CampaignError("Summary request has no frozen stage authority")
    role, purpose, contract = STAGES[stage]
    expected_purpose = (
        "handoff_reduce" if stage == "final" else "handoff_audit" if stage == "audit" else purpose
    )
    expected_contract = {
        "constraint": "constraint-reduce-v1",
        "recovery": "recovery-extract-v1",
        "constraint_audit": "constraint-audit-v1",
        "recovery_audit": "recovery-audit-v1",
    }.get(stage, "")
    # Contract jobs share the established handoff_audit transport purpose.
    if expected_contract:
        expected_purpose = "handoff_audit"
    if (
        request.role != role
        or request.purpose != expected_purpose
        or request.coordinates.get("contract", "") != expected_contract
        or any(candidate not in candidates(spec, role, stage) for candidate in request.candidates)
    ):
        raise CampaignError("Summary request differs from its frozen stage authority")
    if stage == "final":
        from .campaign_summary import FINAL_PROMPT_VERSION

        if (
            request.coordinates.get("summary_spec", {}).get("prompt_version")
            != FINAL_PROMPT_VERSION
        ):
            raise CampaignError("Final synthesis lacks its qualified final prompt contract")
    if stage == "audit":
        from .campaign_fidelity import FINAL_AUDIT_PROMPT_VERSION

        if (
            request.coordinates.get("audit_prompt_version") != FINAL_AUDIT_PROMPT_VERSION
            or not isinstance(request.coordinates.get("final_frontier"), list)
            or not request.coordinates["final_frontier"]
        ):
            raise CampaignError("Final synthesis audit lacks its qualified frontier contract")
    return stage


def validate_request(snapshot, request):
    stage = request_stage(snapshot.spec, request)
    if stage:
        options, reasons = qualified_candidates(
            snapshot.spec, snapshot.config_snapshot.get("reasoning_capabilities", {}), stage
        )
        if any(candidate not in options for candidate in request.candidates):
            raise CampaignError("Summary stage qualification changed: " + "; ".join(reasons))
    return stage


def reserve_auditor(runtime, snapshot, authors, *, reserved=""):
    """Reserve one qualified independent family without occupying a session slot.

    All three owner-authority audits must remain possible. Availability is
    checked now and again at launch; losing it creates a wait, never permission
    for a summary author to audit itself.
    """
    from .campaign_handoff_flow import _cap, validate_fidelity_qualification
    from .campaign_headless import provider_family

    for candidate in candidates(snapshot.spec, "handoff_audit", "audit"):
        family = provider_family(candidate.model)
        if not family or family in authors or (reserved and family != reserved):
            continue
        try:
            for stage in ("audit", "constraint_audit", "recovery_audit"):
                options = []
                for option in candidates(snapshot.spec, "handoff_audit", stage):
                    if provider_family(option.model) != family:
                        continue
                    _, purpose, contract = STAGES[stage]
                    try:
                        cap = _cap(snapshot, option, "reasoning")
                        validate_fidelity_qualification(
                            cap, candidate=option, purpose=purpose, contract=contract
                        )
                    except CampaignError:
                        continue
                    options.append(option)
                if not options:
                    raise CampaignError("Reserved family lacks a required audit contract")
                if (
                    hasattr(runtime, "_role")
                    and runtime._role(
                        snapshot,
                        "handoff_audit",
                        allowed_candidates=tuple(options),
                        permissions="reasoning",
                        require_headless=True,
                        summary_stage=stage,
                    )
                    is None
                ):
                    raise CampaignError("Reserved audit family is currently unavailable")
        except CampaignError:
            continue
        return family
    raise CampaignError("No independent qualified audit family can be reserved")


def independent_families(stage_options):
    """Families that can audit every contract without exhausting any producer stage."""
    from .campaign_headless import provider_family

    families = {
        stage: {provider_family(candidate.model) for candidate in options} - {""}
        for stage, options in stage_options.items()
    }
    audits = ("audit", "constraint_audit", "recovery_audit")
    producers = ("leaf", "intermediate", "final", "constraint", "recovery")
    reserved = set.intersection(*(families.get(stage, set()) for stage in audits))
    return tuple(
        sorted(
            family
            for family in reserved
            if all(families.get(stage, set()) - {family} for stage in producers)
        )
    )
