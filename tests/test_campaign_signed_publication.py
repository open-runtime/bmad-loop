"""Real SSH signatures survive the scoped writer's private Git promotion."""

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git

from bmad_loop.campaign_native_git import publish
from bmad_loop.campaign_scoped_tools import ScopedTools


@pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS scoped writer")
def test_scoped_ssh_signed_commit_preserves_exact_signature_on_publication(
    project, tmp_path, monkeypatch
):
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        pytest.skip("SSH signing executable unavailable")
    # Never use an owner's signer, agent or key. The canonical repository is the
    # ordinary isolated fixture; only this test's private configuration changes.
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    signing = tmp_path / "ephemeral-signing"
    signing.mkdir()
    key = signing / "signer"
    generated = subprocess.run(
        [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C", "bmad-test", "-f", str(key)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert generated.returncode == 0, generated.stderr
    allowed = signing / "allowed_signers"
    identity = "bmad-signing-fixture@example.invalid"
    allowed.write_text(identity + ' namespaces="git" ' + key.with_suffix(".pub").read_text())
    root = project.project
    for name, value in {
        "commit.gpgsign": "true",
        "gpg.format": "ssh",
        "gpg.ssh.program": ssh_keygen,
        "gpg.ssh.allowedSignersFile": str(allowed),
        "user.signingKey": str(key),
        "user.email": identity,
    }.items():
        git(root, "config", "--local", name, value)
    workspace = tmp_path / "signed-worker"
    branch = "bmad-signed-native"
    git(root, "worktree", "add", "-b", branch, str(workspace))
    baseline = git(workspace, "rev-parse", "HEAD")
    (workspace / "src").mkdir()
    directory = tmp_path / "signed-job"
    directory.mkdir()
    grants = directory / "grants.json"
    grants.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace": str(workspace),
                "scopes": ["src"],
                "config_digest": "signed-commit-fixture",
                "git_branch": branch,
                "protected_branches": ["main"],
            }
        )
    )
    tool = ScopedTools(grants, directory / "calls")
    result = tool.execute(
        "signed-commit",
        {
            "command": [
                str(Path(sys.executable).resolve()),
                "-c",
                """
import base64,json,subprocess
from pathlib import Path
def git(*args):
    result=subprocess.run(['git',*args],capture_output=True,check=True)
    return result.stdout
assert git('config','--bool','commit.gpgsign').strip()==b'true'
Path('src/signed.txt').write_text('signed owned contribution\\n')
git('add','src/signed.txt')
git('commit','-qm','Preserve a real scoped signature')
head=git('rev-parse','HEAD').decode().strip()
raw=git('cat-file','commit',head)
assert b'gpgsig -----BEGIN SSH SIGNATURE-----' in raw
git('verify-commit',head)
assert git('show','--no-patch','--format=%G?',head).strip()==b'G'
print(json.dumps({'head':head,'commit':base64.b64encode(raw).decode()}))
""",
            ]
        },
    )
    assert result["success"], result
    output = result["contentItems"][0]["text"]
    native = next(json.loads(line) for line in output.splitlines() if line.startswith('{"head":'))
    head = native["head"]
    signed_commit = base64.b64decode(native["commit"], validate=True)
    # The canonical ref still exposes only the original graph until the actual
    # controller's seal-and-CAS publication path runs.
    assert git(workspace, "rev-parse", "HEAD") == baseline
    assert publish(workspace, baseline, directory=directory, final=True) == head
    canonical = subprocess.run(
        ["git", "-C", str(workspace), "cat-file", "commit", head],
        capture_output=True,
        check=True,
    ).stdout
    assert canonical == signed_commit
    verified = subprocess.run(
        ["git", "-C", str(workspace), "verify-commit", head],
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    assert "Good" in verified.stderr and identity in verified.stderr
    assert git(workspace, "show", "--no-patch", "--format=%G?", head) == "G"
    assert git(root, "rev-parse", "main") == baseline
    assert git(workspace, "status", "--porcelain") == ""
