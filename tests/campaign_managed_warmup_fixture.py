"""Scripted native headless writer for the normal warmup supervisor fixture."""

import json
from pathlib import Path

from test_campaign_headless import file_digest, fixture_capability


def install(body, tmp_path, prepared, prefix, tail):
    _, _, _, _, cap = prepared
    script = tmp_path / "managed-writer.py"
    script.write_text("""import json,sys,time
from pathlib import Path
markers=Path(sys.argv[1]); markers.mkdir(exist_ok=True)
sys.stdin.buffer.read()
print(json.dumps({"type":"system","model":"gpt-test","session_id":"managed-warmup"}),flush=True)
print(json.dumps({"type":"assistant","text":sys.argv[2]}),flush=True)
(markers/"native-prefix-ready").touch()
deadline=time.monotonic()+120
while not (markers/"append-history").exists():
    if time.monotonic()>deadline: raise RuntimeError("managed warmup never applied")
    time.sleep(.01)
print(json.dumps({"type":"assistant","text":sys.argv[3]}),flush=True)
print(json.dumps({"type":"result","is_error":True,"error":{"code":"quota_exceeded"},"usage":{"input_tokens":12,"output_tokens":8}}),flush=True)
""")
    cap = fixture_capability(
        tmp_path,
        {
            **cap,
            "argv": [cap["binary"], str(script), str(tmp_path / "parallel-markers"), prefix, tail],
            "dependencies": [{"path": str(script), "sha256": file_digest(script)}],
        },
    )
    manifest = tmp_path / "managed-writer-capability.json"
    manifest.write_text(json.dumps(cap))
    setup = """
    if request["kind"]=="execution" and request["unit_id"].endswith(":first") and not request.get("handoff_id"):
        from bmad_loop.adapters.headless import HeadlessAdapter
        from test_campaign_headless import ChildMux
        global NATIVE_ADAPTER,NATIVE_HANDLE
        NATIVE_ADAPTER=HeadlessAdapter(run_dir=run_dir,profile=adapter.profile,policy=policy,mux=ChildMux())
        NATIVE_ADAPTER.set_native_job_root(envelope.directory/"native"/run_dir.name)
        NATIVE_ADAPTER.set_capability(json.loads(Path(__MANIFEST__).read_text()),None,request["headless_coordinates"])
        original_start=adapter.start_session
        def start(spec):
            global NATIVE_HANDLE
            original_start(spec)
            NATIVE_HANDLE=NATIVE_ADAPTER.start_session(spec)
            return NATIVE_HANDLE
        adapter.start_session=start
        adapter.live_history_binding=NATIVE_ADAPTER.live_history_binding
""".replace("__MANIFEST__", repr(str(manifest)))
    # Import the maintained fixture's zero-token process transport in the child.
    setup = "    sys.path.insert(0," + repr(str(Path(__file__).parent.resolve())) + ")\n" + setup
    body = body.replace(
        "    from bmad_loop.campaign_reflection_flow import observe_adapter",
        setup + "    from bmad_loop.campaign_reflection_flow import observe_adapter",
    )
    start = body.index('            history=Path(request["execution"]).parent/"live-history.jsonl"')
    end = body.index('        packet_path=Path(request["handoff_packet"]["path"])', start)
    body = body[:start] + """            deadline=time.monotonic()+120
            while not (MARKERS/"native-prefix-ready").exists():
                if time.monotonic()>deadline: raise RuntimeError("managed writer never started")
                threading.Event().wait(.01)
            event("history-ready",request)
            while not (MARKERS/"append-history").exists():
                if time.monotonic()>deadline: raise RuntimeError("live warmup never applied")
                threading.Event().wait(.01)
            result=NATIVE_ADAPTER.wait_for_completion(NATIVE_HANDLE,session)
            event("history-appended",request)
            event("quota",request)
            return result
""" + body[end:]
    return body.replace(
        'assert packet["resolved_constraints"]',
        'assert packet["obligations"] and packet["current_constraint_audits"]',
    )
