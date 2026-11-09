"""Zero-token scoped tool lifecycle, bounds, and actual macOS denial proofs."""

import json
import sys
from pathlib import Path

import pytest
from conftest import git, read_pid, wait_pid_gone

from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_scoped_tools import ScopedTools


@pytest.fixture
def scoped(project, tmp_path):
    workspace = project.project
    (workspace / "src").mkdir()
    directory = tmp_path / "scoped-tool"
    directory.mkdir()
    grants = directory / "grants.json"
    grants.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace": str(workspace),
                "scopes": ["src"],
                "config_digest": "frozen",
            }
        )
    )
    return workspace, grants, directory / "calls"


def run(tool, code, call_id="one", *args):
    return tool.execute(
        call_id, {"command": [str(Path(sys.executable).resolve()), "-c", code, *args]}
    )


def receipt(directory):
    return next(
        json.loads(p.read_text())["receipt"]
        for p in directory.glob("*.json")
        if p.name != "budget.json"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS confinement")
def test_scoped_writer_and_foreground_child_cannot_escape_filesystem_grants(scoped, tmp_path):
    workspace, grants, directory = scoped
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    result = run(
        ScopedTools(grants, directory),
        r"""import os,sys,subprocess
from pathlib import Path
Path('src/owned.txt').write_text('accepted')
assert Path(sys.argv[1]).read_text()=='original'
for operation in [lambda:Path(sys.argv[1]).write_text('escaped'),lambda:Path('unowned.txt').write_text('escaped')]:
 try:operation()
 except PermissionError:pass
 else:raise AssertionError('unconfined')
child=subprocess.run([sys.executable,'-c',"from pathlib import Path; Path('src/child.txt').write_text('child')"])
assert child.returncode==0
print('confined')
""",
        "one",
        str(outside),
    )
    assert result["success"], result
    assert "confined" in result["contentItems"][0]["text"]
    assert (workspace / "src/owned.txt").read_text() == "accepted"
    assert outside.read_text() == "original"
    assert receipt(directory)["processes_quiescent"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS confinement")
def test_duplicates_return_cached_result_and_restart_keeps_budget(scoped):
    workspace, grants, directory = scoped
    tool = ScopedTools(grants, directory, max_calls=1)
    code = "from pathlib import Path; p=Path('src/counter'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    first = run(tool, code)
    assert first["success"], first
    assert run(ScopedTools(grants, directory, max_calls=1), code) == first
    assert (workspace / "src/counter").read_text() == "x"
    with pytest.raises(CampaignError, match="changed arguments"):
        run(tool, "print('different')")
    assert not run(tool, code, "two")["success"]
    with pytest.raises(CampaignError, match="budget changed"):
        ScopedTools(grants, directory, max_calls=2)


@pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS confinement")
@pytest.mark.parametrize("mode", ["timeout", "output", "background", "double-fork", "detached"])
def test_command_bounds_and_background_child_are_settled(scoped, mode, reap_leftovers):
    workspace, grants, directory = scoped
    background = mode in {"background", "double-fork", "detached"}
    # Foreground exits/detachment drive these cases; 30 seconds is only a hang
    # guard. A fixed two-second write raced teardown on loaded process tables.
    tool = ScopedTools(grants, directory, timeout_s=30 if background else 1, max_output_bytes=512)
    child = (
        "import os,time; from pathlib import Path; "
        "Path('src/background.pid').write_text(str(os.getpid())); "
        "exec(\"while not Path('src/released-after-return').exists(): time.sleep(0.01)\"); "
        "Path('src/late.txt').write_text('late')"
    )
    wait_ready = "exec(\"while not Path('src/background.pid').exists(): time.sleep(0.01)\")"
    code = {
        "timeout": "import time; time.sleep(30)",
        "output": "print('x'*100000)",
        "background": (
            "import subprocess,sys,time; from pathlib import Path; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            + wait_ready
        ),
        "double-fork": (
            "import os,time; from pathlib import Path; p=os.fork(); "
            f"exec({wait_ready!r}) if p else None; os._exit(0) if p else None; "
            "p=os.fork(); os._exit(0) if p else None; " + child
        ),
        "detached": (
            "import subprocess,sys,time; from pathlib import Path; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
            + wait_ready
            + "; time.sleep(60)"
        ),
    }[mode]
    result = run(tool, code)
    job = read_pid(workspace / "src/background.pid")
    if job is not None:
        reap_leftovers.append(job)
    assert not result["success"], result
    record = receipt(directory)
    assert record["processes_quiescent"] is True
    assert record["quiescence_scope"] == "owned-session-and-observed-descendants"
    assert record["reason"]
    if background:
        assert job is not None, "background writer never reached its readiness barrier"
        identities = {row["pid"]: row["identity"] for row in record["processes"]}
        assert identities.get(job) is not None, "writer was not birth-stamped in teardown"
        assert wait_pid_gone(job), "writer survived a quiescent scoped-tool return"
        assert "timeout" not in record["reason"], "foreground lifecycle barrier never completed"
        # If the owned process survived, it can only write after this marker.
        (workspace / "src/released-after-return").touch()
    assert not (workspace / "src/late.txt").exists()
    assert len(result["contentItems"][0]["text"].encode()) < 1024


def test_immutable_grants_and_pending_call_cannot_launch(scoped, monkeypatch):
    _, grants, directory = scoped
    tool = ScopedTools(grants, directory)
    called = []
    monkeypatch.setattr(tool, "_run", lambda *args: called.append(args))
    grants.write_text(grants.read_text().replace('"frozen"', '"changed"'))
    with pytest.raises(CampaignError, match="grants changed"):
        run(tool, "print(1)")
    assert not called


def test_uncertain_prior_call_is_not_replayed(scoped, monkeypatch):
    from bmad_loop.campaign_jobs import digest

    _, grants, directory = scoped
    tool = ScopedTools(grants, directory)
    arguments = {"command": ["x"]}
    (directory / (digest("one") + ".json")).write_text(
        json.dumps({"call_id": "one", "arguments": arguments, "state": "submitted"})
    )
    monkeypatch.setattr(tool, "_run", lambda *a: pytest.fail("uncertain call replayed"))
    assert "not replayed" in tool.execute("one", arguments)["contentItems"][0]["text"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": "echo x"},
        {"command": ["x"] * 257},
        {"command": ["x", "a" * 32768]},
        {"command": ["x"], "timeout": 10000},
    ],
)
def test_malformed_argv_cannot_launch(scoped, arguments):
    _, grants, directory = scoped
    with pytest.raises(CampaignError, match="bounded argv"):
        ScopedTools(grants, directory).execute("one", arguments)


@pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS confinement")
def test_git_writer_keeps_history_and_target_objects_readonly(scoped, project, tmp_path):
    _, grants, directory = scoped
    workspace = tmp_path / "writer"
    git(project.project, "worktree", "add", "-b", "scoped-writer", str(workspace))
    (workspace / "src").mkdir()
    raw = json.loads(grants.read_text())
    raw.update(workspace=str(workspace), git_branch="scoped-writer", protected_branches=["main"])
    grants.write_text(json.dumps(raw))
    original = git(project.project, "rev-parse", "HEAD").strip()
    result = run(
        ScopedTools(grants, directory),
        """import subprocess,sys
from pathlib import Path
Path('src/work.txt').write_text('work')
subprocess.run(['git','add','src/work.txt'],check=True)
subprocess.run(['git','commit','-qm','owned'],check=True)
assert subprocess.run(['git','update-ref','refs/heads/main','HEAD'],capture_output=True).returncode!=0
common=Path(sys.argv[1])
objects=[p for p in (common/'objects').glob('*/*') if p.is_file()]
assert objects
for operation in [lambda:objects[0].unlink(),lambda:objects[0].write_bytes(b'bad'),lambda:Path('src/object-link').hardlink_to(objects[0])]:
 try:operation()
 except PermissionError:pass
 else:raise AssertionError('object damaged')
""",
        "one",
        str(project.project / ".git"),
    )
    assert result["success"], result
    assert git(project.project, "rev-parse", "HEAD").strip() == original
