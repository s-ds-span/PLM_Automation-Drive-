#!/usr/bin/env python3
"""
UART Logger - ST-Link UART Logging Tool

Usage:
  Main panel (PCM & MainsMID from config):
    python3 uart_logger.py capture -h <IP> -u <USERNAME>

  Subpanel (PCM & MainsMID from config):
    python3 uart_logger.py capture -h <IP> -u <USERNAME> --sub

  Specific modules (branches, etc.):
    python3 uart_logger.py capture -h <IP> -u <USERNAME> -d <SERIAL1> -d <SERIAL2>

Config: Auto-fetched from GitHub repo. Set GITHUB_CONFIG_REPO env var or use -g flag.
  Example repo URL: https://raw.githubusercontent.com/your-org/your-repo/main/configs/
  Each panel should have its own YAML file named after its IP (e.g. 10.91.0.98.yml)
  or a master panels_config.yml with all panels.
"""

import argparse
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import signal
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
import getpass
import yaml

# ── GitHub config ─────────────────────────────────────────────────────────────
# Set this env var to your raw GitHub base URL (trailing slash required), e.g.:
#   export GITHUB_CONFIG_REPO="https://raw.githubusercontent.com/your-org/repo/main/configs/"
# Or pass via CLI flag: -g <URL>
DEFAULT_GITHUB_BASE = os.environ.get("GITHUB_CONFIG_REPO", "https://github.com/spanio/intern_India/tree/main/conf/rigs")


# ── Terminal colour helpers ────────────────────────────────────────────────────
COLOURS = {
    "PCM":      "\033[96m",   # cyan
    "MainsMID": "\033[93m",   # yellow
    "OTHER":    "\033[95m",   # magenta
    "RESET":    "\033[0m",
    "GREEN":    "\033[92m",
    "RED":      "\033[91m",
    "BOLD":     "\033[1m",
}

MODULE_COLOUR_MAP: Dict[str, str] = {}   # populated at runtime


def _colour_for(module_name: str) -> str:
    if module_name not in MODULE_COLOUR_MAP:
        if module_name.startswith("PCM"):
            MODULE_COLOUR_MAP[module_name] = COLOURS["PCM"]
        elif module_name.startswith("MainsMID"):
            MODULE_COLOUR_MAP[module_name] = COLOURS["MainsMID"]
        else:
            MODULE_COLOUR_MAP[module_name] = COLOURS["OTHER"]
    return MODULE_COLOUR_MAP[module_name]


def print_capture(module_name: str, line: str, filter_str: str):
    """Print a captured (filter-matched) line to terminal, colour-coded per module."""
    col = _colour_for(module_name)
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag = f"{COLOURS['BOLD']}{col}[{module_name}]{COLOURS['RESET']}"
    match_tag = f"{COLOURS['GREEN']}[MATCH: '{filter_str}']{COLOURS['RESET']}"
    print(f"{tag} {match_tag} {ts} | {line.strip()}", flush=True)


def print_info(msg: str):
    print(msg, flush=True)


# ── Live capture pop-up window ─────────────────────────────────────────────────

def open_live_capture_window(module_name: str, capture_file: str):
    """
    Open a new terminal window that tail -f's the capture file.
    Supports macOS (Terminal.app / iTerm2) and Linux (gnome-terminal / xterm / xfce4-terminal).
    The window stays open and updates live as new captured lines are written.
    """
    title   = f"CAPTURE: {module_name}"
    tail_cmd = f"tail -f '{capture_file}'"

    platform = sys.platform

    try:
        if platform == "darwin":
            # macOS — open a new Terminal.app window
            apple_script = (
                f'tell application "Terminal"\n'
                f'  do script "echo \\"=== Live Capture: {module_name} ===\\"; {tail_cmd}"\n'
                f'  set custom title of front window to "{title}"\n'
                f'  activate\n'
                f'end tell'
            )
            subprocess.Popen(["osascript", "-e", apple_script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        elif platform.startswith("linux"):
            # Try common Linux terminal emulators in order of preference
            launched = False
            for emulator, args in [
                ("gnome-terminal", ["gnome-terminal", "--title", title, "--", "bash", "-c", f"{tail_cmd}; read"]),
                ("xfce4-terminal", ["xfce4-terminal", "--title", title, "-e", f"bash -c '{tail_cmd}; read'"]),
                ("xterm",          ["xterm", "-title", title, "-e", f"bash -c '{tail_cmd}; read'"]),
                ("konsole",        ["konsole", "--title", title, "-e", f"bash -c '{tail_cmd}; read'"]),
            ]:
                if subprocess.run(["which", emulator], capture_output=True).returncode == 0:
                    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    launched = True
                    break

            if not launched:
                print_info(f"  [!] No supported terminal emulator found for live pop-up ({module_name}).")
                print_info(f"      Run manually: tail -f '{capture_file}'")
        else:
            print_info(f"  [!] Live pop-up not supported on {platform}.")
            print_info(f"      Run manually: tail -f '{capture_file}'")

        # Small delay so windows open in a predictable order (PCM first, then MainsMID)
        time.sleep(0.4)

    except Exception as e:
        print_info(f"  [!] Could not open live window for {module_name}: {e}")


# ── GitHub YAML loader ─────────────────────────────────────────────────────────

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Print token status at import time so it's visible immediately on run
if GITHUB_TOKEN:
    print(f"  ✓ GITHUB_TOKEN loaded ({GITHUB_TOKEN[:8]}...)", flush=True)
else:
    print("  ⚠ GITHUB_TOKEN not set — private repos will fail.", flush=True)
    print("    Run: export GITHUB_TOKEN=\"ghp_yourtoken\"", flush=True)


def _fetch_url(url: str, timeout: int = 10) -> Optional[str]:
    """Fetch text content from a URL. Returns None on 404, raises on other errors."""
    try:
        headers = {"User-Agent": "uart-logger/3"}
        if GITHUB_TOKEN and "githubusercontent.com" in url:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8")
            print_info(f"  ✓ HTTP 200 OK — got {len(content)} bytes")
            return content

    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        print_info(f"  ✗ HTTP {e.code} for {url}")
        print_info(f"    Response: {body}")
        if e.code == 404:
            return None
        if e.code in (401, 403):
            print_info(
                f"  ✗ GitHub auth error ({e.code}) — repo may be private.\n"
                f"    Set a personal access token:\n"
                f"      export GITHUB_TOKEN=<your_token>\n"
                f"    Then re-run. Token needs at least 'repo' (read) scope."
            )
            return None
        return None
    except Exception as e:
        print_info(f"  ✗ Network error fetching {url}: {e}")
        return None


def load_config_from_github(base_url: str, host: str) -> Optional[Dict]:
    """
    Try to load YAML config from GitHub.

    Strategy (in order):
      1. <base_url>/<host>.yml          – per-panel file named after the IP
      2. <base_url>/<host>.yaml
      3. <base_url>/panels_config.yml   – master file with all panels
      4. <base_url>/panels_config.yaml

    Returns parsed YAML dict or None.
    """
    # Auto-convert GitHub browser URLs to raw content URLs.
    # e.g. https://github.com/org/repo/tree/main/configs/
    #   -> https://raw.githubusercontent.com/org/repo/main/configs/
    if "github.com" in base_url and "raw.githubusercontent.com" not in base_url:
        base_url = re.sub(
            r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*)",
            r"https://raw.githubusercontent.com/\1/\2/refs/heads/\3/\4",
            base_url.rstrip("/"),
        )
        # Handle bare repo URL with no path after branch (edge case)
        base_url = re.sub(
            r"https://github\.com/([^/]+)/([^/]+)/?$",
            r"https://raw.githubusercontent.com/\1/\2/refs/heads/main",
            base_url,
        )
        # Ensure there is always a trailing slash so the filename is appended correctly
        base_url = base_url.rstrip("/") + "/"
        print_info(f"  (Auto-converted to raw URL: {base_url})")

    base = base_url.rstrip("/") + "/"

    candidates = [
        f"{base}{host}.yml",
        f"{base}{host}.yaml",
        f"{base}panels_config.yml",
        f"{base}panels_config.yaml",
    ]

    for url in candidates:
        print_info(f"  Trying GitHub: {url}")
        text = _fetch_url(url)
        if text is not None:
            print_info(f"  ✓ Loaded config from: {url}")
            data = yaml.safe_load(text)

            # Support two YAML layouts:
            #
            # Layout A — per-panel file with metadata/panel/probes at top level:
            #   metadata:
            #     name: in-sys-wall-001-type
            #   probes:
            #     <serial>:
            #       module: PCM
            #       uart_device: /dev/serial/by-id/...
            #
            # Layout B — master file with all panels under a 'panels' key:
            #   panels:
            #     10.91.0.162:
            #       name: ...
            #       probes: [...]
            #
            if data and "panels" not in data:
                # Layout A: normalise into Layout B so the rest of the code is uniform
                probe_list = []
                raw_probes = data.get("probes", {})
                if isinstance(raw_probes, dict):
                    # probes is a dict keyed by hw-serial
                    for hw_serial, probe_info in raw_probes.items():
                        entry = dict(probe_info)
                        entry.setdefault("hw_serial", hw_serial)
                        probe_list.append(entry)
                elif isinstance(raw_probes, list):
                    probe_list = raw_probes

                panel_name = (data.get("metadata") or {}).get("name", host)
                data = {
                    "panels": {
                        host: {
                            "name": panel_name,
                            "probes": probe_list,
                        }
                    }
                }

            return data

    print_info(f"  ✗ No config found at GitHub base: {base_url}")
    return None


# ── Main logger class ──────────────────────────────────────────────────────────

class UARTLogger:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        config_file: str = None,
        github_base: str = None,
        output_dir: str = None,
        is_subpanel: bool = False,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.output_dir = output_dir or os.path.expanduser("~/uart_logs")
        self.config_file = config_file
        self.github_base = github_base or DEFAULT_GITHUB_BASE
        self.is_subpanel = is_subpanel
        self.panel_name = None
        self.processes = {}
        self.threads = {}
        self.running = True
        self.filter_strings = []
        self.log_files = {}
        self.capture_files = {}
        self.file_handles = {}
        self.last_setup_error: Optional[str] = None

        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

    # ── Config loading ─────────────────────────────────────────────────────────

    def load_config(self) -> Optional[Dict]:
        """
        Load panel configuration. Priority:
          1. Explicit -c / --config local file
          2. GitHub repo (if -g or GITHUB_CONFIG_REPO env var set)
          3. Local fallback paths (same dir, cwd, home)
        """
        # 1. Explicit local file
        if self.config_file and Path(self.config_file).exists():
            print_info(f"  Loading config from: {self.config_file}")
            with open(self.config_file, "r") as f:
                return yaml.safe_load(f)

        # 2. GitHub
        if self.github_base:
            print_info(f"\n  Fetching config from GitHub ({self.github_base})...")
            cfg = load_config_from_github(self.github_base, self.host)
            if cfg:
                return cfg
            print_info("  Falling back to local config search...")

        # 3. Local fallback
        local_paths = [
            Path(__file__).parent / "panels_config.yml",
            Path.cwd() / "panels_config.yml",
            Path.home() / "panels_config.yml",
        ]
        for p in local_paths:
            if p.exists():
                print_info(f"  Loading local config from: {p}")
                with open(p, "r") as f:
                    return yaml.safe_load(f)

        return None

    def get_default_probes(self) -> List[Dict]:
        """Get PCM and MainsMID probes from config for this host"""
        config = self.load_config()

        if not config or "panels" not in config:
            print_info("WARNING: No config file found or invalid format.")
            return []

        if self.host not in config["panels"]:
            print_info(f"WARNING: Host {self.host} not found in config.")
            print_info(f"Available hosts: {list(config['panels'].keys())}")
            return []

        panel = config["panels"][self.host]
        self.panel_name = panel.get("name", self.host)

        if self.is_subpanel:
            probes = panel.get("subpanel", [])
            panel_type = "SUBPANEL"
            if not probes:
                print_info(f"WARNING: No subpanel config for {self.host}")
                return []
        else:
            probes = panel.get("probes", [])
            panel_type = "MAIN PANEL"

        print_info(f"  Loading {panel_type} probes...")

        default_probes = []
        for probe in probes:
            module = probe.get("module", "")
            if module.startswith("PCM") or module.startswith("MainsMID"):
                # Support both 'uart_device' (GitHub YAML) and legacy key names
                device = probe.get("uart_device") or probe.get("device") or probe.get("path", "")
                print_info(f"  Default module: {module} -> {device}")
                # Normalise so the rest of the code always uses 'uart_device'
                probe = dict(probe)
                probe["uart_device"] = device
                default_probes.append(probe)

        return default_probes

    # ── SSH / local capture helpers ─────────────────────────────────────────────

    def _local_ips(self) -> set[str]:
        ips = {"127.0.0.1", "::1", "localhost"}
        try:
            ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
        try:
            proc = subprocess.run(
                ["hostname", "-I"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            ips.update(part.strip() for part in proc.stdout.split() if part.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ips

    def is_local_host(self) -> bool:
        """True when capture host is this machine (no SSH / sshpass needed)."""
        host = (self.host or "").strip().lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        return host in self._local_ips()

    def _sshpass_error(self) -> Optional[str]:
        if self.is_local_host():
            return None
        if shutil.which("sshpass"):
            return None
        return (
            "sshpass is not installed on the machine running the dashboard. "
            "Install on that host: sudo apt install sshpass — or if uvicorn runs on "
            "the same wall PC as the ST-Links, set Log capture host to 127.0.0.1."
        )

    def _run_on_target(self, command: str, *, timeout: int = 30) -> str:
        try:
            if self.is_local_host():
                proc = subprocess.run(
                    ["bash", "-lc", command],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                return proc.stdout + proc.stderr
            err = self._sshpass_error()
            if err:
                return f"Error: {err}"
            inner = command.replace("'", "'\"'\"'")
            cmd = (
                f"sshpass -p '{self.password}' ssh -o StrictHostKeyChecking=no "
                f"{self.username}@{self.host} \"bash -lc '{inner}'\""
            )
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return proc.stdout + proc.stderr
        except Exception as exc:
            return f"Error: {exc}"

    def _popen_stream_on_target(self, command: str) -> subprocess.Popen:
        err = self._sshpass_error()
        if err:
            raise RuntimeError(err)
        if self.is_local_host():
            return subprocess.Popen(
                ["bash", "-lc", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
            )
        inner = command.replace("'", "'\"'\"'")
        ssh_cmd = (
            f"sshpass -p '{self.password}' ssh -o StrictHostKeyChecking=no "
            f"{self.username}@{self.host} \"bash -lc '{inner}'\""
        )
        return subprocess.Popen(
            ssh_cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )

    def ssh_command(self, command: str, timeout: int = 30) -> str:
        return self._run_on_target(command, timeout=timeout)

    def get_devices(self) -> Dict[str, Dict[str, str]]:
        output = self.ssh_command("pio device list")
        devices = {}
        current_device = current_serial = current_description = None

        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("/dev/tty"):
                if current_device and (current_serial or current_description):
                    devices[current_device] = {
                        "path": current_device,
                        "serial": current_serial or "",
                        "description": current_description or "",
                    }
                current_device = line
                current_serial = current_description = None
            elif "SER=" in line and current_device:
                m = re.search(r"SER=([A-Za-z0-9]+)", line)
                if m:
                    current_serial = m.group(1)
            elif line.startswith("Description:") and current_device:
                current_description = line.replace("Description:", "").strip()

        if current_device and (current_serial or current_description):
            devices[current_device] = {
                "path": current_device,
                "serial": current_serial or "",
                "description": current_description or "",
            }
        return devices

    def match_device(self, search_str: str, device_map: Dict[str, Dict[str, str]]) -> Optional[tuple]:
        for dev_path, info in device_map.items():
            if (
                search_str.lower() in info["serial"].lower()
                or info["serial"].lower() in search_str.lower()
            ):
                return (info["serial"], dev_path)
        return None

    # ── File setup ─────────────────────────────────────────────────────────────

    def setup_files(self, module_name: str, serial: str):
        panel_suffix = f"{self.panel_name}_{self.host}" if self.panel_name else self.host

        if self.is_subpanel:
            folder_name = f"subpanel_{module_name}_{panel_suffix}"
            file_prefix = f"subpanel_{module_name}"
        else:
            folder_name = f"{module_name}_{panel_suffix}"
            file_prefix = module_name

        module_dir = Path(self.output_dir) / folder_name
        module_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file     = module_dir / f"{file_prefix}_uart_log_{timestamp}.log"
        capture_file = module_dir / f"{file_prefix}_uart_capture_{timestamp}.log"

        self.log_files[module_name]     = str(log_file)
        self.capture_files[module_name] = str(capture_file)

        # Line-buffered + explicit UTF-8 so headers and each line hit the filesystem promptly.
        # Default block buffering leaves tiny headers only in RAM, so HTTP downloads looked empty.
        io_kw = dict(encoding="utf-8", newline="\n", buffering=1)
        self.file_handles[module_name] = {
            "log":     open(log_file, "w", **io_kw),
            "capture": open(capture_file, "w", **io_kw),
        }

        panel_type = "Subpanel" if self.is_subpanel else "Main Panel"
        for key, label in [("log", "UART Log"), ("capture", "UART Capture")]:
            fh = self.file_handles[module_name][key]
            fh.write(f"=== {label} Started: {datetime.now().isoformat()} ===\n")
            fh.write(f"=== {panel_type} | Module: {module_name} | Serial: {serial} ===\n\n")

        self.file_handles[module_name]["log"].flush()
        self.file_handles[module_name]["capture"].flush()

        return log_file, capture_file

    @staticmethod
    def _remote_uart_command(device_path: str) -> str:
        """
        Shell command on the wall PC to stream UART bytes to stdout.

        ``pio device monitor`` often prints nothing when run under SSH with a
        piped stdout (dashboard capture). ``stty`` + ``cat`` works non-interactively.
        Set ``UART_USE_PIO_MONITOR=1`` to force PlatformIO monitor instead.
        """
        dev = shlex.quote(device_path)
        if os.environ.get("UART_USE_PIO_MONITOR", "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            return f"pio device monitor -p {dev} -b 115200 --raw --echo"
        return f"stty -F {dev} 115200 cs8 -cstopb -parenb raw -echo 2>/dev/null; cat {dev}"

    def _log_note(self, module_name: str, message: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  [uart-logger] {message}\n"
        handles = self.file_handles.get(module_name) or {}
        for key in ("log", "capture"):
            fh = handles.get(key)
            if fh is None or fh.closed:
                continue
            try:
                fh.write(line)
                fh.flush()
            except OSError:
                pass

    # ── Logging thread ─────────────────────────────────────────────────────────

    def log_device_thread(self, module_name: str, device_path: str):
        """
        Thread: streams UART data for one module.

        Terminal output policy:
          • Raw logs  → written to log file ONLY (silent on terminal).
          • Captured  → written to capture file AND printed to terminal
                        with a colour-coded, clearly labelled prefix.
        """
        monitor_cmd = self._remote_uart_command(device_path)
        transport = "local" if self.is_local_host() else "ssh"
        self._log_note(module_name, f"device={device_path}")
        self._log_note(module_name, f"transport={transport} host={self.host}")
        self._log_note(module_name, f"stream_cmd={monitor_cmd}")

        try:
            process = self._popen_stream_on_target(monitor_cmd)
            self.processes[module_name] = process

            skip_patterns = [
                "--- Miniterm",
                "--- Quit:",
                "--- port:",
                "port is",
                "baudrate is",
                "parity is",
                "databits",
                "stopbits",
                "flowcontrol",
                "escape is",
                "local echo",
                "noinit",
                "noreset",
                "hangup",
                "nolock",
                "send_cmd",
                "receive_cmd",
                "imap is",
                "omap is",
                "emap is",
                "logfile",
                "initstring",
                "exit_after",
                "exit is",
                "picocom",
                "Terminal ready",
                "Connection to",
                "Disconnected",
                "FATAL",
            ]

            while self.running and process.poll() is None:
                try:
                    line = process.stdout.readline()
                    if line == "":
                        if process.poll() is not None:
                            break
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    if any(p.lower() in line.lower() for p in skip_patterns):
                        continue

                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    timestamped = f"{ts}  {line}\n"

                    if module_name in self.file_handles:
                        self.file_handles[module_name]["log"].write(timestamped)
                        self.file_handles[module_name]["log"].flush()
                        write_capture = False
                        if self.filter_strings:
                            for filter_str in self.filter_strings:
                                if filter_str in line:
                                    write_capture = True
                                    break
                        else:
                            write_capture = True
                        if write_capture:
                            self.file_handles[module_name]["capture"].write(timestamped)
                            self.file_handles[module_name]["capture"].flush()

                    if not self.filter_strings:
                        col = _colour_for(module_name)
                        print(
                            f"{COLOURS['BOLD']}{col}[{module_name}]{COLOURS['RESET']} "
                            f"{ts}  {line}",
                            flush=True,
                        )

                except Exception as e:
                    print_info(f"[{module_name}] Error reading line: {e}")
                    self._log_note(module_name, f"read error: {e}")
                    break

            code = process.poll()
            if code is not None:
                hint = (
                    "Check device path, dialout group, and that nothing else holds the port."
                )
                if code == 127:
                    hint = (
                        "Exit 127 often means a missing command (e.g. sshpass on the "
                        "dashboard host: sudo apt install sshpass) or use host 127.0.0.1 "
                        "when uvicorn runs on the wall PC."
                    )
                self._log_note(
                    module_name,
                    f"stream ended (exit {code}). {hint}",
                )

        except Exception as e:
            print_info(f"[{module_name}] Failed to start: {e}")
            self._log_note(module_name, f"failed to start: {e}")

    # ── Filter input ───────────────────────────────────────────────────────────

    def get_multiple_filter_strings(self) -> List[str]:
        print_info("\n" + "=" * 60)
        print_info("ENTER FILTER STRINGS")
        print_info("=" * 60)
        print_info("\nEnter filter strings to capture (one per line).")
        print_info("Press Enter on empty line when done.\n")

        filters = []
        count = 1
        while True:
            try:
                s = input(f"Filter string {count}: ").strip()
                if s == "":
                    if not filters:
                        print_info("No filters entered — all data will be shown on terminal.")
                    break
                filters.append(s)
                count += 1
            except EOFError:
                break
        return filters

    # ── Start / Stop ───────────────────────────────────────────────────────────

    def start(
        self,
        serials: Optional[List[str]] = None,
        filter_strings: Optional[List[str]] = None,
        dashboard: bool = False,
    ) -> bool:
        print_info("\n" + "=" * 60)
        print_info("UART Logger")
        print_info("=" * 60)

        ssh_err = self._sshpass_error()
        if ssh_err:
            print_info(f"\nERROR: {ssh_err}")
            self.last_setup_error = ssh_err
            return False
        if self.is_local_host():
            print_info(f"\nLocal capture (host {self.host} is this machine — no SSH).")
        else:
            print_info(f"\nRemote capture via SSH to {self.username}@{self.host} (sshpass).")

        matched: Dict[str, str] = {}

        if not serials:
            print_info(f"\nNo devices specified — loading defaults from config...")
            default_probes = self.get_default_probes()

            if not default_probes:
                msg = (
                    f"No PCM/MainsMID probes for host {self.host} in panels_config.yml "
                    "(or config missing / YAML invalid)"
                )
                print_info("\nERROR: No default probes found. Either:")
                print_info("  1. Add panel config to panels_config.yml (or GitHub)")
                print_info("  2. Specify devices with -d flag")
                self.last_setup_error = msg
                return False

            print_info(f"\nUsing device paths from config...")
            for probe in default_probes:
                module_name  = probe["module"]
                uart_device  = probe["uart_device"]
                matched[module_name] = uart_device
                print_info(f"  {module_name} -> {uart_device}")
        else:
            print_info(f"\nConnecting to {self.username}@{self.host}...")
            device_map = self.get_devices()

            if not device_map:
                print_info("ERROR: Could not get device list. Check SSH connection.")
                return False

            print_info(f"\nFound {len(device_map)} devices:")
            for dev_path, info in device_map.items():
                print_info(f"  {info['serial']} -> {dev_path}")

            print_info("\nMatching requested serials...")
            for req_serial in serials:
                result = self.match_device(req_serial, device_map)
                if result:
                    full_serial, path = result
                    matched[full_serial] = path
                    print_info(f"  '{req_serial}' -> {path} (matched: {full_serial})")
                else:
                    print_info(f"  '{req_serial}' -> NOT FOUND")

            if not matched:
                print_info("\nERROR: No devices matched. Check serial numbers.")
                return False

        # Setup files
        print_info(f"\nSetting up UART logging for {len(matched)} device(s)...")
        for module_name, device_path in matched.items():
            serial_match = re.search(r"STLINK-V3_([A-Za-z0-9]+)-if", device_path)
            serial = serial_match.group(1) if serial_match else module_name

            log_f, cap_f = self.setup_files(module_name, serial)
            print_info(f"  {module_name}:")
            print_info(f"    Device:  {device_path}")
            print_info(f"    Log:     {log_f}")
            print_info(f"    Capture: {cap_f}")

        # Filter strings: dashboard passes explicit list (possibly empty); CLI prompts.
        if filter_strings is not None:
            self.filter_strings = list(filter_strings)
        else:
            self.filter_strings = self.get_multiple_filter_strings()

        if self.filter_strings:
            print_info(f"\n*** Filters set ({len(self.filter_strings)} total): ***")
            for i, f in enumerate(self.filter_strings, 1):
                print_info(f"  {i}. '{f}'")
            print_info(
                "\nOnly lines matching ANY of these strings will appear on terminal.\n"
                "All raw data continues to be written to log files.\n"
            )
            for module_name in self.file_handles:
                self.file_handles[module_name]["capture"].write(
                    f"=== Filters: {', '.join(self.filter_strings)} ===\n\n"
                )
                self.file_handles[module_name]["capture"].flush()

            if not dashboard:
                print_info("\n── Opening live capture windows (one per module) ─────")
                for module_name in matched:
                    cap_path = self.capture_files[module_name]
                    print_info(f"  ↗  {module_name}: {cap_path}")
                    open_live_capture_window(module_name, cap_path)
                print_info("─────────────────────────────────────────────────────")
                print_info("  Each window shows ONLY that module's captured lines.")
                print_info("  No output mixing possible.\n")
        else:
            print_info("\nNo filters set — all data will be printed (colour-coded per module).\n")

        # Start logging threads
        for module_name, device_path in matched.items():
            t = threading.Thread(
                target=self.log_device_thread,
                args=(module_name, device_path),
                daemon=True,
            )
            self.threads[module_name] = t
            t.start()

        if dashboard:
            return True

        print_info("Press Ctrl+C to stop logging...\n")
        print_info("-" * 60)

        try:
            while self.running:
                time.sleep(0.5)
                if not any(t.is_alive() for t in self.threads.values()):
                    break
        except KeyboardInterrupt:
            print_info("\n\nStopping...")

        self.stop()
        return True

    def stop(self):
        self.running = False

        for ident, proc in self.processes.items():
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        for module_name, handles in self.file_handles.items():
            try:
                handles["log"].write(f"\n=== Log Ended: {datetime.now().isoformat()} ===\n")
                handles["capture"].write(f"\n=== Capture Ended: {datetime.now().isoformat()} ===\n")
                handles["log"].close()
                handles["capture"].close()
            except Exception:
                pass

        print_info("\n" + "=" * 60)
        print_info("LOGGING COMPLETE")
        print_info("=" * 60)
        print_info(f"\nFiles saved to: {self.output_dir}")
        for module_name in self.log_files:
            print_info(f"\n  {module_name}:")
            print_info(f"    Log:     {self.log_files[module_name]}")
            print_info(f"    Capture: {self.capture_files[module_name]}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="UART Logger - ST-Link UART Logging Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Main panel (PCM & MainsMID from GitHub config):
    python3 uart_logger.py capture -h 10.91.0.98 -u dsw -g https://raw.githubusercontent.com/org/repo/main/configs/

  Set GitHub base permanently via env var:
    export GITHUB_CONFIG_REPO="https://raw.githubusercontent.com/org/repo/main/configs/"
    python3 uart_logger.py capture -h 10.91.0.98 -u dsw

  Subpanel:
    python3 uart_logger.py capture -h 10.91.0.98 -u dsw --sub

  Specific modules (manual serial):
    python3 uart_logger.py capture -h 10.91.0.98 -u dsw -d 0042001B -d 0033001E

GitHub YAML naming convention (any of these work):
  <base>/<ip>.yml           e.g. 10.91.0.98.yml          (per-panel file)
  <base>/panels_config.yml  (master file, all panels under 'panels' key)

Password will be prompted securely (hidden input).
Enter filter strings one per line; empty line to finish.
Only CAPTURED (filter-matched) lines appear on terminal - colour-coded per module.
Press Ctrl+C to stop.
        """,
    )

    subparsers = parser.add_subparsers(dest="command")

    cap = subparsers.add_parser("capture", help="Start UART logging", add_help=False)
    cap.add_argument("-h", "--host",     required=True, dest="ssh_host", help="Lab cell IP")
    cap.add_argument("-u", "--username", required=True, help="SSH username")
    cap.add_argument(
        "-d", "--device", action="append", default=None,
        help="Serial number(s) — if omitted, uses PCM & MainsMID from config",
    )
    cap.add_argument("--sub", action="store_true", dest="is_subpanel",
                     help="Capture subpanel instead of main panel")
    cap.add_argument("-c", "--config",   help="Path to local panels_config.yml (optional)")
    cap.add_argument(
        "-g", "--github",
        help=(
            "Raw GitHub base URL for YAML configs, e.g. "
            "https://raw.githubusercontent.com/org/repo/main/configs/ "
            "(overrides GITHUB_CONFIG_REPO env var)"
        ),
    )
    cap.add_argument("-o", "--output",   help="Output directory (default: ~/uart_logs)")
    cap.add_argument("--help", action="help", help="Show this help message")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "capture":
        panel_type = "SUBPANEL" if args.is_subpanel else "MAIN PANEL"
        print("\n" + "=" * 60)
        print(f"SSH AUTHENTICATION ({panel_type})")
        print("=" * 60)
        password = getpass.getpass(f"\nEnter password for {args.username}@{args.ssh_host}: ")

        logger = UARTLogger(
            host=args.ssh_host,
            username=args.username,
            password=password,
            config_file=args.config,
            github_base=getattr(args, "github", None),
            output_dir=args.output,
            is_subpanel=args.is_subpanel,
        )

        signal.signal(signal.SIGINT, lambda s, f: None)
        logger.start(args.device)


if __name__ == "__main__":
    main()
