"""Load-bank relay selection for target current (subset-sum per channel)."""

from __future__ import annotations

CHANNELS: dict[int, list[str]] = {
    1: ["100", "200", "400", "800", "1kW"],
    2: ["100", "200", "400", "800", "1kW"],
    3: ["100", "200", "400", "800", "1.5kW", "2kW"],
    4: ["100", "200", "400", "800", "1.5kW", "2kW"],
}

LOAD_WATTS: dict[str, int] = {
    "100": 100,
    "200": 200,
    "400": 400,
    "800": 800,
    "1kW": 1000,
    "1.5kW": 1500,
    "2kW": 2000,
}

CHANNEL_VOLTAGE_V: dict[int, int] = {1: 120, 2: 120, 3: 240, 4: 240}

DEFAULT_VOLTAGES: dict[int, str] = {1: "120", 2: "120", 3: "240", 4: "240"}

ALLOWED_VOLTAGES = ("120", "240")


def normalize_channel_voltages(
    voltages: dict[int, str] | None = None,
) -> dict[int, str]:
    """Return Ch1–4 voltages as ``\"120\"`` / ``\"240\"`` strings."""
    out = dict(DEFAULT_VOLTAGES)
    if not voltages:
        return out
    for ch in (1, 2, 3, 4):
        raw = voltages.get(ch)
        if raw is None:
            continue
        text = str(raw).strip().upper().replace("V", "")
        if text in ALLOWED_VOLTAGES:
            out[ch] = text
    return out


def channel_voltage_v(
    channel: int,
    voltages: dict[int, str] | None = None,
) -> int:
    normalized = normalize_channel_voltages(voltages)
    return int(normalized.get(channel, DEFAULT_VOLTAGES[channel]))


def select_loads_for_channel(
    channel: int,
    target_amps: float,
    *,
    voltage_v: int | None = None,
) -> tuple[set[str], int]:
    """Pick load steps for one channel without exceeding target amps.

    Undershoot-only (monotonic): never selects a combination above ``target_amps``.
    When the exact amps are achievable, returns that combo (e.g. 44 A → 44 A).
    """
    voltage = int(voltage_v) if voltage_v is not None else CHANNEL_VOLTAGE_V[channel]
    target_w = max(0.0, float(target_amps)) * voltage
    steps = CHANNELS[channel]
    elements = [(load, LOAD_WATTS[load]) for load in steps]
    achievable: dict[int, list[int]] = {0: []}
    for idx, (_, watts) in enumerate(elements):
        for prev_sum in list(achievable.keys()):
            new_sum = prev_sum + watts
            if new_sum <= target_w and new_sum not in achievable:
                achievable[new_sum] = achievable[prev_sum] + [idx]
    if not achievable:
        return set(), 0
    best_w = max(achievable)
    chosen = {elements[i][0] for i in achievable[best_w]}
    return chosen, best_w


def loads_to_amps(
    loads_by_channel: dict[int, set[str]],
    *,
    channel_voltages: dict[int, str] | None = None,
) -> float:
    """Convert selected relay steps to approximate panel amps."""
    total = 0.0
    for ch, steps in loads_by_channel.items():
        volts = float(channel_voltage_v(int(ch), channel_voltages))
        if volts <= 0:
            continue
        for name in steps:
            watts = float(LOAD_WATTS.get(str(name), 0))
            if watts > 0:
                total += watts / volts
    return round(total, 2)


def max_channel_amps(
    channel: int,
    *,
    channel_voltages: dict[int, str] | None = None,
) -> float:
    """Maximum amps one LB2 channel can deliver with all steps on."""
    volts = float(channel_voltage_v(channel, channel_voltages))
    if volts <= 0:
        return 0.0
    watts = float(sum(LOAD_WATTS[name] for name in CHANNELS.get(channel, [])))
    return watts / volts


def max_panel_amps(
    panel_channels: tuple[int, ...],
    *,
    channel_voltages: dict[int, str] | None = None,
) -> float:
    """Hardware ceiling for the configured panel channels / voltages."""
    return round(
        sum(
            max_channel_amps(ch, channel_voltages=channel_voltages)
            for ch in panel_channels
        ),
        2,
    )


def _fill_residual_amps(
    selected: dict[int, set[str]],
    *,
    total_amps: float,
    panel_channels: tuple[int, ...],
    channel_voltages: dict[int, str] | None = None,
) -> dict[int, set[str]]:
    """Add unused steps while keeping total applied amps ≤ ``total_amps``.

    Improves precision after an even split without ever overshooting the target.
    """
    budget = max(0.0, float(total_amps))
    out = {ch: set(steps) for ch, steps in selected.items()}
    # Prefer larger watt steps first so residual fills with fewer toggles.
    candidates: list[tuple[int, str, float]] = []
    for ch in panel_channels:
        volts = float(channel_voltage_v(ch, channel_voltages))
        if volts <= 0:
            continue
        for name in CHANNELS.get(ch, []):
            if name in out.get(ch, set()):
                continue
            amps = float(LOAD_WATTS[name]) / volts
            candidates.append((ch, name, amps))
    candidates.sort(key=lambda row: row[2], reverse=True)

    changed = True
    while changed:
        changed = False
        current = loads_to_amps(out, channel_voltages=channel_voltages)
        residual = budget - current
        if residual <= 1e-6:
            break
        for ch, name, amps in candidates:
            if name in out.get(ch, set()):
                continue
            if amps <= residual + 1e-9:
                out.setdefault(ch, set()).add(name)
                changed = True
                break
    return out


def select_loads_panel_total(
    total_amps: float,
    panel_channels: tuple[int, ...] = (1, 2, 3, 4),
    *,
    channel_voltages: dict[int, str] | None = None,
) -> dict[int, set[str]]:
    """Select panel loads for ``total_amps`` without ever exceeding it.

    Even-split first pass, then residual fill. Steady-state can still hit the
    exact target when a step combination exists (e.g. 44 A → 44 A); otherwise
    the largest achievable value ≤ target is used (never an overshoot path).
    """
    if not panel_channels:
        return {}
    target = max(0.0, float(total_amps))
    if target <= 0.0:
        return {ch: set() for ch in panel_channels}
    per_ch = target / len(panel_channels)
    selected = {
        ch: select_loads_for_channel(
            ch,
            per_ch,
            voltage_v=channel_voltage_v(ch, channel_voltages),
        )[0]
        for ch in panel_channels
    }
    return _fill_residual_amps(
        selected,
        total_amps=target,
        panel_channels=panel_channels,
        channel_voltages=channel_voltages,
    )


def parse_load_state(loads: dict[str, bool]) -> dict[int, set[str]]:
    """Parse API loads dict like ``3_1kW`` → channel → set of step names."""
    out: dict[int, set[str]] = {ch: set() for ch in CHANNELS}
    for key, on in loads.items():
        if not on:
            continue
        parts = key.split("_", 1)
        if len(parts) != 2:
            continue
        try:
            ch = int(parts[0])
        except ValueError:
            continue
        if ch in out:
            out[ch].add(parts[1])
    return out


def all_load_keys(channel: int) -> list[str]:
    return list(CHANNELS[channel])
