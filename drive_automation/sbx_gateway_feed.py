"""Mirror gateway SBX traffic into the dashboard Modbus log.

Wall FTDI sniffing is often silent on Drive cells (no RS-485 tap on the bus).
Manual "comms" that work use gateway ``span-sbx-seriald`` / eve MQTT. This feed
writes parseable ``MsgDriveStatus ... charge_rate_a=N`` lines into the same
``Modbus logs_*.txt`` file the pipeline already reads.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_JOURNAL_SKIP = re.compile(
    r"queueEventExtDrop|requestStats|mqttMessageRequestTimeout",
    re.I,
)


def _keep_journal_line(text: str) -> bool:
    """Keep seriald errors / charge_rate; drop MQTT queue spam that floods the UI."""
    if _CHARGE_RE.search(text):
        return True
    lower = text.lower()
    if "error" in lower or "warn" in lower or "commsloss" in lower:
        return True
    return not bool(_JOURNAL_SKIP.search(text))

_WRITE_LOCK = threading.Lock()


def _ts() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S") + f":{now.microsecond // 1000:03d}"


def _latest_modbus(session_dir: Path) -> Optional[Path]:
    matches = sorted(
        session_dir.glob("Modbus logs_*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _append(path: Path, line: str) -> None:
    with _WRITE_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            if not line.endswith("\n"):
                fh.write("\n")


def _status_line(*, source: str, charging_state: str, charge_rate_a: float) -> str:
    state = (charging_state or "UNKNOWN").replace(" ", "_")
    return (
        f"{_ts()} | {source} | MsgDriveStatus charging_state={state} "
        f"charge_rate_a={int(charge_rate_a)} | RX\n"
    )


def _resolve_log_path(session_dir: Path, stop: threading.Event) -> Path:
    deadline = time.monotonic() + 45.0
    while not stop.is_set() and time.monotonic() < deadline:
        found = _latest_modbus(session_dir)
        if found is not None:
            return found
        time.sleep(0.4)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = session_dir / f"Modbus logs_{stamp}.txt"
    _append(
        path,
        f"=== SBX (Drive) Log Started : {datetime.now().isoformat()} ===\n"
        "=== Source              : gateway MQTT / span-sbx-seriald "
        "(USB silent fallback) ===\n"
        "=== Format             : TIMESTAMP | SOURCE | DESCRIPTION | DIRECTION ===\n\n",
    )
    return path


def run_gateway_sbx_mirror(session_dir: Path, stop: threading.Event) -> None:
    """Append gateway MQTT (+ journal) SBX status into Modbus logs."""
    use_gateway = (os.environ.get("USE_GATEWAY") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not use_gateway:
        return
    enabled = (os.environ.get("DRIVE_SBX_GATEWAY_FEED") or "1").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        return

    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = _resolve_log_path(session_dir, stop)
    if stop.is_set():
        return

    _append(
        log_path,
        f"=== gateway_feed        : mirroring span-sbx / eve MQTT into this file "
        f"({datetime.now().isoformat()}) ===\n",
    )

    try:
        from drive_automation.gateway import build_gateway_shell_from_env
        from drive_automation.panel_observe import PanelObserver
    except Exception as exc:
        _append(log_path, f"=== ERROR: gateway feed import failed: {exc} ===\n")
        return

    try:
        shell = build_gateway_shell_from_env()
        observer = PanelObserver(shell)
    except Exception as exc:
        _append(log_path, f"=== ERROR: gateway shell failed: {exc} ===\n")
        return

    stats = {"mqtt_hits": 0, "journal_hits": 0, "last_cr": None}

    def mqtt_loop() -> None:
        last_cr: Optional[float] = None
        while not stop.is_set():
            try:
                snap = observer.poll_status()
            except Exception as exc:
                _append(log_path, f"=== WARN: MQTT poll failed: {exc} ===\n")
                time.sleep(2.0)
                continue
            if snap is not None and snap.charge_rate_a is not None:
                cr = float(snap.charge_rate_a)
                if last_cr is None or abs(cr - last_cr) >= 0.01:
                    last_cr = cr
                    stats["last_cr"] = cr
                    stats["mqtt_hits"] = int(stats["mqtt_hits"]) + 1
                    _append(
                        log_path,
                        _status_line(
                            source="gateway-mqtt",
                            charging_state=snap.charging_state or "UNKNOWN",
                            charge_rate_a=cr,
                        ),
                    )
            time.sleep(1.0)

    def journal_loop() -> None:
        try:
            proc = shell.popen(
                "journalctl -u span-sbx-seriald -f -n 80 --no-pager -o cat"
            )
        except Exception as exc:
            _append(
                log_path,
                f"=== WARN: journalctl span-sbx-seriald not started: {exc} ===\n",
            )
            return
        while not stop.is_set():
            try:
                if proc.poll() is not None:
                    break
                line = ""
                if proc.stdout is not None:
                    line = proc.stdout.readline()
            except Exception:
                break
            if not line:
                time.sleep(0.05)
                continue
            text = (
                line if isinstance(line, str) else line.decode("utf-8", errors="replace")
            ).rstrip()
            if not text:
                continue
            if not _keep_journal_line(text):
                continue
            stats["journal_hits"] = int(stats["journal_hits"]) + 1
            m = _CHARGE_RE.search(text)
            if m:
                cr = float(m.group(1))
                stats["last_cr"] = cr
                _append(
                    log_path,
                    _status_line(
                        source="gateway-journal",
                        charging_state="FROM_JOURNAL",
                        charge_rate_a=cr,
                    ),
                )
            else:
                _append(log_path, f"{_ts()} | gateway-journal | {text} | RX\n")
        try:
            proc.terminate()
        except Exception:
            pass

    threads = [
        threading.Thread(target=mqtt_loop, name="sbx-gw-mqtt", daemon=True),
        threading.Thread(target=journal_loop, name="sbx-gw-journal", daemon=True),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    _append(
        log_path,
        f"=== gateway_feed ended mqtt_hits={stats['mqtt_hits']} "
        f"journal_hits={stats['journal_hits']} ===\n",
    )
