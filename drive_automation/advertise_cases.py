"""JAMA cases: user-max charge current advertise vs commissioned breaker.

Case 1 — set integer user-max (0 A and 6–48 A capped by breaker) and check
``advertised_current_a`` follows.

Case 2 — set user-max above breaker rating; advertised must not exceed rating.

All gateway commands go through ``GatewayShell.run`` (spanssh already applied).
Pilot Charge is set via EV Simulator HTTP (``EvClient``), not gateway SSH.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from typing import Any, Callable, Optional

from drive_automation.gateway import GatewayError, GatewayShell
from drive_automation.jama_cases import CaseIdFactory, emit_jama_case

EmitFn = Callable[..., None]


def _run_text(shell: GatewayShell, command: str, *, timeout_s: float = 60) -> str:
    code, out, err = shell.run(command, timeout_s=timeout_s)
    text = (out or err or "").strip()
    if code != 0 and not text:
        raise GatewayError(
            f"Gateway command failed (exit {code}): {command}\n{err or ''}"
        )
    return text


def read_breaker_rating_a(shell: GatewayShell, instance_id: int) -> float:
    """``span-pcm mt -i <id> cb get-state`` → amps from breaker_rating_milliamps."""
    raw = _run_text(shell, f"span-pcm mt -i {int(instance_id)} cb get-state")
    match = re.search(r"breaker_rating_milliamps:\s*([0-9]+)", raw)
    if not match:
        raise GatewayError(
            f"Could not parse breaker_rating_milliamps for instance {instance_id}. "
            f"Raw: {raw[:400]}"
        )
    return int(match.group(1)) / 1000.0


def read_user_max_charge_current_a(shell: GatewayShell) -> Optional[float]:
    raw = _run_text(
        shell, "cat /data/span/eve/user-max-charge-current", timeout_s=20
    )
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for key in (
                "user_max_charge_current_a",
                "userMaxChargeCurrentA",
                "max_charge_current_a",
            ):
                if key in data and data[key] is not None:
                    return float(data[key])
        return float(data)
    except (TypeError, ValueError, json.JSONDecodeError):
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)", raw)
        return float(match.group(1)) if match else None


def set_user_max_charge_current_a(shell: GatewayShell, amps: int) -> None:
    payload = json.dumps({"user_max_charge_current_a": int(amps)})
    cmd = (
        "mosquitto_pub -t local/eve/user-max-charge-current/intent "
        f"-m {shlex.quote(payload)} -q 1"
    )
    _run_text(shell, cmd, timeout_s=30)


def read_advertised_current_a(
    shell: GatewayShell,
    *,
    wait_sec: float = 5.0,
    topic: str = "local/eve/#",
) -> Optional[float]:
    """One-shot MQTT sample; parse ``advertised_current_a``."""
    w = max(1, int(wait_sec))
    cmd = (
        f"mosquitto_sub -h 127.0.0.1 -p 1883 -t {shlex.quote(topic)} "
        f"-v -C 1 -W {w}"
    )
    text = _run_text(shell, cmd, timeout_s=wait_sec + 15)
    match = re.search(r'"advertised_current_a"\s*:\s*([0-9]+(?:\.[0-9]+)?)', text)
    if match:
        return float(match.group(1))
    for line in reversed(text.splitlines()):
        line = line.strip()
        if "{" not in line:
            continue
        blob = line[line.find("{") :]
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "advertised_current_a" in data:
            return float(data["advertised_current_a"])
    return None


def ensure_pilot_charge(ev: Any, *, attempts: int = 3) -> dict[str, Any]:
    """Force PilotDingus Charge via EV HTTP; return a fresh pilot reading."""
    reading = ev.pilot_with_retry(attempts=attempts, delay_sec=2.0)
    if str(reading.get("pilot_mode") or "").lower() != "charge":
        ev.set_mode_with_retry("charge", attempts=attempts, delay_sec=10.0)
        time.sleep(1.0)
        reading = ev.pilot_with_retry(attempts=attempts, delay_sec=2.0)
    if str(reading.get("pilot_mode") or "").lower() != "charge":
        raise RuntimeError(
            f"Pilot not in Charge mode (got {reading.get('pilot_mode')!r})"
        )
    return reading


def advertise_setpoints_for_rating(rating_a: float) -> list[int]:
    """0 A plus representative integers in 6–48 A, capped by breaker rating."""
    rating_int = max(0, int(round(float(rating_a))))
    hi = min(48, rating_int)
    points: list[int] = [0]
    if hi >= 6:
        points.append(6)
    if hi >= 16 and 16 not in points:
        points.append(16)
    mid = (6 + hi) // 2
    if mid >= 6 and mid not in points:
        points.append(mid)
    if hi >= 6 and hi not in points:
        points.append(hi)
    return sorted(points)


def _emit_adv_case(
    emit: EmitFn,
    case_ids: Optional[CaseIdFactory],
    *,
    title: str,
    signal: str,
    expected: str,
    observed: str,
    result: str,
    evidence: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> None:
    if case_ids is not None:
        emit_jama_case(
            emit,
            case_id=case_ids.next("ADV"),
            section="Advertise limits",
            title=title,
            signal=signal,
            expected=expected,
            observed=observed,
            sources="gateway",
            result=result,
            evidence=evidence,
            metrics=metrics,
        )
    else:
        emit(
            "advertise_case",
            ok=str(result).lower() == "pass",
            title=title,
            expected=expected,
            observed=observed,
            metrics=metrics,
            message=f"{title}: {result} — {observed}",
        )


def advertise_integer_rates(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    instance_id: int,
    case_ids: Optional[CaseIdFactory] = None,
    setpoints: Optional[list[int]] = None,
    settle_sec: float = 3.0,
    tolerance_a: float = 1.0,
    restore_fraction: float = 0.8,
) -> dict[str, Any]:
    """
    Case 1: for each setpoint (0 and integers in 6–48 A ≤ breaker), publish
    user-max intent and require advertised_current_a to match.
    """
    ensure_pilot_charge(ev)
    rating_a = read_breaker_rating_a(shell, instance_id)
    targets = list(setpoints) if setpoints is not None else advertise_setpoints_for_rating(
        rating_a
    )
    # Never ask above commissioned breaker.
    targets = [int(t) for t in targets if 0 <= int(t) <= int(round(rating_a))]
    if not targets:
        targets = [0]

    results: list[dict[str, Any]] = []
    all_ok = True

    for amps in targets:
        set_user_max_charge_current_a(shell, amps)
        time.sleep(settle_sec)
        advertised = read_advertised_current_a(shell, wait_sec=5.0)
        if advertised is None:
            ok = False
            observed = f"advertised_current_a missing after user_max={amps} A"
        else:
            ok = abs(float(advertised) - float(amps)) <= float(tolerance_a)
            observed = (
                f"user_max={amps} A → advertised_current_a={advertised} A "
                f"(breaker={rating_a} A)"
            )
        all_ok = all_ok and ok
        results.append(
            {"setpoint_a": amps, "advertised_current_a": advertised, "ok": ok}
        )
        _emit_adv_case(
            emit,
            case_ids,
            title=f"Advertise user-max {amps} A",
            signal="advertised_current_a",
            expected=f"advertised_current_a ≈ {amps} A (±{tolerance_a:g})",
            observed=observed,
            result="pass" if ok else "fail",
            evidence=[
                {
                    "stream": "gateway",
                    "ts": "",
                    "label": "user-max intent",
                    "line": f"published user_max_charge_current_a={amps}",
                },
                {
                    "stream": "gateway",
                    "ts": "",
                    "label": "advertised",
                    "line": observed[:400],
                },
            ],
            metrics={
                "breaker_instance_id": instance_id,
                "breaker_rating_a": rating_a,
                "setpoint_a": amps,
                "advertised_current_a": advertised,
            },
        )

    # Leave Drive at the usual 80% draw cap for the rest of the pipeline.
    restore_a = int(round(float(rating_a) * float(restore_fraction)))
    restore_a = max(0, min(restore_a, int(round(rating_a))))
    try:
        set_user_max_charge_current_a(shell, restore_a)
        time.sleep(1.0)
    except Exception as exc:
        emit(
            "advertise_restore_warn",
            error=str(exc),
            message=f"Could not restore user-max to {restore_a} A: {exc}",
        )

    return {
        "ok": all_ok,
        "breaker_rating_a": rating_a,
        "setpoints": targets,
        "results": results,
        "restored_user_max_a": restore_a,
    }


def obeys_breaker_rating_as_max(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    instance_id: int,
    case_ids: Optional[CaseIdFactory] = None,
    overshoot_a: int = 10,
    settle_sec: float = 3.0,
    restore_fraction: float = 0.8,
) -> dict[str, Any]:
    """
    Case 2: set user-max above commissioned breaker; advertised must stay
    at or below breaker rating.
    """
    ensure_pilot_charge(ev)

    rating_a = read_breaker_rating_a(shell, instance_id)
    rating_int = int(round(rating_a))
    prior_user_max = read_user_max_charge_current_a(shell)

    over_a = rating_int + max(1, int(overshoot_a))
    set_user_max_charge_current_a(shell, over_a)
    time.sleep(settle_sec)

    advertised = read_advertised_current_a(shell, wait_sec=5.0)
    if advertised is None:
        ok = False
        observed = "advertised_current_a not seen on MQTT"
    else:
        ok = float(advertised) <= float(rating_a) + 0.01
        observed = (
            f"advertised_current_a={advertised} A after setting "
            f"user_max={over_a} A (breaker={rating_a} A)"
        )

    _emit_adv_case(
        emit,
        case_ids,
        title="Obeys breaker rating as max advertised current",
        signal="advertised_current_a vs breaker_rating",
        expected=f"advertised_current_a <= {rating_a} A after user_max={over_a}",
        observed=observed,
        result="pass" if ok else "fail",
        evidence=[
            {
                "stream": "gateway",
                "ts": "",
                "label": "cb get-state",
                "line": f"instance={instance_id} breaker_rating_a={rating_a}",
            },
            {
                "stream": "gateway",
                "ts": "",
                "label": "user-max intent",
                "line": f"published user_max_charge_current_a={over_a}",
            },
            {
                "stream": "gateway",
                "ts": "",
                "label": "advertised",
                "line": observed[:400],
            },
        ],
        metrics={
            "breaker_instance_id": instance_id,
            "breaker_rating_a": rating_a,
            "prior_user_max_a": prior_user_max,
            "published_user_max_a": over_a,
            "advertised_current_a": advertised,
        },
    )

    restore_a = int(round(float(rating_a) * float(restore_fraction)))
    restore_a = max(0, min(restore_a, rating_int))
    try:
        set_user_max_charge_current_a(shell, restore_a)
        time.sleep(1.0)
    except Exception as exc:
        emit(
            "advertise_restore_warn",
            error=str(exc),
            message=f"Could not restore user-max to {restore_a} A: {exc}",
        )

    return {
        "ok": ok,
        "breaker_rating_a": rating_a,
        "published_user_max_a": over_a,
        "advertised_current_a": advertised,
        "prior_user_max_a": prior_user_max,
        "restored_user_max_a": restore_a,
    }


def run_advertise_limit_cases(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    instance_id: int,
    case_ids: Optional[CaseIdFactory] = None,
    settle_sec: float = 3.0,
) -> dict[str, Any]:
    """Run Case 1 then Case 2; return combined summary."""
    case1 = advertise_integer_rates(
        shell,
        ev,
        emit,
        instance_id=instance_id,
        case_ids=case_ids,
        settle_sec=settle_sec,
    )
    case2 = obeys_breaker_rating_as_max(
        shell,
        ev,
        emit,
        instance_id=instance_id,
        case_ids=case_ids,
        settle_sec=settle_sec,
    )
    return {
        "ok": bool(case1.get("ok")) and bool(case2.get("ok")),
        "case1": case1,
        "case2": case2,
    }
