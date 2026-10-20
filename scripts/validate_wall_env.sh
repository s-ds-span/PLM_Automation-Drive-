#!/usr/bin/env bash
# Validate a walls/<panel>.env before deploy.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WALL_ENV="${1:-}"

if [[ -z "${WALL_ENV}" ]]; then
  echo "Usage: $0 walls/<panel>.env" >&2
  exit 1
fi
if [[ "${WALL_ENV}" != /* ]]; then
  WALL_ENV="${ROOT}/${WALL_ENV}"
fi
if [[ ! -f "${WALL_ENV}" ]]; then
  echo "Wall env not found: ${WALL_ENV}" >&2
  exit 1
fi

# shellcheck disable=SC1090
if [[ -f "${ROOT}/walls/_shared.env" ]]; then
  source "${ROOT}/walls/_shared.env"
fi
# shellcheck disable=SC1090
source "${WALL_ENV}"

errors=0
warn() { echo "WARN: $*" >&2; }
fail() { echo "ERROR: $*" >&2; errors=$((errors + 1)); }

[[ -n "${WALL_IP:-}" ]] || fail "WALL_IP is required"
[[ -n "${WALL_SSH_USER:-}" ]] || fail "WALL_SSH_USER is required"

if [[ -z "${PANEL_SSH_PASS:-}" ]]; then
  warn "PANEL_SSH_PASS is empty (set in wall env or walls/_shared.env)"
fi
if [[ -z "${DRIVE_PASSWORD:-}" ]]; then
  warn "DRIVE_PASSWORD is empty (LB1/LB2 may fail without it)"
fi

if [[ -n "${WALL_IP:-}" ]] && ! [[ "${WALL_IP}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  fail "WALL_IP must be an IPv4 address (got ${WALL_IP})"
fi

if [[ "${errors}" -gt 0 ]]; then
  exit 1
fi
echo "OK: $(basename "${WALL_ENV}") → ${WALL_SSH_USER}@${WALL_IP}"
