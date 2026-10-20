"""Gateway UART / journal capture for Drive dashboard.

Streams gateway logs over SSH (cert preferred via ``GatewayShell``, password
fallback). Primary source is ``span-sbx-seriald`` journal; optional ST-Link /
ACM serial devices are included when present.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


def _ts_file() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class UartCapture:
    """Non-interactive UART/journal capture suitable for dashboard start/stop."""

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str = "",
        output_dir: str,
        identity_file: str = "",
        auth_mode: str = "auto",
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.output_dir = output_dir
        self.identity_file = identity_file or os.environ.get(
            "GATEWAY_SSH_IDENTITY", "~/.ssh/id_ecdsa"
        )
        self.auth_mode = auth_mode or "auto"
        self.log_files: dict[str, str] = dict()
        self.capture_files: dict[str, str] = dict()
        self.last_error = ""
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _shell(self):
        from drive_automation.gateway import GatewayShell

        return GatewayShell(
            ssh_host=self.host,
            ssh_user=self.username,
            ssh_password=self.password,
            identity_file=self.identity_file,
            auth_mode=self.auth_mode,
        )

    def start(self, filter_strings: Optional[list[str]] = None) -> bool:
        """Start background streams. Returns False on setup failure."""
        filters = [s.strip() for s in (filter_strings or []) if s and s.strip()]
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        stamp = _ts_file()

        sources: list[tuple[str, str]] = [
            (
                "span-sbx-seriald",
                "journalctl -u span-sbx-seriald -f -n 200 --no-pager -o short-iso",
            ),
            (
                "span-pcm",
                "journalctl -u span-pcm -f -n 100 --no-pager -o short-iso 2>/dev/null || true",
            ),
        ]

        probe = self._run_remote(
            "ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null; "
            "pio device list 2>/dev/null | head -40 || true"
        )
        for line in probe.splitlines():
            line = line.strip()
            if line.startswith("/dev/ttyACM") or line.startswith("/dev/ttyUSB"):
                dev = line.split()[0]
                name = Path(dev).name
                sources.append(
                    (
                        f"serial-{name}",
                        f"stty -F {dev} 115200 raw -echo 2>/dev/null; cat {dev}",
                    )
                )

        code, out, err = self._run_remote_rc("echo ok")
        if code != 0 or "ok" not in out:
            self.last_error = err or out or "SSH to gateway failed"
            return False

        self._stop.clear()
        for module, cmd in sources:
            log_path = str(Path(self.output_dir) / f"{module}_uart_log_{stamp}.log")
            cap_path = str(
                Path(self.output_dir) / f"{module}_uart_capture_{stamp}.log"
            )
            self.log_files[module] = log_path
            self.capture_files[module] = cap_path
            th = threading.Thread(
                target=self._stream_one,
                args=(module, cmd, log_path, cap_path, filters),
                name=f"uart-{module}",
                daemon=True,
            )
            self._threads.append(th)
            th.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=5)
        self._threads.clear()

    def _stream_one(
        self,
        module: str,
        remote_cmd: str,
        log_path: str,
        cap_path: str,
        filters: list[str],
    ) -> None:
        try:
            proc = self._shell().popen(remote_cmd)
        except Exception as exc:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write(f"=== ERROR starting {module}: {exc} ===\n")
            return

        with (
            open(log_path, "w", buffering=1, encoding="utf-8") as log_fh,
            open(cap_path, "w", buffering=1, encoding="utf-8") as cap_fh,
        ):
            log_fh.write(
                f"=== {module} UART/journal started {datetime.now().isoformat()} ===\n"
            )
            log_fh.write(f"=== host {self.username}@{self.host} ===\n")
            cap_fh.write(f"=== {module} filtered capture ===\n")
            while not self._stop.is_set():
                if proc.poll() is not None:
                    break
                line = ""
                if hasattr(proc, "stdout") and proc.stdout is not None:
                    if hasattr(proc.stdout, "readline"):
                        line = proc.stdout.readline()
                    elif hasattr(proc, "readline"):
                        line = proc.readline()
                if not line:
                    time.sleep(0.05)
                    continue
                text = (
                    line
                    if isinstance(line, str)
                    else line.decode("utf-8", errors="replace")
                )
                stamped = (
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {text.rstrip()}\n"
                )
                log_fh.write(stamped)
                for filt in filters:
                    if filt and filt in text:
                        cap_fh.write(stamped)
                        break
            try:
                proc.terminate()
            except Exception:
                pass
            log_fh.write(f"\n=== {module} ended {datetime.now().isoformat()} ===\n")

    def _run_remote(self, command: str) -> str:
        code, out, err = self._run_remote_rc(command)
        return (out or "") + (err or "")

    def _run_remote_rc(self, command: str) -> tuple[int, str, str]:
        try:
            return self._shell().run(command, timeout_s=30)
        except Exception as exc:
            return 255, "", str(exc)
