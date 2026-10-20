"""Configuration from environment and optional config.env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv(_PROJECT_ROOT / "config.env")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _optional_int(name: str) -> Optional[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass
class DriveConfig:
    ev_url: str = "http://10.91.1.51:8091"
    lb2_url: str = "http://10.91.1.66:8080"
    operator: str = "Automation"
    password: str = ""
    ev_password_override: str = ""
    lb2_password_override: str = ""
    ev_max_amps: int = 30
    lb2_start_delay_sec: float = 10.0
    telemetry_interval_sec: float = 0.5
    slew_a_per_s: float = 13.0
    panel_channels: tuple[int, ...] = (3, 4)
    log_dir: Path = field(default_factory=lambda: _PROJECT_ROOT / "logs")
    artifacts_dir: Path = field(default_factory=lambda: _PROJECT_ROOT / "artifacts")
    # Gateway (preferred: spanssh-config; legacy: LAN SSH + cert/password).
    # Mandatory for one-click pipeline (commission, ratings, feeder amps).
    use_gateway: bool = True
    gateway_ssh_host: str = ""
    gateway_ssh_user: str = "root"
    gateway_ssh_password: str = ""
    gateway_ssh_identity: str = "~/.ssh/id_ecdsa"
    gateway_ssh_auth: str = "auto"  # auto | spanssh | cert | password | agent | system
    gateway_serial: str = ""
    gateway_spanssh_config: str = ""
    drive_serial: str = ""
    drive_timezone: str = "local"
    breaker_rating_a: float = 60.0
    # DER circuit-breaker instance for span-pcm ``cb get-state`` (advertise cases).
    breaker_instance_id: Optional[int] = None
    drive_top_tab: Optional[int] = None
    drive_bottom_tab: Optional[int] = None
    # Panel ratings (Phase 4) — read-only defaults unused; kept for env/docs compat.
    site_utility_rating_ma: int = 125000
    conductor_rating_ma: int = 100000
    feeder_breaker_rating_a: float = 100.0
    # One-click pipeline behavior
    phase_delay_sec: float = 0.0
    ev_draw_fraction: float = 0.8
    # Closed-loop load controller defaults
    load_loop_interval_sec: float = 1.0
    load_deadband_a: float = 0.75
    aggregation_timeout_sec: float = 180.0
    sustain_timeout_sec: float = 90.0
    recovery_timeout_sec: float = 60.0
    run_commission: bool = True

    @property
    def ev_password(self) -> str:
        """loadbank1 Pi SSH password (EV Simulator login)."""
        return self.ev_password_override or self.password

    @property
    def lb2_password(self) -> str:
        """loadbank2 Pi SSH password (Load Bank-2 login)."""
        return self.lb2_password_override or self.password

    @classmethod
    def from_env(cls) -> "DriveConfig":
        log_dir = Path(os.environ.get("DRIVE_LOG_DIR", str(_PROJECT_ROOT / "logs")))
        artifacts_dir = Path(
            os.environ.get("DRIVE_ARTIFACTS_DIR", str(_PROJECT_ROOT / "artifacts"))
        )
        return cls(
            ev_url=os.environ.get("DRIVE_EV_URL", "http://10.91.1.51:8091").rstrip("/"),
            lb2_url=os.environ.get("DRIVE_LB2_URL", "http://10.91.1.66:8080").rstrip("/"),
            operator=os.environ.get("DRIVE_OPERATOR", "Automation"),
            password=os.environ.get("DRIVE_PASSWORD", ""),
            ev_password_override=os.environ.get("DRIVE_EV_PASSWORD", ""),
            lb2_password_override=os.environ.get("DRIVE_LB2_PASSWORD", ""),
            ev_max_amps=_int("DRIVE_EV_MAX_AMPS", 30),
            lb2_start_delay_sec=_float("DRIVE_LB2_START_DELAY_SEC", 10.0),
            telemetry_interval_sec=_float("DRIVE_TELEMETRY_INTERVAL_SEC", 0.5),
            slew_a_per_s=_float("DRIVE_SLEW_A_PER_S", 13.0),
            log_dir=log_dir,
            artifacts_dir=artifacts_dir,
            use_gateway=_bool("USE_GATEWAY", True),
            gateway_ssh_host=os.environ.get("GATEWAY_SSH_HOST", "").strip(),
            gateway_ssh_user=os.environ.get("GATEWAY_SSH_USER", "root").strip() or "root",
            gateway_ssh_password=(
                os.environ.get("GATEWAY_SSH_PASS")
                or os.environ.get("GATEWAY_SSH_PASSWORD")
                or ""
            ),
            gateway_ssh_identity=(
                os.environ.get("GATEWAY_SSH_IDENTITY", "~/.ssh/id_ecdsa").strip()
                or "~/.ssh/id_ecdsa"
            ),
            gateway_ssh_auth=(
                os.environ.get("GATEWAY_SSH_AUTH", "auto").strip().lower() or "auto"
            ),
            gateway_serial=os.environ.get("GATEWAY_SERIAL", "").strip(),
            gateway_spanssh_config=(
                os.environ.get("GATEWAY_SPANSSH_CONFIG")
                or os.environ.get("SPANSSH_CONFIG")
                or ""
            ).strip(),
            drive_serial=os.environ.get("DRIVE_SERIAL", "").strip(),
            drive_timezone=os.environ.get("DRIVE_TIMEZONE", "local"),
            breaker_rating_a=_float("DRIVE_BREAKER_RATING_A", 60.0),
            breaker_instance_id=_optional_int("DRIVE_BREAKER_INSTANCE_ID"),
            drive_top_tab=_optional_int("DRIVE_TOP_TAB"),
            drive_bottom_tab=_optional_int("DRIVE_BOTTOM_TAB"),
            site_utility_rating_ma=_int("DRIVE_SITE_UTILITY_MA", 125000),
            conductor_rating_ma=_int("DRIVE_CONDUCTOR_MA", 100000),
            feeder_breaker_rating_a=_float("DRIVE_FEEDER_BREAKER_A", 100.0),
            phase_delay_sec=_float("DRIVE_PHASE_DELAY_SEC", 0.0),
            ev_draw_fraction=_float("DRIVE_EV_DRAW_FRACTION", 0.8),
            load_loop_interval_sec=_float("DRIVE_LOAD_LOOP_INTERVAL_SEC", 1.0),
            load_deadband_a=_float("DRIVE_LOAD_DEADBAND_A", 0.75),
            aggregation_timeout_sec=_float("DRIVE_AGGREGATION_TIMEOUT_SEC", 180.0),
            sustain_timeout_sec=_float("DRIVE_SUSTAIN_TIMEOUT_SEC", 90.0),
            recovery_timeout_sec=_float("DRIVE_RECOVERY_TIMEOUT_SEC", 60.0),
            run_commission=_bool("DRIVE_RUN_COMMISSION", True),
        )
