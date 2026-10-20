#!/usr/bin/env bash
# Scaffold a new per-wall env file.
#
# Usage:
#   ./new_wall.sh 10.91.0.99 in-new-wall XG-2532-3HLKT
#   ./setup_wall.sh walls/in-new-wall.env
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <WALL_IP> <wall-name> [gateway-serial]" >&2
  echo "Example: $0 10.91.0.99 in-new-wall XG-2532-3HLKT" >&2
  exit 1
fi

WALL_IP="$1"
WALL_SLUG="$2"
GATEWAY_SERIAL="${3:-}"

OUT="${ROOT}/walls/${WALL_SLUG}.env"
if [[ -f "${OUT}" ]]; then
  echo "Already exists: ${OUT}" >&2
  exit 1
fi

cp "${ROOT}/walls/_template.env" "${OUT}"

# Portable in-place edit (macOS + Linux sed)
sed_inplace() {
  if sed --version >/dev/null 2>&1; then
    sed -i "$@"
  else
    sed -i '' "$@"
  fi
}

sed_inplace "s/^WALL_NAME=.*/WALL_NAME=${WALL_SLUG}/" "${OUT}"
sed_inplace "s/^WALL_IP=.*/WALL_IP=${WALL_IP}/" "${OUT}"
if [[ -n "${GATEWAY_SERIAL}" ]]; then
  sed_inplace "s/^WALL_GATEWAY_SERIAL=.*/WALL_GATEWAY_SERIAL=${GATEWAY_SERIAL}/" "${OUT}"
  sed_inplace "s/^GATEWAY_SERIALS=.*/GATEWAY_SERIALS=${GATEWAY_SERIAL}/" "${OUT}"
fi

echo "Created: walls/${WALL_SLUG}.env"
echo ""
echo "Next:"
echo "  1. Edit passwords in walls/${WALL_SLUG}.env (or walls/_shared.env)"
echo "  2. Ensure panels_config.yml has ${WALL_IP} with gateway_serial + user_name"
echo "  3. ./setup_wall.sh walls/${WALL_SLUG}.env --start   # install systemd unit"
