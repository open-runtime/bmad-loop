"""Run bounded production-supervisor campaigns continuously against scripted peers.

Use --smoke for one cycle or --qualify for four hours, followed by a bounded
final drain. Results are written in a new directory outside the checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from bmad_loop.campaign_provenance import inspect as installed_provenance

ROOT = Path(__file__).resolve().parents[1]
FOUR_HOURS = 4 * 60 * 60
CYCLE_TIMEOUT = 30 * 60


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, timeout=20).strip()


def source_subject() -> dict:
    """Hash exact Git-reported changes; preserve deletions and symlink identities."""
    paths = set()
    for args in (
        ("diff", "--name-only", "-z", "HEAD"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        names = subprocess.check_output(["git", *args], cwd=ROOT, timeout=20)
        paths.update(os.fsdecode(name) for name in names.split(b"\0") if name)
    files = {}
    for name in sorted(paths):
        path = ROOT / name
        if path.is_symlink():
            files[name] = {"symlink": os.readlink(path)}
        elif path.is_file():
            with path.open("rb") as stream:
                files[name] = {"sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
        else:
            files[name] = {"kind": "directory" if path.is_dir() else "absent"}
    return {"commit": git("rev-parse", "HEAD"), "dirty_paths": files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true")
    mode.add_argument("--qualify", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--stage-history",
        type=Path,
        action="append",
        required=True,
        help="repeat for the original long controller-stage JSONL fixture transcripts",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(ROOT) or output.exists():
        parser.error("choose a new output directory outside the checkout")
    commit = git("rev-parse", "HEAD")
    preimage = git("status", "--porcelain")
    subject = installed_provenance(expected_commit=commit)
    if args.qualify and (preimage or not subject["matches_expected"]):
        parser.error(
            "four-hour qualification requires a clean checkout and matching installed runtime"
        )
    output.mkdir(parents=True, mode=0o700)
    inputs = output / "inputs"
    inputs.mkdir()
    histories = []
    for index, original in enumerate(args.stage_history):
        path = inputs / f"stage-{index}.jsonl"
        with original.open("rb") as source, path.open("xb") as target:
            shutil.copyfileobj(source, target)
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        histories.append({"source": str(original.resolve()), "path": str(path), "sha256": checksum})
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "mode": "qualification" if args.qualify else "smoke",
        "commit": commit,
        "clean_at_start": not preimage,
        "python": sys.executable,
        "python_version": sys.version,
        "runtime": subject,
        "checkout_at_start": source_subject(),
        "archived_stage_inputs": histories,
        "started_at": time.time(),
        "status": "running",
        "cycles": [],
    }

    def save(status: str) -> None:
        report.update(status=status, elapsed_seconds=time.monotonic() - started)
        tmp = output / "report.json.tmp"
        tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        tmp.replace(output / "report.json")

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    save("running")
    try:
        while not report["cycles"] or (args.qualify and time.monotonic() - started < FOUR_HOURS):
            cycle = len(report["cycles"]) + 1
            directory = output / f"cycle-{cycle:04d}"
            directory.mkdir()
            details = directory / "campaign.json"
            command = [
                sys.executable,
                str(ROOT / "scripts/run_test_benchmark.py"),
                "--metrics-dir",
                str(directory / "pytest-metrics"),
                "--timeout",
                str(CYCLE_TIMEOUT),
                "--grace",
                "15",
                "--",
                "-q",
                "--tb=short",
                "--basetemp",
                str(directory / "sandbox"),
                "tests/campaign_recovery_soak_case.py::test_recovery_soak_cycle",
            ]
            print(f"Running cycle {cycle} at {directory}", flush=True)
            before = time.monotonic()
            entry = {
                "cycle": cycle,
                "report": str(details),
                "log": str(directory / "pytest.log"),
                "started_at": time.time(),
            }
            report["cycles"].append(entry)
            save("running")
            with (directory / "pytest.log").open("x", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "RECOVERY_SOAK_REPORT": str(details),
                        "RECOVERY_SOAK_HISTORIES": json.dumps(histories),
                    },
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    code = process.wait()
                except BaseException:
                    # The benchmark wrapper owns pytest and its bounded teardown.
                    # Forward cancellation even when only this runner was signaled.
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=60)
                    raise
            entry.update(seconds=time.monotonic() - before, exit=code)
            entry["checkout_at_exit"] = source_subject()
            save("running")
            if (
                code
                or not details.is_file()
                or json.loads(details.read_text())["status"] != "passed"
            ):
                raise RuntimeError(f"cycle {cycle} failed or lacks verified campaign evidence")
            if args.qualify and (
                git("rev-parse", "HEAD") != commit
                or git("status", "--porcelain")
                or not installed_provenance(expected_commit=commit)["matches_expected"]
            ):
                raise RuntimeError("candidate changed during qualification")
        save("four_hour_workload_passed" if args.qualify else "smoke_passed")
        return 0
    except BaseException:
        save("incomplete")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
