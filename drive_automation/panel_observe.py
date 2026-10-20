"""Observe panel/Drive status via gateway MQTT (charge_rate, discovery, health)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from drive_automation.gateway import GatewayShell


@dataclass
class PanelSnapshot:
    raw_status: dict[str, Any] = field(default_factory=dict)
    discovery_state: str = ""
    charge_rate_a: Optional[float] = None
    rms_current_a: Optional[float] = None
    charging_state: str = ""
    hardware_id: str = ""
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovery_state": self.discovery_state,
            "charge_rate_a": self.charge_rate_a,
            "rms_current_a": self.rms_current_a,
            "charging_state": self.charging_state,
            "hardware_id": self.hardware_id,
            "ts": self.ts,
        }


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_nested(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    for nested_key in ("drive", "status", "data", "chargePoint", "ev_charger"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            found = _extract_nested(nested, *keys)
            if found is not None:
                return found
    return None


def parse_eve_status(payload: str | dict[str, Any]) -> PanelSnapshot:
    """Normalize eve MQTT status JSON into a PanelSnapshot."""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return PanelSnapshot(ts=time.time())
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return PanelSnapshot(raw_status={"raw": text[:500]}, ts=time.time())
    else:
        data = dict(payload)

    charge_rate = _as_float(
        _extract_nested(
            data,
            "charge_rate_a",
            "chargeRateA",
            "charge_rate",
            "target_charge_rate_a",
        )
    )
    rms = _as_float(
        _extract_nested(
            data,
            "rms_current_a",
            "rms_current",
            "rmsCurrentA",
            "current_a",
        )
    )
    charging_state = str(
        _extract_nested(
            data,
            "charging_state",
            "chargingState",
            "charge_state",
            "state",
        )
        or ""
    )
    discovery = str(data.get("discovery_state") or data.get("discoveryState") or "")
    hardware_id = str(data.get("hardware_id") or data.get("hardwareId") or "")
    return PanelSnapshot(
        raw_status=data,
        discovery_state=discovery,
        charge_rate_a=charge_rate,
        rms_current_a=rms,
        charging_state=charging_state,
        hardware_id=hardware_id,
        ts=time.time(),
    )


class PanelObserver:
    """Poll gateway MQTT for Drive/eve status over SSH."""

    def __init__(
        self,
        shell: GatewayShell,
        *,
        status_topic: str = "local/eve/data/v1/status",
        wait_sec: float = 2.0,
    ) -> None:
        self.shell = shell
        self.status_topic = status_topic
        self.wait_sec = wait_sec
        self.last: PanelSnapshot = PanelSnapshot()

    def poll_status(self) -> PanelSnapshot:
        cmd = (
            f"mosquitto_sub -t {self.status_topic} -C 1 -W {max(1, int(self.wait_sec))}"
        )
        code, out, err = self.shell.run(cmd, timeout_s=self.wait_sec + 10)
        text = (out or "").strip() or (err or "").strip()
        if code != 0 and not text:
            self.last = PanelSnapshot(ts=time.time())
            return self.last
        # mosquitto_sub may print connection noise — take last JSON-looking line
        candidate = text
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                candidate = line
                break
        self.last = parse_eve_status(candidate)
        return self.last

    def throttle_setpoint_a(self) -> Optional[float]:
        """Best panel-sourced charge current the CP is commanded/throttled to."""
        snap = self.last if self.last.ts else self.poll_status()
        if snap.charge_rate_a is not None:
            return snap.charge_rate_a
        return None


def scrape_journal_snippet(
    shell: GatewayShell,
    *,
    unit: str = "span-sbx-seriald",
    lines: int = 80,
) -> str:
    code, out, err = shell.run(
        f"journalctl -u {unit} -n {int(lines)} --no-pager",
        timeout_s=45,
    )
    return (out or err or "").strip()


def find_charge_rate_in_text(text: str) -> Optional[float]:
    """Best-effort parse charge_rate from log/MQTT dump text."""
    match = re.search(
        r"charge_rate(?:_a)?\s*[:=]\s*(\d+(?:\.\d+)?)",
        text,
        re.I,
    )
    if match:
        return float(match.group(1))
    return None
