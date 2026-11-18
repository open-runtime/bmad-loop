"""Bind a finalizer's own commit independently of a concurrently moving HEAD."""

import os
import subprocess

import pytest
from conftest import git

from bmad_loop import finalization_recovery, verify


@pytest.mark.parametrize(
    "race", ["none", "changed-descendant", "empty-descendant", "same-tree-sibling"]
)
def test_finalization_never_adopts_a_foreign_ref_update(project, tmp_path, monkeypatch, race):
    repo = project.project
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "owned-product.txt").write_text("accepted owned product\n")
    index = verify._bound_real_index_path(repo)
    real = verify._run_git
    actual = {}

    def foreign_git(*args, payload=None):
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=payload,
            capture_output=True,
            check=True,
            timeout=10,
            env={**os.environ, "GIT_INDEX_FILE": str(tmp_path / "foreign-index")},
        )
        return result.stdout.decode().strip()

    def concurrent_update(cmd, root, **kwargs):
        result = real(cmd, root, **kwargs)
        if cmd[3] == "commit" and result.returncode == 0:
            assert not actual, "finalizer repeated a commit"
            own = git(repo, "rev-parse", "HEAD")
            tree = git(repo, "rev-parse", own + "^{tree}")
            actual.update(own=own, own_tree=tree)
            before = index.read_bytes()
            if race != "none":
                if race == "changed-descendant":
                    foreign_git("read-tree", own)
                    blob = foreign_git("hash-object", "-w", "--stdin", payload=b"foreign work\n")
                    foreign_git(
                        "update-index", "--add", "--cacheinfo", f"100644,{blob},foreign-only.txt"
                    )
                    tree = foreign_git("write-tree")
                parent = baseline if race == "same-tree-sibling" else own
                foreign = foreign_git(
                    "commit-tree", tree, "-p", parent, "-m", "independent ref writer"
                )
                branch = git(repo, "symbolic-ref", "HEAD")
                foreign_git("update-ref", "--no-deref", branch, foreign, own)
                actual["foreign"] = foreign
            assert index.read_bytes() == before  # The peer only changed the ref.
        return result

    monkeypatch.setattr(verify, "_run_git", concurrent_update)
    if race == "none":
        result = verify.finalize_commit(repo, baseline, "owned finalization", append_only=True)
        assert result == actual["own"]
        assert finalization_recovery.pending(repo) is None
        assert git(repo, "write-tree") == actual["own_tree"]
    else:
        with pytest.raises(verify.GitFinalizationUncertain):
            verify.finalize_commit(repo, baseline, "owned finalization", append_only=True)
        assert git(repo, "rev-parse", "HEAD") == actual["foreign"]
        record = finalization_recovery.pending(repo)
        assert record is not None and record["state"] == "uncertain"
        evidence = record["commit_evidence"]
        assert evidence["head"] == actual["own"] != actual["foreign"]
        assert git(repo, "rev-parse", evidence["recovery_ref"]) == actual["own"]
