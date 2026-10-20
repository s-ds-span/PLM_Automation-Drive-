#!/usr/bin/env bash
# Deploy + configure the dashboard on a wall PC using a per-wall env file.
#
# Usage:
#   ./new_wall.sh 10.91.0.99 my-panel XG-xxxx     # scaffold walls/my-panel.env
#   ./setup_wall.sh walls/my-panel.env            # deploy + write config.env + bootstrap
#   ./setup_wall.sh walls/my-panel.env --start    # install systemd unit + start dashboard
#   ./setup_wall.sh --list                        # show wall env files
#   ./setup_wall.sh walls/my-panel.env --render   # preview remote config.env
#
# Secrets: copy walls/_shared.env.example → walls/_shared.env (optional, gitignored)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

FORCE_CONFIG=0
START_DASH=0
CODE_ONLY=0
RENDER_ONLY=0
WALL_ENV=""

usage() {
  sed -n '2,12p' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --list)
      echo "Wall env files:"
      find "${ROOT}/walls" -maxdepth 1 -name '*.env' ! -name '_shared.env' -print 2>/dev/null | sort
      echo ""
      echo "Examples (copy and edit):"
      find "${ROOT}/walls" -maxdepth 1 -name '*.env.example' -print 2>/dev/null | sort
      exit 0
      ;;
    --force-config) FORCE_CONFIG=1; shift ;;
    --start) START_DASH=1; shift ;;
    --code-only) CODE_ONLY=1; shift ;;
    --render) RENDER_ONLY=1; shift ;;
    -*) echo "Unknown option: $1" >&2; usage 1 ;;
    *)
      if [[ -z "${WALL_ENV}" ]]; then
        WALL_ENV="$1"
      else
        echo "Unexpected argument: $1" >&2
        usage 1
      fi
      shift
      ;;
  esac
done

if [[ -z "${WALL_ENV}" ]]; then
  echo "ERROR: pass a wall env file, e.g. ./setup_wall.sh walls/in-qa-wall-001.env" >&2
  usage 1
fi
if [[ "${WALL_ENV}" != /* ]]; then
  WALL_ENV="${ROOT}/${WALL_ENV}"
fi
if [[ ! -f "${WALL_ENV}" ]]; then
  echo "Wall env not found: ${WALL_ENV}" >&2
  echo "Create one: ./new_wall.sh <IP> <name> [gateway-serial]" >&2
  exit 1
fi

if [[ "${RENDER_ONLY}" -eq 1 ]]; then
  exec "${ROOT}/scripts/render_config_env.sh" "${WALL_ENV}"
fi

"${ROOT}/scripts/validate_wall_env.sh" "${WALL_ENV}"
"${ROOT}/scripts/ensure_panel_in_config.sh" "${WALL_ENV}"

# shellcheck disable=SC1090
if [[ -f "${ROOT}/walls/_shared.env" ]]; then
  source "${ROOT}/walls/_shared.env"
fi
# shellcheck disable=SC1090
source "${WALL_ENV}"

: "${WALL_IP:?Set WALL_IP in ${WALL_ENV}}"
: "${WALL_SSH_USER:?Set WALL_SSH_USER in ${WALL_ENV}}"

LAB_HOST="${WALL_SSH_USER}@${WALL_IP}"
REMOTE_DIR="${DRIVE_LAB_DIR:-drive_automation}"
WALL_LABEL="${WALL_NAME:-${WALL_IP}}"

echo "=== Drive dashboard setup: ${WALL_LABEL} ==="
echo "  SSH target : ${LAB_HOST}"
echo "  Env file   : ${WALL_ENV}"
echo "  Remote dir : ~/${REMOTE_DIR}/"
echo ""

HOST_SHORT="$(hostname -s 2>/dev/null || hostname || echo unknown)"
if [[ "${HOST_SHORT}" == *wall* ]] || [[ "${HOST_SHORT}" == *qa* ]]; then
  echo "WARNING: Deploying FROM a lab/wall host — prefer running from your Mac."
  echo ""
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

REMOTE_HAS_CONFIG=0
if ssh "${LAB_HOST}" "test -f ~/${REMOTE_DIR}/config.env"; then
  REMOTE_HAS_CONFIG=1
fi

# Same always-on pattern as lucid-dashboard: systemd on the wall PC.
_remote_service() {
  local mode="$1"
  # -tt so sudo can prompt even when this script is not on a TTY.
  ssh -tt "${LAB_HOST}" "cd ~/${REMOTE_DIR} && chmod +x scripts/install_dashboard_service.sh run_dashboard.sh && ./scripts/install_dashboard_service.sh ${mode}"
}

_restart_or_start_dashboard() {
  if [[ "${START_DASH}" -eq 1 ]]; then
    echo "Installing/starting drive-dashboard systemd unit on ${LAB_HOST} ..."
    if _remote_service ""; then
      echo "  Logs: ssh ${LAB_HOST} 'sudo journalctl -u drive-dashboard -f'"
    else
      echo "WARN: systemd install failed (need sudo). Falling back to nohup ..."
      ssh "${LAB_HOST}" "cd ~/${REMOTE_DIR} && (pkill -f 'dashboard/app.py' 2>/dev/null || true); nohup ./run_dashboard.sh > dashboard.log 2>&1 &"
      echo "  Log: ssh ${LAB_HOST} 'tail -f ~/${REMOTE_DIR}/dashboard.log'"
    fi
  elif ssh "${LAB_HOST}" "systemctl is-enabled drive-dashboard >/dev/null 2>&1"; then
    echo "Restarting existing drive-dashboard service on ${LAB_HOST} ..."
    _remote_service "--restart" || echo "WARN: restart failed — on the wall run: sudo systemctl restart drive-dashboard"
  else
    echo "NOTE: drive-dashboard unit not enabled — start manually or re-run with --start"
  fi
}

if [[ "${CODE_ONLY}" -eq 1 ]]; then
  echo "Code-only deploy complete (--code-only)."
  # Still restart/start so new Python is loaded (rsync alone is not enough).
  _restart_or_start_dashboard
  exit 0
fi

if [[ "${REMOTE_HAS_CONFIG}" -eq 0 || "${FORCE_CONFIG}" -eq 1 ]]; then
  echo "Writing config.env on ${LAB_HOST} ..."
  "${ROOT}/scripts/render_config_env.sh" "${WALL_ENV}" | \
    ssh "${LAB_HOST}" "cat > ~/${REMOTE_DIR}/config.env"
else
  echo "Remote config.env exists — keeping it (use --force-config to regenerate from wall env)."
fi

echo "Bootstrapping venv on ${LAB_HOST} ..."
ssh "${LAB_HOST}" "cd ~/${REMOTE_DIR} && chmod +x bootstrap_wall_pc.sh run_dashboard.sh setup_wall.sh new_wall.sh scripts/*.sh 2>/dev/null; SKIP_CONFIG_CREATE=1 ./bootstrap_wall_pc.sh"

# Do NOT auto-sync spanssh-config — overwriting a working wall
# ~/.ssh/span/<serial>/spanssh-config breaks gateway SSH. Generate/fix it
# once on the lab cell (or run scripts/sync_spanssh_to_wall.sh manually;
# that script skips if the wall file already exists unless --force).

DASH_PORT="${DRIVE_DASHBOARD_PORT:-8765}"
echo ""
echo "=== Done: ${WALL_LABEL} ==="
echo "  Dashboard : http://${WALL_IP}:${DASH_PORT}/"
echo "  SSH       : ssh ${LAB_HOST}"
echo "  Config    : ~/${REMOTE_DIR}/config.env"
echo "  Service   : drive-dashboard  (sudo systemctl start|stop|restart)"
echo "  Logs      : sudo journalctl -u drive-dashboard -f"
echo ""

_restart_or_start_dashboard
