#!/usr/bin/env python3
"""CLI entry for drive automation throttle test."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drive_automation.config import DriveConfig
from drive_automation.orchestrator import (
    AvailabilityResult,
    TestParams,
    ThrottleTestOrchestrator,
    check_availability,
)


def _prompt_float(label: str, default: float) -> float:
    raw = input(f"{label} [{default}]: ").strip()
    if not raw:
        return default
    return float(raw)


def _prompt_int(label: str, default: int) -> int:
    raw = input(f"{label} [{default}]: ").strip()
    if not raw:
        return default
    return int(raw)


def _prompt_str(label: str, default: str = "") -> str:
    raw = input(f"{label} [{default}]: ").strip()
    return raw or default


def _resolve_lb2_usage(avail: AvailabilityResult, *, non_interactive: bool, skip_lb2: bool) -> bool:
    """Return True if LB2 should be used."""
    if avail.lb2_available:
        return True
    if non_interactive:
        if skip_lb2:
            print(
                '{"event":"lb2_skipped","message":"LB2 in use — skipping (non-interactive)"}',
                flush=True,
            )
            return False
        operator = avail.lb2_operator or "another user"
        raise SystemExit(
            f"Load Bank-2 is in use by {operator}. "
            "Use --skip-lb2 to run EV-only or wait for release."
        )
    operator = avail.lb2_operator or "another user"
    print(f"\nLoad Bank-2 is in use by: {operator}")
    print("  1) Skip Load Bank-2 (EV Simulator only)")
    print("  2) Exit test")
    choice = input("Choose [1/2]: ").strip()
    if choice == "1":
        print('{"event":"lb2_skipped","message":"User chose to skip LB2"}', flush=True)
        return False
    raise SystemExit("Test exited by user (LB2 unavailable).")


def build_params(args: argparse.Namespace, *, use_lb2: bool) -> TestParams:
    common = dict(
        run_commission=bool(args.run_commission),
        drive_serial=str(args.drive_serial or ""),
        breaker_rating_a=float(args.breaker_rating_a),
        use_dynamic_load=not bool(args.no_dynamic_load),
        sustain_timeout_sec=float(args.throttle_sec),
    )
    if args.non_interactive:
        return TestParams(
            charger_label=args.charger_label,
            test_name=args.test_name,
            notes=args.notes,
            ev_max_amps=args.ev_max_amps,
            lb2_max_amps=args.lb2_max_amps,
            throttle_duration_sec=args.throttle_sec,
            recovery_duration_sec=args.recovery_sec,
            iterations=args.iterations,
            lb2_ramp_step_interval_sec=args.lb2_ramp_interval,
            lb2_start_delay_sec=args.lb2_start_delay,
            use_lb2=use_lb2,
            **common,
        )
    return TestParams(
        charger_label=_prompt_str("Charger label", args.charger_label or "TestCharger"),
        test_name=_prompt_str("Test name", args.test_name or "ThrottleRun01"),
        notes=_prompt_str("Notes", ""),
        ev_max_amps=_prompt_int("EV max current (A)", args.ev_max_amps),
        lb2_max_amps=_prompt_float("LB2 max panel current (A)", args.lb2_max_amps),
        throttle_duration_sec=_prompt_float("Sustain/verify timeout (sec)", args.throttle_sec),
        recovery_duration_sec=_prompt_float("Recovery duration (sec)", args.recovery_sec),
        iterations=_prompt_int("Iterations", args.iterations),
        lb2_ramp_step_interval_sec=_prompt_float(
            "LB2 ramp step interval (sec)", args.lb2_ramp_interval
        ),
        lb2_start_delay_sec=_prompt_float(
            "Delay before LB2 (sec)", args.lb2_start_delay
        ),
        use_lb2=use_lb2,
        **common,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive automation throttle test")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--skip-lb2", action="store_true", help="Skip LB2 if unavailable")
    parser.add_argument("--force-skip-lb2", action="store_true", help="Never use LB2")
    parser.add_argument("--run-commission", action="store_true", help="Include Phase 1 commission")
    parser.add_argument(
        "--decommission",
        action="store_true",
        help="Run SEPARATE decommission only (not part of one-click pipeline)",
    )
    parser.add_argument("--breaker-instance-id", type=int, default=None)
    parser.add_argument("--charger-label", default="")
    parser.add_argument("--test-name", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--drive-serial", default="")
    parser.add_argument("--ev-max-amps", type=int, default=30)
    parser.add_argument("--lb2-max-amps", type=float, default=40.0)
    parser.add_argument("--throttle-sec", type=float, default=90.0)
    parser.add_argument("--recovery-sec", type=float, default=60.0)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--lb2-ramp-interval", type=float, default=5.0)
    parser.add_argument("--lb2-start-delay", type=float, default=10.0)
    parser.add_argument("--breaker-rating-a", type=float, default=60.0)
    parser.add_argument("--no-dynamic-load", action="store_true")
    args = parser.parse_args()

    config = DriveConfig.from_env()

    if args.decommission:
        from drive_automation.commissioning import decommission_drive
        from drive_automation.gateway import build_gateway_shell_from_env

        shell = build_gateway_shell_from_env(
            host=config.gateway_ssh_host,
            user=config.gateway_ssh_user,
            password=config.gateway_ssh_password,
            drive_serial=args.drive_serial or config.drive_serial,
            identity_file=config.gateway_ssh_identity,
            auth_mode=config.gateway_ssh_auth,
            gateway_serial=config.gateway_serial,
        )

        def _emit(event: str, **payload: Any) -> None:
            print(json.dumps({"event": event, **payload}, default=str), flush=True)

        try:
            result = decommission_drive(
                shell,
                breaker_instance_id=args.breaker_instance_id,
                emit=_emit,
            )
        except Exception as exc:
            print(json.dumps({"event": "error", "message": str(exc)}), flush=True)
            return 1
        return 0 if result.get("ok") else 1

    if not config.ev_password:
        print(
            "Error: set DRIVE_EV_PASSWORD or DRIVE_PASSWORD in config.env "
            "(loadbank1 Pi SSH password, not lab-cell password).",
            file=sys.stderr,
        )
        return 1

    avail = check_availability(config)
    print(
        f'{{"event":"availability","ev_available":{str(avail.ev_available).lower()},'
        f'"lb2_available":{str(avail.lb2_available).lower()},'
        f'"lb2_operator":"{avail.lb2_operator}"}}',
        flush=True,
    )

    if not avail.ev_available:
        print("Error: EV Simulator is not available.", file=sys.stderr)
        return 1

    if args.force_skip_lb2:
        use_lb2 = False
    elif avail.lb2_available:
        use_lb2 = True
    else:
        use_lb2 = _resolve_lb2_usage(
            avail,
            non_interactive=args.non_interactive,
            skip_lb2=args.skip_lb2,
        )

    params = build_params(args, use_lb2=use_lb2)
    if not use_lb2:
        params.lb2_max_amps = 0.0

    config.log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = config.log_dir / f"run_{stamp}.jsonl"

    orch = ThrottleTestOrchestrator(config, params, log_path=log_path)
    try:
        orch.run()
    except Exception as exc:
        print(f'{{"event":"error","message":"{exc}"}}', flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

