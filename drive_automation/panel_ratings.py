"""Panel site/conductor/feeder ratings (span-pcm) and live feeder amps (traits-cli)."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from drive_automation.commissioning import get_panel_capacity
from drive_automation.gateway import GatewayError, GatewayShell

EmitFn = Callable[..., None]


def _noop_emit(event: str, **payload: Any) -> None:
    return None

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Gateway: PANEL_ID in thing.env is the traits-cli --resource-id for that panel.
_THING_ENV_PATH = "/data/span/provisioning/thing.env"
_cached_panel_resource_id: Optional[str] = None
_cached_panel_resource_resolved = False


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def resolve_traits_resource_id(
    shell: GatewayShell,
    *,
    emit: EmitFn = _noop_emit,
    quiet: bool = False,
) -> Optional[str]:
    """Resolve traits-cli resource id (= PANEL_ID) for the connected gateway.

    Prefer ``TRAITS_FEEDER_RESOURCE_ID`` env override; otherwise read::

        cat /data/span/provisioning/thing.env   # PANEL_ID=<hex>
    """
    global _cached_panel_resource_id, _cached_panel_resource_resolved

    override = (os.environ.get("TRAITS_FEEDER_RESOURCE_ID") or "").strip()
    if override:
        return override

    if _cached_panel_resource_resolved:
        return _cached_panel_resource_id

    panel_id: Optional[str] = None
    try:
        out = shell.run_ok(f"cat {_THING_ENV_PATH}", timeout_s=20)
        for raw in (out or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == "PANEL_ID":
                panel_id = val.strip().strip('"').strip("'")
                break
    except Exception as exc:
        if not quiet:
            emit(
                "feeder_amps_error",
                error=str(exc),
                path=_THING_ENV_PATH,
                message=(
                    f"Could not read PANEL_ID from {_THING_ENV_PATH}: {exc}"
                ),
            )
        _cached_panel_resource_resolved = True
        _cached_panel_resource_id = None
        return None

    _cached_panel_resource_resolved = True
    _cached_panel_resource_id = panel_id or None
    if not panel_id and not quiet:
        emit(
            "feeder_amps_error",
            path=_THING_ENV_PATH,
            message=(
                f"No PANEL_ID in {_THING_ENV_PATH} — set TRAITS_FEEDER_RESOURCE_ID "
                "or fix gateway provisioning"
            ),
        )
    return _cached_panel_resource_id


def clear_traits_resource_id_cache() -> None:
    """Force re-read of thing.env on next feeder sample (tests / reconnect)."""
    global _cached_panel_resource_id, _cached_panel_resource_resolved
    _cached_panel_resource_id = None
    _cached_panel_resource_resolved = False


def _parse_milliamps(text: str, key: str) -> Optional[int]:
    match = re.search(rf"{key}\s*[:=]\s*(\d+)", text, re.I)
    return int(match.group(1)) if match else None


def _parse_amps(text: str, key: str) -> Optional[float]:
    match = re.search(rf"{key}\s*[:=]\s*([\d.]+)", text, re.I)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def read_panel_ratings(
    shell: GatewayShell,
    *,
    emit: EmitFn = _noop_emit,
) -> dict[str, Any]:
    """Read site/conductor/busbar/feeder ratings via ``span-pcm mt panel get`` only.

    No set-/update- writes — Phase 4 is display + limit calculation from live panel state.
    """
    capacity = get_panel_capacity(shell, emit=emit)
    try:
        raw = shell.run_ok("span-pcm mt panel get", timeout_s=60)
    except GatewayError:
        raw = str(capacity.get("raw") or "")

    site_a = capacity.get("site_utility_rating_a")
    busbar_a = capacity.get("busbar_rating_a")

    cond_ma = (
        _parse_milliamps(raw, "conductor_rating_milliamps")
        or _parse_milliamps(raw, "feed_conductor_rating_milliamps")
    )
    conductor_a = (cond_ma / 1000.0) if cond_ma is not None and cond_ma > 0 else None

    feeder_a = (
        _parse_amps(raw, "breaker_rating_amp")
        or _parse_amps(raw, "feeder_breaker_rating_a")
        or _parse_amps(raw, "feeder_breaker_rating_amp")
        or (
            (_parse_milliamps(raw, "feeder_breaker_rating_milliamps") or 0) / 1000.0
            or None
        )
    )
    if isinstance(feeder_a, float) and feeder_a <= 0:
        feeder_a = None

    candidates = [v for v in (site_a, conductor_a, busbar_a) if v is not None and v > 0]
    limit_a = min(candidates) if candidates else None
    throttle_threshold_a = (0.8 * limit_a) if limit_a is not None else None

    result: dict[str, Any] = {
        "site_utility_rating_a": site_a,
        "site_utility_rating_ma": (
            int(round(float(site_a) * 1000)) if site_a is not None else None
        ),
        "conductor_rating_a": conductor_a,
        "conductor_rating_ma": int(cond_ma) if cond_ma is not None else None,
        "busbar_rating_a": busbar_a,
        "feeder_breaker_rating_a": feeder_a,
        "limit_a": limit_a,
        "throttle_threshold_a": throttle_threshold_a,
        "commissioning_status": capacity.get("commissioning_status"),
    }
    emit(
        "panel_ratings",
        **result,
        message=(
            f"Panel ratings (read) — site={site_a} A, conductor={conductor_a} A, "
            f"busbar={busbar_a} A, feeder_breaker={feeder_a} A, "
            f"limit=min(...)={limit_a} A, 80% threshold={throttle_threshold_a} A"
        ),
    )
    return result

# Back-compat alias — callers that still pass write targets are ignored.


def apply_and_read_panel_ratings(
    shell: GatewayShell,
    *,
    site_utility_ma: int = 125000,
    conductor_ma: int = 100000,
    feeder_breaker_a: float = 100.0,
    emit: EmitFn = _noop_emit,
) -> dict[str, Any]:
    """Deprecated alias for :func:`read_panel_ratings` (read-only; args ignored)."""
    return read_panel_ratings(shell, emit=emit)


def _ma_avg(node: dict[str, Any], key: str = "currentRmsMilliamps") -> Optional[float]:
    try:
        return float(node.get(key, {}).get("avg", 0)) / 1000.0
    except (TypeError, ValueError, AttributeError):
        return None


def _extract_feeder_from_traits_blob(blob: str) -> Optional[dict[str, float]]:
    """Parse traits-cli TraitNotify JSON — prefer ``threeWire`` (lab-verified).

    Working gateway script uses ``instancePowerStatsSamples[].threeWire``, not
    ``panelPower.feeder``. When multiple instances appear, pick the max combined A
    (feeder / mains tends to be the largest among branch samples).
    """
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None

    best: Optional[dict[str, float]] = None
    metrics = (data.get("TraitNotify") or {}).get("metrics") or []
    for m in metrics:
        payload = ((m.get("payload") or {}).get("payload")) or {}
        samples = payload.get("resourcePowerSamples") or []
        for res in samples:
            for inst in res.get("instancePowerStatsSamples") or []:
                # Lab-verified path
                tw = inst.get("threeWire")
                if isinstance(tw, dict):
                    combined = tw.get("combined") or {}
                    line_a = tw.get("lineA") or {}
                    line_b = tw.get("lineB") or {}
                    c = _ma_avg(combined)
                    a = _ma_avg(line_a)
                    b = _ma_avg(line_b)
                    w = _ma_avg(combined, "realPowerMilliwatts")
                    if c is None:
                        continue
                    cand = {
                        "combined_a": float(c),
                        "line_a_a": float(a if a is not None else 0.0),
                        "line_b_a": float(b if b is not None else 0.0),
                        "power_w": float(w if w is not None else 0.0),
                        "source": "threeWire",
                    }
                    if best is None or cand["combined_a"] > best["combined_a"]:
                        best = cand
                    continue
                # Legacy fallback (older trait shape)
                if "panelPower" in inst:
                    f = (inst.get("panelPower") or {}).get("feeder") or {}
                    try:
                        cand = {
                            "combined_a": float(
                                f["combined"]["currentRmsMilliamps"]["avg"]
                            )
                            / 1000.0,
                            "line_a_a": float(
                                f["lineA"]["currentRmsMilliamps"]["avg"]
                            )
                            / 1000.0,
                            "line_b_a": float(
                                f["lineB"]["currentRmsMilliamps"]["avg"]
                            )
                            / 1000.0,
                            "power_w": float(
                                f["combined"]["realPowerMilliwatts"]["avg"]
                            )
                            / 1000.0,
                            "source": "panelPower.feeder",
                        }
                    except (KeyError, TypeError, ValueError):
                        continue
                    if best is None or cand["combined_a"] > best["combined_a"]:
                        best = cand
    return best


def sample_feeder_amps(
    shell: GatewayShell,
    *,
    timeout_s: float = 45.0,
    emit: EmitFn = _noop_emit,
    quiet: bool = False,
) -> Optional[dict[str, float]]:
    """One-shot traits-cli watch sample for live feeder current.

    Resource id is required by current traits-cli builds. Resolution order:

    1. ``TRAITS_FEEDER_RESOURCE_ID`` env override
    2. ``PANEL_ID`` from ``/data/span/provisioning/thing.env`` on the gateway

    Lab recipe (short flags)::

        traits-cli -r <PANEL_ID> watch -v 1 -p 1 -t 26 -i 401

    Over SSH without a TTY, traits-cli often **block-buffers** stdout, so we wrap
    with ``stdbuf -oL`` (line-buffered).

    When ``quiet=True``, timeout/start failures do not emit ``feeder_amps_error``
    (background watcher uses this to avoid flooding the dashboard log).
    """
    resource = resolve_traits_resource_id(shell, emit=emit, quiet=quiet)
    version = (os.environ.get("TRAITS_FEEDER_VERSION") or "1").strip()
    page = (os.environ.get("TRAITS_FEEDER_PAGE") or "1").strip()
    trait = (os.environ.get("TRAITS_FEEDER_TRAIT") or "26").strip()
    instance = (os.environ.get("TRAITS_FEEDER_INSTANCE") or "401").strip()
    if not resource:
        if not quiet:
            emit(
                "feeder_amps_error",
                message=(
                    "traits-cli requires --resource-id (PANEL_ID). "
                    f"Could not resolve from env or {_THING_ENV_PATH}"
                ),
            )
        return None

    # -r PANEL_ID is required on current gateway traits-cli builds.
    watch = (
        f"traits-cli -r {resource} watch -v {version} -p {page} "
        f"-t {trait} -i {instance}"
    )
    # Line-buffer stdout; keep stderr so CLI errors are visible in the sample.
    cmd = f"stdbuf -oL -eL {watch} 2>&1"
    if not quiet:
        emit(
            "feeder_amps_probe",
            resource_id=resource,
            cmd=watch,
            message=f"Sampling feeder amps via traits-cli -r {resource} -t {trait} -i {instance}",
        )
    try:
        proc = shell.popen(cmd)
    except Exception as exc:
        if not quiet:
            emit(
                "feeder_amps_error",
                error=str(exc),
                cmd=cmd,
                resource_id=resource,
                message=f"traits-cli watch failed to start: {exc}",
            )
        return None

    msg_lines: list[str] = []
    capturing = False
    # Lab script path: accumulate message-column JSON by brace depth (no table chrome).
    json_chunks: list[str] = []
    brace_depth = 0
    collecting_json = False
    deadline = time.monotonic() + float(timeout_s)
    sample: Optional[dict[str, float]] = None
    raw_lines = 0
    blocks_seen = 0
    parse_fail = 0
    non_pipe_snip: list[str] = []
    try:
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            raw_lines += 1
            line = _strip_ansi(line)

            # Fast path: whole TraitNotify JSON on one line (no table chrome).
            if "TraitNotify" in line and "{" in line:
                start = line.find("{")
                if start >= 0:
                    parsed = _extract_feeder_from_traits_blob(line[start:])
                    if parsed is not None:
                        sample = parsed
                        break

            # Lab-verified: message column of traits-cli table (| ... | json |).
            if line.startswith("|") and not line.startswith("+-") and not line.startswith("+="):
                parts = line.split("|")
                if len(parts) >= 4:
                    cell = parts[3].strip()
                    if cell:
                        if not collecting_json and "{" in cell:
                            collecting_json = True
                            json_chunks = []
                            brace_depth = 0
                        if collecting_json:
                            json_chunks.append(cell)
                            brace_depth += cell.count("{") - cell.count("}")
                            if brace_depth <= 0 and len(json_chunks) >= 1:
                                collecting_json = False
                                blob = " ".join(json_chunks).strip()
                                json_chunks = []
                                parsed = _extract_feeder_from_traits_blob(blob)
                                if parsed is not None:
                                    sample = parsed
                                    break
                                parse_fail += 1

            # Alternate: header separator starts a message block
            if re.match(r"^\+=+\+=+\+=+\+", line):
                capturing = True
                msg_lines = []
                continue
            # End of block — join message column and parse JSON
            if capturing and re.match(r"^\+-+\+-+\+-+\+", line):
                capturing = False
                blocks_seen += 1
                raw_json = " ".join(msg_lines).strip()
                msg_lines = []
                if not raw_json:
                    continue
                parsed = _extract_feeder_from_traits_blob(raw_json)
                if parsed is None:
                    parse_fail += 1
                    continue
                sample = parsed
                break
            if capturing:
                parts = line.split("|")
                if len(parts) >= 4:
                    content = parts[3].strip()
                    if content:
                        msg_lines.append(content)
                elif "TraitNotify" in line and "{" in line:
                    start = line.find("{")
                    msg_lines.append(line[start:])
            elif not line.startswith("|") and len(non_pipe_snip) < 8:
                non_pipe_snip.append(line.strip()[:160])
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    if sample is None:
        hint = ""
        if raw_lines == 0:
            hint = (
                " — no stdout lines (often SSH pipe buffering or traits-cli not "
                "publishing; cmd uses stdbuf -oL). Verify on gateway: "
                f"`{watch}`"
            )
        elif blocks_seen == 0 and parse_fail == 0:
            hint = (
                " — got stdout but no TraitNotify/threeWire JSON parsed "
                f"(resource={resource}, -t {trait} -i {instance})"
            )
        elif parse_fail:
            hint = (
                " — TraitNotify seen but threeWire/feeder JSON not found"
            )
        if non_pipe_snip:
            hint += f" · cli: {'; '.join(non_pipe_snip)[:200]}"
        if not quiet:
            emit(
                "feeder_amps_error",
                message=f"No feeder amps sample from traits-cli within timeout{hint}",
                cmd=watch,
                resource_id=resource,
                raw_lines=raw_lines,
                blocks_seen=blocks_seen,
                parse_fail=parse_fail,
                detail="; ".join(non_pipe_snip)[:300] if non_pipe_snip else None,
            )
        return None
    emit(
        "feeder_amps",
        combined_a=sample["combined_a"],
        line_a_a=sample["line_a_a"],
        line_b_a=sample["line_b_a"],
        power_w=sample.get("power_w"),
        source=sample.get("source"),
        resource_id=resource,
        message=(
            f"Live feeder {sample['combined_a']:.3f} A "
            f"(L1={sample['line_a_a']:.3f} A, L2={sample['line_b_a']:.3f} A)"
        ),
    )
    return sample


class FeederAmpsWatcher:
    """Background feeder amps poller for dashboard meter updates."""

    def __init__(
        self,
        shell: GatewayShell,
        emit: EmitFn,
        *,
        interval_sec: float = 5.0,
        error_emit_interval_sec: float = 120.0,
    ) -> None:
        self._shell = shell
        self._emit = emit
        self._interval = float(interval_sec)
        self._error_emit_interval = float(error_emit_interval_sec)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_sample: Optional[dict[str, float]] = None
        self._last_error_emit = 0.0
        self._consecutive_misses = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="feeder-amps-watch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        # Short poll timeout so a hung traits-cli cannot block longer than the
        # interval; quiet=True avoids spamming feeder_amps_error every cycle.
        poll_timeout = min(8.0, max(3.0, self._interval + 2.0))
        while not self._stop.is_set():
            try:
                sample = sample_feeder_amps(
                    self._shell,
                    timeout_s=poll_timeout,
                    emit=self._emit,
                    quiet=True,
                )
                if sample is not None:
                    self.last_sample = sample
                    self._consecutive_misses = 0
                else:
                    self._consecutive_misses += 1
                    now = time.monotonic()
                    if (
                        self._consecutive_misses == 1
                        or (now - self._last_error_emit) >= self._error_emit_interval
                    ):
                        self._emit(
                            "feeder_amps_error",
                            misses=self._consecutive_misses,
                            message=(
                                "No feeder amps sample from traits-cli "
                                f"(background poll; next log in "
                                f"{self._error_emit_interval:g}s if still failing)"
                            ),
                        )
                        self._last_error_emit = now
            except Exception as exc:
                now = time.monotonic()
                if (now - self._last_error_emit) >= self._error_emit_interval:
                    self._emit(
                        "feeder_amps_error",
                        error=str(exc),
                        message=f"Feeder amps poll error: {exc}",
                    )
                    self._last_error_emit = now
            self._stop.wait(self._interval)
