"""Collect EV test export + run logs into a downloadable lab-cell artifact."""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._\-]+$")


def sanitize_run_id(run_id: str) -> str:
    text = (run_id or "").strip()
    if not _SAFE_RUN_ID.match(text):
        raise ValueError(f"Invalid artifact run id: {run_id!r}")
    return text


def resolve_artifact_dir(artifacts_root: Path, run_id: str) -> Path:
    root = artifacts_root.resolve()
    target = (root / sanitize_run_id(run_id)).resolve()
    if not str(target).startswith(str(root)):
        raise ValueError("Invalid artifact path.")
    return target


def _copy_into(run_dir: Path, src: Path, dest_name: str) -> Optional[str]:
    if not src.is_file():
        return None
    dest = run_dir / dest_name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest_name


def build_run_artifact(
    *,
    artifacts_root: Path,
    run_id: str,
    log_path: Optional[Path],
    ev_zip_bytes: Optional[bytes],
    ev_zip_name: str,
    meta: dict[str, Any],
    pilot_log_path: Optional[Path] = None,
    modbus_log_path: Optional[Path] = None,
    ev_loads_log_path: Optional[Path] = None,
    lb2_log_path: Optional[Path] = None,
    panel_log_path: Optional[Path] = None,
) -> dict[str, Any]:
    """
    Write ``artifacts/<run_id>/`` with EV zip (if any), run JSONL, stream JSONLs
    (pilot / EV loads / LB2 / panel), Modbus/SBX log, HTML report, manifest,
    and a combined.zip for download.
    """
    run_dir = resolve_artifact_dir(artifacts_root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    files: list[str] = []
    ev_name = ""
    if ev_zip_bytes:
        safe_name = Path(ev_zip_name or "ev_test.evtest.zip").name
        if not safe_name.lower().endswith(".zip"):
            safe_name = f"{safe_name}.evtest.zip"
        ev_path = run_dir / safe_name
        ev_path.write_bytes(ev_zip_bytes)
        files.append(safe_name)
        ev_name = safe_name

    log_name = ""
    if log_path and Path(log_path).is_file():
        src = Path(log_path)
        log_name = src.name
        dest = run_dir / log_name
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        files.append(log_name)

    stream_names: dict[str, str] = {}
    for key, src_path, default_name in (
        ("pilot_log", pilot_log_path, f"{run_id}_pilot.jsonl"),
        ("ev_loads_log", ev_loads_log_path, f"{run_id}_ev_loads.jsonl"),
        ("lb2_log", lb2_log_path, f"{run_id}_lb2.jsonl"),
        ("panel_log", panel_log_path, f"{run_id}_panel.jsonl"),
    ):
        if src_path is None:
            continue
        copied = _copy_into(
            run_dir,
            Path(src_path),
            Path(src_path).name or default_name,
        )
        if copied:
            stream_names[key] = copied
            files.append(copied)

    modbus_name = ""
    if modbus_log_path is not None:
        src = Path(modbus_log_path)
        dest_name = f"Modbus_logs_{run_id}.txt"
        if src.name:
            safe = re.sub(r"[^\w.\- ]+", "_", src.name).strip() or dest_name
            dest_name = safe
        copied = _copy_into(run_dir, src, dest_name)
        if copied:
            modbus_name = copied
            files.append(copied)

    report_url = f"/api/artifacts/{run_id}/report"
    report_name = ""
    try:
        from drive_automation.report import write_drive_report

        report_info = write_drive_report(
            artifacts_root=artifacts_root,
            run_id=run_id,
            log_path=Path(log_path) if log_path else None,
            meta=meta,
            download_url=f"/api/artifacts/{run_id}/download",
            modbus_url=f"/api/artifacts/{run_id}/modbus" if modbus_name else "",
            pilot_url=f"/api/artifacts/{run_id}/pilot" if stream_names.get("pilot_log") else "",
            ev_loads_url=(
                f"/api/artifacts/{run_id}/ev_loads"
                if stream_names.get("ev_loads_log")
                else ""
            ),
            lb2_url=(
                f"/api/artifacts/{run_id}/lb2" if stream_names.get("lb2_log") else ""
            ),
            panel_url=(
                f"/api/artifacts/{run_id}/panel" if stream_names.get("panel_log") else ""
            ),
        )
        if (run_dir / "drive_report.html").is_file():
            report_name = "drive_report.html"
            files.append(report_name)
        if (run_dir / "report_context.json").is_file():
            files.append("report_context.json")
        report_url = report_info.get("report_url") or report_url
    except Exception:
        report_name = ""

    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "ev_zip": ev_name or None,
        "run_log": log_name or None,
        "pilot_log": stream_names.get("pilot_log"),
        "ev_loads_log": stream_names.get("ev_loads_log"),
        "lb2_log": stream_names.get("lb2_log"),
        "panel_log": stream_names.get("panel_log"),
        "modbus_log": modbus_name or None,
        "report": report_name or None,
        "report_url": report_url if report_name else None,
        "modbus_url": f"/api/artifacts/{run_id}/modbus" if modbus_name else None,
        "pilot_url": (
            f"/api/artifacts/{run_id}/pilot" if stream_names.get("pilot_log") else None
        ),
        "ev_loads_url": (
            f"/api/artifacts/{run_id}/ev_loads"
            if stream_names.get("ev_loads_log")
            else None
        ),
        "lb2_url": (
            f"/api/artifacts/{run_id}/lb2" if stream_names.get("lb2_log") else None
        ),
        "panel_url": (
            f"/api/artifacts/{run_id}/panel" if stream_names.get("panel_log") else None
        ),
        **meta,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    files.append("manifest.json")

    combined_name = f"drive_run_{run_id}.zip"
    combined_path = run_dir / combined_name
    with zipfile.ZipFile(combined_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            path = run_dir / name
            if path.is_file() and name != combined_name:
                zf.write(path, arcname=name)

    return {
        "ok": True,
        "run_id": run_id,
        "dir": str(run_dir),
        "combined_zip": str(combined_path),
        "combined_name": combined_name,
        "files": files,
        "ev_zip": ev_name or None,
        "run_log": log_name or None,
        "pilot_log": stream_names.get("pilot_log"),
        "ev_loads_log": stream_names.get("ev_loads_log"),
        "lb2_log": stream_names.get("lb2_log"),
        "panel_log": stream_names.get("panel_log"),
        "modbus_log": modbus_name or None,
        "report": report_name or None,
        "report_url": report_url if report_name else None,
        "modbus_url": manifest.get("modbus_url"),
        "pilot_url": manifest.get("pilot_url"),
        "ev_loads_url": manifest.get("ev_loads_url"),
        "lb2_url": manifest.get("lb2_url"),
        "panel_url": manifest.get("panel_url"),
        "download_url": f"/api/artifacts/{run_id}/download",
        "manifest": manifest,
    }
