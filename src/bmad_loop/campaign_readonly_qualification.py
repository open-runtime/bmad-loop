"""Independent zero-token write-denial probe for a native read-only route.

The same outer sandbox and frozen grants protect both native read tools and
their descendants. A native read-only label alone is not qualification.
"""

import json
import subprocess
import sys
from pathlib import Path

from .campaign_model import CampaignError

CHECKS = {
    "owned",
    "new_product",
    "sibling",
    "outside",
    "target_ref",
    "worker_ref",
    "git_index",
    "object_overwrite",
    "readable",
    "descendant",
}
NATIVE_POLICY = {"type": "readOnly", "networkAccess": False}


def probe_command(probe):
    root = Path(probe["root"])
    code = """import json,os,subprocess,sys
from pathlib import Path
checks={}
for name,path in json.loads(sys.argv[1]).items():
 try: Path(path).write_text('forbidden change')
 except PermissionError: checks[name]=True
 else: checks[name]=False
path=Path('src/owned.txt')
checks['readable']=path.read_text()==sys.argv[2]
child=subprocess.run([sys.executable,'-c',"from pathlib import Path; Path('src/owned.txt').write_text('forbidden')"],capture_output=True)
checks['descendant']=child.returncode!=0 and b'PermissionError' in child.stderr
print(json.dumps(checks))
"""
    targets = {
        "owned": root / "workspace/src/owned.txt",
        "new_product": root / "workspace/new-file.txt",
        "sibling": root / "workspace/src/sibling.txt",
        "outside": root / "outside.txt",
        "target_ref": root / "repository/.git/refs/heads/main",
        "worker_ref": root / "repository/.git/refs/heads/bmad-qualification",
        "git_index": root / "repository/.git/worktrees/workspace/index",
        "object_overwrite": next((root / "repository/.git/objects/pack").glob("*.pack")),
    }
    return [
        str(Path(sys.executable).resolve()),
        "-c",
        code,
        json.dumps({name: str(path) for name, path in targets.items()}),
        probe["workspace_readback"],
    ]


def native_checks(result):
    if not isinstance(result, dict) or result.get("exitCode") != 0:
        raise CampaignError("Native read-only containment command did not complete")
    try:
        checks = json.loads(result["stdout"])
    except (KeyError, ValueError, TypeError) as exc:
        raise CampaignError("Native read-only containment output is invalid") from exc
    if (
        not isinstance(checks, dict)
        or set(checks) != CHECKS
        or any(value is not True for value in checks.values())
    ):
        raise CampaignError("Native read-only containment did not deny every protected write")
    return checks


def containment(envelope):
    from .campaign_execution_sandbox import command

    root = Path(envelope["probe"]["root"])
    argv, cwd, env = command(root / "job/grants.json", probe_command(envelope["probe"]))
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise CampaignError("Read-only containment probe did not complete")
    checks = json.loads(result.stdout)
    if set(checks) != CHECKS:
        raise CampaignError("Read-only containment probe is incomplete")
    return checks
