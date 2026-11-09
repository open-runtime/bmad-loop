"""Large stage context is preserved without exceeding process argument bounds."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_stage_prompt import MAX_PACKET_BYTES, instruction, materialize


def large_context():
    from bmad_loop.campaign_discovery import CoverageReceipt

    rows = [
        {
            "id": index + 1,
            "number": index + 1,
            "title": "Selected work" if index == 528 else "Unrelated portfolio work",
            "body": "Preserve exact owner acceptance: café. " if index == 528 else "z" * 29_500,
            "state": "open",
        }
        for index in range(960)
    ]
    inventory = CoverageReceipt(
        1,
        "a/b",
        "/repository/.git",
        100,
        ("complete",),
        True,
        (),
        (),
        tuple(rows[:640]),
        tuple(rows[640:]),
        (),
        (),
        (),
    ).to_dict()
    inventory["digest"] = "exact-inventory"
    return {
        "unit": {
            "id": "s:epic:unit",
            "scopes": ["src/owned.py"],
            "acceptance": ["Never drop accepted scope"],
        },
        "acceptance_ledger": [{"id": "criterion", "text": "Exercise the actual startup path"}],
        "owner_decisions": ["Preserve committed history and finish only after merge"],
        "feedback": [{"required": True, "text": "Address the recorded blocker"}],
        "inventory": inventory,
        "available_evidence_ids": ["ref:" + str(index) + ":" + "a" * 90 for index in range(4000)],
        "external_ownership": [],
    }


def indexed_record(index, position):
    row = index["records"][position]
    with Path(row["path"]).open("rb") as stream:
        stream.seek(row["offset"])
        raw = stream.read(row["size_bytes"])
    assert hashlib.sha256(raw).hexdigest() == row["sha256"]
    return json.loads(raw)


@pytest.mark.parametrize("multiplier", [1, 2])
def test_large_inventory_is_exact_indexed_source_with_bounded_required_context(
    tmp_path, multiplier
):
    from bmad_loop.campaign_stage_prompt import context_artifacts, prepare_context

    context = large_context()
    for key in ("issues", "pulls"):
        context["inventory"][key] *= multiplier
    projected, descriptors = prepare_context(tmp_path, context, issue_numbers=(529,))
    assert len(json.dumps(context).encode()) > 27 * 1024 * 1024
    assert len(json.dumps(projected).encode()) < 64 * 1024
    for key in ("unit", "acceptance_ledger", "owner_decisions", "feedback"):
        assert projected[key] == context[key]
    assert projected["inventory"]["inline_projection"] is True
    assert projected["inventory"]["full_source_required"] is True
    assert len(descriptors) > 2 and all(
        row["size_bytes"] <= MAX_PACKET_BYTES for row in descriptors
    )
    assert len(context_artifacts(tmp_path, descriptors)) == len(descriptors)
    index = json.loads(Path(projected["retrievable_context"]["index"]["path"]).read_text())
    selected = projected["inventory"]["selected"][0]
    assert selected["number"] == 529
    assert indexed_record(index, selected["source_record"]) == context["inventory"]["issues"][528]
    reconstructed = {}
    for key, layout in index["layout"].items():
        if layout["kind"] == "object":
            reconstructed[key] = {
                **layout["fields"],
                **{name: [None] * count for name, count in layout["collections"].items()},
            }
        elif layout["kind"] == "array":
            reconstructed[key] = [None] * layout["count"]
    for position, row in enumerate(index["records"]):
        target = reconstructed[row["key"]]
        if row["collection"] and row["collection"] != "_fields":
            target = target[row["collection"]]
        target[row["position"]] = indexed_record(index, position)
    encoded = json.dumps(
        reconstructed, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == index["source_sha256"]
    assert len(encoded) == index["source_size_bytes"]
    assert prepare_context(tmp_path, context, issue_numbers=(529,)) == (projected, descriptors)
    data = Path(descriptors[0]["path"])
    original = data.read_bytes()
    data.write_bytes(b"!" + original[1:])
    with pytest.raises(CampaignError, match="changed"):
        context_artifacts(tmp_path, descriptors)


def test_selected_view_reserves_mapped_issue_and_pr_ahead_of_related_fillers(tmp_path):
    from bmad_loop.campaign_stage_prompt import prepare_context

    context = large_context()
    filler = "retained-title-" + "x" * 700
    context["inventory"]["issues"] = [
        {
            "id": index,
            "number": index,
            "title": f"intake {index} {filler}",
            "state": "open",
        }
        for index in range(1, 50)
    ] + [
        {
            "id": 900,
            "number": 900,
            "title": f"mapped unit {filler}",
            "state": "open",
        }
    ]
    context["inventory"]["pulls"] = [
        {
            "id": 1000 + index,
            "number": 1000 + index,
            "title": f"path overlap {index} {filler}",
            "state": "open",
            "files": [{"filename": "src/owned.py"}],
        }
        for index in range(1, 50)
    ] + [
        {
            "id": 42,
            "number": 42,
            "title": f"mapped pr {filler}",
            "state": "open",
            "files": [{"filename": "src/owned.py"}],
        }
    ]
    # Mapped identities lead the number lists; older intake/path rows would
    # otherwise consume the 16 KiB selected window first in inventory order.
    view, _ = prepare_context(
        tmp_path,
        context,
        issue_numbers=(900, *range(1, 50)),
        pr_numbers=(42,),
    )
    selected_numbers = {(row["collection"], row["number"]) for row in view["inventory"]["selected"]}
    assert ("issues", 900) in selected_numbers
    assert ("pulls", 42) in selected_numbers
    assert view["inventory"]["selected_records_omitted"] > 0


@pytest.mark.parametrize("shape", ["files", "changed_files_detail", "discovery_path_coverage"])
def test_large_coverage_metadata_is_indexed_and_renamed_scope_remains_navigable(tmp_path, shape):
    from bmad_loop.campaign_stage_prompt import prepare_context

    context = large_context()
    metadata = {"expanded_issue_roots": ["exact-root-" + "x" * 100] * 4000}
    context["inventory"]["github_coverage"] = metadata
    context["inventory"]["pulls"][0][shape] = (
        {"changed_paths": ["other/moved.py", "src/owned.py"], "complete": True}
        if shape == "discovery_path_coverage"
        else [{"filename": "other/moved.py", "previous_filename": "src/owned.py"}]
    )
    view, _ = prepare_context(tmp_path, context)
    assert "github_coverage" not in view["inventory"]
    assert "github_coverage" in view["inventory"]["source_metadata_fields"]
    index = json.loads(Path(view["retrievable_context"]["index"]["path"]).read_text())
    position = next(
        position
        for position, row in enumerate(index["records"])
        if row["collection"] == "_fields" and row["position"] == "github_coverage"
    )
    assert indexed_record(index, position) == metadata
    selected = view["inventory"]["selected"]
    assert len(selected) == 1 and selected[0]["number"] == 641
    assert indexed_record(index, selected[0]["source_record"]) == context["inventory"]["pulls"][0]


@pytest.mark.skipif(sys.platform != "darwin", reason="actual supported scoped read-only tools")
def test_scoped_readonly_tool_seeks_selected_record_without_dumping_inventory(project, tmp_path):
    from bmad_loop.campaign_headless import readonly_grants
    from bmad_loop.campaign_scoped_tools import ScopedTools
    from bmad_loop.campaign_stage_prompt import context_artifacts, prepare_context

    directory = tmp_path / "controller"
    projected, descriptors = prepare_context(directory, large_context(), issue_numbers=(529,))
    grants = readonly_grants(
        project.project,
        {"head": "exact"},
        {"permissions": "read-only-tools"},
        read_only_paths=descriptors,
    )
    path = directory / "grants.json"
    path.write_text(json.dumps(grants))
    result = ScopedTools(path, directory / "calls", max_calls=1, max_output_bytes=4096).execute(
        "selected-issue",
        {
            "command": [
                str(Path(sys.executable).resolve()),
                "-B",
                "-c",
                """import hashlib,json,sys
from pathlib import Path
index=json.loads(Path(sys.argv[1]).read_text())
row=next(row for row in index['records'] if row['key']=='inventory' and row['collection']=='issues' and row.get('number')==529)
with Path(row['path']).open('rb') as stream:
 stream.seek(row['offset']); raw=stream.read(row['size_bytes'])
assert hashlib.sha256(raw).hexdigest()==row['sha256']
print(json.dumps(json.loads(raw),ensure_ascii=False))
""",
                projected["retrievable_context"]["index"]["path"],
            ]
        },
    )
    assert result["success"], result
    text = result["contentItems"][0]["text"]
    assert "Preserve exact owner acceptance: café." in text and len(text.encode()) < 4096
    context_artifacts(directory, descriptors)


@pytest.mark.parametrize("permissions,grant", [("no-tools", True), ("read-only-tools", False)])
def test_external_context_requires_tool_permission_and_exact_grant(
    project, tmp_path, monkeypatch, permissions, grant
):
    from test_campaign_workers import envelope, worker_request

    from bmad_loop import campaign_workers as workers
    from bmad_loop.campaign_stage_prompt import prepare_context

    context = envelope(project.project, tmp_path / "execution")
    projected, descriptors = prepare_context(context.directory, large_context())
    launched = []

    class Adapter:
        requires_inline_context = True
        capability = {"permissions": permissions}
        grants = {"read_only_paths": descriptors if grant else []}

        def run(self, spec):
            launched.append(spec)
            raise AssertionError("unqualified retrieval launched")

    monkeypatch.setattr(workers, "_adapters", lambda *args, **kwargs: {"triage": Adapter()})
    request = worker_request(
        project.project,
        context,
        kind="stage",
        role="triage",
        candidate={"profile": "claude"},
        profile_key="triage",
        prompt=json.dumps(projected),
        read_only=True,
        context_artifacts=descriptors,
    )
    with pytest.raises(CampaignError, match="qualified tool route|exact read-only retrieval grant"):
        workers._stage(request, context)
    assert launched == []


def test_stage_result_refuses_context_changed_during_session(project, tmp_path, monkeypatch):
    from test_campaign_workers import envelope, worker_request

    from bmad_loop import campaign_workers as workers
    from bmad_loop.adapters.base import SessionResult
    from bmad_loop.campaign_stage_prompt import prepare_context

    context = envelope(project.project, tmp_path / "execution")
    projected, descriptors = prepare_context(context.directory, large_context())
    calls = []

    class Adapter:
        requires_inline_context = True
        capability = {"permissions": "read-only-tools"}
        grants = {"read_only_paths": descriptors}

        def run(self, spec):
            calls.append(spec)
            path = Path(descriptors[0]["path"])
            original = path.read_bytes()
            path.write_bytes(b"!" + original[1:])
            return SessionResult(status="completed", result_json={"status": "done"})

        def read_usage(self, result):
            return None

    monkeypatch.setattr(workers, "_adapters", lambda *args, **kwargs: {"triage": Adapter()})
    request = worker_request(
        project.project,
        context,
        kind="stage",
        role="triage",
        candidate={"profile": "claude"},
        profile_key="triage",
        prompt=json.dumps(projected),
        read_only=True,
        context_artifacts=descriptors,
    )
    with pytest.raises(CampaignError, match="changed during the session"):
        workers._stage(request, context)
    assert len(calls) == 1


def test_required_owner_obligations_are_not_shortened_to_make_inventory_fit(tmp_path):
    from bmad_loop.campaign_stage_prompt import prepare_context

    context = large_context()
    context["owner_decisions"] = ["exact owner instruction " * 4000]
    with pytest.raises(CampaignError, match="required campaign obligations exceed"):
        prepare_context(tmp_path, context)
    assert not list(tmp_path.iterdir())


def test_custom_json_prompt_keeps_original_contract_and_retrieval_context():
    from bmad_loop.campaign_stage_prompt import attach_context

    original = {"owner": "Preserve every criterion", "result_contract": {"kind": "decision-v1"}}
    context = {"retrievable_context": {"index": {"path": "/controller/index.json"}}}
    prompt = "Follow this decision contract.\n\n" + json.dumps(original)
    prefix, _, body = attach_context(prompt, context).partition("\n\n")
    assert prefix == "Follow this decision contract."
    assert json.loads(body) == {**original, "campaign_context": context}


def test_inventory_larger_than_cli_argv_is_readable_as_exact_packet(tmp_path):
    context = "Requirements and source evidence:\n" + "a" * (2 * 1024 * 1024)
    packet = materialize(tmp_path, context)
    assert packet.path.read_text() == context
    assert packet.size_bytes == len(context.encode())
    assert len(instruction(packet).encode()) < 2048
    assert str(packet.path) in instruction(packet) and packet.sha256 in instruction(packet)
    packet.verify()


def test_reentry_reuses_exact_packet_and_refuses_session_rewrite(tmp_path):
    packet = materialize(tmp_path, "original instruction")
    assert materialize(tmp_path, "original instruction").sha256 == packet.sha256
    packet.path.write_text("original instructioX")
    with pytest.raises(CampaignError, match="changed"):
        packet.verify()
    with pytest.raises(CampaignError, match="changed"):
        materialize(tmp_path, "original instruction")


def test_packet_redirect_and_unbounded_inputs_are_refused(tmp_path):
    other = tmp_path / "other"
    other.write_text("original instruction")
    try:
        (tmp_path / "stage-packet.md").symlink_to(other)
    except OSError:
        pytest.skip("filesystem cannot create symbolic links")
    with pytest.raises(CampaignError, match="safely"):
        materialize(tmp_path, "original instruction")
    with pytest.raises(CampaignError, match="byte bound"):
        materialize(tmp_path, "x" * (MAX_PACKET_BYTES + 1))


def test_stage_worker_delivers_large_packet_through_short_adapter_prompt(
    project, tmp_path, monkeypatch
):
    from test_campaign_workers import envelope, worker_request

    from bmad_loop import campaign_workers as workers
    from bmad_loop.adapters.base import SessionResult

    context = envelope(project.project, tmp_path / "execution")
    context.directory.mkdir()
    calls = []

    class Adapter:
        def run(self, spec):
            calls.append(spec)
            assert len(spec.prompt.encode()) < 2048
            assert str(context.directory / "stage-packet.md") in spec.prompt
            assert "complete inventory" in (context.directory / "stage-packet.md").read_text()
            return SessionResult(
                status="completed",
                result_json={"status": "done"},
                session_id="scripted",
                transcript_path="",
            )

        def read_usage(self, result):
            return None

    monkeypatch.setattr(workers, "_adapters", lambda *args, **kwargs: {"triage": Adapter()})
    request = worker_request(
        project.project,
        context,
        kind="stage",
        role="triage",
        candidate={"profile": "claude"},
        profile_key="triage",
        prompt="complete inventory\n" + "x" * (2 * 1024 * 1024),
        read_only=True,
    )
    result = workers._stage(request, context)
    assert result["outcome"] == "stage_complete" and len(calls) == 1
    assert result["input_packet"]["size_bytes"] > 2 * 1024 * 1024
