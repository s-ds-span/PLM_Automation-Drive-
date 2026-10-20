#!/usr/bin/env bash
# Ensure panels_config.yml has a stub entry for WALL_IP (gateway_serial + user_name).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WALL_ENV="${1:-}"
PANELS_YML="${ROOT}/panels_config.yml"

if [[ -z "${WALL_ENV}" ]]; then
  echo "Usage: $0 walls/<panel>.env" >&2
  exit 1
fi
if [[ "${WALL_ENV}" != /* ]]; then
  WALL_ENV="${ROOT}/${WALL_ENV}"
fi
# shellcheck disable=SC1090
if [[ -f "${ROOT}/walls/_shared.env" ]]; then
  source "${ROOT}/walls/_shared.env"
fi
# shellcheck disable=SC1090
source "${WALL_ENV}"

: "${WALL_IP:?WALL_IP required}"
: "${WALL_SSH_USER:?WALL_SSH_USER required}"

if grep -qE "^[[:space:]]*${WALL_IP}:[[:space:]]*$" "${PANELS_YML}" 2>/dev/null; then
  echo "panels_config.yml already has ${WALL_IP}"
  exit 0
fi

WALL_NAME="${WALL_NAME:-${WALL_IP}}"
GATEWAY="${WALL_GATEWAY_SERIAL:-}"

{
  echo ""
  echo "  # Auto-added by scripts/ensure_panel_in_config.sh ($(date -u +%Y-%m-%d))"
  echo "  ${WALL_IP}:"
  if [[ -n "${GATEWAY}" ]]; then
    echo "    gateway_serial: ${GATEWAY}"
  fi
  echo "    user_name: ${WALL_SSH_USER}"
  echo "    name: \"${WALL_NAME}\""
  echo "    probes:"
  echo "      - module: PCM"
  echo "        uart_device: /dev/serial/by-id/usb-STMicroelectronics_STLINK-V3_REPLACE_ME-if01"
  echo "      - module: MainsMID"
  echo "        uart_device: /dev/serial/by-id/usb-STMicroelectronics_STLINK-V3_REPLACE_ME-if01"
} >> "${PANELS_YML}"

echo "Added stub panel entry for ${WALL_IP} → panels_config.yml"
echo "  Edit ST-Link paths under ${WALL_IP} before UART capture."
