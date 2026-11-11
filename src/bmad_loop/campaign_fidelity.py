"""Independent semantic qualification for exact native routes and purposes.

Transport canaries establish input and lifecycle behavior. These separate proofs
bind a designated corpus and actual independent review to the precise model,
effort, earned transport evidence and semantic operation being admitted.
"""

from pathlib import Path

from .campaign_jobs import digest
from .campaign_model import CampaignError

HISTORY_AUDIT_PROMPT_VERSION = "history-audit-v3"
FINAL_AUDIT_PROMPT_VERSION = "history-final-audit-v1"

PURPOSES = {
    "handoff_map",
    "handoff_reduce",
    "handoff_final",
    "handoff_final_audit",
    "handoff_audit",
    "constraint_reduce",
    "constraint_audit",
    "recovery_extract",
    "recovery_audit",
}


def route_binding(cap, purpose, candidate=None):
    if purpose not in PURPOSES or len(cap.get("efforts", [])) != 1:
        raise CampaignError("Semantic fidelity requires an exact purpose and earned effort")
    effort = candidate.effort if candidate is not None else cap["efforts"][0]
    if effort != cap["efforts"][0] or (
        candidate is not None
        and (candidate.profile, candidate.model) != (cap["profile"], cap["model"])
    ):
        raise CampaignError("Semantic fidelity route does not match its candidate")
    return {
        "purpose": purpose,
        "route": {
            **{key: cap[key] for key in ("profile", "model", "cli_version", "binary_sha256")},
            "effort": effort,
            "transport_qualification_sha256": cap["qualification"]["sha256"],
        },
    }


def semantic_contract(purpose, audit_schema, contract=""):
    """Return only the schemas, readers and prompts this operation actually uses.

    Generic qualification must bind this digest in the independent native result,
    not merely in the editable outer manifest. Finite contracts retain their
    separately earned schema digest protocol; unused history headers do not apply.
    """
    from .campaign_summary import (
        EXTRACT_PROMPT_VERSION,
        FINAL_PROMPT_VERSION,
        REDUCE_PROMPT_VERSION,
        summary_schema,
        summary_schema_template,
    )
    from .campaign_transcripts import READER_VERSIONS

    result = {"schema_version": 1, "purpose": purpose}
    if contract == "constraints-v1" and purpose in {"constraint_reduce", "constraint_audit"}:
        from .campaign_handoff_constraints import AUDIT_SCHEMA, REDUCTION_SCHEMA

        return {
            **result,
            "contract": contract,
            "schemas_digest": digest([REDUCTION_SCHEMA, AUDIT_SCHEMA]),
        }
    if contract == "recovery-v1" and purpose in {"recovery_extract", "recovery_audit"}:
        from .campaign_handoff_recovery import AUDIT_SCHEMA, SCHEMA

        return {**result, "contract": contract, "schemas_digest": digest([SCHEMA, AUDIT_SCHEMA])}
    if contract or purpose not in {
        "handoff_map",
        "handoff_reduce",
        "handoff_final",
        "handoff_final_audit",
        "handoff_audit",
    }:
        raise CampaignError("Unknown semantic fidelity purpose/contract combination")
    result.update(
        reader_versions=dict(READER_VERSIONS), summary_schema_digest=digest(summary_schema())
    )
    result["prompt_version"] = {
        "handoff_map": EXTRACT_PROMPT_VERSION,
        "handoff_reduce": REDUCE_PROMPT_VERSION,
        "handoff_final": FINAL_PROMPT_VERSION,
        "handoff_audit": HISTORY_AUDIT_PROMPT_VERSION,
        "handoff_final_audit": FINAL_AUDIT_PROMPT_VERSION,
    }[purpose]
    if purpose in {"handoff_map", "handoff_reduce", "handoff_final"}:
        result["summary_job_schema_digest"] = digest(summary_schema_template())
    if purpose in {"handoff_audit", "handoff_final_audit"}:
        result["audit_schema_digest"] = digest(audit_schema)
    return result


def contract_header(purpose, audit_schema, contract=""):
    """Identify current contract declarations before checking their proof closure."""
    actual_contract = semantic_contract(purpose, audit_schema, contract)
    expected = {
        "schema_version": 2,
        "structural_pass": True,  # nosec B105: validation outcome, not a credential.
    }
    if contract:
        field = (
            "constraint_schemas_digest"
            if contract == "constraints-v1"
            else "recovery_schemas_digest"
        )
        expected[field] = actual_contract["schemas_digest"]
    else:
        expected.update(
            {key: actual_contract[key] for key in ("reader_versions", "summary_schema_digest")}
        )
        if purpose in {"handoff_map", "handoff_reduce", "handoff_final"}:
            expected["summary_job_schema_digest"] = actual_contract["summary_job_schema_digest"]
        if purpose == "handoff_final_audit":
            expected["audit_schema_digest"] = actual_contract["audit_schema_digest"]
        if purpose == "handoff_audit":
            expected.update(
                audit_schema_digest=actual_contract["audit_schema_digest"],
                audit_prompt_version=HISTORY_AUDIT_PROMPT_VERSION,
            )
    return actual_contract, expected


def validate(cap, audit_schema, *, candidate=None, purpose="handoff_audit", contract=""):
    from .campaign_headless import file_digest, provider_family, read_json

    binding = cap.get("semantic_qualification") or {}
    if not binding:
        raise CampaignError("Semantic handoff fidelity qualification is missing")
    if binding.get("schema_version") != 2:
        raise CampaignError(
            "Semantic handoff fidelity requires purpose-bound schema-2 qualification"
        )
    proofs = binding.get("proofs")
    if proofs is None:
        proofs = [{key: binding.get(key) for key in ("path", "sha256")}]
    if not isinstance(proofs, list) or not 1 <= len(proofs) <= 32:
        raise CampaignError("Semantic handoff fidelity proof list is invalid")
    target = route_binding(cap, purpose, candidate)
    actual_contract, expected = contract_header(purpose, audit_schema, contract)

    failures = []
    for proof in proofs:
        try:
            _proof(
                proof,
                cap,
                target,
                expected,
                contract,
                actual_contract,
                file_digest,
                read_json,
                provider_family,
            )
            return binding
        except (CampaignError, OSError, ValueError, TypeError, KeyError) as exc:
            failures.append(str(exc))
    raise CampaignError(
        "No matching semantic fidelity proof: " + "; ".join(dict.fromkeys(failures))
    )


def _proof(
    proof, cap, target, expected, contract, actual_contract, file_digest, read_json, provider_family
):
    if not isinstance(proof, dict) or not proof.get("path"):
        raise CampaignError("Semantic handoff fidelity qualification is missing")
    path = Path(proof["path"])
    if file_digest(path) != proof.get("sha256"):
        raise CampaignError("Semantic handoff qualification artifact changed")
    evidence = read_json(path)
    bindings = evidence.get("route_bindings")
    if (
        any(evidence.get(key) != value for key, value in expected.items())
        or not isinstance(bindings, list)
        or target not in bindings
    ):
        raise CampaignError(
            "Semantic fidelity proof does not match current readers/templates/schema/route/purpose"
        )
    corpus = evidence.get("corpus")
    if not isinstance(corpus, list) or not corpus:
        raise CampaignError("Semantic fidelity corpus is missing")
    for source in corpus:
        if not source.get("critical_ids") or file_digest(Path(source["source_path"])) != source.get(
            "source_sha256"
        ):
            raise CampaignError("Semantic fidelity corpus changed or lacks critical obligations")
    independent = evidence.get("independent_review") or {}
    if not independent.get("native_receipt_path") or file_digest(
        Path(independent["native_receipt_path"])
    ) != independent.get("sha256"):
        raise CampaignError("Independent native fidelity receipt is missing or changed")
    receipt = read_json(Path(independent["native_receipt_path"]))
    observed = provider_family(receipt.get("observed_model", ""))
    result = receipt.get("result") or {}
    if (
        "audit_prompt_version" in expected
        and result.get("audit_prompt_version") != expected["audit_prompt_version"]
    ):
        raise CampaignError("Independent native fidelity receipt did not review this audit prompt")
    if contract:
        field = (
            "constraint_schemas_digest"
            if contract == "constraints-v1"
            else "recovery_schemas_digest"
        )
        if result.get("contract_schema_digests", {}).get(contract) != expected[field]:
            raise CampaignError("Independent native fidelity receipt did not review this contract")
    if result.get("route_bindings_digest") != digest(bindings):
        raise CampaignError(
            "Independent native fidelity receipt did not review these purposes and efforts"
        )
    if not contract and result.get("semantic_contract_digests", {}).get(
        target["purpose"]
    ) != digest(actual_contract):
        raise CampaignError(
            "Independent native fidelity receipt did not review this semantic contract"
        )
    if (
        receipt.get("outcome") != "completed"
        or receipt.get("quiescent") is not True
        or receipt.get("input_complete") is not True
        or not receipt.get("session_id")
        or not observed
        or observed == provider_family(cap["model"])
        or result.get("verdict") != "passed"
        or result.get("corpus_digest") != digest(corpus)
        or any(
            result.get(key) != []
            for key in ("missing_critical_ids", "invented_authority", "unresolved_conflicts")
        )
    ):
        raise CampaignError("Independent native fidelity validation has not passed")
