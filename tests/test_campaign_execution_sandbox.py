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


@pytest.mark.parametrize("target", ["git", "git-parent", "product", "lifecycle"])
def test_explicit_runtime_grants_cannot_expose_controller_authority(grants, target):
    value, path = grants
    workspace = Path(value["workspace"])
    targets = {
        "git": workspace / ".git",
        "git-parent": workspace.parent,
        "product": workspace / "src",
        "lifecycle": path.parent,
    }
    with pytest.raises(CampaignError, match="runtime grant overlaps"):
        write_paths({**value, "runtime_write_paths": [str(targets[target])]}, path.parent)


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
    from bmad_loop.campaign_native_git import publish

    publish(
        workspace,
        git(project.project, "rev-parse", "main").strip(),
        directory=path.parent,
        final=True,
    )
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


@pytest.fixture
def git_writer(grants, project, tmp_path):
    value, path = grants
    workspace = tmp_path / "owned-worker"
    git(project.project, "worktree", "add", "-b", "bmad-test", str(workspace))
    (workspace / "src").mkdir()
    value.update(workspace=str(workspace), git_branch="bmad-test", protected_branches=["main"])
    path.write_text(json.dumps(value))
    return path, workspace


def _native(path, code, *arguments):
    argv, cwd, environment = command(
        path, [str(Path(sys.executable).resolve()), "-c", code, *map(str, arguments)]
    )
    result = subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result, environment


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS execution confinement")
def test_whole_product_scope_keeps_git_and_controller_state_readonly(git_writer):
    path, workspace = git_writer
    value = json.loads(path.read_text())
    value["scopes"] = ["."]
    path.write_text(json.dumps(value))
    before = (workspace / ".git").read_bytes()
    for area in ("runs", "campaigns"):
        directory = workspace / ".bmad-loop" / area
        directory.mkdir(parents=True)
        (directory / "authority.json").write_text("controller")
    _native(
        path,
        """import os,subprocess
from pathlib import Path
Path('owned.txt').write_text('product')
Path('.bmad-loop/campaign-source').mkdir()
Path('.bmad-loop/campaign-source/story.md').write_text('owned story')
Path('.bmad-loop/policy.toml').write_text('explicitly owned config')
for name in ['.git','.bmad-loop/runs/authority.json','.bmad-loop/campaigns/authority.json']:
 p=Path(name)
 for action in [lambda:p.write_text('forged'),lambda:p.unlink(),lambda:p.rename(Path('moved')),
                lambda:os.replace(Path('owned.txt'),p),lambda:os.link(p,Path('alias'))]:
  try:action()
  except PermissionError:pass
  else:raise AssertionError('protected metadata mutation escaped: '+name)
try:Path('.bmad-loop').rename('.bmad-loop-moved')
except PermissionError:pass
else:raise AssertionError('controller ancestor rename escaped')
subprocess.run(['git','add','owned.txt'],check=True)
subprocess.run(['git','commit','-qm','owned whole-product change'],check=True)
""",
    )
    assert (workspace / ".git").read_bytes() == before
    assert (workspace / ".bmad-loop/campaign-source/story.md").read_text() == "owned story"
    for area in ("runs", "campaigns"):
        assert (workspace / ".bmad-loop" / area / "authority.json").read_text() == "controller"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS execution confinement")
def test_native_ref_snapshot_reads_main_and_remote_without_granting_ref_writes(
    git_writer, project, tmp_path
):
    path, workspace = git_writer
    baseline = git(project.project, "rev-parse", "HEAD").strip()
    git(project.project, "update-ref", "refs/remotes/origin/main", baseline)
    code = """import subprocess,sys
def git(*args):return subprocess.check_output(['git',*args],text=True).strip()
assert git('rev-parse','main')==sys.argv[1]
assert git('rev-parse','origin/main')==sys.argv[1]
for ref in ['refs/heads/main','refs/remotes/origin/main']:
 result=subprocess.run(['git','update-ref',ref,'HEAD'],capture_output=True)
 assert result.returncode!=0, 'protected ref mutation allowed'
result=subprocess.run(['git','pack-refs','--all'],capture_output=True)
assert result.returncode!=0, 'immutable packed refs became writable'
"""
    _native(path, code, baseline)
    successor = git(
        project.project, "commit-tree", baseline + "^{tree}", "-p", baseline, "-m", "new main"
    ).strip()
    git(project.project, "update-ref", "refs/heads/main", successor)
    git(project.project, "update-ref", "refs/remotes/origin/main", successor)
    _native(path, code, baseline)  # Existing session keeps its frozen ref view.
    next_directory = tmp_path / "next-ref-job"
    next_directory.mkdir()
    next_grants = next_directory / "grants.json"
    next_grants.write_text(path.read_text())
    _native(next_grants, code, successor)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS execution confinement")
@pytest.mark.parametrize("packed", [False, True])
def test_canonical_objects_cannot_be_deleted_overwritten_or_hardlinked(git_writer, project, packed):
    path, workspace = git_writer
    common = project.project / ".git"
    head = git(project.project, "rev-parse", "main").strip()
    if packed:
        git(project.project, "repack", "-ad")
        git(project.project, "prune-packed")
        protected = next((common / "objects/pack").glob("*.pack"))
    else:
        protected = common / "objects" / head[:2] / head[2:]
    before = protected.read_bytes()
    code = """import os,sys
from pathlib import Path
protected=Path(sys.argv[1]); shadow=Path(sys.argv[2]); alias=Path('src/alias')
for action in [lambda: protected.unlink(), lambda: protected.write_bytes(b'corrupt'), lambda: protected.chmod(0o666), lambda: os.link(protected,alias)]:
 try: action()
 except PermissionError: pass
 else: raise AssertionError('existing object escaped confinement')
try:
 shadow.parent.mkdir(parents=True,exist_ok=True)
 shadow.write_bytes(b'fake loose copy of an existing packed object')
except PermissionError: pass
else: raise AssertionError('canonical object shadow escaped confinement')
"""
    _native(path, code, protected, common / "objects" / head[:2] / head[2:])
    assert protected.read_bytes() == before
    assert git(project.project, "cat-file", "-t", head).strip() == "commit"
    assert not (workspace / "src/alias").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS execution confinement")
def test_new_objects_are_visible_durable_and_cannot_be_reopened(git_writer, project, tmp_path):
    path, workspace = git_writer
    code = """import os,subprocess
from pathlib import Path
Path('src/owned.txt').write_text('accepted')
subprocess.run(['git','add','src/owned.txt'],check=True)
subprocess.run(['git','commit','-qm','accepted'],check=True)
head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
obj=Path(os.environ['GIT_OBJECT_DIRECTORY'])/head[:2]/head[2:]
for action in [lambda: obj.write_bytes(b'corrupt'),lambda: obj.unlink(),lambda: os.link(obj,Path('src/alias'))]:
 try: action()
 except PermissionError: pass
 else: raise AssertionError('existing bank object was mutable')
"""
    _, environment = _native(path, code)
    head = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], env=environment, text=True
    ).strip()
    bank = Path(environment["GIT_OBJECT_DIRECTORY"])
    object_path = bank / head[:2] / head[2:]
    assert object_path.is_file()
    from bmad_loop.campaign_native_git import publish

    assert (
        publish(
            workspace,
            git(project.project, "rev-parse", "main").strip(),
            directory=path.parent,
            final=True,
        )
        == head
    )
    assert git(project.project, "show", head + ":src/owned.txt") == "accepted"

    # A later launch can read the older bank through canonical alternates, but
    # cannot acquire writes to it. Removing a worktree must not remove its bank.
    next_directory = tmp_path / "next-job"
    next_directory.mkdir()
    next_grants = next_directory / "grants.json"
    next_grants.write_text(path.read_text())
    _, successor_env = _native(
        next_grants,
        """import sys
from pathlib import Path
path=Path(sys.argv[1])
try: path.unlink()
except PermissionError: pass
else: raise AssertionError('successor deleted earlier bank object')
""",
        object_path,
    )
    assert successor_env["GIT_OBJECT_DIRECTORY"] != str(bank)
    git(project.project, "worktree", "remove", str(workspace))
    assert object_path.is_file()
    assert git(project.project, "show", head + ":src/owned.txt") == "accepted"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS execution confinement")
def test_bank_cannot_shadow_a_canonical_packed_hash(git_writer, project):
    path, _ = git_writer
    git(project.project, "repack", "-ad")
    git(project.project, "prune-packed")
    head = git(project.project, "rev-parse", "main").strip()
    blob = git(project.project, "rev-parse", head + ":src.txt").strip()
    expected = git(project.project, "cat-file", "blob", blob)
    _native(
        path,
        """import os,sys,zlib
from pathlib import Path
head=sys.argv[1]; target=Path(os.environ['GIT_OBJECT_DIRECTORY'])/head[:2]/head[2:]
target.parent.mkdir(parents=True,exist_ok=True)
target.write_bytes(zlib.compress(b'blob 5\\0fake\\n'))
""",
        blob,
    )
    assert git(project.project, "cat-file", "blob", blob) == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS execution confinement")
def test_legacy_global_bank_exposure_refuses_resume_without_changing_user_alternates(
    git_writer, project
):
    path, _ = git_writer
    common = project.project / ".git"
    bank = common / "bmad-loop-object-banks" / ("1" * 64)
    bank.mkdir(parents=True)
    alternates = common / "objects/info/alternates"
    original = "unrelated-user-alternate\n" + str(bank) + "\n"
    alternates.write_text(original)
    with pytest.raises(CampaignError, match="globally visible"):
        command(path, [str(Path(sys.executable).resolve())], platform="darwin")
    assert alternates.read_text() == original
