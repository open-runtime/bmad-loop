"""Admission may reuse stable files; real launches always read exact content."""

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path

import pytest
from test_campaign_headless import finish
from test_campaign_headless import prepared as prepared

from bmad_loop import campaign_admission_files as files
from bmad_loop.adapters.profile import get_profile
from bmad_loop.campaign_headless import admission_validation_pass
from bmad_loop.campaign_model import CampaignError
from bmad_loop.campaign_route_readiness import inspect


@pytest.fixture(autouse=True)
def clear_admission_cache():
    with files._LOCK:
        files._CACHE.clear()
    yield
    with files._LOCK:
        files._CACHE.clear()


def count_reads(monkeypatch):
    observed = []
    original = files._read

    def read(path, **kwargs):
        observed.append(Path(path))
        return original(path, **kwargs)

    monkeypatch.setattr(files, "_read", read)
    return observed


def test_admission_reuses_stable_digest_but_default_and_threads_read_fresh(tmp_path, monkeypatch):
    path = tmp_path / "binary"
    path.write_bytes(b"native")
    reads = count_reads(monkeypatch)
    expected = hashlib.sha256(b"native").hexdigest()
    with files.admission_reads():
        assert files.file_digest(path) == expected
        assert files.file_digest(path) == expected
        # ContextVar state does not leak to independent launch threads.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(files.file_digest, path).result() == expected
    assert reads == [path, path]
    with files.admission_reads():
        assert files.file_digest(path) == expected
    assert reads == [path, path]
    assert files.file_digest(path) == expected
    assert reads == [path, path, path]


def test_same_size_mutation_with_restored_mtime_invalidates_admission(tmp_path, monkeypatch):
    path = tmp_path / "native"
    path.write_bytes(b"before")
    reads = count_reads(monkeypatch)
    with files.admission_reads():
        before = files.file_digest(path)
        original = path.stat()
        path.write_bytes(b"after!")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        after = files.file_digest(path)
    assert after != before
    assert after == hashlib.sha256(b"after!").hexdigest()
    assert reads == [path, path]


def test_permission_changes_revalidate_and_missing_files_refuse_cached_proof(tmp_path, monkeypatch):
    path = tmp_path / "native"
    path.write_bytes(b"contents")
    reads = count_reads(monkeypatch)
    with files.admission_reads():
        expected = files.file_digest(path)
        path.chmod(0o700)
        assert files.file_digest(path) == expected
        assert reads == [path, path]
        path.unlink()
        with pytest.raises(CampaignError, match="unavailable"):
            files.file_digest(path)


def test_redirect_checks_are_live_on_cache_hits(tmp_path, monkeypatch):
    target = tmp_path / "original"
    target.write_bytes(b"original")
    sibling = tmp_path / "replacement"
    sibling.write_bytes(b"replaced")
    reads = count_reads(monkeypatch)
    with files.admission_reads():
        files.file_digest(target)
        target.unlink()
        try:
            target.symlink_to(sibling)
        except OSError:
            pytest.skip("symlink creation unavailable")
        with pytest.raises(CampaignError, match="redirected"):
            files.file_digest(target)
    assert reads == [target]


def test_parent_redirection_rechecks_resolved_path_even_same_inode(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "native").write_bytes(b"same inode")
    os.link(first / "native", second / "native")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(first, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation unavailable")
    path = alias / "native"
    reads = count_reads(monkeypatch)
    with files.admission_reads():
        expected = files.file_digest(path)
        alias.unlink()
        alias.symlink_to(second, target_is_directory=True)
        assert files.file_digest(path) == expected
    assert reads == [path, path]


def test_bounded_json_cache_returns_copies_and_honors_each_read_limit(tmp_path, monkeypatch):
    path = tmp_path / "proof.json"
    data = {"nested": {"checks": ["passed"]}}
    path.write_text(json.dumps(data))
    reads = count_reads(monkeypatch)
    with files.admission_reads():
        first = files.read_json(path, limit=1000)
        first["nested"]["checks"].append("forged")
        assert files.read_json(path, limit=1000) == data
        assert reads == [path]
        with pytest.raises(CampaignError, match="oversized"):
            files.read_json(path, limit=2)


def test_cache_size_and_per_record_limits_bound_retained_json(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MAX_ENTRIES", 2)
    monkeypatch.setattr(files, "MAX_JSON_CACHE_BYTES", 24)
    monkeypatch.setattr(files, "MAX_JSON_ENTRY_BYTES", 16)
    with files._LOCK:
        files._CACHE.clear()
    small = [tmp_path / f"{index}.json" for index in range(3)]
    for path in small:
        path.write_text('{"ok":true}')
    large = tmp_path / "large.json"
    large.write_text(json.dumps({"larger": "x" * 20}))
    reads = count_reads(monkeypatch)
    with files.admission_reads():
        for path in small:
            files.read_json(path, limit=100)
        assert len(files._CACHE) == 2
        assert sum(row[2] for row in files._CACHE.values()) <= 24
        files.read_json(small[0], limit=100)  # Evicted oldest entry.
        files.read_json(large, limit=100)
        files.read_json(large, limit=100)  # Too large to retain, still valid to read.
    assert reads == [*small, small[0], large, large]


def test_digest_refuses_file_changed_during_read(tmp_path, monkeypatch):
    path = tmp_path / "native"
    path.write_bytes(b"before")
    original = files._identity
    calls = 0

    def identity(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"after!")
        return original(value)

    monkeypatch.setattr(files, "_identity", identity)
    with pytest.raises(CampaignError, match="changed while reading"):
        files.file_digest(path)


@pytest.mark.parametrize("action", ["prepare", "launch"])
def test_native_prepare_and_launch_never_reuse_admission_file_cache(
    prepared, tmp_path, monkeypatch, action
):
    runner, path, request, candidate, cap = prepared
    reads = count_reads(monkeypatch)
    routes = {candidate.profile + ":" + candidate.model: cap}
    profile = get_profile(candidate.profile)
    binary = Path(cap["binary"])
    with files.admission_reads():
        # Simulate separate coordinator passes; stat caching survives, whole
        # validation memoization does not conceal the second admission check.
        for _ in range(2):
            with admission_validation_pass():
                assert inspect(profile, candidate, routes, "reasoning")["ready"]
        assert reads.count(binary) == 1
        if action == "prepare":
            runner.prepare(
                tmp_path / "another-job",
                request,
                candidate,
                cap,
                launch_id="another-launch",
                generation=1,
            )
        else:
            runner.launch(path)
        assert reads.count(binary) == 2
    if action == "launch":
        assert finish(runner, path)["outcome"] == "completed"


def test_cached_admission_cannot_authorize_changed_launch_even_if_metadata_spoofed(
    prepared, monkeypatch
):
    runner, path, _, candidate, cap = prepared
    routes = {candidate.profile + ":" + candidate.model: cap}
    script = Path(cap["dependencies"][0]["path"])
    original_identity = files._identity
    identity = original_identity(script)
    with files.admission_reads():
        assert inspect(get_profile(candidate.profile), candidate, routes, "reasoning")["ready"]
        script.write_text(script.read_text().replace("gpt-test", "gpt-fake"))
        monkeypatch.setattr(
            files,
            "_identity",
            lambda path: identity if path == script else original_identity(path),
        )
        assert inspect(get_profile(candidate.profile), candidate, routes, "reasoning")["ready"]
        with pytest.raises(CampaignError, match="changed"):
            runner.launch(path)
    assert not runner.mux.processes
