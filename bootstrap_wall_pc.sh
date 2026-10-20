#!/usr/bin/env bash
# First-time setup on a wall PC after deploy (run ON the wall, or via deploy_to_lab.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ ! -f config.env ]]; then
  if [[ "${SKIP_CONFIG_CREATE:-0}" == "1" ]]; then
    echo "config.env missing — run setup_wall.sh from your Mac to generate it."
  else
    cp config.env.example config.env
    echo "Created config.env from config.env.example — edit passwords and paths before use."
  fi
elif [[ "${SKIP_CONFIG_CREATE:-0}" == "1" ]]; then
  echo "Using existing config.env"
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PORT="${DRIVE_DASHBOARD_PORT:-8765}"
echo ""
echo "Bootstrap complete."
echo "  1. Edit ~/drive_automation/config.env (PANEL_SSH_PASS, GATEWAY_SERIALS, DRIVE_SBX_*)"
echo "  2. Always-on: ./scripts/install_dashboard_service.sh"
echo "     (or debug: ./run_dashboard.sh)"
echo "  3. Open:  http://${HOST_IP:-<wall-ip>}:${PORT}/"
echo "  Logs: sudo journalctl -u drive-dashboard -f"
