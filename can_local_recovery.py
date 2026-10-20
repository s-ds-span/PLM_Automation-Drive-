#!/usr/bin/env python3
"""
Local CAN interface recovery on the machine running the dashboard (not over SSH).

Runs kernel modules, brings can0 up at 1 Mbit/s, and verifies with a short candump.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any

_DEFAULT_IFACE = "can0"
_DEFAULT_BITRATE = "1000000"


def _iface() -> str:
    return (os.environ.get("CAN_INTERFACE") or _DEFAULT_IFACE).strip()


def _bitrate() -> str:
    return (os.environ.get("CAN_BITRATE") or _DEFAULT_BITRATE).strip()


def _enabled() -> bool:
    return (os.environ.get("CAN_LOCAL_RECOVERY", "1")).strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _candump_verify_s() -> float:
    try:
        return max(1.0, float(os.environ.get("CAN_CANDUMP_VERIFY_S", "3")))
    except ValueError:
        return 3.0


def _run_local(
    cmd: list[str],
    *,
    timeout_s: float = 45.0,
    shell: bool = False,
) -> dict[str, Any]:
    """Run a command on this host (never SSH). Returns ok, code, stdout, stderr."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=shell,
        )
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "cmd": " ".join(cmd) if not shell else cmd[0],
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "code": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout_s}s",
            "cmd": " ".join(cmd) if not shell else cmd[0],
        }
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "code": -1,
            "stdout": "",
            "stderr": str(exc),
            "cmd": " ".join(cmd) if not shell else cmd[0],
        }


def _sudo(cmd: str, *, timeout_s: float = 30.0) -> dict[str, Any]:
    return _run_local(["sudo", "-n", "sh", "-c", cmd], timeout_s=timeout_s)


def recover_local_can_interface(*, audit_fn=None) -> dict[str, Any]:
    """
    Bring up local CAN on the dashboard host (modprobe + ip link + candump check).

    ``audit_fn`` optional callable(message) for dashboard action log lines.
    """
    if not _enabled():
        return {"ok": False, "skipped": True, "reason": "CAN_LOCAL_RECOVERY disabled"}

    def _log(msg: str) -> None:
        if audit_fn:
            audit_fn(msg)

    iface = _iface()
    bitrate = _bitrate()
    steps: list[dict[str, Any]] = []

    _log(f"[CAN recovery] Starting local recovery on {iface} (not via SSH)")

    for mod in ("can", "can_raw", "can_dev", "peak_usb"):
        r = _sudo(f"modprobe {mod}")
        steps.append({"step": f"modprobe {mod}", **r})
        _log(
            f"[CAN recovery] modprobe {mod}: "
            + ("OK" if r["ok"] else f"FAIL ({r.get('stderr') or r.get('code')})")
        )

    r_down = _sudo(f"ip link set {iface} down")
    steps.append({"step": f"ip link set {iface} down", **r_down})
    _log(
        f"[CAN recovery] {iface} down: "
        + ("OK" if r_down["ok"] else f"FAIL ({r_down.get('stderr') or r_down.get('code')})")
    )
    time.sleep(0.5)

    r_up = _sudo(
        f"ip link set {iface} up type can bitrate {bitrate}",
        timeout_s=20.0,
    )
    steps.append({"step": f"ip link set {iface} up", **r_up})
    _log(
        f"[CAN recovery] {iface} up @ {bitrate}: "
        + ("OK" if r_up["ok"] else f"FAIL ({r_up.get('stderr') or r_up.get('code')})")
    )
    time.sleep(0.5)

    candump_ok = False
    candump_detail = ""
    candump_bin = shutil.which("candump")
    if not candump_bin:
        candump_detail = "candump not found in PATH"
        _log(f"[CAN recovery] verify: {candump_detail}")
    else:
        verify_s = _candump_verify_s()
        r_dump = _run_local(
            ["timeout", str(int(verify_s) + 1), candump_bin, iface, "-n", "1"],
            timeout_s=verify_s + 5.0,
        )
        candump_ok = r_dump["ok"] and bool(r_dump.get("stdout"))
        candump_detail = r_dump.get("stdout") or r_dump.get("stderr") or ""
        steps.append({"step": f"candump {iface}", **r_dump, "saw_frame": candump_ok})
        _log(
            f"[CAN recovery] candump {iface}: "
            + ("saw traffic" if candump_ok else f"no frame ({candump_detail[:120]})")
        )

    link_ok = r_up["ok"]
    ok = link_ok and (candump_ok or candump_bin is None)
    return {
        "ok": ok,
        "iface": iface,
        "bitrate": bitrate,
        "link_up": link_ok,
        "candump_ok": candump_ok,
        "candump_detail": candump_detail[:500],
        "steps": steps,
    }


def recovery_error_message(rec: dict[str, Any] | None) -> str:
    """User-facing text after recovery was attempted but MID/CAN logs still missing."""
    iface = (rec or {}).get("iface") or _iface()
    where = (rec or {}).get("target") or "capture host"
    hint = (
        f"CAN recovery on {where} ({iface}: modprobe, ip link down/up, candump) "
        "was attempted but no MID relay status appeared in capture logs."
    )
    if rec and not rec.get("link_up"):
        hint += " CAN interface may not have come up (check sudo NOPASSWD, peak_usb, wiring)."
    elif rec and rec.get("link_up") and not rec.get("candump_ok"):
        hint += " Interface is up but candump saw no frames."
    elif rec and rec.get("skipped"):
        hint += f" Recovery skipped ({rec.get('reason', 'disabled')})."
    return hint


def _local_ips() -> set[str]:
    ips: set[str] = {"127.0.0.1", "::1", "localhost"}
    try:
        import socket

        host = socket.gethostname()
        if host:
            ips.add(host.lower())
        for info in socket.getaddrinfo(host, None):
            if info[4] and info[4][0]:
                ips.add(str(info[4][0]))
    except OSError:
        pass
    return ips


def is_local_capture_host(host: str) -> bool:
    """True when capture SSH target is this machine (local CAN recovery applies)."""
    h = (host or "").strip().lower()
    if not h:
        return False
    if h in ("127.0.0.1", "localhost", "::1"):
        return True
    return h in _local_ips()


def _remote_sudo(client: Any, cmd: str, *, timeout_s: float = 30.0) -> dict[str, Any]:
    wrapped = f"sudo -n sh -c {_sh_single(cmd)}"
    login_cmd = f"bash -lc {_sh_single(wrapped)}"
    try:
        _, stdout, stderr = client.exec_command(login_cmd, timeout=timeout_s)
        out = (stdout.read().decode(errors="replace") + stderr.read().decode(errors="replace")).strip()
        code = stdout.channel.recv_exit_status()
        return {"ok": code == 0, "code": code, "stdout": out, "stderr": out, "cmd": cmd}
    except Exception as exc:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(exc), "cmd": cmd}


def _sh_single(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def recover_remote_can_via_ssh(
    username: str,
    host: str,
    password: str,
    *,
    audit_fn=None,
) -> dict[str, Any]:
    """
    Run the same CAN up/down sequence on the remote capture host over SSH.
    """
    if not _enabled():
        return {"ok": False, "skipped": True, "reason": "CAN_LOCAL_RECOVERY disabled"}

    try:
        import paramiko
    except ImportError:
        return {
            "ok": False,
            "skipped": True,
            "reason": "paramiko not installed",
            "target": f"{username}@{host}",
        }

    def _log(msg: str) -> None:
        if audit_fn:
            audit_fn(msg)

    iface = _iface()
    bitrate = _bitrate()
    steps: list[dict[str, Any]] = []
    target = f"{username}@{host}"
    _log(f"[CAN recovery] Starting remote recovery on {target} ({iface})")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=int(os.environ.get("SSH_PORT", "22")),
            username=username,
            password=password,
            timeout=float(os.environ.get("SSH_TIMEOUT", "10")),
        )
    except Exception as exc:
        return {
            "ok": False,
            "target": target,
            "iface": iface,
            "error": f"SSH connect failed: {exc}",
            "steps": steps,
        }

    try:
        for mod in ("can", "can_raw", "can_dev", "peak_usb"):
            r = _remote_sudo(client, f"modprobe {mod}")
            steps.append({"step": f"modprobe {mod}", **r})
            _log(
                f"[CAN recovery] {target} modprobe {mod}: "
                + ("OK" if r["ok"] else f"FAIL ({r.get('stderr') or r.get('code')})")
            )

        r_down = _remote_sudo(client, f"ip link set {iface} down")
        steps.append({"step": f"ip link set {iface} down", **r_down})
        time.sleep(0.5)

        r_up = _remote_sudo(
            client,
            f"ip link set {iface} up type can bitrate {bitrate}",
            timeout_s=20.0,
        )
        steps.append({"step": f"ip link set {iface} up", **r_up})
        _log(
            f"[CAN recovery] {target} {iface} up @ {bitrate}: "
            + ("OK" if r_up["ok"] else f"FAIL ({r_up.get('stderr') or r_up.get('code')})")
        )
        time.sleep(0.5)

        verify_s = int(_candump_verify_s())
        r_dump = _remote_sudo(
            client,
            f"timeout {verify_s + 1} candump {iface} -n 1",
            timeout_s=verify_s + 8.0,
        )
        candump_ok = r_dump["ok"] and bool(r_dump.get("stdout"))
        steps.append({"step": f"candump {iface}", **r_dump, "saw_frame": candump_ok})
        _log(
            f"[CAN recovery] {target} candump {iface}: "
            + ("saw traffic" if candump_ok else f"no frame ({(r_dump.get('stdout') or r_dump.get('stderr') or '')[:120]})")
        )

        link_ok = bool(r_up["ok"])
        ok = link_ok and candump_ok
        return {
            "ok": ok,
            "target": target,
            "iface": iface,
            "bitrate": bitrate,
            "link_up": link_ok,
            "candump_ok": candump_ok,
            "steps": steps,
        }
    finally:
        client.close()


def recover_can_on_capture_host(
    *,
    host: str,
    username: str = "",
    password: str = "",
    audit_fn=None,
) -> dict[str, Any]:
    """Local or remote CAN ifdown/ifup + module load, depending on capture host."""
    if is_local_capture_host(host):
        rec = recover_local_can_interface(audit_fn=audit_fn)
        rec["target"] = "local wall PC"
        return rec
    if not username or password is None:
        return {
            "ok": False,
            "skipped": True,
            "reason": "remote recovery requires username and password",
            "target": host,
        }
    return recover_remote_can_via_ssh(username, host, password, audit_fn=audit_fn)
