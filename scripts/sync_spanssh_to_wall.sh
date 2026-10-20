#!/usr/bin/env bash
# Optional: copy Mac ~/.ssh/span/<SERIAL>/ keys to a lab cell.
#
# IMPORTANT: Never modifies Mac ~/.ssh/span/<SERIAL>/spanssh-config (or keys).
# By default, if the wall already has spanssh-config, this script does NOTHING
# to it — overwriting a working remote.it config breaks gateway SSH until
# span_ssh regenerates it.
#
# Prefer generating spanssh ON the lab cell:
#   ssh -F ~/.ssh/span/<SERIAL>/spanssh-config <SERIAL>
#
# Usage:
#   ./scripts/sync_spanssh_to_wall.sh walls/in-sys-wall-005.env
#   ./scripts/sync_spanssh_to_wall.sh XC-2610-3JFDY systems@10.91.1.181
#   ./scripts/sync_spanssh_to_wall.sh ... --force   # overwrite wall spanssh-config
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERIAL=""
LAB_HOST=""
FORCE=0
ARGS=()

for arg in "$@"; do
  case "${arg}" in
    --force) FORCE=1 ;;
    *) ARGS+=("${arg}") ;;
  esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

if [[ $# -eq 1 && -f "${1}" ]]; then
  WALL_ENV="$1"
  [[ "${WALL_ENV}" != /* ]] && WALL_ENV="${ROOT}/${WALL_ENV}"
  # shellcheck disable=SC1090
  [[ -f "${ROOT}/walls/_shared.env" ]] && source "${ROOT}/walls/_shared.env"
  # shellcheck disable=SC1090
  source "${WALL_ENV}"
  SERIAL="${WALL_GATEWAY_SERIAL:-${GATEWAY_SERIAL:-}}"
  LAB_HOST="${WALL_SSH_USER}@${WALL_IP}"
elif [[ $# -ge 2 ]]; then
  SERIAL="$1"
  LAB_HOST="$2"
else
  echo "Usage:" >&2
  echo "  $0 <GATEWAY_SERIAL> <user@wall-ip> [--force]" >&2
  echo "  $0 walls/<panel>.env [--force]" >&2
  exit 1
fi

if [[ -z "${SERIAL}" || -z "${LAB_HOST}" ]]; then
  echo "ERROR: need GATEWAY_SERIAL and wall SSH target." >&2
  exit 1
fi

SRC="${HOME}/.ssh/span/${SERIAL}"
if [[ ! -f "${SRC}/spanssh-config" ]]; then
  echo "ERROR: missing ${SRC}/spanssh-config" >&2
  echo "  Run span_ssh for ${SERIAL} on this Mac (or on the lab cell)." >&2
  exit 1
fi

REMOTE_HOME="$(ssh "${LAB_HOST}" 'printf %s "$HOME"')"
REMOTE_DIR="${REMOTE_HOME}/.ssh/span/${SERIAL}"
REMOTE_CFG="${REMOTE_DIR}/spanssh-config"

if ssh "${LAB_HOST}" "test -f '${REMOTE_CFG}'"; then
  if [[ "${FORCE}" -ne 1 ]]; then
    echo "=== Keeping existing wall spanssh-config (not overwritten) ==="
    echo "  ${LAB_HOST}:${REMOTE_CFG}"
    echo "  Gateway SSH relies on this file — leave it alone once it works."
    echo "  To overwrite intentionally: $0 $* --force"
    echo ""
    echo "Verify:"
    echo "  ssh ${LAB_HOST} \"ssh -F ~/.ssh/span/${SERIAL}/spanssh-config ${SERIAL} 'echo ok'\""
    exit 0
  fi
  echo "WARN: --force will overwrite ${LAB_HOST}:${REMOTE_CFG}"
fi

# Staging only — never write back into ${SRC} on the Mac.
SPAN_REL="~/.ssh/span/${SERIAL}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/spanssh-sync.XXXXXX")"
cleanup() { rm -rf "${TMP}"; }
trap cleanup EXIT

cp -R "${SRC}/." "${TMP}/"
python3 - "${TMP}/spanssh-config" "${SPAN_REL}" "${SERIAL}" <<'PY'
"""Rewrite IdentityFile/CertificateFile to ~/.ssh/span/<serial>/… (OpenSSH-safe).

Operates only on the staging copy — never the Mac source tree.
"""
import re
import sys
from pathlib import Path

cfg_path = Path(sys.argv[1])
span_rel = sys.argv[2].rstrip("/")
serial = sys.argv[3]
text = cfg_path.read_text(encoding="utf-8")

pat_quoted = re.compile(
    r'"[^"]*/\.ssh/span/' + re.escape(serial) + r'/([^"]+)"'
)
text = pat_quoted.sub(lambda m: f"{span_rel}/{m.group(1)}", text)

pat_unquoted = re.compile(
    r"(?m)^(\s*(?:IdentityFile|CertificateFile)\s+)"
    r"(\S*/\.ssh/span/"
    + re.escape(serial)
    + r"/)(\S+)\s*$"
)
text = pat_unquoted.sub(lambda m: f"{m.group(1)}{span_rel}/{m.group(3)}", text)
text = text.replace('"', "")

cfg_path.write_text(text, encoding="utf-8")
print("staging spanssh-config for wall:")
print(text)
PY

echo "=== Sync spanssh ${SERIAL} → ${LAB_HOST}:${REMOTE_DIR}/ ==="
ssh "${LAB_HOST}" \
  "mkdir -p '${REMOTE_DIR}' && chmod 700 '${REMOTE_HOME}/.ssh' '${REMOTE_HOME}/.ssh/span' '${REMOTE_DIR}'"

rsync -avz --chmod=Fu=rw,Fg=,Fo=,Du=rwx,Dg=,Do= \
  "${TMP}/" "${LAB_HOST}:${REMOTE_DIR}/"

ssh "${LAB_HOST}" "chmod 600 '${REMOTE_CFG}' 2>/dev/null || true"

echo ""
echo "Mac source left unchanged: ${SRC}/spanssh-config"
echo "Verify ON the lab cell:"
echo "  ssh ${LAB_HOST} \"ssh -F ~/.ssh/span/${SERIAL}/spanssh-config ${SERIAL} 'echo ok'\""
