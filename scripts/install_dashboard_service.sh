#!/usr/bin/env bash
# Install / enable / restart the drive-dashboard systemd unit on THIS wall PC.
#
# Run ON the wall (or via setup_wall.sh --start). Requires sudo for systemctl.
#
# Usage:
#   ./scripts/install_dashboard_service.sh              # install + enable + start
#   ./scripts/install_dashboard_service.sh --restart     # restart if already installed
#   ./scripts/install_dashboard_service.sh --status
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SERVICE_NAME="drive-dashboard"
UNIT_TEMPLATE="${ROOT}/deploy/drive-dashboard.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
ACTION="install"

usage() {
  sed -n '2,12p' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --restart) ACTION="restart"; shift ;;
    --status) ACTION="status"; shift ;;
    *) echo "Unknown option: $1" >&2; usage 1 ;;
  esac
done

if ! command -v systemctl >/dev/null 2>&1; then
  echo "ERROR: systemctl not found — this host is not systemd." >&2
  exit 1
fi

sudo_sys() {
  if sudo -n true >/dev/null 2>&1; then
    sudo -n "$@"
  else
    sudo "$@"
  fi
}

unit_installed() {
  [[ -f "${UNIT_DST}" ]] || systemctl cat "${SERVICE_NAME}" >/dev/null 2>&1
}

stop_legacy_nohup() {
  # Old setup_wall.sh --start used nohup; free port 8765 before systemd binds it.
  if pgrep -f 'dashboard/app.py' >/dev/null 2>&1; then
    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
      return 0
    fi
    echo "Stopping leftover nohup dashboard/app.py ..."
    pkill -f 'dashboard/app.py' 2>/dev/null || true
    sleep 1
  fi
}

show_status() {
  systemctl status "${SERVICE_NAME}" --no-pager -l || true
  echo ""
  echo "Logs: sudo journalctl -u ${SERVICE_NAME} -f"
}

if [[ "${ACTION}" == "status" ]]; then
  show_status
  exit 0
fi

if [[ "${ACTION}" == "restart" ]]; then
  if ! unit_installed; then
    echo "Service ${SERVICE_NAME} is not installed. Run without --restart first:"
    echo "  ${ROOT}/scripts/install_dashboard_service.sh"
    exit 1
  fi
  stop_legacy_nohup
  sudo_sys systemctl restart "${SERVICE_NAME}"
  sleep 1
  show_status
  exit 0
fi

# --- install ---
[[ -f "${UNIT_TEMPLATE}" ]] || { echo "Missing ${UNIT_TEMPLATE}" >&2; exit 1; }
[[ -x "${ROOT}/run_dashboard.sh" ]] || chmod +x "${ROOT}/run_dashboard.sh"
[[ -f "${ROOT}/config.env" ]] || {
  echo "WARN: ${ROOT}/config.env is missing — dashboard will start with defaults."
}

USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"
HOME_DIR="${HOME:-}"
if [[ -z "${HOME_DIR}" ]]; then
  HOME_DIR="$(getent passwd "${USER_NAME}" | cut -d: -f6)"
fi

SUPP_LINE=""
if getent group dialout >/dev/null 2>&1; then
  SUPP_LINE="SupplementaryGroups=dialout"
fi

TMP_UNIT="$(mktemp)"
trap 'rm -f "${TMP_UNIT}"' EXIT
sed \
  -e "s|__USER__|${USER_NAME}|g" \
  -e "s|__GROUP__|${GROUP_NAME}|g" \
  -e "s|__HOME__|${HOME_DIR}|g" \
  -e "s|__ROOT__|${ROOT}|g" \
  -e "s|__SUPPLEMENTARY_GROUPS__|${SUPP_LINE}|g" \
  "${UNIT_TEMPLATE}" > "${TMP_UNIT}"

echo "Installing ${UNIT_DST} (User=${USER_NAME} WorkingDirectory=${ROOT})"
sudo_sys cp "${TMP_UNIT}" "${UNIT_DST}"
sudo_sys chmod 644 "${UNIT_DST}"

stop_legacy_nohup
sudo_sys systemctl daemon-reload
sudo_sys systemctl enable "${SERVICE_NAME}"
sudo_sys systemctl restart "${SERVICE_NAME}"
sleep 1
show_status

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PORT="8765"
if [[ -f "${ROOT}/config.env" ]]; then
  _line="$(grep -E '^[[:space:]]*DRIVE_DASHBOARD_PORT=' "${ROOT}/config.env" | tail -1 || true)"
  if [[ -n "${_line}" ]]; then
    PORT="${_line#*=}"
    PORT="${PORT%\"}"; PORT="${PORT#\"}"
    PORT="${PORT%\'}"; PORT="${PORT#\'}"
  fi
fi
echo ""
echo "Dashboard: http://${HOST_IP:-<wall-ip>}:${PORT}/"
echo "Start    : sudo systemctl start ${SERVICE_NAME}"
echo "Stop     : sudo systemctl stop ${SERVICE_NAME}"
echo "Restart  : sudo systemctl restart ${SERVICE_NAME}"
echo "Logs     : sudo journalctl -u ${SERVICE_NAME} -f"
