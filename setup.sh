#!/usr/bin/env bash
# Interactive end-to-end wall-PC setup wizard.
#
# Wraps the whole flow into one guided run:
#   1. Collect wall details (name, IP, SSH user, gateway serial, SBX USB, passwords)
#   2. Write walls/<name>.env  (+ optional shared secrets in walls/_shared.env)
#   3. Validate the wall env
#   4. Deploy + write remote config.env + bootstrap venv  (setup_wall.sh)
#   5. Optionally start the dashboard
#
# Non-interactive building blocks are still available directly:
#   ./new_wall.sh <IP> <name> [serial]      # scaffold only
#   ./setup_wall.sh walls/<name>.env --start # deploy only
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------
c_bold=$'\033[1m'; c_dim=$'\033[2m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_red=$'\033[31m'; c_rst=$'\033[0m'

hr() { printf '%s\n' "----------------------------------------------------------------"; }
say() { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$c_bold" "$*" "$c_rst"; }
warn() { printf '%sWARN:%s %s\n' "$c_yel" "$c_rst" "$*" >&2; }
err()  { printf '%sERROR:%s %s\n' "$c_red" "$c_rst" "$*" >&2; }

# ask "Prompt" "default" VARNAME
ask() {
  local prompt="$1" default="${2:-}" __var="$3" reply
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [${default}]: " reply || true
    reply="${reply:-$default}"
  else
    read -r -p "$prompt: " reply || true
  fi
  printf -v "$__var" '%s' "$reply"
}

# ask_required "Prompt" "default" VARNAME  (loops until non-empty)
ask_required() {
  local prompt="$1" default="${2:-}" __var="$3"
  while true; do
    ask "$prompt" "$default" "$__var"
    [[ -n "${!__var}" ]] && break
    err "This value is required."
  done
}

# ask_secret "Prompt" VARNAME  (hidden; confirms when set)
ask_secret() {
  local prompt="$1" __var="$2" a b
  while true; do
    read -r -s -p "$prompt: " a || true; echo
    if [[ -z "$a" ]]; then
      printf -v "$__var" '%s' ""
      return 0
    fi
    read -r -s -p "  confirm: " b || true; echo
    if [[ "$a" == "$b" ]]; then
      printf -v "$__var" '%s' "$a"
      return 0
    fi
    err "Entries did not match — try again (or leave blank to skip)."
  done
}

confirm() {
  local prompt="$1" default="${2:-N}" reply
  if [[ "$default" == "Y" ]]; then
    read -r -p "$prompt [Y/n]: " reply || true
    reply="${reply:-Y}"
  else
    read -r -p "$prompt [y/N]: " reply || true
    reply="${reply:-N}"
  fi
  [[ "$reply" =~ ^[Yy] ]]
}

# Escape a value for safe single-quoted bash env assignment.
sq() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

require_tool() { command -v "$1" >/dev/null 2>&1 || { err "'$1' is required but not found in PATH."; exit 1; }; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
require_tool ssh
require_tool rsync
[[ -f "${ROOT}/setup_wall.sh" ]] || { err "setup_wall.sh not found — run from the project root."; exit 1; }
[[ -f "${ROOT}/walls/_template.env" ]] || { err "walls/_template.env not found."; exit 1; }
mkdir -p "${ROOT}/walls"

clear 2>/dev/null || true
say "${c_bold}Drive automation — wall PC setup wizard${c_rst}"
hr
say "This wizard configures ONE wall PC and deploys the dashboard to it."
say "${c_dim}Run it from your Mac/laptop, not from the wall PC itself.${c_rst}"
hr

HOST_SHORT="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown)"
if [[ "${HOST_SHORT}" == *wall* || "${HOST_SHORT}" == *qa* ]]; then
  warn "You appear to be on a lab/wall host (${HOST_SHORT}). Prefer running from your Mac."
fi

# ---------------------------------------------------------------------------
# 1. Pick or create a wall env
# ---------------------------------------------------------------------------
EXISTING=()
while IFS= read -r _f; do
  [[ -n "$_f" ]] && EXISTING+=("$_f")
done < <(find "${ROOT}/walls" -maxdepth 1 -name '*.env' ! -name '_shared.env' ! -name '_template.env' -print 2>/dev/null | sort)

WALL_ENV=""
REUSE=0
if [[ "${#EXISTING[@]}" -gt 0 ]]; then
  step "Existing wall configs"
  local_i=1
  for f in "${EXISTING[@]}"; do
    printf "  %d) %s\n" "$local_i" "walls/$(basename "$f")"
    local_i=$((local_i + 1))
  done
  printf "  n) Create a NEW wall config\n"
  ask "Select an existing wall to reconfigure/deploy, or 'n' for new" "n" pick
  if [[ "$pick" != "n" && "$pick" =~ ^[0-9]+$ && "$pick" -ge 1 && "$pick" -le "${#EXISTING[@]}" ]]; then
    WALL_ENV="${EXISTING[$((pick - 1))]}"
    REUSE=1
    say "Using ${c_grn}walls/$(basename "$WALL_ENV")${c_rst}"
  fi
fi

# Defaults (also used to prefill when reconfiguring)
WALL_NAME=""; WALL_IP=""; WALL_SSH_USER="qa"; WALL_GATEWAY_SERIAL=""; GATEWAY_SERIALS=""
DRIVE_SBX_PORT="/dev/ttyUSB0"; DRIVE_SBX_SERIAL=""; DRIVE_SBX_SERIAL_STRICT="1"
DRIVE_DASHBOARD_PORT="8765"; DRIVE_LAB_DIR="drive_automation"
DRIVE_EV_URL="http://10.91.1.51:8091"; DRIVE_LB2_URL="http://10.91.1.66:8080"
DRIVE_OPERATOR="Automation"
GATEWAY_SSH_HOST="10.91.1.150"; GATEWAY_SSH_USER="root"; GATEWAY_SSH_AUTH="auto"; GATEWAY_SSH_IDENTITY="~/.ssh/id_ecdsa"
PANEL_SSH_PASS=""; DRIVE_PASSWORD=""

if [[ "$REUSE" -eq 1 ]]; then
  # shellcheck disable=SC1090
  source "$WALL_ENV"
fi

step "Wall PC details"
ask_required "Wall name (slug, e.g. in-qa-wall-003)" "${WALL_NAME:-}" WALL_NAME
# Normalize slug for filename
WALL_SLUG="$(printf '%s' "$WALL_NAME" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9._-')"
ask_required "Wall IP (IPv4)" "${WALL_IP:-}" WALL_IP
while ! [[ "$WALL_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; do
  err "Not a valid IPv4 address."
  ask_required "Wall IP (IPv4)" "" WALL_IP
done
ask_required "Wall SSH user" "${WALL_SSH_USER:-qa}" WALL_SSH_USER
ask "Gateway serial (this wall's SPAN gateway, optional)" "${WALL_GATEWAY_SERIAL:-}" WALL_GATEWAY_SERIAL
ask "Gateway dropdown serials (comma-separated)" "${GATEWAY_SERIALS:-${WALL_GATEWAY_SERIAL:-}}" GATEWAY_SERIALS

step "SBX USB capture (FT232R — never the ST-Link ttyACM)"
ask "SBX serial device port" "${DRIVE_SBX_PORT:-/dev/ttyUSB0}" DRIVE_SBX_PORT
ask "SBX adapter serial (blank = auto-detect by device type)" "${DRIVE_SBX_SERIAL:-}" DRIVE_SBX_SERIAL

ADV=0
if confirm "Configure advanced options (URLs, gateway SSH, ports)?" "N"; then ADV=1; fi
if [[ "$ADV" -eq 1 ]]; then
  step "Advanced"
  ask "Dashboard port" "${DRIVE_DASHBOARD_PORT:-8765}" DRIVE_DASHBOARD_PORT
  ask "Remote install dir (under \$HOME)" "${DRIVE_LAB_DIR:-drive_automation}" DRIVE_LAB_DIR
  ask "EV Simulator URL" "${DRIVE_EV_URL}" DRIVE_EV_URL
  ask "Load Bank-2 URL" "${DRIVE_LB2_URL}" DRIVE_LB2_URL
  ask "Operator name" "${DRIVE_OPERATOR}" DRIVE_OPERATOR
  ask "Gateway SSH host" "${GATEWAY_SSH_HOST}" GATEWAY_SSH_HOST
  ask "Gateway SSH user" "${GATEWAY_SSH_USER}" GATEWAY_SSH_USER
  ask "Gateway SSH identity" "${GATEWAY_SSH_IDENTITY}" GATEWAY_SSH_IDENTITY
fi

# ---------------------------------------------------------------------------
# 2. Passwords (per-wall or shared)
# ---------------------------------------------------------------------------
step "Passwords"
SHARED_ENV="${ROOT}/walls/_shared.env"
USE_SHARED=0
if [[ -f "$SHARED_ENV" ]]; then
  say "Found ${c_grn}walls/_shared.env${c_rst} (shared secrets for all walls)."
  if confirm "Store/keep passwords in walls/_shared.env (shared across walls)?" "Y"; then USE_SHARED=1; fi
else
  if confirm "Store passwords in a shared walls/_shared.env (reused by all walls)?" "N"; then USE_SHARED=1; fi
fi

say "${c_dim}Leave blank to keep any existing value.${c_rst}"
ask_secret "Wall SSH password (PANEL_SSH_PASS / CAPTURE)" NEW_PANEL_PASS
ask_secret "Load-bank Pi SSH password (DRIVE_PASSWORD)" NEW_DRIVE_PASS
[[ -n "$NEW_PANEL_PASS" ]] && PANEL_SSH_PASS="$NEW_PANEL_PASS"
[[ -n "$NEW_DRIVE_PASS" ]] && DRIVE_PASSWORD="$NEW_DRIVE_PASS"

# ---------------------------------------------------------------------------
# 3. Write files
# ---------------------------------------------------------------------------
WALL_ENV="${ROOT}/walls/${WALL_SLUG}.env"

if [[ -f "$WALL_ENV" && "$REUSE" -eq 0 ]]; then
  if ! confirm "walls/${WALL_SLUG}.env already exists — overwrite?" "N"; then
    err "Aborted to avoid clobbering ${WALL_ENV}."
    exit 1
  fi
fi

if [[ "$USE_SHARED" -eq 1 ]]; then
  step "Writing walls/_shared.env"
  {
    echo "# Shared secrets for ALL wall PCs (gitignored). Sourced before each wall env."
    echo "PANEL_SSH_PASS=$(sq "$PANEL_SSH_PASS")"
    echo "DRIVE_PASSWORD=$(sq "$DRIVE_PASSWORD")"
  } > "$SHARED_ENV"
  chmod 600 "$SHARED_ENV" 2>/dev/null || true
  say "  wrote ${c_grn}walls/_shared.env${c_rst} (chmod 600)"
fi

step "Writing walls/${WALL_SLUG}.env"
{
  echo "# Per-wall setup file — generated by setup.sh on $(date '+%Y-%m-%d %H:%M:%S')"
  echo "# Re-run: ./setup.sh   (or ./setup_wall.sh walls/${WALL_SLUG}.env)"
  echo ""
  echo "WALL_NAME=$(sq "$WALL_NAME")"
  echo "WALL_IP=$(sq "$WALL_IP")"
  echo "WALL_SSH_USER=$(sq "$WALL_SSH_USER")"
  echo "WALL_GATEWAY_SERIAL=$(sq "$WALL_GATEWAY_SERIAL")"
  echo "GATEWAY_SERIALS=$(sq "$GATEWAY_SERIALS")"
  echo ""
  if [[ "$USE_SHARED" -eq 1 ]]; then
    echo "# Passwords come from walls/_shared.env"
  else
    echo "PANEL_SSH_PASS=$(sq "$PANEL_SSH_PASS")"
    echo "DRIVE_PASSWORD=$(sq "$DRIVE_PASSWORD")"
  fi
  echo ""
  echo "# SBX USB on this wall (FT232R — never ST-Link ttyACM)"
  echo "DRIVE_SBX_PORT=$(sq "$DRIVE_SBX_PORT")"
  echo "DRIVE_SBX_SERIAL=$(sq "$DRIVE_SBX_SERIAL")"
  echo "DRIVE_SBX_SERIAL_STRICT=$(sq "$DRIVE_SBX_SERIAL_STRICT")"
  echo ""
  echo "# Lab endpoints / gateway"
  echo "DRIVE_EV_URL=$(sq "$DRIVE_EV_URL")"
  echo "DRIVE_LB2_URL=$(sq "$DRIVE_LB2_URL")"
  echo "DRIVE_OPERATOR=$(sq "$DRIVE_OPERATOR")"
  echo "DRIVE_DASHBOARD_PORT=$(sq "$DRIVE_DASHBOARD_PORT")"
  echo "USE_GATEWAY=1"
  echo "GATEWAY_SSH_HOST=$(sq "$GATEWAY_SSH_HOST")"
  echo "GATEWAY_SSH_USER=$(sq "$GATEWAY_SSH_USER")"
  echo "GATEWAY_SSH_AUTH=$(sq "$GATEWAY_SSH_AUTH")"
  echo "GATEWAY_SSH_IDENTITY=$(sq "$GATEWAY_SSH_IDENTITY")"
  echo ""
  echo "DRIVE_LAB_DIR=$(sq "$DRIVE_LAB_DIR")"
} > "$WALL_ENV"
chmod 600 "$WALL_ENV" 2>/dev/null || true
say "  wrote ${c_grn}walls/${WALL_SLUG}.env${c_rst} (chmod 600)"

# ---------------------------------------------------------------------------
# 4. Validate
# ---------------------------------------------------------------------------
step "Validating wall env"
if ! "${ROOT}/scripts/validate_wall_env.sh" "$WALL_ENV"; then
  err "Validation failed — fix walls/${WALL_SLUG}.env and re-run ./setup.sh"
  exit 1
fi

# ---------------------------------------------------------------------------
# 5. Summary + deploy
# ---------------------------------------------------------------------------
step "Summary"
hr
printf "  Wall        : %s (%s@%s)\n" "$WALL_NAME" "$WALL_SSH_USER" "$WALL_IP"
printf "  Gateway     : %s\n" "${WALL_GATEWAY_SERIAL:-<none>}"
printf "  Serials     : %s\n" "${GATEWAY_SERIALS:-<none>}"
printf "  SBX device  : %s (serial: %s)\n" "$DRIVE_SBX_PORT" "${DRIVE_SBX_SERIAL:-auto}"
printf "  Dashboard   : http://%s:%s/\n" "$WALL_IP" "$DRIVE_DASHBOARD_PORT"
printf "  Passwords   : %s\n" "$([[ "$USE_SHARED" -eq 1 ]] && echo 'walls/_shared.env' || echo "walls/${WALL_SLUG}.env")"
hr

say "${c_dim}Preview remote config.env: ./setup_wall.sh walls/${WALL_SLUG}.env --render${c_rst}"

if ! confirm "Deploy to ${WALL_SSH_USER}@${WALL_IP} now?" "Y"; then
  say ""
  say "Skipped deploy. When ready run:"
  say "  ${c_bold}./setup_wall.sh walls/${WALL_SLUG}.env --start${c_rst}"
  exit 0
fi

START_FLAG=""
if confirm "Install always-on systemd service (drive-dashboard, like lucid)?" "Y"; then START_FLAG="--start"; fi

FORCE_FLAG=""
if confirm "Overwrite remote config.env from this wall env (recommended)?" "Y"; then FORCE_FLAG="--force-config"; fi

step "Deploying (setup_wall.sh)"
set -x
"${ROOT}/setup_wall.sh" "walls/${WALL_SLUG}.env" ${FORCE_FLAG} ${START_FLAG}
set +x

step "Done"
say "Dashboard: ${c_grn}http://${WALL_IP}:${DRIVE_DASHBOARD_PORT}/${c_rst}"
say "Logs     : ssh ${WALL_SSH_USER}@${WALL_IP} 'sudo journalctl -u drive-dashboard -f'"
say "Re-run   : ./setup.sh"
