"""Bounded stat-identity reuse for read-only route admission, never launch proof.

Only an explicit context enables reuse. Preparing or executing a native job uses
fresh anchored reads by default. Identity checks remain live even on cache hits;
metadata-preserving hostile mutation is outside admission trust and is caught by
the independent full content check before launch.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import threading
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from .campaign_model import CampaignError
from .platform_util import is_link_like

_ENABLED: ContextVar[bool] = ContextVar("campaign_admission_file_reuse", default=False)
_LOCK = threading.RLock()
Identity = tuple[int, int, int, int, int, int, str]
Key = tuple[str, str]
_CACHE: OrderedDict[Key, tuple[Identity, str | dict[str, Any], int]] = OrderedDict()
MAX_ENTRIES = 256
MAX_JSON_ENTRY_BYTES = 1024 * 1024
MAX_JSON_CACHE_BYTES = 8 * 1024 * 1024


@contextmanager
def admission_reads():
    token = _ENABLED.set(True)
    try:
        yield
    finally:
        _ENABLED.reset(token)


@contextmanager
def fresh_reads():
    """Force full validation, including when called by an admission observer."""
    token = _ENABLED.set(False)
    try:
        yield
    finally:
        _ENABLED.reset(token)


def _stamp(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _identity(path: Path) -> Identity:
    if is_link_like(path):
        raise CampaignError("Qualified file is redirected")
    try:
        entry = path.stat()
    except OSError as exc:
        raise CampaignError("Qualified file is unavailable") from exc
    if not stat.S_ISREG(entry.st_mode):
        raise CampaignError("Qualified file must be regular")
    # A directory/symlink ancestor may change while the final pathname remains
    # spelled identically. Resolving is read-only and repeated on every hit.
    return (*_stamp(entry), str(path.resolve(strict=True)))


def _lookup(key: Key, identity: Identity) -> str | dict[str, Any] | None:
    with _LOCK:
        row = _CACHE.get(key)
        if row is None or row[0] != identity:
            _CACHE.pop(key, None)
            return None
        _CACHE.move_to_end(key)
        return copy.deepcopy(row[1])


def _remember(key: Key, identity: Identity, value: str | dict[str, Any], weight: int) -> None:
    if weight > MAX_JSON_ENTRY_BYTES:
        return
    with _LOCK:
        _CACHE[key] = (identity, copy.deepcopy(value), weight)
        _CACHE.move_to_end(key)
        while (
            len(_CACHE) > MAX_ENTRIES
            or sum(row[2] for row in _CACHE.values()) > MAX_JSON_CACHE_BYTES
        ):
            _CACHE.popitem(last=False)


def _read(
    path: Path, *, limit: int | None = None, digest: bool = False
) -> tuple[Identity, str | bytes]:
    before = _identity(path)
    if limit is not None and before[3] > limit:
        raise CampaignError("Headless record is oversized")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _stamp(opened) != before[:6]:
            raise CampaignError("Qualified file changed while opening")
        if digest:
            result = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                result.update(chunk)
            value = result.hexdigest()
        else:
            if limit is None:
                raise ValueError("Bounded record reads require a byte limit")
            value = stream.read(limit + 1)
            if len(value) > limit:
                raise CampaignError("Headless record grew beyond its byte bound")
        if _stamp(os.fstat(stream.fileno())) != before[:6] or _identity(path) != before:
            raise CampaignError("Qualified file changed while reading")
    return before, value


def file_digest(path: Path) -> str:
    path = path.absolute()
    key = ("digest", str(path))
    if _ENABLED.get():
        identity = _identity(path)
        value = _lookup(key, identity)
        if value is not None:
            if _identity(path) != identity:
                raise CampaignError("Qualified file changed during admission")
            return cast(str, value)
    identity, value = _read(path, digest=True)
    value = cast(str, value)
    if _ENABLED.get():
        _remember(key, identity, value, 0)
    return value


def read_json(path: Path, *, limit: int) -> dict[str, Any]:
    path = path.absolute()
    key = ("json", str(path))
    if _ENABLED.get():
        identity = _identity(path)
        if identity[3] > limit:
            raise CampaignError("Headless record is oversized")
        value = _lookup(key, identity)
        if value is not None:
            if _identity(path) != identity:
                raise CampaignError("Qualified file changed during admission")
            return cast(dict[str, Any], value)
    identity, raw = _read(path, limit=limit)
    raw = cast(bytes, raw)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise CampaignError("Headless record must be an object")
    if _ENABLED.get():
        _remember(key, identity, value, len(raw))
    return value
