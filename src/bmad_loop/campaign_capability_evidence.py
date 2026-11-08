"""Bind advertised native capabilities to the exact canary that earned them.

Qualification files are operator-owned evidence, not an editable checklist. A
manifest may narrow measured limits, but cannot change the selected effort,
launcher, transport or permission scope while retaining another route's proof.
"""

from pathlib import Path

from .campaign_jobs import digest
from .campaign_model import CampaignError


def request_binding(path: Path) -> dict:
    from .campaign_headless import file_digest

    return {"path": str(path), "sha256": file_digest(path)}


def validate(capability, candidate) -> None:
    try:
        _validate(capability, candidate)
    except CampaignError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CampaignError("Malformed native qualification evidence") from exc


def _validate(capability, candidate) -> None:
    from .campaign_headless import file_digest, read_json
    from .campaign_qualification import validate_probe_envelope

    qualification = capability["qualification"]
    path = Path(qualification.get("path", ""))
    if file_digest(path) != qualification.get("sha256"):
        raise CampaignError("Native qualification evidence changed")
    report = read_json(path)
    if report.get("schema_version") not in {1, 2} or report.get("ok") is not True:
        raise CampaignError("Native qualification report has not passed")
    request_path = path.parent / "request.json"
    if report.get("schema_version") == 2 and report.get("request_binding") != request_binding(
        request_path
    ):
        raise CampaignError("Native qualification request binding changed")
    # Version 1 evidence predates the explicit envelope hash. Its supported read
    # path binds the frozen sibling request through the report and owned receipt;
    # it never upgrades or rewrites an existing proof in place.
    envelope = read_json(request_path)
    validate_probe_envelope(envelope)
    if (
        digest(envelope["request"]) != envelope.get("request_digest")
        or digest(envelope["capability"]) != envelope.get("capability_digest")
        or report.get("request_digest") != envelope["request_digest"]
        or envelope["selection"] not in envelope["request"]["candidates"]
        or envelope["selection"]["profile"] != candidate.profile
        or envelope["selection"]["model"] != candidate.model
        or capability.get("efforts") != [envelope["selection"]["effort"]]
        or candidate.effort != envelope["selection"]["effort"]
    ):
        raise CampaignError("Native qualification belongs to a different request or effort")
    # Semantic fidelity has its own independent evidence validator; attaching
    # that evidence does not change the executable native transport route.
    adjusted = {
        "qualification",
        "semantic_qualification",
        "input_tokens",
        "output_tokens",
        "max_turns",
        "efforts",
    }
    proposal = envelope["capability"]
    if {k: v for k, v in capability.items() if k not in adjusted} != {
        k: v for k, v in proposal.items() if k not in adjusted
    }:
        raise CampaignError("Native qualification route, executable or permissions changed")
    receipt = report.get("receipt", {})
    expected = envelope["probe"].get("expected")
    writer = envelope["permissions"] == "workspace-tools"
    readonly = envelope["permissions"] == "read-only-tools"
    if writer or readonly:
        from .campaign_execution_qualification import expected_result

        expected = expected_result(envelope["probe"])
        checks = {
            "native_completed",
            "nonce",
            "scoped_commit",
            "canonical_objects",
            "content",
            "clean",
            "branch",
            "main",
            "protected",
            "attribution",
            "input_complete",
            "usage",
            "target_ref",
            "sibling",
            "outside",
            "protected_after_probe",
            "object_unlink",
            "object_overwrite",
            "object_hardlink",
            "packed_object_shadow",
            "bank_shadow_readback",
        }
        if readonly:
            checks -= {
                "object_unlink",
                "object_hardlink",
                "packed_object_shadow",
                "bank_shadow_readback",
            }
            checks |= {"owned", "new_product", "worker_ref", "git_index", "readable", "descendant"}
        if envelope["capability"]["protocol"] == "codex-native-review":
            from .campaign_codex_review_pair import validate_receipt

            validate_receipt(receipt, envelope["capability"], envelope["request"]["coordinates"])
            checks.add("native_review")
        confinement = envelope["capability"].get("confinement")
        if confinement in {"codex-native-read-only-v1", "codex-scoped-tools-v1"}:
            from .campaign_readonly_qualification import native_checks

            native_checks(receipt.get("native_confinement"))
        if confinement == "codex-scoped-tools-v1":
            from .campaign_execution_qualification import scoped_tool_checks

            earned = scoped_tool_checks(envelope)
            if not all(earned.values()):
                raise CampaignError("Native scoped writer lacks completed tool evidence")
            checks |= {*earned, "native_builtin_confinement"}
        if confinement == "agy-scoped-command-v1":
            from .campaign_execution_qualification import scoped_tool_checks

            earned = scoped_tool_checks(envelope)
            if not all(earned.values()):
                raise CampaignError("Native AGY writer lacks completed scoped tool evidence")
            checks |= set(earned)
        if any(report.get("checks", {}).get(key) is not True for key in checks):
            raise CampaignError("Native writer qualification lacks verified containment")
        covered_input = report.get("input_tokens")
        covered_output = report.get("configured_output_tokens")
    else:
        if report.get("probe_digest") != envelope["probe_digest"]:
            raise CampaignError("Native qualification canary identity changed")
        covered_input = report.get("coverage", {}).get("input_tokens")
        covered_output = report.get("coverage", {}).get("configured_output_tokens")
    limits = envelope["request"]["limits"]
    # The worker writes its native receipt before process exit. The owning
    # observer adds quiescence and window-death evidence after independent readback.
    projected = {"quiescent", "completion_evidence", "presentation_status"}
    native_receipt = read_json(path.parent / "receipt.json")
    if (
        {k: v for k, v in receipt.items() if k not in projected}
        != {k: v for k, v in native_receipt.items() if k not in projected}
        or receipt.get("request_digest") != envelope["request_digest"]
        or receipt.get("launch_id") != envelope["launch_id"]
        or receipt.get("generation") != envelope["generation"]
        or receipt.get("job_id") != envelope["request"]["id"]
        or receipt.get("outcome") != "completed"
        or receipt.get("quiescent") is not True
        or receipt.get("processes_quiescent") is not True
        or receipt.get("completion_evidence") != "owned-window-death-and-process-readback"
        or receipt.get("input_complete") is not True
        or receipt.get("truncated") is True
        or receipt.get("result") != expected
        or receipt.get("input_delivery") != envelope["input"]
        or receipt.get("selected_model") != candidate.model
        or receipt.get("observed_model")
        not in [candidate.model, *capability.get("served_model_aliases", [])]
        or not receipt.get("session_id")
        or type(receipt.get("turns")) is not int
        or not 1 <= receipt["turns"] <= limits["max_turns"]
        or type(receipt.get("usage", {}).get("output_tokens")) is not int
        or receipt.get("usage", {}).get("output_tokens", 0) < 1
    ):
        raise CampaignError("Native qualification lacks matching completed lifecycle evidence")
    required = {
        "full_input",
        "scoped_tools" if writer else "read_only_tools" if readonly else "no_tools",
        "window_completion",
        "quiescence",
        "model_attribution",
        "structured_output",
        "turn_bound",
        "output_bound",
    }
    if (
        covered_input != envelope["input"]["token_upper_bound"]
        or covered_output != limits["output_tokens"]
        or qualification.get("input_tokens") != covered_input
        or qualification.get("output_tokens") != covered_output
        or qualification.get("observed_output_tokens")
        != min(limits["output_tokens"], receipt["usage"]["output_tokens"])
        or qualification.get("native_output_limit_enforced") is not False
        or capability["input_tokens"] > covered_input
        or capability["output_tokens"] > covered_output
        or capability["max_turns"] > limits["max_turns"]
        or set(qualification.get("checks", [])) != required
        or qualification.get("served_models", []) != receipt.get("observed_models", [])
    ):
        raise CampaignError("Native qualification claims exceed the earned evidence")
