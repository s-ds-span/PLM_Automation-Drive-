"""Wall-local timestamps so run/pilot logs line up with SBX Modbus logs."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def resolve_tz_name(tz_name: Optional[str] = None) -> str:
    """Return configured timezone name (``DRIVE_TIMEZONE`` / wall local)."""
    raw = (tz_name or os.environ.get("DRIVE_TIMEZONE") or "").strip()
    if not raw or raw.lower() in ("local", "system"):
        # Match SBX capture host clock (naive local wall time).
        local = datetime.now().astimezone().tzinfo
        key = getattr(local, "key", None)
        return str(key or "local")
    return raw


@lru_cache(maxsize=16)
def _zoneinfo(tz_name: str) -> Any:
    if tz_name == "local":
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo or timezone.utc


def now_local(tz_name: Optional[str] = None) -> datetime:
    """Aware datetime in the configured wall timezone."""
    name = resolve_tz_name(tz_name)
    return datetime.now(_zoneinfo(name))


def stamp_fields(tz_name: Optional[str] = None) -> dict[str, str]:
    """Timestamp fields for JSONL / SSE events.

    ``ts`` includes the UTC offset (e.g. ``...+05:30``) so it lines up with
    Modbus lines after converting, and ``ts_local`` matches the SBX style
    ``YYYY-MM-DD HH:MM:SS`` used on the wall.
    """
    now = now_local(tz_name)
    return {
        "ts": now.isoformat(timespec="milliseconds"),
        "ts_local": now.strftime("%Y-%m-%d %H:%M:%S"),
        "tz": resolve_tz_name(tz_name),
    }
