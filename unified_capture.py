#!/usr/bin/env python3
"""
unified_capture.py — CAN Bus + Drive SBX Unified Log Capture Tool
═════════════════════════════════════════════════════════════════
SSH into a lab cell and simultaneously capture:
  • CAN bus capture  via spanbus logger (PEAK PCAN-USB FD)
  • SBX / "Modbus" logs via picocom on /dev/ttyUSB* (variable PayloadLen)

Drive differs from Lucid: serial frames are SBX, not fixed 8-byte Modbus RTU.
Log filename stays ``Modbus logs_*.txt`` so the Lucid-style dashboard UI works.

Output folder layout
────────────────────
  <output_dir>/<user>@<host>/
    CAN bus logs_<timestamp>.txt     <- all spanbus output
    CAN bus capture_<timestamp>.txt  <- filtered: MID_RELAY / DER / NFT states
    Modbus logs_<timestamp>.txt      <- decoded SBX frames + UNDECODED junk bytes

Usage
-----
  python3 unified_logger.py capture -h 10.91.0.96 -u systems
  python3 unified_logger.py capture -h 10.91.0.96 -u systems -o ./lab_logs
  python3 unified_logger.py capture -h 10.91.0.96 -u systems --duration 120

Press Ctrl+C to stop both capture streams at any time.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import inspect
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import paramiko

# =========================================================================
# CONFIGURATION  -- adjust if your remote environment differs
# =========================================================================
PCAN_DEVICE_STRING = "PEAK System PCAN-USB FD"
SPANBUS_COMMAND    = "spanbus logger -i socketcan -d can0"
# Drive SBX RS-485 baud — sbx-ctl defaults to 57600 (NOT Lucid Modbus 115200).
MODBUS_BAUD        = int(os.environ.get("DRIVE_SBX_BAUD") or "57600")
SSH_PORT           = 22
SSH_TIMEOUT        = 10   # seconds for initial connection

# CAN payload types that are written to the filtered capture file
CAN_FILTER_TYPES = [
    "PAYLOAD_MID_RELAY_STATE",
    "PAYLOAD_DER_LANDED_CIRCUIT_CONTROL_STATE",
    "PAYLOAD_NFT_LANDED_CIRCUIT_CONTROL_STATE",
]

# =========================================================================
# TIMESTAMP HELPERS
# =========================================================================


def ts() -> str:
    """YYYY-MM-DD HH:MM:SS  -- used in log lines"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts_file() -> str:
    """YYYYMMDD_HHMMSS  -- used in file names"""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# =========================================================================
# SSH HELPERS
# =========================================================================


def _sh(s: str) -> str:
    """Wrap a string in single quotes safe for:  bash -lc '<s>'"""
    return "'" + s.replace("'", "'\\''") + "'"


def _local_ssh_host(host: str) -> str:
    """Use loopback when capturing on the same wall as the dashboard.

    systemd has no SSH agent; paramiko to the LAN IP often stalls. Loopback
    still talks to local sshd, but with a short banner/auth timeout.
    """
    raw = (host or "").strip()
    if not raw:
        return raw
    names = {"127.0.0.1", "localhost", "::1"}
    try:
        names.add(socket.gethostname())
        names.add(socket.getfqdn())
        names.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    try:
        ips = subprocess.check_output(
            ["hostname", "-I"], text=True, timeout=1
        ).split()
        names.update(ip.strip() for ip in ips if ip.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return "127.0.0.1" if raw in names else raw


def connect_ssh(username: str, host: str, password: str) -> paramiko.SSHClient:
    """Open and return an authenticated paramiko SSH connection."""
    host = _local_ssh_host(host)
    print(f"\n[*] Connecting to {username}@{host}:{SSH_PORT} ...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        # Wall paramiko may be older than 3.x (no gss_auth kwarg).
        kwargs: dict = {
            "hostname": host,
            "port": SSH_PORT,
            "username": username,
            "password": password or None,
            "timeout": SSH_TIMEOUT,
            "allow_agent": False,
            "look_for_keys": True,
        }
        accepted = inspect.signature(client.connect).parameters
        for key, value in (
            ("banner_timeout", SSH_TIMEOUT),
            ("auth_timeout", SSH_TIMEOUT),
            ("gss_auth", False),
            ("gss_kex", False),
        ):
            if key in accepted:
                kwargs[key] = value
        client.connect(**kwargs)
        print(f"[OK] Connected to {host}")
        return client
    except paramiko.AuthenticationException as exc:
        print("[X] Authentication failed. Check your username / password.")
        raise ConnectionError(
            "Authentication failed. Check your username / password."
        ) from exc
    except Exception as exc:
        print(f"[X] Connection error: {exc}")
        raise ConnectionError(f"SSH connection error: {exc}") from exc


def run_remote(client: paramiko.SSHClient, command: str) -> str:
    """
    Run a command on the remote host inside a LOGIN shell and return
    combined stdout + stderr as a decoded string.

    KEY FIX: wrapping in  bash -lc '...'  sources /etc/profile and
    ~/.bashrc so tools installed outside /usr/bin (spanbus, pio,
    picocom ...) are on PATH even though paramiko does NOT start a
    login shell by default.
    """
    login_cmd = f"bash -lc {_sh(command)}"
    _, stdout, stderr = client.exec_command(login_cmd)
    return stdout.read().decode(errors="replace") + stderr.read().decode(errors="replace")


def resolve_tool(client: paramiko.SSHClient, name: str) -> Optional[str]:
    """
    Locate `name` on the remote host with `which`.
    Returns the full path, or None with a printed warning if not found.
    """
    path = run_remote(client, f"which {name} 2>/dev/null").strip()
    if path and not path.startswith("bash:") and not path.startswith("which:"):
        return path
    print(f"[!] WARNING: '{name}' not found on remote PATH.")
    return None


def open_channel(client: paramiko.SSHClient, command: str) -> paramiko.Channel:
    """
    Open a new paramiko PTY channel and run `command` inside a login
    shell so the remote user's full PATH is loaded.
    """
    transport = client.get_transport()
    channel   = transport.open_session()
    channel.get_pty(term="dumb", width=220, height=24)
    channel.exec_command(f"bash -lc {_sh(command)}")
    return channel


def open_channel_binary(client: paramiko.SSHClient, command: str) -> paramiko.Channel:
    """Run ``command`` without a PTY so binary serial bytes are not mangled.

    SBX frames contain control bytes (0x00/0x0A/0x0D/0x60/…). A PTY can
    swallow or rewrite them — picocom-over-PTY then looks like a silent port.
    """
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("SSH transport is not connected")
    channel = transport.open_session()
    channel.exec_command(f"bash -lc {_sh(command)}")
    return channel


def _sbx_baud_default() -> int:
    raw = (os.environ.get("DRIVE_SBX_BAUD") or "").strip()
    if raw.isdigit():
        return int(raw)
    return 57600


def _sbx_baud_candidates() -> list[int]:
    """Baud rates to try when auto-probing (env DRIVE_SBX_BAUD wins if set alone)."""
    fixed = (os.environ.get("DRIVE_SBX_BAUD") or "").strip()
    if fixed.isdigit() and (os.environ.get("DRIVE_SBX_BAUD_LOCK") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return [int(fixed)]
    preferred = _sbx_baud_default()
    # sbx-ctl default 57600; also try Lucid-ish 115200 and a couple neighbors.
    ordered = [preferred, 57600, 115200, 38400, 19200]
    out: list[int] = []
    for b in ordered:
        if b not in out:
            out.append(b)
    return out


def _remote_sbx_stream_cmd(device_path: str, baud: Optional[int] = None) -> str:
    """Non-interactive SBX byte stream on the capture host.

    Default ``stty`` + ``cat`` matches lucid UART capture (works under SSH).
    Set ``DRIVE_SBX_USE_PICOCOM=1`` only for interactive-style debugging.
    """
    rate = int(baud or _sbx_baud_default())
    dev = shlex.quote(device_path)
    if os.environ.get("DRIVE_SBX_USE_PICOCOM", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return f"picocom -b {rate} -f n --quiet --nolock {dev}"
    return (
        f"stty -F {dev} {rate} cs8 -cstopb -parenb raw -echo clocal "
        f"2>/dev/null; exec cat {dev}"
    )


def _list_port_holders(client: paramiko.SSHClient, device_path: str) -> str:
    return run_remote(
        client,
        f"fuser -v {shlex.quote(device_path)} 2>&1 || "
        f"lsof {shlex.quote(device_path)} 2>&1 || true",
    ).strip()


def _reclaim_serial_port(client: paramiko.SSHClient, device_path: str) -> str:
    """Free ``device_path`` so our reader is the only consumer.

    FTDI delivers RX to one open fd — leftover manual ``picocom`` / prior
    ``stty``+``cat`` sessions make automation see ``raw_bytes=0`` while
    ``hexdump`` works once those holders are gone.
    """
    before = _list_port_holders(client, device_path)
    # Kill anything holding the tty (picocom, cat, stty, hexdump, dd, …).
    kill_out = run_remote(
        client,
        f"fuser -k -TERM {shlex.quote(device_path)} 2>&1 || true; "
        f"sleep 0.4; "
        f"fuser -k -KILL {shlex.quote(device_path)} 2>&1 || true; "
        f"sleep 0.3",
    ).strip()
    after = _list_port_holders(client, device_path)
    summary = (
        f"before={before[:240]!r} kill={kill_out[:120]!r} after={after[:240]!r}"
    )
    print(f"[SBX ] Reclaimed {device_path}: {summary}")
    return summary


def _sample_serial_chunk(
    client: paramiko.SSHClient,
    device_path: str,
    baud: int,
    seconds: float = 1.2,
) -> bytes:
    """Grab a short raw sample at ``baud`` (hex-encoded over SSH for safety)."""
    dev = shlex.quote(device_path)
    sec = max(0.5, float(seconds))
    out = run_remote(
        client,
        f"stty -F {dev} {int(baud)} cs8 -cstopb -parenb raw -echo clocal 2>/dev/null; "
        f"timeout {sec} dd if={dev} bs=512 count=16 2>/dev/null | "
        f"python3 -c 'import sys; print(sys.stdin.buffer.read().hex())'",
    ).strip()
    run_remote(client, f"fuser -k -KILL {dev} 2>/dev/null || true; sleep 0.15")
    hex_only = "".join(ch for ch in out if ch in "0123456789abcdefABCDEF")
    if len(hex_only) < 2:
        return b""
    if len(hex_only) % 2:
        hex_only = hex_only[:-1]
    try:
        return bytes.fromhex(hex_only)
    except ValueError:
        return b""


def _score_sbx_sample(sample: bytes) -> dict:
    """Score a raw sample for how SBX-like it is (frames > sof/eof pairing)."""
    from drive_automation.sbx_decode import SbxStreamFramer as _Framer

    sof = sample.count(0x60)
    eof = sample.count(0x62)
    esc = sample.count(0x5E)
    frames = 0
    if sample:
        try:
            frames, _junk = _Framer().feed(sample)
            frames = len(frames)
        except Exception:
            frames = 0
    # Prefer real decoded frames; else prefer balanced SOF/EOF presence.
    score = frames * 1000 + min(sof, eof) * 10 + sof + eof
    return {
        "bytes": len(sample),
        "sof": sof,
        "eof": eof,
        "esc": esc,
        "frames": frames,
        "score": score,
        "hex32": sample[:32].hex(" ") if sample else "",
    }


def _probe_best_sbx_baud(
    client: paramiko.SSHClient,
    device_path: str,
) -> tuple[int, list[dict]]:
    """Try candidate bauds; return (best_baud, per-baud stats)."""
    results: list[dict] = []
    best_baud = _sbx_baud_default()
    best_score = -1
    for baud in _sbx_baud_candidates():
        sample = _sample_serial_chunk(client, device_path, baud, seconds=1.2)
        stats = _score_sbx_sample(sample)
        stats["baud"] = baud
        results.append(stats)
        print(
            f"[SBX ] baud probe {baud}: bytes={stats['bytes']} "
            f"sof={stats['sof']} eof={stats['eof']} frames={stats['frames']} "
            f"score={stats['score']}"
        )
        if stats["score"] > best_score:
            best_score = int(stats["score"])
            best_baud = baud
    return best_baud, results


def _probe_serial_bytes(
    client: paramiko.SSHClient,
    device_path: str,
    seconds: float = 1.5,
    baud: Optional[int] = None,
) -> int:
    """Read briefly after reclaim; return byte count (0 = still silent)."""
    rate = int(baud or _sbx_baud_default())
    sample = _sample_serial_chunk(client, device_path, rate, seconds=seconds)
    return len(sample)

# =========================================================================
# CAN CAPTURE
# =========================================================================


def detect_pcan(client: paramiko.SSHClient) -> bool:
    """Return True if a PCAN-USB FD device appears in lsusb output."""
    print("\n[*] Checking for PCAN device via lsusb ...")
    output = run_remote(client, "lsusb")
    found  = False
    for line in output.splitlines():
        print(f"    {line}")
        if PCAN_DEVICE_STRING.lower() in line.lower():
            print(f"[OK] PCAN detected: {line.strip()}")
            found = True
    return found


def can_iface_exists(client: paramiko.SSHClient, iface: str = "can0") -> bool:
    """True when SocketCAN interface exists on the capture host."""
    out = run_remote(client, f"ip link show {iface} 2>/dev/null").strip()
    return bool(out) and "does not exist" not in out.lower()


def _write_stub_log(path: str, title: str, lines: list[str]) -> None:
    """Write a short status stub so the dashboard tail shows a real message."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{title}\n")
            fh.write(f"Started : {datetime.datetime.now().isoformat()}\n")
            fh.write("=" * 60 + "\n")
            for line in lines:
                fh.write(f"{line.rstrip()}\n")
            fh.write("=" * 60 + "\n")
    except OSError as exc:
        print(f"[!] Failed to write stub {path}: {exc}")


def _sbx_miss_reason() -> str:
    """Build a user-facing explanation when no SBX serial was selected."""
    env_serial = _read_sbx_env("DRIVE_SBX_SERIAL")
    env_port = _read_sbx_env("DRIVE_SBX_PORT")
    strict = _read_sbx_env("DRIVE_SBX_SERIAL_STRICT", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    parts = [
        "SBX / Modbus USB serial not available on the wall PC.",
        "Plug in the FT232R RS-485 adapter (DRIVE SBX), then Start capture again.",
    ]
    if env_serial:
        parts.append(
            f"config.env expects DRIVE_SBX_SERIAL={env_serial}"
            + (" (STRICT)" if strict else "")
            + "."
        )
    if env_port:
        parts.append(f"DRIVE_SBX_PORT={env_port} was also checked.")
    parts.append("Hint: ls /dev/serial/by-id/ | grep -i FTDI   and   pio device list")
    return " ".join(parts)


def _can_miss_reason(*, had_pcan: bool, iface_up: bool, recovery: Optional[dict] = None) -> str:
    iface = (os.environ.get("CAN_INTERFACE") or "can0").strip()
    if not had_pcan:
        return (
            f"CAN bus down: PEAK {PCAN_DEVICE_STRING!r} not seen in lsusb on the wall PC. "
            f"Plug in the PCAN-USB FD adapter (creates {iface}), then Start capture again."
        )
    if not iface_up:
        hint = ""
        if recovery and not recovery.get("ok"):
            try:
                from can_local_recovery import recovery_error_message

                hint = " " + recovery_error_message(recovery)
            except ImportError:
                hint = " CAN interface recovery failed."
        return (
            f"CAN bus down: PCAN is present but {iface} is not up.{hint} "
            f"Try: sudo ip link set {iface} up type can bitrate 1000000"
        )
    return f"CAN capture could not start on {iface}."


def capture_can(
    client:       paramiko.SSHClient,
    log_path:     str,
    capture_path: str,
    username:     str,
    host:         str,
    stop_event:   threading.Event,
) -> None:
    """
    Stream spanbus logger output from the remote host to two files:
      • log_path     -- every line (full CAN log)
      • capture_path -- only lines matching CAN_FILTER_TYPES (filtered log)

    Each output line is prefixed with a local timestamp before writing.
    """
    print(f"[CAN ] Starting capture -> {log_path}")
    print(f"[CAN ] Filtered capture -> {capture_path}")
    print(f"[CAN ] Active filters   : {CAN_FILTER_TYPES}")

    # Verify spanbus is available (with helpful error if not)
    if not resolve_tool(client, "spanbus"):
        print(f"[CAN ] 'spanbus' not found on remote host -- CAN capture aborted.")
        print(f"[CAN ] Confirm spanbus is installed and on PATH for the login shell.")
        for path in (log_path, capture_path):
            with open(path, "w") as fh:
                fh.write("=== ERROR: spanbus not found on remote host ===\n")
        return

    channel = open_channel(client, SPANBUS_COMMAND)

    with (
        open(log_path,     "w", buffering=1, encoding="utf-8") as log_fh,
        open(capture_path, "w", buffering=1, encoding="utf-8") as cap_fh,
    ):
        for fh, label in ((log_fh, "CAN Bus Logs"), (cap_fh, "CAN Bus Filtered Capture")):
            fh.write(f"{label}\n")
            fh.write(f"Host    : {username}@{host}\n")
            fh.write(f"Command : {SPANBUS_COMMAND}\n")
            fh.write(f"Started : {datetime.datetime.now().isoformat()}\n")

        cap_fh.write(f"Filters : {', '.join(CAN_FILTER_TYPES)}\n")
        for fh in (log_fh, cap_fh):
            fh.write("=" * 60 + "\n")

        while not stop_event.is_set():
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="replace")
                for line in chunk.splitlines(keepends=True):
                    stamped = f"{ts()} | {line}"
                    log_fh.write(stamped)
                    print(f"[CAN ] {stamped}", end="", flush=True)
                    if any(f in line for f in CAN_FILTER_TYPES):
                        cap_fh.write(stamped)
                log_fh.flush()
                cap_fh.flush()
            elif channel.exit_status_ready():
                while channel.recv_ready():
                    chunk = channel.recv(4096).decode(errors="replace")
                    for line in chunk.splitlines(keepends=True):
                        stamped = f"{ts()} | {line}"
                        log_fh.write(stamped)
                        print(f"[CAN ] {stamped}", end="", flush=True)
                        if any(f in line for f in CAN_FILTER_TYPES):
                            cap_fh.write(stamped)
                print(f"\n[CAN ] Remote spanbus process exited.")
                break
            else:
                time.sleep(0.05)

        for fh in (log_fh, cap_fh):
            fh.write("\n" + "=" * 60 + "\n")
            fh.write(f"Stopped : {datetime.datetime.now().isoformat()}\n")

    channel.close()
    print(f"\n[CAN ] Full log ended    -> {log_path}")
    print(f"[CAN ] Filtered capture ended -> {capture_path}")

# =========================================================================
# SBX USB DEVICE DETECTION (same strategy as lucid updatedd_log.get_usb_device)
# =========================================================================
# Lucid Modbus pick order (updatedd_log._pick_modbus_device):
#   1. Description contains "USB Single Serial"
#   2. /dev/ttyUSB* (not Pico)
#   3. /dev/ttyACM* excluding ST-Link / Pico  (last resort)
#
# Drive adds FT232R (VID:PID=0403:6001) as the preferred RS-485 adapter and
# never treats ST-Link / Pico as SBX. Env overrides (like lucid PILOT_DINGUS_*):
#   DRIVE_SBX_PORT=…           force device path if it exists on capture host
#   DRIVE_SBX_SERIAL=…         filter pio SER=
#   DRIVE_SBX_SERIAL_STRICT=1  fail if serial filter matches nothing

SBX_FT232_VID_PID = "0403:6001"
_SBX_EXCLUDE_DESC = (
    "pico",
    "stlink",
    "st-link",
    "st link",
    "stm32",
    "cmsis",
    "j-link",
    "jlink",
    "black magic",
)


def _read_sbx_env(key: str, default: str = "") -> str:
    """Prefer process env; fall back to config.env (dashboard may not have sourced it)."""
    val = (os.environ.get(key) or "").strip()
    if val:
        return val
    cfg = Path(__file__).resolve().parent / "config.env"
    if not cfg.is_file():
        return default
    prefix = f"{key}="
    for line in cfg.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        return line[len(prefix) :].strip().strip('"').strip("'") or default
    return default


def _parse_pio_device_list(output: str) -> Dict[str, Dict[str, str]]:
    """Parse ``pio device list`` into {path: {serial, description, vid_pid}}."""
    devices: Dict[str, Dict[str, str]] = dict()
    cur_dev: Optional[str] = None
    cur_ser: Optional[str] = None
    cur_desc: Optional[str] = None
    cur_vid: Optional[str] = None

    # Match lucid updatedd_log.get_usb_device parser (+ VID:PID capture).
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("/dev/tty"):
            if cur_dev and (cur_ser or cur_desc or cur_vid):
                devices[cur_dev] = {
                    "serial": cur_ser or "",
                    "description": cur_desc or "",
                    "vid_pid": cur_vid or "",
                }
            cur_dev, cur_ser, cur_desc, cur_vid = line, None, None, None
        elif not cur_dev:
            continue
        elif "VID:PID=" in line:
            m = re.search(r"VID:PID=([0-9A-Fa-f]{4}:[0-9A-Fa-f]{4})", line)
            if m:
                cur_vid = m.group(1).lower()
            m2 = re.search(r"SER=([A-Za-z0-9]+)", line)
            if m2:
                cur_ser = m2.group(1)
        elif "SER=" in line:
            m = re.search(r"SER=([A-Za-z0-9]+)", line)
            if m:
                cur_ser = m.group(1)
        elif line.startswith("Description:"):
            cur_desc = line.replace("Description:", "").strip()

    if cur_dev and (cur_ser or cur_desc or cur_vid):
        devices[cur_dev] = {
            "serial": cur_ser or "",
            "description": cur_desc or "",
            "vid_pid": cur_vid or "",
        }
    return devices


def _desc_excluded_from_sbx(desc: str) -> bool:
    """Skip Pico (CAN) and debugger adapters (ST-Link / CMSIS / J-Link)."""
    d = (desc or "").lower()
    return any(tok in d for tok in _SBX_EXCLUDE_DESC)


def _is_ft232_sbx_adapter(info: Dict[str, str]) -> bool:
    desc = (info.get("description") or "").lower()
    vid_pid = (info.get("vid_pid") or "").lower()
    if _desc_excluded_from_sbx(desc):
        return False
    return (
        vid_pid == SBX_FT232_VID_PID
        or "ft232r" in desc
        or "ft232" in desc
        or "ftdi" in desc
    )


def _ident_for(path: str, info: Dict[str, str], default_prefix: str = "") -> str:
    ser = (info.get("serial") or "").strip()
    desc = (info.get("description") or "").strip()
    if ser and default_prefix:
        return f"{default_prefix}_{ser}"
    if ser:
        return ser
    if desc:
        return desc.replace(" ", "_")
    return path.split("/")[-1]


def _pick_sbx_device(devices: Dict[str, Dict[str, str]]) -> Optional[tuple]:
    """
    Lucid ``_pick_modbus_device`` order, with Drive FT232R preferred first.

    Preference:
      0. FT232R / FTDI (0403:6001), optional DRIVE_SBX_SERIAL filter
      1. Description contains "USB Single Serial"  (lucid Modbus adapter)
      2. /dev/ttyUSB* that is not Pico / debugger
      3. /dev/ttyACM* excluding ST-Link / Pico  (last resort only)
    """
    env_serial = _read_sbx_env("DRIVE_SBX_SERIAL").upper()
    strict = _read_sbx_env("DRIVE_SBX_SERIAL_STRICT", "0").lower() in (
        "1",
        "true",
        "yes",
    )

    # -- Pass 0: FT232R / FTDI RS-485 (Drive SBX) ---------------------------
    ft232: list[tuple[str, Dict[str, str]]] = [
        (path, info)
        for path, info in devices.items()
        if _is_ft232_sbx_adapter(info)
    ]
    if env_serial:
        matched = [
            (p, i)
            for p, i in ft232
            if env_serial in (i.get("serial") or "").upper()
        ]
        if matched:
            ft232 = matched
        elif ft232 and strict:
            seen = ", ".join(
                sorted({(i.get("serial") or "?") for _, i in ft232})
            ) or "(none)"
            print(
                f"    [!] DRIVE_SBX_SERIAL={env_serial!r} not found among FT232R "
                f"devices (seen SER={seen}). Run: pio device list"
            )
            return None
        elif ft232 and not matched:
            print(
                f"    [!] DRIVE_SBX_SERIAL={env_serial!r} not in FT232R list — "
                f"using first FT232R (set DRIVE_SBX_SERIAL_STRICT=1 to fail)"
            )

    if ft232:
        path, info = ft232[0]
        desc = info.get("description", "") or "FT232R_USB_UART"
        print(
            f"[OK] SBX device (FT232R {info.get('vid_pid') or SBX_FT232_VID_PID}): "
            f"{path}  SER={info.get('serial') or '—'}  ('{desc}')"
        )
        return _ident_for(path, info, "FT232R"), path, desc

    # -- Pass 1: lucid "USB Single Serial" ---------------------------------
    for path, info in devices.items():
        desc = info.get("description", "")
        if _desc_excluded_from_sbx(desc):
            continue
        if "usb single serial" in desc.lower():
            print(f"[OK] SBX device (USB Single Serial): {path}  ('{desc}')")
            return _ident_for(path, info), path, desc

    # -- Pass 2: any ttyUSB* that is not Pico / debugger -------------------
    for path, info in devices.items():
        if "/dev/ttyUSB" not in path:
            continue
        desc = info.get("description", "")
        if _desc_excluded_from_sbx(desc):
            continue
        print(f"[OK] SBX device (ttyUSB fallback): {path}  ('{desc or path}')")
        return _ident_for(path, info), path, desc or path

    # -- Pass 3: ttyACM last resort (never ST-Link / Pico) -----------------
    # Prefer failing over picking a debugger when any ttyUSB existed above.
    for path, info in devices.items():
        if "/dev/ttyACM" not in path:
            continue
        desc = info.get("description", "")
        if _desc_excluded_from_sbx(desc):
            print(f"    [-] skipping ACM (debugger/Pico): {path}  ('{desc}')")
            continue
        # Empty description on ACM is usually ST-Link / Pico — do not guess.
        if not desc.strip():
            print(f"    [-] skipping ACM (no description): {path}")
            continue
        print(f"[OK] SBX device (ttyACM fallback): {path}  ('{desc}')")
        return _ident_for(path, info), path, desc

    print("    [!] No suitable SBX USB serial (FT232R / ttyUSB) found. Run: pio device list")
    return None


def _remote_path_exists(client: paramiko.SSHClient, path: str) -> bool:
    quoted = path.replace("'", "'\"'\"'")
    out = run_remote(client, f"test -e '{quoted}' && echo yes || echo no").strip()
    return out.splitlines()[-1].strip() == "yes" if out else False


def get_usb_device(client: paramiko.SSHClient) -> Optional[tuple]:
    """
    Find the Drive SBX RS-485 USB serial on the capture host.

    Same strategy as lucid ``updatedd_log.get_usb_device``, plus FT232R and
    ``DRIVE_SBX_PORT`` / ``DRIVE_SBX_SERIAL`` overrides from env or config.env.
    """
    print("\n[*] Probing SBX USB serial (lucid Modbus pick + FT232R) ...")

    env_port = _read_sbx_env("DRIVE_SBX_PORT")
    pio_found = resolve_tool(client, "pio")
    devices: Dict[str, Dict[str, str]] = dict()

    if pio_found:
        output = run_remote(client, "pio device list")
        devices = _parse_pio_device_list(output)
        if devices:
            print("\n[*] pio device list:")
            for path, info in devices.items():
                print(
                    f"    {path}  VID:PID={info.get('vid_pid') or '—'}  "
                    f"SER={info.get('serial') or '—'}  "
                    f"'{info.get('description') or ''}'"
                )

    # Force path when set and present on the capture host (even if pio missed it).
    # Respect DRIVE_SBX_SERIAL + STRICT so a stale PORT cannot bind the wrong FTDI.
    if env_port:
        if env_port in devices or _remote_path_exists(client, env_port):
            info = dict(
                devices.get(env_port)
                or {
                    "serial": "",
                    "description": "DRIVE_SBX_PORT",
                    "vid_pid": "",
                }
            )
            if not info.get("serial"):
                # Recover SER= from by-id symlink that points at this tty.
                tty_leaf = env_port.rsplit("/", 1)[-1]
                by_id = run_remote(
                    client,
                    "ls -l /dev/serial/by-id/ 2>/dev/null | "
                    f"grep -F '{tty_leaf}' | awk '{{print $9}}' | head -1",
                ).strip()
                m = re.search(r"FT232R_USB_UART_([A-Za-z0-9]+)-", by_id or "")
                if m:
                    info["serial"] = m.group(1)
            env_serial = _read_sbx_env("DRIVE_SBX_SERIAL").upper()
            strict = _read_sbx_env("DRIVE_SBX_SERIAL_STRICT", "0").lower() in (
                "1",
                "true",
                "yes",
            )
            port_ser = (info.get("serial") or "").upper()
            if _desc_excluded_from_sbx(info.get("description", "")):
                print(
                    f"    [!] DRIVE_SBX_PORT={env_port!r} looks like Pico/debugger — "
                    "ignoring override"
                )
            elif env_serial and strict and (
                not port_ser or env_serial not in port_ser
            ):
                # STRICT: never bind PORT when SER is unknown or mismatches —
                # a stale /dev/ttyUSB0 can be a different FTDI with no SBX traffic.
                print(
                    f"    [!] DRIVE_SBX_PORT={env_port!r} SER={port_ser or 'unknown'!r} "
                    f"does not match DRIVE_SBX_SERIAL={env_serial!r} (STRICT) — "
                    "ignoring PORT override"
                )
            else:
                print(
                    f"[OK] Using DRIVE_SBX_PORT → {env_port}"
                    + (f"  SER={port_ser}" if port_ser else "")
                )
                return (
                    _ident_for(env_port, info, "FT232R"),
                    env_port,
                    info.get("description") or env_port,
                )
        else:
            print(
                f"    [!] DRIVE_SBX_PORT={env_port!r} not found on host — "
                "continuing auto-detect"
            )

    if devices:
        result = _pick_sbx_device(devices)
        if result:
            return result

    # Lucid strategy 2: direct /dev listing (ttyUSB before ttyACM in pick order)
    print("    [*] Falling back to /dev listing ...")
    raw = run_remote(client, "ls -1 /dev/ttyUSB* /dev/ttyACM* 2>/dev/null").strip()
    if not raw:
        print("    No /dev/ttyUSB* or /dev/ttyACM* on remote host.")
        return None

    fallback: Dict[str, Dict[str, str]] = dict()
    for dev in raw.splitlines():
        dev = dev.strip()
        if not dev:
            continue
        # Enrich from pio if we already saw this path
        fallback[dev] = devices.get(dev) or {
            "serial": "",
            "description": "",
            "vid_pid": "",
        }
        print(f"    {dev}")

    # Prefer ttyUSB even when pio gave empty descriptions
    usb_only = {p: i for p, i in fallback.items() if "/dev/ttyUSB" in p}
    if usb_only:
        result = _pick_sbx_device(usb_only)
        if result:
            return result

    return _pick_sbx_device(fallback)


def get_pico_can_device(client: paramiko.SSHClient) -> Optional[tuple]:
    """Pico serial for CAN bus (description contains ``pico``)."""
    print("\n[*] Probing for Pico serial (CAN BUS) via pio device list ...")

    if not resolve_tool(client, "pio"):
        print("    [!] pio not found -- cannot auto-detect Pico serial for CAN.")
        return None

    output = run_remote(client, "pio device list")
    devices = _parse_pio_device_list(output)

    for path, info in devices.items():
        desc = info.get("description", "")
        if "pico" in desc.lower():
            ident = desc.replace(" ", "_") if desc else path.split("/")[-1]
            print(f"[OK] Pico serial (CAN BUS): {path}  ('{desc}')")
            return ident, path, desc

    print("    [!] No Pico serial device found in pio device list.")
    return None

# =========================================================================
# SBX (Drive "Modbus" stream) — variable PayloadLen framing
# =========================================================================
# Physical capture is still USB serial (picocom), but frames are SBX:
#   [ 0x60 | Ver | Rcvr | Sndr | HdrCRC | Txn | MsgType | PayloadLen
#         | Payload(N) | PayloadCRC | 0x62 ]
# Log file name stays "Modbus logs_*.txt" for Lucid-compatible dashboard UI.
# =========================================================================

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from drive_automation.sbx_decode import SbxJunkChunk, SbxStreamFramer  # noqa: E402


def _sbx_log_timestamp() -> str:
    now_dt = datetime.datetime.now()
    ms_part = now_dt.microsecond // 1000
    return now_dt.strftime("%Y-%m-%d %H:%M:%S") + f":{ms_part:03d}"


def _write_sbx_modbus_line(
    log_fh,
    *,
    hex_frame: str,
    description: str,
    direction: str = "RX",
    echo: bool = True,
) -> None:
    stamped = f"{_sbx_log_timestamp()} | {hex_frame}  | {description} | {direction}\n"
    log_fh.write(stamped)
    log_fh.flush()
    if echo:
        print(f"[SBX ] {stamped}", end="", flush=True)


def _write_sbx_junk_line(log_fh, junk: SbxJunkChunk) -> None:
    if not junk.raw:
        return
    _write_sbx_modbus_line(
        log_fh,
        hex_frame=junk.raw.hex(" "),
        description=junk.summary_line(),
    )


def decode_sbx_line(frame) -> str:
    """Human-readable one-liner for a decoded SBX frame."""
    return frame.summary_line()


def capture_modbus(
    ssh_client:  paramiko.SSHClient,
    device_path: str,
    identifier:  str,
    log_path:    str,
    stop_event:  threading.Event,
) -> None:
    """
    Stream raw SBX bytes from the remote RS-485 USB serial device, frame with
    variable PayloadLen, and write timestamped decoded lines to log_path.
    """
    print(f"[SBX ] Starting capture -> {log_path}")

    holders_before = _list_port_holders(ssh_client, device_path)
    reclaim_info = _reclaim_serial_port(ssh_client, device_path)
    selected_baud, baud_probe = _probe_best_sbx_baud(ssh_client, device_path)
    probe_n = 0
    for row in baud_probe:
        if int(row.get("baud") or 0) == int(selected_baud):
            probe_n = int(row.get("bytes") or 0)
            break
    holders_after = _list_port_holders(ssh_client, device_path)
    stream_cmd = _remote_sbx_stream_cmd(device_path, baud=selected_baud)

    channel = open_channel_binary(ssh_client, stream_cmd)
    framer = SbxStreamFramer()
    transport_info = ssh_client.get_transport().getpeername()
    remote_host = transport_info[0] if transport_info else "unknown"
    raw_bytes = 0
    frame_count = 0
    sof_count = 0
    eof_count = 0
    esc_count = 0
    last_heartbeat = time.monotonic()
    first_chunk_hex = ""

    with open(log_path, "w", buffering=1, encoding="utf-8") as log_fh:
        log_fh.write(f"=== SBX (Drive) Log Started : {datetime.datetime.now().isoformat()} ===\n")
        log_fh.write(f"=== Device             : {identifier} ({device_path}) ===\n")
        log_fh.write(f"=== Host               : {remote_host} ===\n")
        log_fh.write(f"=== baud               : {selected_baud} (auto-probed) ===\n")
        for row in baud_probe:
            log_fh.write(
                f"=== baud_probe         : {row.get('baud')} bytes={row.get('bytes')} "
                f"sof={row.get('sof')} eof={row.get('eof')} frames={row.get('frames')} "
                f"score={row.get('score')} hex32={row.get('hex32')} ===\n"
            )
        log_fh.write(f"=== stream_cmd         : {stream_cmd} ===\n")
        log_fh.write(f"=== port_reclaim       : {reclaim_info[:280]} ===\n")
        log_fh.write(f"=== probe_bytes        : {probe_n} ===\n")
        if holders_before:
            log_fh.write(f"=== port_holders_before: {holders_before[:300]} ===\n")
        if holders_after:
            log_fh.write(f"=== port_holders_after : {holders_after[:300]} ===\n")
        if probe_n <= 0:
            log_fh.write(
                "=== WARN: probe saw 0 bytes after reclaim — check wiring / another "
                "host holding the RS-485 bus ===\n"
            )
        log_fh.write(
            "=== Format             : TIMESTAMP | RAW (hex) | DESCRIPTION | DIRECTION ===\n"
            "===                      (DESCRIPTION may be decoded SBX or UNDECODED junk) ===\n\n"
        )

        while not stop_event.is_set():
            if channel.recv_ready():
                raw = channel.recv(512)
                if raw and not first_chunk_hex:
                    first_chunk_hex = raw[:32].hex(" ")
                sof_count += raw.count(0x60)
                eof_count += raw.count(0x62)
                esc_count += raw.count(0x5E)
                raw_bytes += len(raw)
                frames, junk_chunks = framer.feed(raw)
                for junk in junk_chunks:
                    _write_sbx_junk_line(log_fh, junk)
                for frame in frames:
                    frame_count += 1
                    hex_frame = frame.raw_wire.hex(" ")
                    description = decode_sbx_line(frame)
                    _write_sbx_modbus_line(
                        log_fh,
                        hex_frame=hex_frame,
                        description=description,
                        direction=frame.direction,
                    )
            elif channel.recv_stderr_ready():
                err = channel.recv_stderr(256)
                if err:
                    msg = err.decode("utf-8", errors="replace").strip()
                    if msg:
                        log_fh.write(f"=== STDERR: {msg} ===\n")
                        log_fh.flush()
            elif channel.exit_status_ready():
                print("\n[SBX ] Remote SBX stream process exited.")
                break
            else:
                now = time.monotonic()
                if now - last_heartbeat >= 5.0:
                    last_heartbeat = now
                    log_fh.write(
                        f"=== heartbeat raw_bytes={raw_bytes} frames={frame_count} "
                        f"sof={sof_count} eof={eof_count} esc={esc_count} "
                        f"baud={selected_baud} ===\n"
                    )
                    log_fh.flush()
                time.sleep(0.01)

        trailing = framer.flush_remaining()
        if trailing is not None:
            _write_sbx_junk_line(log_fh, trailing)

        log_fh.write(
            f"\n=== SBX stats: raw_bytes={raw_bytes} frames={frame_count} "
            f"baud={selected_baud} ===\n"
        )
        log_fh.write(f"=== SBX Log Ended : {datetime.datetime.now().isoformat()} ===\n")

    try:
        channel.close()
    except Exception:
        pass
    # Ensure remote cat/picocom does not linger and block the next run.
    try:
        _reclaim_serial_port(ssh_client, device_path)
    except Exception:
        pass
    print(
        f"[SBX ] Capture ended -> {log_path} "
        f"(raw_bytes={raw_bytes} frames={frame_count} baud={selected_baud})"
    )

# =========================================================================
# MAIN ORCHESTRATOR
# =========================================================================


def run_capture(
    username: str,
    host: str,
    password: str,
    output_dir: str,
    duration: Optional[int],
    modbus_filters: Optional[List[str]] = None,
    stop_requested: Optional[threading.Event] = None,
    reuse_existing_logs: bool = False,
    skip_can: bool = False,
) -> None:
    """
    Run CAN + Modbus capture.

    When ``stop_requested`` is set (e.g. dashboard API), ``.set()`` stops capture;
    do not rely on Ctrl+C in that mode.

    ``modbus_filters`` is accepted for API compatibility with ``unified_logss``;
    this backend only writes a full Modbus log (no separate filtered capture file),
    so non-empty filters are noted and ignored.

    ``skip_can`` skips PCAN/SocketCAN entirely (used by one-click pipeline when
    CAN hardware is unavailable).
    """

    # 1. SSH connect FIRST -- no folders created until auth succeeds
    client = connect_ssh(username, host, password)

    # 2. Create output folder only after successful login
    host_label = f"{username}@{host}"
    folder     = Path(output_dir) / host_label
    folder.mkdir(parents=True, exist_ok=True)

    def _latest_log(pattern: str) -> Path | None:
        hits = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime if p.is_file() else 0)
        return hits[-1] if hits else None

    stamp = ts_file()
    can_log_path = str(folder / f"CAN bus logs_{stamp}.txt")
    can_capture_path = str(folder / f"CAN bus capture_{stamp}.txt")
    modbus_log_path = str(folder / f"Modbus logs_{stamp}.txt")

    if reuse_existing_logs:
        ts_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cont = f"\n{'=' * 20} CAPTURE RESTART (append) {'=' * 20} [{ts_now}]\n"
        for pat, var in (
            ("CAN bus logs_*.txt", "can_log_path"),
            ("CAN bus capture_*.txt", "can_capture_path"),
            ("Modbus logs_*.txt", "modbus_log_path"),
        ):
            existing = _latest_log(pat)
            if existing is not None:
                try:
                    with existing.open("a", encoding="utf-8") as fh:
                        fh.write(cont)
                except OSError:
                    pass
                if var == "can_log_path":
                    can_log_path = str(existing)
                elif var == "can_capture_path":
                    can_capture_path = str(existing)
                elif var == "modbus_log_path":
                    modbus_log_path = str(existing)

    print(f"\n[*] Output folder : {folder.resolve()}")
    print(f"    CAN log       : CAN bus logs_{stamp}.txt")
    print(f"    CAN capture   : CAN bus capture_{stamp}.txt")
    print(f"    SBX/Modbus log: Modbus logs_{stamp}.txt")

    try:
        import dashboard_logs as _dashlog

        _dashlog.notify_unified_capture_files(
            session_dir=str(Path(output_dir).resolve()),
            can_log=can_log_path,
            can_capture=can_capture_path,
            modbus_log=modbus_log_path,
            modbus_capture="",
        )
    except ImportError:
        pass

    # 3. Detect PCAN (lsusb) AND Pico serial (pio device list) for CAN bus
    warnings: List[str] = []
    can_recovery: Optional[dict] = None
    had_pcan = False
    can_ready = False
    pico_device = None
    iface_name = (os.environ.get("CAN_INTERFACE") or "can0").strip()

    if skip_can:
        print("\n[*] CAN bus capture masked (skip_can=True) — SBX/Modbus only.\n")
        _write_stub_log(
            can_log_path,
            "CAN Bus Logs — MASKED",
            [
                "=== INFO: CAN bus capture is masked for this run ===",
                "One-click pipeline is running SBX/Modbus (+ UART) only.",
                "Unset DRIVE_MASK_CANBUS or start Log Capture without mask to enable CAN.",
            ],
        )
        _write_stub_log(
            can_capture_path,
            "CAN Bus Filtered Capture — MASKED",
            ["=== INFO: CAN bus capture is masked for this run ==="],
        )
    else:
        can_ready = detect_pcan(client)
        had_pcan = can_ready
        pico_device = get_pico_can_device(client)
        if not can_ready:
            print(f"\n[!] '{PCAN_DEVICE_STRING}' not detected via lsusb.\n")
        if pico_device:
            pico_ident, pico_path, pico_desc = pico_device
            print(f"[OK] Pico serial confirmed for CAN BUS: {pico_path}  ('{pico_desc}')")
        else:
            print("[!] No Pico serial (CAN BUS) found in pio device list.")

        if can_ready:
            # Lucid-style: modprobe + ip link down/up + candump before spanbus.
            try:
                from can_local_recovery import recover_can_on_capture_host

                print(f"\n[*] Ensuring SocketCAN {iface_name} is up (Lucid-style recovery) ...")
                can_recovery = recover_can_on_capture_host(
                    host=host,
                    username=username,
                    password=password,
                    audit_fn=lambda m: print(m),
                )
                print(
                    f"[*] CAN recovery: ok={can_recovery.get('ok')} "
                    f"link_up={can_recovery.get('link_up')} "
                    f"candump_ok={can_recovery.get('candump_ok')}"
                )
            except Exception as exc:
                print(f"[!] CAN recovery skipped: {exc}")
                can_recovery = {"ok": False, "error": str(exc)}

            if not can_iface_exists(client, iface_name):
                print(f"[!] {iface_name} still missing after recovery — CAN capture skipped.")
                can_ready = False
        elif not can_ready and not pico_device:
            print("[!] Neither PCAN adapter nor Pico serial detected -- CAN capture skipped.\n")

    # 4. Detect Modbus USB serial device (USB Single Serial via pio device list)
    modbus_device = get_usb_device(client)
    if not modbus_device:
        print("\n[!] No Modbus USB serial device found -- Modbus capture skipped.\n")

    can_ok: Optional[bool]
    if skip_can:
        can_ok = None  # masked — not a failure
    else:
        can_ok = bool(can_ready)
    modbus_ok = bool(modbus_device)

    if not skip_can and not can_ok:
        can_msg = _can_miss_reason(
            had_pcan=had_pcan,
            iface_up=False,
            recovery=can_recovery,
        )
        warnings.append(can_msg)
        _write_stub_log(
            can_log_path,
            "CAN Bus Logs — NOT STARTED",
            [f"=== ERROR: {can_msg} ==="],
        )
        _write_stub_log(
            can_capture_path,
            "CAN Bus Filtered Capture — NOT STARTED",
            [f"=== ERROR: {can_msg} ==="],
        )

    if not modbus_ok:
        sbx_msg = _sbx_miss_reason()
        warnings.append(sbx_msg)
        _write_stub_log(
            modbus_log_path,
            "Modbus / SBX Logs — NOT STARTED",
            [f"=== ERROR: {sbx_msg} ==="],
        )

    try:
        import dashboard_logs as _dashlog

        _dashlog.notify_unified_capture_health(
            can_ok=can_ok,
            modbus_ok=modbus_ok,
            warnings=warnings,
            error=(
                None
                if (can_ok or skip_can or modbus_ok)
                else "No capture targets available (CAN and SBX/Modbus both unavailable)."
            ),
        )
    except ImportError:
        pass

    if not can_ready and not modbus_device:
        client.close()
        raise RuntimeError(
            "No capture targets available (no CAN path and no Modbus serial). "
            + " | ".join(warnings)
        )

    if modbus_filters:
        print(
            "\n[*] Note: modbus_filters are ignored by this backend "
            "(full Modbus log only; no filtered capture file)."
        )

    # 5. Shared stop event (dashboard sets stop_requested)
    stop = stop_requested if stop_requested is not None else threading.Event()
    threads: List[threading.Thread] = []

    # 6. CAN capture thread
    if can_ready:
        threads.append(threading.Thread(
            target=capture_can,
            args=(client, can_log_path, can_capture_path, username, host, stop),
            daemon=True, name="CAN-capture",
        ))

    # 7. Modbus capture thread
    if modbus_device:
        ident, dev_path, _ = modbus_device
        threads.append(threading.Thread(
            target=capture_modbus,
            args=(client, dev_path, ident, modbus_log_path, stop),
            daemon=True, name="MOD-capture",
        ))

    for t in threads:
        t.start()

    active = ("CAN + Modbus" if (can_ready and modbus_device)
              else "CAN only" if can_ready else "Modbus only")
    if warnings:
        print("\n[!] Capture warnings:")
        for w in warnings:
            print(f"    - {w}")
    print("\n" + "=" * 55)
    if stop_requested is not None:
        print(f"  Capturing: {active}.  Stop from the dashboard when done.")
    else:
        print(f"  Capturing: {active}.  Press Ctrl+C to stop.")
    if duration:
        print(f"  Auto-stopping in {duration} seconds.")
    print("=" * 55 + "\n")

    # 8. Wait for Ctrl+C, duration, or external stop_requested
    try:
        if duration:
            deadline = time.time() + float(duration)
            while time.time() < deadline:
                if stop.is_set():
                    break
                time.sleep(min(0.5, max(0.0, deadline - time.time())))
        else:
            while any(t.is_alive() for t in threads):
                if stop.is_set():
                    break
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\n[*] Ctrl+C -- stopping all capture streams ...")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=8)
        client.close()

    # 9. Summary
    print("\n" + "=" * 55)
    print("  CAPTURE COMPLETE")
    print("=" * 55)
    print(f"\n  Folder : {folder.resolve()}")
    if can_ready:
        print(f"  CAN log : CAN bus logs_{stamp}.txt")
        print(f"  CAN cap: CAN bus capture_{stamp}.txt")
    if modbus_device:
        print(f"  Modbus : Modbus logs_{stamp}.txt")
    print()

# =========================================================================
# CLI
# =========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified CAN + Modbus logger for SPAN lab cells",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  python3 unified_logger.py capture -h 10.91.0.96 -u systems
  python3 unified_logger.py capture -h 10.91.0.96 -u systems -o ./lab_logs
  python3 unified_logger.py capture -h 10.91.0.96 -u systems --duration 120

Output
------
  systems@10.91.0.96/
    CAN bus logs_<timestamp>.txt     <- all spanbus output
    CAN bus capture_<timestamp>.txt  <- filtered: MID_RELAY / DER / NFT states
    Modbus logs_<timestamp>.txt      <- all 8-byte RTU frames
        """,
    )
    sub = parser.add_subparsers(dest="command")
    cap = sub.add_parser("capture", help="Start unified capture", add_help=False)
    cap.add_argument("-h", "--host",     required=True, dest="ssh_host", metavar="IP",
                     help="Lab cell IP address")
    cap.add_argument("-u", "--username", required=True, metavar="USER",
                     help="SSH username (e.g. systems)")
    cap.add_argument("-o", "--output",   default=".",   metavar="DIR",
                     help="Base output directory (default: current dir)")
    cap.add_argument("--duration",       type=int, default=None, metavar="SEC",
                     help="Auto-stop after N seconds (default: run until Ctrl+C)")
    cap.add_argument(
        "--sbx-only",
        action="store_true",
        help="Capture SBX/Modbus only (skip CAN). Also set by DRIVE_MASK_CANBUS=1",
    )
    cap.add_argument("--help", action="help", help="Show this help message")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    print("\n" + "=" * 55)
    print("  SSH AUTHENTICATION")
    print("=" * 55)
    password = getpass.getpass(f"\nPassword for {args.username}@{args.ssh_host}: ")

    signal.signal(signal.SIGINT, lambda *_: None)

    mask_can = (os.environ.get("DRIVE_MASK_CANBUS") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    try:
        run_capture(
            username=args.username,
            host=args.ssh_host,
            password=password,
            output_dir=args.output,
            duration=args.duration,
            modbus_filters=None,
            stop_requested=None,
            skip_can=bool(args.sbx_only or mask_can),
        )
    except (ConnectionError, RuntimeError) as exc:
        print(f"\n[X] {exc}")
        sys.exit(1)

if __name__ == "__main__":
    main()
