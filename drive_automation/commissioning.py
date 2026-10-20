"""SPAN Drive commission (one-click) and separate decommission via gateway SSH."""

from __future__ import annotations

import json
import re
import shlex
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from drive_automation.gateway import GatewayError, GatewayShell

EmitFn = Callable[..., None]


@dataclass


class CommissionParams:
    drive_serial: str
    # Physical breaker positions (panel tab numbers). Branch-controller instance
    # IDs are derived directly: bc_id = position + bc_id_offset (e.g. 6 -> 14, 8 -> 16).
    top_position: Optional[int] = None
    bottom_position: Optional[int] = None
    bc_id_offset: int = 8
    top_tab_number: Optional[int] = None
    bottom_tab_number: Optional[int] = None
    top_bc_instance: Optional[int] = None
    bottom_bc_instance: Optional[int] = None
    breaker_rating_ma: int = 60000
    timezone: str = "America/Los_Angeles"
    hardware_id: str = ""
    branch_id: str = ""
    building_id: str = ""
    part_number: str = "1-01400-04"
    max_bc_probe: int = 48


@dataclass


class CommissionResult:
    ok: bool
    breaker_instance_id: Optional[int] = None
    top_bc_instance: Optional[int] = None
    bottom_bc_instance: Optional[int] = None
    top_tab_number: Optional[int] = None
    hardware_id: str = ""
    branch_id: str = ""
    building_id: str = ""
    discovery_state: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _noop_emit(event: str, **payload: Any) -> None:
    return None


def _uuid_hex() -> str:
    return uuid.uuid4().hex


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _shell_quote_file(path: str, content: str) -> str:
    """Write file on remote via heredoc (content must not contain EOF marker)."""
    return (
        f"mkdir -p /data/span/mqtt-bridge/shadows && "
        f"cat > {shlex.quote(path)} << 'DRIVE_SHADOW_EOF'\n"
        f"{content}"
        f"\nDRIVE_SHADOW_EOF"
    )


def _parse_instance_index(text: str) -> Optional[int]:
    match = re.search(r"(?:index|instance[_-]?id)\s*[:=]\s*(\d+)", text, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d+)\b", text)
    return int(match.group(1)) if match else None


def _microtrait_bad_request(text: str) -> bool:
    return bool(re.search(r"status\s*:\s*BadRequest", text, re.I))


def _parse_breaker_instance_from_create(text: str) -> Optional[int]:
    """Parse breaker instance id from create-circuit-breaker output.

    Must not use the generic NodeId ``index`` — BadRequest replies still contain
    ``NodeId { index: 1 }`` which previously caused a false breaker id.
    """
    if _microtrait_bad_request(text):
        return None
    if re.search(r"breaker\s*:\s*None\b", text, re.I):
        return None
    # CreateCircuitBreakerResponse { breaker: Some(InstanceId { index: N }) }
    match = re.search(
        r"CreateCircuitBreakerResponse\s*\{[\s\S]*?breaker\s*:\s*Some\([\s\S]*?"
        r"(?:InstanceId\s*\{\s*)?index\s*:\s*(\d+)",
        text,
        re.I,
    )
    if match:
        return int(match.group(1))
    match = re.search(
        r"breaker\s*:\s*Some\(\s*(?:InstanceId\s*\{\s*)?index\s*:\s*(\d+)",
        text,
        re.I,
    )
    if match:
        return int(match.group(1))
    return None


def _parse_panel_tab(text: str) -> Optional[int]:
    match = re.search(r"panel_tab_number\s*[:=]\s*(\d+)", text, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"tab(?:Number|_number)\s*[:=]\s*(\d+)", text, re.I)
    return int(match.group(1)) if match else None


def _has_circuit_breaker(text: str) -> bool:
    lower = text.lower()
    if "circuit_breaker: none" in lower or "circuit_breaker:none" in lower:
        return False
    if re.search(r"circuit_breaker\s*[:=]\s*none", lower):
        return False
    if "circuit_breaker" in lower and "none" not in lower.split("circuit_breaker", 1)[-1][:40]:
        return True
    return bool(re.search(r"circuit_breaker\s*[:=]\s*\d+", lower))


def _same_rail(tab_a: int, tab_b: int) -> bool:
    return (tab_a % 2) == (tab_b % 2)


def _parse_milliamps(text: str, key: str) -> Optional[int]:
    match = re.search(rf"{key}\s*[:=]\s*(\d+)", text, re.I)
    return int(match.group(1)) if match else None


def get_panel_capacity(shell: GatewayShell, *, emit: EmitFn = _noop_emit) -> dict[str, Any]:
    """Read the commissioned panel capacity via ``span-pcm mt panel get``.

    The site utility rating is the panel's main capacity limit used for throttle
    verification (fixed per panel; only changes with manual commissioning).
    Returns amps for the site utility / busbar ratings plus commissioning status.
    """
    out = shell.run_ok("span-pcm mt panel get", timeout_s=60)
    site_ma = _parse_milliamps(out, "site_utility_rating_milliamps")
    busbar_ma = _parse_milliamps(out, "busbar_rating_milliamps")
    status_match = re.search(r"commissioning_status\s*[:=]\s*([A-Za-z_]+)", out, re.I)
    result: dict[str, Any] = {
        # 0 mA is treated as unset — panels often report 0 until site utility is commissioned.
        "site_utility_rating_a": (
            (site_ma / 1000.0) if site_ma is not None and site_ma > 0 else None
        ),
        "busbar_rating_a": (busbar_ma / 1000.0) if busbar_ma is not None else None,
        "commissioning_status": status_match.group(1) if status_match else "",
        "raw": out[:800],
    }
    emit(
        "panel_capacity",
        site_utility_rating_a=result["site_utility_rating_a"],
        busbar_rating_a=result["busbar_rating_a"],
        commissioning_status=result["commissioning_status"],
        message=(
            f"Panel site utility rating "
            f"{result['site_utility_rating_a']} A"
        ),
    )
    return result


def discover_free_bc_pair(
    shell: GatewayShell,
    *,
    max_probe: int = 48,
    preferred_top_tab: Optional[int] = None,
    preferred_bottom_tab: Optional[int] = None,
    emit: EmitFn = _noop_emit,
) -> tuple[int, int, int, int]:
    """Return (top_bc_id, bottom_bc_id, top_tab, bottom_tab) for a free same-rail pair."""
    free: list[tuple[int, int]] = []
    for i in range(1, max_probe + 1):
        code, out, err = shell.run(f"span-pcm mt -i {i} bc get-state", timeout_s=30)
        text = (out or err or "").strip()
        if code != 0 and not text:
            continue
        tab = _parse_panel_tab(text)
        if tab is None:
            continue
        if _has_circuit_breaker(text):
            continue
        free.append((i, tab))
        emit(
            "bc_discovered",
            instance=i,
            panel_tab_number=tab,
            free=True,
            message=f"BC instance {i} tab {tab} free",
        )

    if preferred_top_tab is not None and preferred_bottom_tab is not None:
        top_id = next((i for i, t in free if t == preferred_top_tab), None)
        bot_id = next((i for i, t in free if t == preferred_bottom_tab), None)
        if top_id is None or bot_id is None:
            raise GatewayError(
                f"Preferred tabs {preferred_top_tab}/{preferred_bottom_tab} "
                "not found as free branch controllers."
            )
        if not _same_rail(preferred_top_tab, preferred_bottom_tab):
            raise GatewayError("Preferred tabs are on different rails.")
        if preferred_top_tab > preferred_bottom_tab:
            return bot_id, top_id, preferred_bottom_tab, preferred_top_tab
        return top_id, bot_id, preferred_top_tab, preferred_bottom_tab

    by_tab = {t: i for i, t in free}
    for tab, inst in sorted(by_tab.items()):
        neighbor = tab + 2
        if neighbor in by_tab and _same_rail(tab, neighbor):
            return inst, by_tab[neighbor], tab, neighbor

    raise GatewayError(
        "No free same-rail adjacent 2-pole branch-controller pair found. "
        "Provide top/bottom tab numbers explicitly."
    )


def commission_drive(
    shell: GatewayShell,
    params: CommissionParams,
    *,
    emit: EmitFn = _noop_emit,
) -> CommissionResult:
    """Phase 1 commissioning — not including decommission."""
    if not params.drive_serial.strip():
        raise GatewayError("DRIVE_SERIAL is required for commissioning.")

    hardware_id = params.hardware_id.strip() or _uuid_hex()
    branch_id = params.branch_id.strip() or _uuid_hex()
    building_id = params.building_id.strip() or _uuid_hex()
    stamp = _iso_now()

    emit("commission_start", message="Starting Drive commissioning", serial=params.drive_serial)

    if params.top_bc_instance and params.bottom_bc_instance:
        # Explicit BC instance IDs (advanced override).
        top_id = int(params.top_bc_instance)
        bottom_id = int(params.bottom_bc_instance)
        top_tab = params.top_tab_number
        bottom_tab = params.bottom_tab_number
        if top_tab is None:
            code, out, err = shell.run(f"span-pcm mt -i {top_id} bc get-state", timeout_s=30)
            top_tab = _parse_panel_tab(out or err or "") or 0
        if bottom_tab is None:
            bottom_tab = (top_tab or 0) + 2
    else:
        # Position-based commissioning (no auto-discovery): the operator gives the
        # physical breaker positions and the BC instance IDs are derived directly.
        top_pos = params.top_position if params.top_position is not None else params.top_tab_number
        bottom_pos = (
            params.bottom_position
            if params.bottom_position is not None
            else params.bottom_tab_number
        )
        if top_pos is None or bottom_pos is None:
            raise GatewayError(
                "Breaker top/bottom positions are required for commissioning "
                "(no auto-discovery)."
            )
        top_tab = int(top_pos)
        bottom_tab = int(bottom_pos)
        offset = int(params.bc_id_offset)
        top_id = top_tab + offset
        bottom_id = bottom_tab + offset

    if top_tab > bottom_tab:
        top_id, bottom_id = bottom_id, top_id
        top_tab, bottom_tab = bottom_tab, top_tab

    emit(
        "commission_pair",
        top_bc=top_id,
        bottom_bc=bottom_id,
        top_tab=top_tab,
        bottom_tab=bottom_tab,
        message=f"Using BC {top_id}/{bottom_id} tabs {top_tab}/{bottom_tab}",
    )

    create_cmd = (
        "span-pcm mt panel create-circuit-breaker --style standard "
        f"--role der --rating-milliamps {int(params.breaker_rating_ma)} "
        "two-pole "
        f"--top-branch-controller-instance-id {top_id} "
        f"--bottom-branch-controller-instance-id {bottom_id} "
        "--subtype standard"
    )
    create_out = shell.run_ok(create_cmd, timeout_s=60)
    if _microtrait_bad_request(create_out) or re.search(
        r"breaker\s*:\s*None\b", create_out, re.I
    ):
        raise GatewayError(
            "create-circuit-breaker failed (BadRequest / breaker=None). "
            "Check top/bottom positions are free same-rail BC pair and no "
            f"existing DER breaker occupies them. Raw: {create_out[:500]}"
        )
    breaker_id = _parse_breaker_instance_from_create(create_out)
    emit(
        "commission_breaker_created",
        breaker_instance_id=breaker_id,
        raw=create_out[:500],
        message=f"DER breaker created (instance {breaker_id})",
    )
    if breaker_id is None:
        raise GatewayError(
            "Could not parse breaker instance id from create-circuit-breaker "
            f"(refusing NodeId fallback). Raw: {create_out[:500]}"
        )

    verify = shell.run_ok(f"span-pcm mt -i {breaker_id} cb get-state", timeout_s=30)
    if _microtrait_bad_request(verify):
        raise GatewayError(
            f"Breaker instance {breaker_id} verify failed (BadRequest). "
            f"Create may not have succeeded. Raw: {verify[:500]}"
        )
    emit("commission_breaker_verified", raw=verify[:500], message="Breaker state verified")

    hardware = {
        hardware_id: {
            "branchId": branch_id,
            "branchIds": [branch_id],
            "brand": "SPAN",
            "connectionType": "UDP",
            "model": "Drive",
            "partNumber": params.part_number,
            "serialNumber": params.drive_serial.strip(),
            "type": "EV_CHARGER",
            "updatedAt": stamp,
        }
    }
    branches = {
        branch_id: {
            "breakerRatingA": int(params.breaker_rating_ma) // 1000,
            "dipole": True,
            "location": "IN_PANEL",
            "tabNumber": int(top_tab),
        }
    }
    building = {
        "buildingId": building_id,
        "createdAt": stamp,
        "timezone": params.timezone,
        "zipcode": "",
    }
    charge_cfg = {
        "DYNAMIC_CHARGING": {
            "chargeMode": "DYNAMIC_CHARGING",
            "isSelectedForPanel": True,
        }
    }

    for name, payload in (
        ("hardware.json", hardware),
        ("branches.json", branches),
        ("building.json", building),
    ):
        path = f"/data/span/mqtt-bridge/shadows/{name}"
        shell.run_ok(_shell_quote_file(path, json.dumps(payload, indent=2)), timeout_s=30)

    pubs = [
        (
            "mosquitto_pub -t local/shadow/hardware -r "
            "-f /data/span/mqtt-bridge/shadows/hardware.json"
        ),
        (
            "mosquitto_pub -t local/shadow/branches -r "
            "-f /data/span/mqtt-bridge/shadows/branches.json"
        ),
        (
            "mosquitto_pub -t local/shadow/building -r "
            "-f /data/span/mqtt-bridge/shadows/building.json"
        ),
        (
            "mosquitto_pub -t local/shadow/panel-drive-charge-config -r "
            f"-m {shlex.quote(json.dumps(charge_cfg))}"
        ),
    ]
    for cmd in pubs:
        shell.run_ok(cmd, timeout_s=30)
    emit("commission_shadows_published", message="MQTT shadows published with retain")

    shell.run_ok("systemctl restart span-sbx-seriald", timeout_s=60)
    shell.run_ok("systemctl restart span-eve", timeout_s=60)
    emit("commission_services_restarted", message="sbx-seriald and eve restarted")

    discovery = ""
    for _ in range(30):
        code, out, err = shell.run(
            "mosquitto_sub -t local/eve/data/v1/status -C 1 -W 3",
            timeout_s=15,
        )
        text = (out or err or "").strip()
        if text:
            try:
                data = json.loads(text)
                discovery = str(data.get("discovery_state") or "")
            except json.JSONDecodeError:
                discovery = text[:200]
            emit(
                "commission_discovery_poll",
                discovery_state=discovery,
                message=f"Discovery: {discovery or 'pending'}",
            )
            if discovery in (
                "DISCOVERY_SUCCESSFUL",
                "SUCCESSFUL",
                "DiscoverySuccessful",
            ):
                discovery = "DISCOVERY_SUCCESSFUL"
                break
            # Eve reports FAILED when the Drive never appears on SBX — stop early.
            if discovery.upper() in ("FAILED", "DISCOVERY_FAILED", "FAILURE"):
                break

    ok = discovery == "DISCOVERY_SUCCESSFUL"
    result = CommissionResult(
        ok=ok,
        breaker_instance_id=breaker_id,
        top_bc_instance=top_id,
        bottom_bc_instance=bottom_id,
        top_tab_number=int(top_tab),
        hardware_id=hardware_id,
        branch_id=branch_id,
        building_id=building_id,
        discovery_state=discovery,
        message=(
            "Commissioning complete — discovery successful"
            if ok
            else (
                f"Commissioning finished but discovery_state={discovery!r}. "
                f"Check Drive serial {params.drive_serial!r} is powered/on-bus, "
                f"SBX link is up, and BC tabs {top_tab}/{bottom_tab} (ids {top_id}/{bottom_id}) "
                "are correct."
            )
        ),
        details={"breaker_verify": verify[:500]},
    )
    emit(
        "commission_complete",
        ok=result.ok,
        breaker_instance_id=breaker_id,
        discovery_state=discovery,
        breaker_rating_ma=int(params.breaker_rating_ma),
        top_position=int(top_tab),
        bottom_position=int(bottom_tab),
        services_restarted=True,
        discovery_successful=ok,
        message=result.message,
    )
    if not ok:
        raise GatewayError(result.message)
    return result


def decommission_drive(
    shell: GatewayShell,
    *,
    breaker_instance_id: Optional[int] = None,
    top_bc_instance: Optional[int] = None,
    bottom_bc_instance: Optional[int] = None,
    emit: EmitFn = _noop_emit,
) -> dict[str, Any]:
    """Separate maintenance action — never part of one-click pipeline teardown."""
    emit("decommission_start", message="Starting Drive decommission (separate action)")

    shell.run("systemctl stop span-eve", timeout_s=60)
    emit("decommission_eve_stopped", message="span-eve stopped")

    if breaker_instance_id is not None:
        code, out, err = shell.run(
            f"span-pcm mt panel delete-circuit-breaker --instance-id {int(breaker_instance_id)}",
            timeout_s=60,
        )
        emit(
            "decommission_breaker_deleted",
            exit_code=code,
            raw=(out or err or "")[:500],
            message=f"Delete breaker {breaker_instance_id} exit={code}",
        )

    for topic in (
        "local/shadow/hardware",
        "local/shadow/branches",
        "local/shadow/building",
        "local/shadow/panel-drive-charge-config",
    ):
        shell.run(f"mosquitto_pub -t {shlex.quote(topic)} -r -n", timeout_s=20)
    emit("decommission_mqtt_cleared", message="Retained MQTT shadows cleared")

    shell.run(
        "rm -f /data/span/mqtt-bridge/shadows/hardware.json "
        "/data/span/mqtt-bridge/shadows/branches.json "
        "/data/span/mqtt-bridge/shadows/building.json",
        timeout_s=20,
    )
    emit("decommission_shadow_files_removed", message="Shadow files removed")

    shell.run("systemctl restart span-sbx-seriald", timeout_s=60)
    shell.run("systemctl start span-eve", timeout_s=60)
    emit("decommission_services_restarted", message="Services restarted")

    code, out, err = shell.run(
        "mosquitto_sub -t local/eve/data/v1/status -C 1 -W 5",
        timeout_s=20,
    )
    status_text = (out or err or "").strip()
    for bc in (top_bc_instance, bottom_bc_instance):
        if bc is None:
            continue
        shell.run(f"span-pcm mt -i {int(bc)} bc get-state", timeout_s=20)

    result = {
        "ok": True,
        "eve_status": status_text[:500],
        "breaker_instance_id": breaker_instance_id,
        "message": "Decommission complete",
    }
    emit("decommission_complete", **result)
    return result
