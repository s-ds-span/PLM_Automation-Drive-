"""Resolve log-capture SSH targets from gateway serial + panels_config.yml."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - lab cell may need pip install pyyaml
    yaml = None  # type: ignore[assignment]

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_SERIAL_RE = re.compile(r"^[A-Z]{2}-[A-Z0-9]{4}-[A-Z0-9]+$", re.I)
_LAST_LOAD_ERROR: str | None = None
_LAST_LOAD_META: dict[str, Any] = {}


def _config_path() -> Path:
    raw = (
        os.environ.get("GATEWAY_CAPTURE_CONFIG_PATH")
        or os.environ.get("PANEL_CONFIG_PATH")
        or ""
    ).strip()
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw))).resolve()
    base = Path(__file__).resolve().parent
    for name in ("panels_config.yml", "panel_config.yml"):
        candidate = base / name
        if candidate.is_file():
            return candidate.resolve()
    return (base / "panels_config.yml").resolve()


def _read_config_env_value(key: str) -> str:
    """Read one key from config.env when the dashboard was not started via run_dashboard.sh."""
    val = (os.environ.get(key) or "").strip()
    if val:
        return val
    cfg = Path(__file__).resolve().parent / "config.env"
    if not cfg.is_file():
        return ""
    prefix = f"{key}="
    for line in cfg.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        return line[len(prefix) :].strip().strip('"').strip("'")
    return ""


LOCAL_CAPTURE_ID = "__local_capture__"


def _norm_serial(serial: str) -> str:
    s = (serial or "").strip()
    for ch in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        s = s.replace(ch, "-")
    return s.upper()


def _serial_from_entry(entry: dict[str, Any], host_key: str) -> str:
    for field in ("gateway_serial", "gatewaySerial", "serial_number", "serial"):
        val = str(entry.get(field) or "").strip()
        if val and not _IP_RE.match(val):
            return val
    key = str(host_key).strip()
    if key and not _IP_RE.match(key) and _SERIAL_RE.match(key):
        return key
    return ""


def _user_from_entry(entry: dict[str, Any]) -> str:
    for field in ("user_name", "username", "user", "ssh_user"):
        val = str(entry.get(field) or "").strip()
        if val:
            return val
    return ""


def _panel_host(host_key: str, entry: dict[str, Any]) -> str:
    key = str(host_key).strip()
    if _IP_RE.match(key):
        return key
    for field in ("host", "panel_ip", "ip", "panel_host"):
        val = str(entry.get(field) or "").strip()
        if val and _IP_RE.match(val):
            return val
    gs = str(entry.get("gateway_serial") or "").strip()
    if gs and _IP_RE.match(gs):
        return gs
    return ""


def _ip_panel_entries(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect panel dicts keyed by IP from several common YAML layouts."""
    found: dict[str, dict[str, Any]] = {}

    def add(key: str, entry: Any) -> None:
        if not isinstance(entry, dict):
            return
        host = str(key).strip()
        if _IP_RE.match(host):
            found[host] = entry

    for root_key in ("panels", "panel", "gateways"):
        block = data.get(root_key)
        if isinstance(block, dict):
            for key, entry in block.items():
                add(str(key), entry)

    for key, entry in data.items():
        if key in ("panels", "panel", "gateways"):
            continue
        add(str(key), entry)

    return found


def _index_panel_entry(
    out: dict[str, tuple[str, dict[str, Any]]],
    host_key: str,
    entry: dict[str, Any],
) -> None:
    serial = _serial_from_entry(entry, host_key)
    if not serial:
        return
    host = _panel_host(host_key, entry)
    if not host:
        return
    out[_norm_serial(serial)] = (host, entry)


def _walk_for_serial_entries(
    node: Any,
    out: dict[str, tuple[str, dict[str, Any]]],
    host_hint: str = "",
) -> None:
    if isinstance(node, dict):
        hint = host_hint
        for key, val in node.items():
            k = str(key).strip()
            if _IP_RE.match(k):
                hint = k
            if isinstance(val, dict):
                _index_panel_entry(out, k if _IP_RE.match(k) else hint, val)
                if k not in ("probes", "subpanel"):
                    _walk_for_serial_entries(val, out, hint or k)
    elif isinstance(node, list):
        for item in node:
            _walk_for_serial_entries(item, out, host_hint)


def _load_panels_by_serial() -> dict[str, tuple[str, dict[str, Any]]]:
    global _LAST_LOAD_ERROR, _LAST_LOAD_META
    _LAST_LOAD_ERROR = None
    _LAST_LOAD_META = {}
    if yaml is None:
        _LAST_LOAD_ERROR = (
            "PyYAML not installed — run: pip install pyyaml (or pip install -r requirements.txt)"
        )
        return {}
    path = _config_path()
    if not path.is_file():
        _LAST_LOAD_ERROR = f"Config file not found: {path}"
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        _LAST_LOAD_ERROR = f"YAML parse error in {path}: {exc}"
        return {}
    except OSError as exc:
        _LAST_LOAD_ERROR = f"Cannot read {path}: {exc}"
        return {}
    if not isinstance(data, dict):
        _LAST_LOAD_ERROR = f"Root of {path} must be a mapping"
        return {}

    panels = _ip_panel_entries(data)
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for host_key, entry in panels.items():
        _index_panel_entry(out, host_key, entry)

    if not out:
        _walk_for_serial_entries(data, out)

    _LAST_LOAD_META = {
        "panel_ips": sorted(panels.keys()),
        "panel_count": len(panels),
        "serials_found": sorted(out.keys()),
        "file_size": path.stat().st_size,
        "file_mtime": path.stat().st_mtime,
    }
    if not out:
        _LAST_LOAD_ERROR = (
            f"No gateway_serial + panel IP entries in {path} "
            f"({len(panels)} IP-keyed panels scanned). "
            "Each panel needs: gateway_serial, user_name, under an IP key or panels: block."
        )
    return out


def _serials_in_config() -> list[str]:
    return sorted(_load_panels_by_serial().keys())


def panel_ssh_password() -> str:
    return (
        _read_config_env_value("PANEL_SSH_PASS")
        or _read_config_env_value("PANEL_SSH_PASSWORD")
        or _read_config_env_value("CAPTURE_SSH_PASS")
        or _read_config_env_value("DRIVE_PASSWORD")
    ).strip()


def _env_serials() -> list[str]:
    raw = _read_config_env_value("GATEWAY_SERIALS")
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _local_capture_target() -> Optional[dict[str, Any]]:
    """Optional CAPTURE_SSH_* row for qa@lab-cell local USB capture."""
    host = _read_config_env_value("CAPTURE_SSH_HOST")
    user = _read_config_env_value("CAPTURE_SSH_USER")
    if not host or not user:
        return None
    pwd_ok = bool(
        _read_config_env_value("CAPTURE_SSH_PASS")
        or panel_ssh_password()
    )
    return {
        "serial": LOCAL_CAPTURE_ID,
        "name": f"Local USB ({user}@{host})",
        "host": host,
        "user_name": user,
        "configured": pwd_ok,
        "is_local": True,
    }


def automation_gateway_serial() -> str:
    """Gateway serial for JAMA/V2H log capture (from .env, not the dashboard dropdown)."""
    explicit = (os.environ.get("AUTOMATION_GATEWAY_SERIAL") or "").strip()
    if explicit:
        return explicit
    serials = _env_serials()
    return serials[0] if serials else ""


def automation_gateway_info() -> dict[str, Any]:
    """Non-secret automation gateway config for the dashboard header."""
    serial = automation_gateway_serial()
    if not serial:
        return {
            "ok": False,
            "gateway_serial": "",
            "error": "Set AUTOMATION_GATEWAY_SERIAL or GATEWAY_SERIALS in .env",
        }
    by_serial = _load_panels_by_serial()
    host, entry = by_serial.get(_norm_serial(serial), ("", {}))
    user = _user_from_entry(entry)
    resolved = resolve_capture_gateway(serial)
    return {
        "ok": bool(resolved.get("ok")),
        "gateway_serial": serial,
        "name": str(entry.get("name") or "").strip(),
        "host": host,
        "user_name": user,
        "configured": bool(host and user),
        "password_configured": bool(panel_ssh_password()),
        "error": str(resolved.get("error") or ""),
    }


def resolve_automation_credentials(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Like resolve_capture_credentials but defaults to AUTOMATION_GATEWAY_SERIAL."""
    payload = payload or {}
    serial = str(
        payload.get("gateway_serial")
        or payload.get("serial_number")
        or payload.get("serial")
        or ""
    ).strip()
    if not serial:
        serial = automation_gateway_serial()
    if not serial:
        return {
            "ok": False,
            "error": "Set AUTOMATION_GATEWAY_SERIAL or GATEWAY_SERIALS in .env",
        }
    return resolve_capture_gateway(serial)


def capture_gateway_debug() -> dict[str, Any]:
    by_serial = _load_panels_by_serial()
    panels = _LAST_LOAD_META.get("panel_ips") or []
    return {
        "config_path": str(_config_path()),
        "panel_config_path_env": (os.environ.get("PANEL_CONFIG_PATH") or "").strip(),
        "gateway_serials_env": _env_serials(),
        "load_error": _LAST_LOAD_ERROR,
        "panel_ips": panels,
        "panel_count": _LAST_LOAD_META.get("panel_count", 0),
        "serials_in_file": sorted(by_serial.keys()),
        "entries": [
            {
                "serial": serial,
                "host": host,
                "user_name": _user_from_entry(entry),
                "name": str(entry.get("name") or "").strip(),
            }
            for serial, (host, entry) in sorted(by_serial.items())
        ],
    }


def list_capture_gateways() -> dict[str, Any]:
    """Serials from env, enriched with panels_config.yml (no secrets)."""
    by_serial = _load_panels_by_serial()
    serials = _env_serials()
    items: list[dict[str, Any]] = []
    local = _local_capture_target()
    if local is not None:
        items.append(local)
    for serial in serials:
        host, entry = by_serial.get(_norm_serial(serial), ("", {}))
        user = _user_from_entry(entry)
        items.append(
            {
                "serial": serial,
                "name": str(entry.get("name") or "").strip(),
                "host": host,
                "user_name": user,
                "configured": bool(host and user),
            }
        )
    pwd = panel_ssh_password()
    cfg_path = _config_path()
    hint = ""
    if not serials and local is None:
        hint = "Set GATEWAY_SERIALS and/or CAPTURE_SSH_HOST in config.env, then restart dashboard."
    elif not cfg_path.is_file():
        hint = f"panels_config.yml not found at {cfg_path} — copy panels_config.yml.example"
    return {
        "ok": True,
        "config_path": str(cfg_path),
        "config_exists": cfg_path.is_file(),
        "password_configured": bool(pwd),
        "serials_in_file": sorted(by_serial.keys()),
        "panel_ips": _LAST_LOAD_META.get("panel_ips") or [],
        "load_error": _LAST_LOAD_ERROR or hint or None,
        "gateways": items,
    }


def resolve_capture_gateway(serial: str) -> dict[str, Any]:
    """Map gateway serial → SSH username, host, password for log capture."""
    serial_n = _norm_serial(serial or "")
    cfg_path = _config_path()
    if serial == LOCAL_CAPTURE_ID:
        local = _local_capture_target()
        if local is None:
            return {"ok": False, "error": "CAPTURE_SSH_HOST / CAPTURE_SSH_USER not configured"}
        password = (
            _read_config_env_value("CAPTURE_SSH_PASS") or panel_ssh_password()
        )
        if not password:
            return {"ok": False, "error": "Set CAPTURE_SSH_PASS or PANEL_SSH_PASS in config.env"}
        return {
            "ok": True,
            "serial": LOCAL_CAPTURE_ID,
            "host": local["host"],
            "username": local["user_name"],
            "password": password,
            "name": local.get("name") or "",
        }
    if not serial_n:
        return {"ok": False, "error": "gateway_serial is required"}
    env_serials = {_norm_serial(s) for s in _env_serials()}
    if env_serials and serial_n not in env_serials:
        return {
            "ok": False,
            "error": f"Gateway serial {serial_n!r} is not listed in GATEWAY_SERIALS",
        }
    by_serial = _load_panels_by_serial()
    if serial_n not in by_serial:
        found = sorted(by_serial.keys())
        parts = [
            f"No panels_config entry with gateway_serial {serial_n!r} in {cfg_path}.",
        ]
        if _LAST_LOAD_ERROR:
            parts.append(_LAST_LOAD_ERROR)
        elif found:
            parts.append(f"Found in file: {', '.join(found)}.")
        else:
            parts.append(
                f"Panel IPs in file: {', '.join(_LAST_LOAD_META.get('panel_ips') or []) or '(none)'}."
            )
        return {"ok": False, "error": " ".join(parts)}
    host, entry = by_serial[serial_n]
    user = _user_from_entry(entry)
    if not host or not user:
        return {
            "ok": False,
            "error": (
                f"Panel for {serial_n!r} in {cfg_path} must include "
                "user_name (host is the panels YAML IP key)"
            ),
        }
    password = panel_ssh_password()
    if not password:
        return {
            "ok": False,
            "error": "Set PANEL_SSH_PASS in the wall PC env for log capture SSH",
        }
    return {
        "ok": True,
        "serial": serial_n,
        "host": host,
        "username": user,
        "password": password,
        "name": str(entry.get("name") or "").strip(),
    }


def resolve_capture_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Accept ``gateway_serial`` (preferred) or legacy ``username``/``host``/``password``.

    Returns dict with ok, username, host, password, serial (optional), error.
    """
    serial = str(
        payload.get("gateway_serial")
        or payload.get("serial_number")
        or payload.get("serial")
        or ""
    ).strip()
    if serial:
        return resolve_capture_gateway(serial)

    user = str(payload.get("username") or payload.get("user") or "").strip()
    host = str(payload.get("host") or "").strip()
    password = payload.get("password")
    if password is not None and not isinstance(password, str):
        password = str(password)
    password = (password or "").strip()
    if not user or not host or not password:
        return {
            "ok": False,
            "error": "gateway_serial is required (or username, host, and password)",
        }
    return {
        "ok": True,
        "username": user,
        "host": host,
        "password": password,
        "serial": "",
    }
