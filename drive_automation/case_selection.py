"""Selectable JAMA case IDs for the dedicated jama-test pipeline.

``DRIVE_CASES`` (comma-separated) or ``TestParams.jama_cases``:
  empty / ``all`` → run every known case
  otherwise → only listed aliases

Aliases (stable; use these in config, not long titles):
  advertise_rates   — Case 1: user-max 0 / 6–48 A samples
  obeys_breaker     — Case 2: advertise never exceeds breaker
  comms_loss        — Case 6: seriald stop → 6 A → restore
  comms_required    — Case 7: comms-required + relay cycle
  no_comms_boot     — Case 8: no SBX on boot (Connect)
  set_charge_rate_0 — Case 10: user-max 0 → charge_rate 0
  power_reset       — Case 11: relay power cycle → SBX back
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

# Canonical IDs used in code + config.
ALL_JAMA_CASES: tuple[str, ...] = (
    "advertise_rates",
    "obeys_breaker",
    "comms_loss",
    "comms_required",
    "no_comms_boot",
    "set_charge_rate_0",
    "power_reset",
)

# Human titles / typos → canonical id
_ALIASES: dict[str, str] = {
    "advertise_rates": "advertise_rates",
    "advertise": "advertise_rates",
    "case1": "advertise_rates",
    "1": "advertise_rates",
    "obeys_breaker": "obeys_breaker",
    "obeys_breaker_rating": "obeys_breaker",
    "case2": "obeys_breaker",
    "2": "obeys_breaker",
    "comms_loss": "comms_loss",
    "comms_loss_throttle": "comms_loss",
    "case6": "comms_loss",
    "6": "comms_loss",
    "comms_required": "comms_required",
    "comms-required": "comms_required",
    "case7": "comms_required",
    "7": "comms_required",
    "no_comms_boot": "no_comms_boot",
    "no_comms_on_boot": "no_comms_boot",
    "case8": "no_comms_boot",
    "8": "no_comms_boot",
    "set_charge_rate_0": "set_charge_rate_0",
    "set_charge_rate": "set_charge_rate_0",
    "case9": "set_charge_rate_0",
    "case10": "set_charge_rate_0",
    "9": "set_charge_rate_0",
    "10": "set_charge_rate_0",
    "power_reset": "power_reset",
    "sbx_power_reset": "power_reset",
    "case11": "power_reset",
    "11": "power_reset",
}

CASE_LABELS: dict[str, str] = {
    "advertise_rates": "Advertise user-max rates (0 / 6–48 A)",
    "obeys_breaker": "Obeys breaker rating as max",
    "comms_loss": "Comms loss throttle down (seriald → 6 A)",
    "comms_required": "Comms-required + relay cycle",
    "no_comms_boot": "No comms on boot (Connect)",
    "set_charge_rate_0": "Set charge rate to 0 A",
    "power_reset": "SBX comms on power reset",
}


def normalize_case_id(raw: str) -> Optional[str]:
    key = str(raw or "").strip().lower().replace(" ", "_")
    if not key:
        return None
    return _ALIASES.get(key)


def parse_cases_csv(raw: str) -> Optional[frozenset[str]]:
    """Return frozenset of case ids, or None meaning 'all'."""
    text = str(raw or "").strip()
    if not text or text.lower() in ("all", "*", "default"):
        return None
    out: set[str] = set()
    for part in text.replace(";", ",").split(","):
        cid = normalize_case_id(part)
        if cid:
            out.add(cid)
    return frozenset(out) if out else None


def cases_from_env(env_name: str = "DRIVE_CASES") -> Optional[frozenset[str]]:
    return parse_cases_csv(os.environ.get(env_name, ""))


def resolve_jama_cases(
    selected: Optional[Iterable[str]] = None,
) -> frozenset[str]:
    """Effective set of cases to run (never empty — defaults to all)."""
    if selected is None:
        from_env = cases_from_env()
        if from_env is None:
            return frozenset(ALL_JAMA_CASES)
        return from_env
    parsed = parse_cases_csv(",".join(str(s) for s in selected))
    if parsed is None:
        return frozenset(ALL_JAMA_CASES)
    return parsed


def case_enabled(cases: frozenset[str], case_id: str) -> bool:
    return case_id in cases
