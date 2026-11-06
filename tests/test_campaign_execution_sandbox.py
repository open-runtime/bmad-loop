"""Zero-token OS confinement; native coding programs are ordinary Python fixtures."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git

from bmad_loop.campaign_execution_sandbox import command, write_paths
from bmad_loop.campaign_model import CampaignError


@pytest.fixture
def grants(project, tmp_path):
    source = project.project / "src"
    source.mkdir()
    directory = tmp_path / "owned-job"
    directory.mkdir()
    value = {
        "schema_version": 1,
        "workspace": str(project.project),
        "scopes": ["src"],
        "config_digest": "frozen-config",
    }
    path = directory / "grants.json"
    path.write_text(json.dumps(value))
    return value, path


def test_native_and_child_writes_are_confined_but_read_only_context_is_readable(grants, tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS execution backend qualification")
    value, path = grants
    outside = tmp_path / "outside.txt"
    outside.write_text("retained history")
    script = """import json,subprocess,sys
from pathlib import Path
owned=Path(sys.argv[1]); outside=Path(sys.argv[2])
owned.write_text('native work')
assert outside.read_text()=='retained history'
try: outside.write_text('unsafe')
except PermissionError: pass
else: raise AssertionError('native escaped write boundary')
child=subprocess.run([sys.executable,'-c',"from pathlib import Path; Path(__import__('sys').argv[1]).write_text('child escape')",str(outside)],capture_output=True)
assert child.returncode!=0 and b'PermissionError' in child.stderr, child
print('confined')
"""
    argv, workspace, environment = command(
        path,
        [
            str(Path(sys.executable).resolve()),
            "-c",
            script,
            str(Path(value["workspace"]) / "src" / "owned.txt"),
            str(outside),
        ],
    )
    result = subprocess.run(argv, cwd=workspace, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "confined"
    assert outside.read_text() == "retained history"


def test_redirected_product_grant_and_read_only_overlap_refused(grants, tmp_path):
    value, path = grants
    root = Path(value["workspace"])
    (root / "redirect").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CampaignError, match="escapes|redirected"):
        write_paths({**value, "scopes": ["redirect/file.txt"]}, path.parent)
    with pytest.raises(CampaignError, match="overlaps"):
        write_paths({**value, "read_only_paths": [str(root / "src")]}, path.parent)


def test_unqualified_platform_cannot_fall_back_to_unrestricted_execution(grants):
    _, path = grants
    with pytest.raises(CampaignError, match="not qualified"):
        command(path, [str(Path(sys.executable).resolve())], platform="win32")


def test_global_native_home_is_not_an_implicit_write_grant(grants):
    value, path = grants
    _, allowed, _ = write_paths(value, path.parent)
    assert all(not Path.home().is_relative_to(root) for root in allowed)
    with pytest.raises(CampaignError, match="home root"):
        write_paths({**value, "runtime_write_paths": [str(Path.home())]}, path.parent)


def test_isolated_git_commit_succeeds_without_permission_to_change_target_ref(
    grants, project, tmp_path
):
    if sys.platform != "darwin":
        pytest.skip("macOS execution backend qualification")
    value, path = grants
    workspace = tmp_path / "worker"
    git(project.project, "worktree", "add", "-b", "bmad-test", str(workspace))
    (workspace / "src").mkdir()
    value.update(workspace=str(workspace), git_branch="bmad-test", protected_branches=["main"])
    path.write_text(json.dumps(value))
    source = """import subprocess,sys
from pathlib import Path
Path('src/owned.txt').write_text('accepted')
subprocess.run(['git','add','src/owned.txt'],check=True)
subprocess.run(['git','commit','-qm','accepted owned work'],check=True)
result=subprocess.run(['git','update-ref','refs/heads/main','HEAD'],capture_output=True)
assert result.returncode!=0, 'sandbox allowed target ref mutation'
"""
    argv, cwd, environment = command(path, [str(Path(sys.executable).resolve()), "-c", source])
    result = subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert git(workspace, "log", "-1", "--format=%s").strip() == "accepted owned work"
    assert git(workspace, "rev-parse", "HEAD") != git(project.project, "rev-parse", "main")


def test_future_file_scope_can_be_created_without_granting_siblings(grants):
    if sys.platform != "darwin":
        pytest.skip("macOS execution backend qualification")
    value, path = grants
    value["scopes"] = ["new/folder/owned.txt"]
    path.write_text(json.dumps(value))
    script = """from pathlib import Path
Path('new/folder/owned.txt').write_text('owned')
try: Path('new/folder/unowned.txt').write_text('escape')
except PermissionError: pass
else: raise AssertionError('sibling write escaped')
"""
    argv, cwd, environment = command(path, [str(Path(sys.executable).resolve()), "-c", script])
    result = subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
