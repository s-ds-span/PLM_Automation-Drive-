"""Safe-exit helpers for dashboard / release buttons."""

from __future__ import annotations

from typing import Any, Optional

from drive_automation.clients import DashboardError, EvClient, Lb2Client
from drive_automation.config import DriveConfig
from drive_automation.orchestrator import check_availability


def _shutdown_ev_with_client(client: EvClient, cfg: DriveConfig) -> dict[str, Any]:
    try:
        client.safe_stop()
        status = client.wait_shutdown(timeout_sec=120)
        try:
            client.logout()
        except Exception:
            pass
        after = check_availability(cfg)
        return {
            "ok": True,
            "unit": "ev",
            "shutdown_status": status,
            "in_use": after.ev_in_use,
            "message": "EV Simulator safe exit complete",
            "via": "existing_session",
        }
    except DashboardError as exc:
        return {"ok": False, "unit": "ev", "error": str(exc), "via": "existing_session"}


def _shutdown_lb2_with_client(client: Lb2Client, cfg: DriveConfig) -> dict[str, Any]:
    try:
        client.safe_shutdown(reason="Dashboard safe-shutdown button")
        status = client.wait_shutdown(timeout_sec=120)
        try:
            client.logout()
        except Exception:
            pass
        after = check_availability(cfg)
        return {
            "ok": True,
            "unit": "lb2",
            "shutdown_status": status,
            "in_use": after.lb2_in_use,
            "message": "Load Bank-2 safe exit complete",
            "via": "existing_session",
        }
    except DashboardError as exc:
        return {"ok": False, "unit": "lb2", "error": str(exc), "via": "existing_session"}


def safe_shutdown_ev(
    config: Optional[DriveConfig] = None,
    *,
    existing_client: Optional[EvClient] = None,
) -> dict[str, Any]:
    """Stop EV Simulator using an existing session cookie if provided."""
    cfg = config or DriveConfig.from_env()
    if existing_client is not None and existing_client.logged_in:
        return _shutdown_ev_with_client(existing_client, cfg)

    if not cfg.ev_password:
        return {"ok": False, "unit": "ev", "error": "DRIVE_EV_PASSWORD / DRIVE_PASSWORD not set"}

    live = check_availability(cfg)
    if not live.ev_in_use:
        return {
            "ok": True,
            "unit": "ev",
            "message": "EV Simulator already free",
            "in_use": False,
        }

    client = EvClient(cfg.ev_url, cfg.operator, cfg.ev_password)
    try:
        client.login()
    except DashboardError as exc:
        return {
            "ok": False,
            "unit": "ev",
            "error": str(exc),
            "hint": "Session may be stuck — use ./release_units.sh --emergency --ev",
            "operator": live.ev.get("operator"),
        }

    try:
        return _shutdown_ev_with_client(client, cfg)
    except Exception as exc:
        return {"ok": False, "unit": "ev", "error": str(exc)}


def safe_shutdown_lb2(
    config: Optional[DriveConfig] = None,
    *,
    existing_client: Optional[Lb2Client] = None,
) -> dict[str, Any]:
    """Shutdown Load Bank-2 using an existing session cookie if provided."""
    cfg = config or DriveConfig.from_env()
    if existing_client is not None and existing_client.logged_in:
        return _shutdown_lb2_with_client(existing_client, cfg)

    if not cfg.lb2_password:
        return {"ok": False, "unit": "lb2", "error": "DRIVE_LB2_PASSWORD / DRIVE_PASSWORD not set"}

    live = check_availability(cfg)
    if not live.lb2_in_use:
        return {
            "ok": True,
            "unit": "lb2",
            "message": "Load Bank-2 already free",
            "in_use": False,
        }

    client = Lb2Client(cfg.lb2_url, cfg.operator, cfg.lb2_password)
    try:
        client.login()
    except DashboardError as exc:
        return {
            "ok": False,
            "unit": "lb2",
            "error": str(exc),
            "hint": "Session may be stuck — use ./release_units.sh --emergency --lb2",
            "operator": live.lb2_operator,
        }

    try:
        return _shutdown_lb2_with_client(client, cfg)
    except Exception as exc:
        return {"ok": False, "unit": "lb2", "error": str(exc)}


def safe_shutdown_units(
    unit: str,
    *,
    ev_client: Optional[EvClient] = None,
    lb2_client: Optional[Lb2Client] = None,
) -> dict[str, Any]:
    """unit: ev | lb2 | both"""
    key = (unit or "both").strip().lower()
    if key not in ("ev", "lb2", "both"):
        return {"ok": False, "error": f"Unknown unit '{unit}'. Use ev, lb2, or both."}

    cfg = DriveConfig.from_env()
    results: dict[str, Any] = {"ok": True, "unit": key, "results": {}}

    if key in ("ev", "both"):
        results["results"]["ev"] = safe_shutdown_ev(cfg, existing_client=ev_client)
    if key in ("lb2", "both"):
        results["results"]["lb2"] = safe_shutdown_lb2(cfg, existing_client=lb2_client)

    results["ok"] = all(r.get("ok") for r in results["results"].values())
    results["message"] = (
        "All requested safe exits OK"
        if results["ok"]
        else "One or more safe exits failed — see results"
    )
    return results
