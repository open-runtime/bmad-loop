"""AGY metadata capacity: refreshed quota plus an exact accessible model catalog.

The fixed /usage and models commands do not start a conversation or model turn.
Native manager/auth initialization may still run; private cwd and coordination
isolate its customizations, without copying credentials or editing user settings.
Only the observed Flash IDs/labels map to the documented Gemini quota group
(https://antigravity.google/docs/cli/commands/usage/). Other models/windows need
explicit schema support, never family-prefix or account-plan guesses.
"""

from __future__ import annotations

import hashlib
import json
import math
import stat
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from .campaign_agy_launcher import child_environment
from .campaign_capacity_transport import MAX_OUTPUT_BYTES, ProbeError, StatusProcess
from .campaign_model import RoleCandidate
from .campaign_scheduler import CapacityObservation
from .platform_util import is_link_like

MAX_AGE_S = 120.0
FLASH_MODELS = {
    "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
}
_TOKENS = {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}
_SETTINGS = (
    '{"disableSlashCommands":false,"statusLine":{"enabled":false},"title":{"enabled":false}}'
)


def _object(value: object, required: set[str], optional: set[str] | None = None) -> dict:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - (optional or set())
    ):
        raise ProbeError("native AGY metadata schema is unsupported")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProbeError("native AGY metadata text is invalid")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbeError("native AGY metadata number is invalid")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ProbeError("native AGY metadata number is invalid") from exc
    if not math.isfinite(number):
        raise ProbeError("native AGY metadata number is invalid")
    return number


def _envelope(raw: object, command: str) -> dict:
    envelope = _object(
        raw,
        {
            "conversation_id",
            "status",
            "response",
            "duration_seconds",
            "num_turns",
            "usage",
            "command",
        },
    )
    tokens = _object(envelope["usage"], _TOKENS)
    if (
        envelope["status"] != "SUCCESS"
        or envelope["conversation_id"] != ""
        or type(envelope["num_turns"]) is not int
        or envelope["num_turns"] != 0
        or any(type(value) is not int or value != 0 for value in tokens.values())
    ):
        raise ProbeError("native AGY metadata is not a successful zero-turn observation")
    if not isinstance(envelope["response"], str) or _number(envelope["duration_seconds"]) < 0:
        raise ProbeError("native AGY metadata envelope is invalid")
    action = _object(envelope["command"], {"name", "data"})
    if action["name"] != command:
        raise ProbeError("native AGY metadata command mismatch")
    return action["data"]


def from_agy(
    raw: dict, candidate: RoleCandidate, *, observed_at: float, now: float
) -> CapacityObservation:
    """Parse a contemporaneous pair; no historical timestamp refresh or grant."""
    pair = _object(raw, {"usage", "models"})
    usage = _object(_envelope(pair["usage"], "usage"), {"description", "groups"})
    catalog = _object(_envelope(pair["models"], "models"), {"models"})
    _text(usage["description"])
    if not isinstance(catalog["models"], list) or not catalog["models"]:
        raise ProbeError("native AGY model catalog is missing")
    models = {}
    for item in catalog["models"]:
        row = _object(item, {"id", "label"})
        identifier, label = _text(row["id"]), _text(row["label"])
        if identifier in models:
            raise ProbeError("native AGY model catalog is ambiguous")
        models[identifier] = label
    if candidate.model not in FLASH_MODELS:
        raise ProbeError("native AGY model has no supported exact quota-group mapping")
    access = candidate.model in models
    if access and models[candidate.model] != FLASH_MODELS[candidate.model]:
        raise ProbeError("native AGY model label does not match its supported quota group")
    observed, current = _number(observed_at), _number(now)
    if not 0 <= current - observed <= MAX_AGE_S:
        raise ProbeError("native AGY observation is stale or future dated")
    if not isinstance(usage["groups"], list) or not usage["groups"]:
        raise ProbeError("native AGY quota groups are missing")
    groups = {}
    bucket_ids = set()
    for item in usage["groups"]:
        group = _object(item, {"name", "description", "buckets"})
        name = _text(group["name"])
        _text(group["description"])
        if name in groups or name not in {"Gemini Models", "Claude and GPT models"}:
            raise ProbeError("native AGY quota group is ambiguous or unsupported")
        if not isinstance(group["buckets"], list) or not group["buckets"]:
            raise ProbeError("native AGY quota windows are missing")
        windows = set()
        remaining, expires = True, observed + MAX_AGE_S
        for item in group["buckets"]:
            bucket = _object(
                item, {"id", "name", "window", "remaining_fraction", "reset_time"}, {"description"}
            )
            identifier = _text(bucket["id"])
            _text(bucket["name"])
            if "description" in bucket:
                _text(bucket["description"])
            window = bucket["window"]
            # Only this spelling has a captured native envelope. A paid-plan
            # second window must not be silently ignored or guessed from docs.
            if window != "weekly" or window in windows or identifier in bucket_ids:
                raise ProbeError("native AGY quota window is ambiguous or unsupported")
            windows.add(window)
            bucket_ids.add(identifier)
            fraction = _number(bucket["remaining_fraction"])
            if not 0 <= fraction <= 1:
                raise ProbeError("native AGY quota fraction is invalid")
            stamp = _text(bucket["reset_time"])
            try:
                reset = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                offset = reset.utcoffset()
                if offset is None or offset.total_seconds() != 0:
                    raise ValueError("not UTC")
                reset_at = reset.timestamp()
            except (ValueError, OverflowError) as exc:
                raise ProbeError("native AGY quota reset is invalid") from exc
            if not current < reset_at:
                raise ProbeError("native AGY quota window is not current")
            remaining &= fraction > 0
            expires = min(expires, reset_at)
        groups[name] = (remaining, expires)
    if "Gemini Models" not in groups:
        raise ProbeError("native AGY requested quota group is missing")
    remaining, expires = groups["Gemini Models"]
    available = access and remaining
    return CapacityObservation(
        available,
        model_access=access,
        concurrency_known=False,
        observed_at=observed,
        expires_at=expires,
        reason=(
            "native AGY Gemini allowance available; campaign concurrency limits apply"
            if available
            else (
                "native AGY requested model is absent from the current catalog"
                if not access
                else "native AGY Gemini allowance exhausted"
            )
        ),
    )


def _settings_pin(path: Path) -> tuple[str | None, dict]:
    if not path.exists() and not path.is_symlink():
        return None, {}
    if is_link_like(path) or not path.is_file() or path.stat().st_size > MAX_OUTPUT_BYTES:
        raise ProbeError("native AGY settings cannot be verified")
    with path.open("rb") as stream:
        data = stream.read(MAX_OUTPUT_BYTES + 1)
    if len(data) > MAX_OUTPUT_BYTES:
        raise ProbeError("native AGY settings exceeded their bound")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProbeError("native AGY settings cannot be verified") from exc
    if not isinstance(value, dict):
        raise ProbeError("native AGY settings cannot be verified")
    return hashlib.sha256(data).hexdigest(), value


def _image_pin(binary: Path) -> tuple:
    if is_link_like(binary) or not binary.is_file():
        raise ProbeError("native AGY executable must be a regular native file")
    info = binary.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_agy(binary: str, env: Mapping[str, str], deadline: float) -> dict:
    """Two fixed metadata calls share one deadline and isolated coordination."""
    # Account/auth settings remain inherited. Unknown provider configuration
    # knobs cannot silently redirect the metadata observer to another context.
    if any(
        key.startswith(("AGY_", "ANTIGRAVITY_")) and key != "AGY_CLI_DISABLE_AUTO_UPDATE"
        for key in env
    ):
        raise ProbeError("native AGY environment overrides require a custom capacity command")
    home = Path(env.get("HOME", ""))
    if not home.is_absolute():
        raise ProbeError("native AGY account HOME is unavailable")
    settings = home / ".gemini/antigravity-cli/settings.json"
    hooks = home / ".gemini/config/hooks.json"
    before = (_image_pin(Path(binary)), _settings_pin(settings), _settings_pin(hooks))
    if before[1][1].get("disableSlashCommands", False) is not False:
        raise ProbeError("native AGY slash-command dispatch is disabled")
    clean = child_environment(
        {
            key: value
            for key, value in env.items()
            if not key.startswith(("HCOM_", "SUPERSET_", "PARADEV_", "BMAD_LOOP_"))
        }
    )
    result = {}
    total = 0
    with tempfile.TemporaryDirectory(prefix="bmad-agy-capacity-") as temporary:
        cwd = Path(temporary).resolve()
        if any((parent / ".git").exists() for parent in (cwd, *cwd.parents)):
            raise ProbeError("native AGY metadata cwd would inherit project settings")
        cwd.chmod(stat.S_IRWXU)
        room = cwd / "hcom"
        room.mkdir(mode=stat.S_IRWXU)
        clean["HCOM_DIR"] = str(room)
        for name, tail in (("usage", ["-p", "/usage"]), ("models", ["models"])):
            if time.monotonic() >= deadline:
                raise ProbeError("native capacity observation timed out")
            argv = [binary, "--settings", _SETTINGS, "--output-format", "json", *tail]
            with StatusProcess(argv, cwd, clean, deadline) as child:
                result[name] = child.json_output()
            total += child.total
            if total > MAX_OUTPUT_BYTES:
                raise ProbeError("native AGY observation output exceeded its aggregate bound")
            if before != (_image_pin(Path(binary)), _settings_pin(settings), _settings_pin(hooks)):
                raise ProbeError(
                    "native AGY executable or global settings changed during observation"
                )
            _envelope(result[name], name)
            if time.monotonic() >= deadline:
                raise ProbeError("native capacity observation timed out")
    return result
