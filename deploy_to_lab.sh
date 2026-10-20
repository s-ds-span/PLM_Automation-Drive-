#!/usr/bin/env bash
# Quick code deploy (legacy). For multi-wall setup prefer: ./setup_wall.sh walls/<panel>.env
#
# Run FROM your Mac, not from a wall PC.
#
# Usage:
#   ./deploy_to_lab.sh
#   DRIVE_LAB_HOST=qa@10.91.0.89 ./deploy_to_lab.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

LAB_HOST="${DRIVE_LAB_HOST:-qa@10.91.0.95}"
REMOTE_DIR="${DRIVE_LAB_DIR:-drive_automation}"

echo "Tip: for full wall setup use ./setup_wall.sh walls/<panel>.env"
echo ""

HOST_SHORT="$(hostname -s 2>/dev/null || hostname || echo unknown)"
echo "Source host: ${HOST_SHORT}"
echo "Source path: ${ROOT}"
echo "Deploying drive_automation to ${LAB_HOST}:~/${REMOTE_DIR}/"
if [[ "${HOST_SHORT}" == *wall* ]] || [[ "${HOST_SHORT}" == *qa* ]]; then
  echo "WARNING: You appear to be deploying FROM a lab/wall host."
  echo "         If UI edits live on your Mac, run ./deploy_to_lab.sh there instead."
fi

rsync -avz --progress \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude 'logs/' \
  --exclude 'artifacts/' \
  --exclude 'capture_sessions/' \
  --exclude '*.jsonl' \
  --exclude 'config.env' \
  --exclude 'walls/_shared.env' \
  --exclude 'walls/*.env' \
  --exclude '.DS_Store' \
  ./ "${LAB_HOST}:~/${REMOTE_DIR}/"

echo ""
echo "Running bootstrap on ${LAB_HOST} (venv + pip; creates config.env if missing) ..."
ssh "${LAB_HOST}" "cd ~/${REMOTE_DIR} && chmod +x bootstrap_wall_pc.sh run_dashboard.sh scripts/*.sh && ./bootstrap_wall_pc.sh"

echo ""
echo "Restarting drive-dashboard if the systemd unit is already installed ..."
ssh -tt "${LAB_HOST}" "cd ~/${REMOTE_DIR} && if systemctl is-enabled drive-dashboard >/dev/null 2>&1; then ./scripts/install_dashboard_service.sh --restart; else echo 'No systemd unit yet — on the wall run: ./scripts/install_dashboard_service.sh'; fi" || true

echo ""
echo "Done. On the lab cell:"
echo "  sudo systemctl restart drive-dashboard"
echo "  sudo journalctl -u drive-dashboard -f"
echo ""
echo "Then hard-refresh the browser (Cmd+Shift+R / Ctrl+Shift+R)."
echo "Expect sidebar + subtitle: 'SPAN Drive charge-point lab pipeline'"
HOST_IP="${LAB_HOST#*@}"
echo "Dashboard: http://${HOST_IP}:8765/"
