"""Lab SSH to the SPAN gateway.

Preferred (no keys installed on the gateway): from the lab cell run

  ssh -F ~/.ssh/span/<GATEWAY_SERIAL>/spanssh-config <GATEWAY_SERIAL> '<cmd>'

That tunnels via remote.it, runs the command on the gateway, and returns to the
lab cell. Avoids the old lab-cell-key-on-gateway folder (cleared on factory reset).

Legacy fallbacks: CA cert + identity, system ssh user@host, password/agent.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional


class GatewayError(Exception):
    """Gateway SSH command failed or gateway is unreachable."""


def _expand_path(path: str) -> Path:
    return Path(os.path.expanduser(path.strip())).resolve()


def _default_identity() -> str:
    return os.environ.get("GATEWAY_SSH_IDENTITY", "~/.ssh/id_ecdsa").strip() or "~/.ssh/id_ecdsa"


def identity_ready(identity_path: str = "") -> bool:
    """True when private key exists and OpenSSH can find a matching ``*-cert.pub``."""
    key = _expand_path(identity_path or _default_identity())
    if not key.is_file():
        return False
    # OpenSSH auto-loads <key>-cert.pub beside the private key.
    cert = key.parent / f"{key.name}-cert.pub"
    return cert.is_file()


def spanssh_config_path(gateway_serial: str = "", config_override: str = "") -> Optional[Path]:
    """Return ``~/.ssh/span/<serial>/spanssh-config`` when it exists."""
    if (config_override or "").strip():
        path = _expand_path(config_override)
        return path if path.is_file() else None
    serial = (gateway_serial or os.environ.get("GATEWAY_SERIAL") or "").strip()
    if not serial:
        return None
    root = (
        os.environ.get("GATEWAY_SPANSSH_DIR")
        or os.environ.get("SPANSSH_DIR")
        or "~/.ssh/span"
    ).strip()
    path = _expand_path(root) / serial / "spanssh-config"
    return path if path.is_file() else None


def spanssh_ready(gateway_serial: str = "", config_override: str = "") -> bool:
    return spanssh_config_path(gateway_serial, config_override) is not None


def _ssh_base_opts(timeout_s: float) -> list[str]:
    """Fail fast — GSSAPI/DNS stalls were freezing the dashboard request thread."""
    connect = max(3, int(min(8, timeout_s)))
    return [
        "-o", "BatchMode=yes",
        "-o", "GSSAPIAuthentication=no",
        "-o", "ConnectTimeout={0}".format(connect),
        "-o", "ServerAliveInterval=2",
        "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=accept-new",
    ]


def ssh_run_spanssh(
    *,
    gateway_serial: str,
    command: str,
    config_file: str = "",
    timeout_s: float = 60,
) -> tuple[int, str, str]:
    """Run via ``ssh -F ~/.ssh/span/<serial>/spanssh-config <serial> …``."""
    serial = (gateway_serial or "").strip()
    if not serial:
        return 255, "", "GATEWAY_SERIAL not set (needed for spanssh-config)"
    cfg = spanssh_config_path(serial, config_file)
    if cfg is None:
        hint = config_file.strip() or f"~/.ssh/span/{serial}/spanssh-config"
        return 255, "", f"spanssh-config not found: {hint}"
    cmd = [
        "ssh",
        "-F",
        str(cfg),
        *_ssh_base_opts(timeout_s),
        serial,
        command,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 255, "", f"SSH timed out after {timeout_s:.0f}s: {exc}"
    except FileNotFoundError:
        return 255, "", "ssh binary not found on PATH"
    except Exception as exc:
        return 255, "", str(exc)


def ssh_run_cert(
    *,
    host: str,
    username: str,
    identity_file: str,
    command: str,
    timeout_s: float = 60,
) -> tuple[int, str, str]:
    """Run a remote command with OpenSSH cert/key auth (preferred lab-cell path)."""
    key = _expand_path(identity_file)
    if not key.is_file():
        return 255, "", f"SSH identity not found: {key}"
    cmd = [
        "ssh",
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PreferredAuthentications=publickey",
        *_ssh_base_opts(timeout_s),
        f"{username}@{host}",
        command,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 255, "", f"SSH timed out after {timeout_s:.0f}s: {exc}"
    except FileNotFoundError:
        return 255, "", "ssh binary not found on PATH"
    except Exception as exc:
        return 255, "", str(exc)


def ssh_run_system(
    *,
    host: str,
    username: str,
    command: str,
    timeout_s: float = 60,
) -> tuple[int, str, str]:
    """Run via default OpenSSH (ssh_config / agent / default keys).

    Matches interactive ``ssh user@host '…'`` on the wall PC. Used when a
    forced ``-i`` + ``IdentitiesOnly=yes`` cert path gets Permission denied
    but bare ssh still works.
    """
    cmd = [
        "ssh",
        *_ssh_base_opts(timeout_s),
        f"{username}@{host}",
        command,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 255, "", f"SSH timed out after {timeout_s:.0f}s: {exc}"
    except FileNotFoundError:
        return 255, "", "ssh binary not found on PATH"
    except Exception as exc:
        return 255, "", str(exc)


def _is_ssh_auth_failure(code: int, err: str, out: str = "") -> bool:
    text = f"{err or ''}\n{out or ''}".lower()
    if code == 0:
        return False
    return (
        "permission denied" in text
        or "authentication failed" in text
        or "no authentication methods" in text
        or "publickey" in text and "denied" in text
    )


def ssh_run_password(
    *,
    host: str,
    username: str,
    password: str,
    command: str,
    timeout_s: float = 60,
) -> tuple[int, str, str]:
    """Run one command over SSH with password or agent (paramiko fallback)."""
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    use_agent = not bool(password) and os.environ.get("GATEWAY_SSH_USE_AGENT", "1") != "0"
    try:
        client.connect(
            host,
            username=username,
            password=password or None,
            timeout=min(8.0, timeout_s),
            banner_timeout=min(8.0, timeout_s),
            auth_timeout=min(8.0, timeout_s),
            allow_agent=use_agent,
            look_for_keys=use_agent,
        )
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout_s)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err
    except Exception as exc:
        return 255, "", str(exc)
    finally:
        client.close()

# Back-compat name used by older call sites / docs


def ssh_run(
    *,
    host: str,
    username: str,
    password: str,
    command: str,
    timeout_s: float = 60,
) -> tuple[int, str, str]:
    return ssh_run_password(
        host=host,
        username=username,
        password=password,
        command=command,
        timeout_s=timeout_s,
    )


class GatewayShell:
    """Run shell commands on the lab gateway via SSH.

    Preferred: ``ssh -F ~/.ssh/span/<serial>/spanssh-config <serial>`` (no
    long-term CA cert). Fallbacks: cert identity, system ssh, password/agent.
    """

    def __init__(
        self,
        *,
        ssh_host: str,
        ssh_user: str = "root",
        ssh_password: str = "",
        drive_serial: str = "",
        identity_file: str = "",
        auth_mode: str = "auto",
        gateway_serial: str = "",
        spanssh_config: str = "",
    ) -> None:
        self.ssh_host = (ssh_host or "").strip()
        self.ssh_user = (ssh_user or "root").strip()
        self.ssh_password = ssh_password or ""
        self.drive_serial = (drive_serial or "").strip()
        self.gateway_serial = (gateway_serial or "").strip()
        self.identity_file = (identity_file or _default_identity()).strip()
        self.spanssh_config = (spanssh_config or "").strip()
        mode = (auth_mode or "auto").strip().lower()
        if mode not in ("auto", "spanssh", "cert", "password", "agent", "system"):
            mode = "auto"
        self.auth_mode = mode
        # Optional dashboard hook: on_command(command, code, out, err, auth)
        self.on_command: Optional[Callable[..., None]] = None

    def _spanssh_cfg(self) -> Optional[Path]:
        return spanssh_config_path(self.gateway_serial, self.spanssh_config)

    def _resolved_auth(self) -> str:
        """Return effective auth: spanssh | cert | system | password | agent | none."""
        if self.auth_mode == "spanssh":
            return "spanssh" if self._spanssh_cfg() is not None else "none"
        if self.auth_mode == "cert":
            return "cert" if identity_ready(self.identity_file) else "none"
        if self.auth_mode == "system":
            return "system"
        if self.auth_mode == "password":
            return "password" if self.ssh_password else "none"
        if self.auth_mode == "agent":
            return "agent"
        # auto: if GATEWAY_SERIAL is set, require spanssh-config (do NOT fall back
        # to root@GATEWAY_SSH_HOST — that path fails with Permission denied on
        # walls that only have spanssh). Legacy LAN/cert only when serial unset.
        if self.gateway_serial:
            return "spanssh" if self._spanssh_cfg() is not None else "none"
        if self._spanssh_cfg() is not None:
            return "spanssh"
        if identity_ready(self.identity_file):
            return "cert"
        if self.ssh_host:
            return "system"
        return "none"

    @property
    def available(self) -> bool:
        auth = self._resolved_auth()
        if auth == "none":
            return False
        if auth == "spanssh":
            return bool(self.gateway_serial)
        return bool(self.ssh_host)

    @property
    def mode(self) -> str:
        auth = self._resolved_auth()
        if auth == "none":
            return "none"
        return f"ssh-{auth}"

    def describe(self, command: str) -> str:
        auth = self._resolved_auth()
        if auth == "spanssh":
            cfg = self._spanssh_cfg()
            return (
                f"ssh -F {cfg} {self.gateway_serial} {command}"
                if cfg
                else f"ssh -F <spanssh-config> {self.gateway_serial} {command}"
            )
        if auth == "cert":
            key = _expand_path(self.identity_file)
            return (
                f"ssh -i {key} -o IdentitiesOnly=yes "
                f"{self.ssh_user}@{self.ssh_host} {command}"
            )
        return f"ssh {self.ssh_user}@{self.ssh_host} {command}"

    def run(self, command: str, *, timeout_s: float = 60) -> tuple[int, str, str]:
        auth = self._resolved_auth()
        if auth == "none":
            raise GatewayError(
                "No gateway auth — place spanssh-config on the wall "
                "(~/.ssh/span/<GATEWAY_SERIAL>/spanssh-config), or set "
                "GATEWAY_SSH_HOST + GATEWAY_SSH_AUTH=system / GATEWAY_SSH_PASS."
            )
        if auth != "spanssh" and not self.ssh_host:
            raise GatewayError("No gateway access — set GATEWAY_SSH_HOST.")

        attempts: list[tuple[str, tuple[int, str, str]]] = []

        def _try(label: str, fn) -> tuple[int, str, str]:
            result = fn()
            attempts.append((label, result))
            return result

        if auth == "spanssh":
            code, out, err = _try(
                "spanssh",
                lambda: ssh_run_spanssh(
                    gateway_serial=self.gateway_serial,
                    command=command,
                    config_file=self.spanssh_config,
                    timeout_s=timeout_s,
                ),
            )
            # No LAN/cert fallback — spanssh is the only path when selected
            # (GATEWAY_SSH_AUTH=spanssh, or auto with GATEWAY_SERIAL set).
        elif auth == "cert":
            code, out, err = _try(
                "cert",
                lambda: ssh_run_cert(
                    host=self.ssh_host,
                    username=self.ssh_user,
                    identity_file=self.identity_file,
                    command=command,
                    timeout_s=timeout_s,
                ),
            )
            if _is_ssh_auth_failure(code, err, out) and self.auth_mode == "auto":
                code, out, err = _try(
                    "system-fallback",
                    lambda: ssh_run_system(
                        host=self.ssh_host,
                        username=self.ssh_user,
                        command=command,
                        timeout_s=timeout_s,
                    ),
                )
        elif auth == "system":
            code, out, err = _try(
                "system",
                lambda: ssh_run_system(
                    host=self.ssh_host,
                    username=self.ssh_user,
                    command=command,
                    timeout_s=timeout_s,
                ),
            )
        else:
            code, out, err = _try(
                auth,
                lambda: ssh_run_password(
                    host=self.ssh_host,
                    username=self.ssh_user,
                    password=self.ssh_password if auth == "password" else "",
                    command=command,
                    timeout_s=timeout_s,
                ),
            )

        self._notify_command(command, code, out, err, auth)
        return code, out, err

    def _notify_command(
        self,
        command: str,
        code: int,
        out: str,
        err: str,
        auth: str,
    ) -> None:
        hook = self.on_command
        if hook is None:
            return
        try:
            hook(
                command=command,
                code=int(code),
                out=out or "",
                err=err or "",
                auth=auth,
            )
        except Exception:
            pass

    def popen(self, command: str):
        """Start a long-running remote command; return a process-like object with stdout."""
        auth = self._resolved_auth()
        if auth == "none":
            raise GatewayError("No gateway auth configured.")
        if auth == "spanssh":
            cfg = self._spanssh_cfg()
            if cfg is None:
                raise GatewayError("spanssh-config not found.")
            cmd = [
                "ssh",
                "-F",
                str(cfg),
                "-o",
                "BatchMode=yes",
                self.gateway_serial,
                command,
            ]
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        if not self.ssh_host:
            raise GatewayError("No gateway access — set GATEWAY_SSH_HOST.")
        if auth in ("cert", "system"):
            use_cert = auth == "cert" and self.auth_mode == "cert"
            if use_cert:
                key = _expand_path(self.identity_file)
                cmd = [
                    "ssh",
                    "-i",
                    str(key),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    f"{self.ssh_user}@{self.ssh_host}",
                    command,
                ]
            else:
                cmd = [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    f"{self.ssh_user}@{self.ssh_host}",
                    command,
                ]
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        use_agent = auth == "agent" or (
            not self.ssh_password and os.environ.get("GATEWAY_SSH_USE_AGENT", "1") != "0"
        )
        client.connect(
            self.ssh_host,
            username=self.ssh_user,
            password=self.ssh_password or None,
            timeout=15,
            allow_agent=use_agent,
            look_for_keys=use_agent,
        )
        transport = client.get_transport()
        if transport is None:
            client.close()
            raise GatewayError("SSH transport unavailable")
        channel = transport.open_session()
        channel.get_pty(term="dumb", width=200, height=40)
        channel.exec_command(command)

        class _ChannelProc:
            def __init__(self, ch, ssh_client):
                self._ch = ch
                self._client = ssh_client
                self.stdout = self

            def readline(self):
                if self._ch.recv_ready():
                    data = self._ch.recv(4096)
                    return data.decode("utf-8", errors="replace")
                return ""

            def poll(self):
                if self._ch.exit_status_ready():
                    return self._ch.recv_exit_status()
                return None

            def terminate(self):
                try:
                    self._ch.close()
                finally:
                    self._client.close()

        return _ChannelProc(channel, client)

    def run_ok(self, command: str, *, timeout_s: float = 60) -> str:
        """Run command; raise GatewayError on non-zero exit."""
        code, out, err = self.run(command, timeout_s=timeout_s)
        text = (out or err or "").strip()
        if code != 0:
            raise GatewayError(
                f"Gateway command failed (exit {code}): {command}\n"
                f"{text or err}\n"
                f"(auth={self.mode}; try: {self.describe(command)!r})"
            )
        return text

    def test_connection(self) -> dict:
        """Probe SSH with ``true``."""
        auth = self._resolved_auth()
        if auth == "none":
            serial = self.gateway_serial or "<GATEWAY_SERIAL>"
            if self.gateway_serial or self.auth_mode in ("spanssh", "auto"):
                msg = (
                    f"spanssh-config missing for {serial}. On the lab cell run "
                    f"span_ssh for {serial} so ~/.ssh/span/{serial}/spanssh-config "
                    f"exists (do not overwrite a working file). "
                    "Do not use GATEWAY_SSH_HOST/publickey for this wall."
                )
            else:
                msg = (
                    "No gateway auth. Set GATEWAY_SERIAL + sync spanssh-config, "
                    "or GATEWAY_SSH_HOST + GATEWAY_SSH_AUTH."
                )
            return {
                "ok": False,
                "mode": self.mode,
                "host": self.ssh_host or None,
                "gateway_serial": self.gateway_serial or None,
                "message": msg,
            }
        if auth != "spanssh" and not self.ssh_host:
            return {
                "ok": False,
                "mode": self.mode,
                "message": "GATEWAY_SSH_HOST not configured",
            }
        code, out, err = self.run("true", timeout_s=8)
        auth = self._resolved_auth()
        cfg = self._spanssh_cfg()
        if code == 0:
            target = (
                self.gateway_serial
                if auth == "spanssh"
                else f"{self.ssh_user}@{self.ssh_host}"
            )
            return {
                "ok": True,
                "mode": self.mode,
                "auth": auth,
                "host": self.ssh_host or None,
                "gateway_serial": self.gateway_serial or None,
                "spanssh_config": str(cfg) if cfg else None,
                "identity": (
                    str(_expand_path(self.identity_file)) if auth == "cert" else None
                ),
                "message": f"Gateway SSH ready ({target}) via {auth}",
                "command": self.describe("true"),
            }
        return {
            "ok": False,
            "mode": self.mode,
            "auth": auth,
            "host": self.ssh_host or None,
            "gateway_serial": self.gateway_serial or None,
            "spanssh_config": str(cfg) if cfg else None,
            "message": (err or out or f"exit {code}").strip(),
            "command": self.describe("true"),
        }


def build_gateway_shell_from_env(
    *,
    host: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    drive_serial: Optional[str] = None,
    identity_file: Optional[str] = None,
    auth_mode: Optional[str] = None,
    gateway_serial: Optional[str] = None,
    spanssh_config: Optional[str] = None,
) -> GatewayShell:
    return GatewayShell(
        ssh_host=host if host is not None else os.environ.get("GATEWAY_SSH_HOST", ""),
        ssh_user=user if user is not None else os.environ.get("GATEWAY_SSH_USER", "root"),
        ssh_password=(
            password
            if password is not None
            else (
                os.environ.get("GATEWAY_SSH_PASS")
                or os.environ.get("GATEWAY_SSH_PASSWORD")
                or ""
            )
        ),
        drive_serial=(
            drive_serial
            if drive_serial is not None
            else os.environ.get("DRIVE_SERIAL", "")
        ),
        identity_file=(
            identity_file
            if identity_file is not None
            else os.environ.get("GATEWAY_SSH_IDENTITY", "~/.ssh/id_ecdsa")
        ),
        auth_mode=(
            auth_mode
            if auth_mode is not None
            else os.environ.get("GATEWAY_SSH_AUTH", "auto")
        ),
        gateway_serial=(
            gateway_serial
            if gateway_serial is not None
            else os.environ.get("GATEWAY_SERIAL", "")
        ),
        spanssh_config=(
            spanssh_config
            if spanssh_config is not None
            else (
                os.environ.get("GATEWAY_SPANSSH_CONFIG")
                or os.environ.get("SPANSSH_CONFIG")
                or ""
            )
        ),
    )


def cert_setup_hint() -> str:
    """Short operator hint for gateway SSH setup on the wall PC."""
    return (
        "Preferred: on the wall PC, generate/use spanssh-config "
        "(ssh -F ~/.ssh/span/<GATEWAY_SERIAL>/spanssh-config <GATEWAY_SERIAL>). "
        "Legacy cert: copy id_ecdsa + id_ecdsa-cert.pub to ~/.ssh/ on the wall."
    )
