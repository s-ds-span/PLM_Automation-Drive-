#!/usr/bin/env bash
# Emergency release: safe-exit EV Simulator (LB1) and/or Load Bank-2 when automation is stuck.
#
# Usage (from lab cell):
#   ./release_units.sh              # both via API
#   ./release_units.sh --lb2        # LB2 only
#   ./release_units.sh --emergency  # Pi SSH emergency (use when API login fails)
#   ./release_units.sh --emergency --lb2
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ -f config.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source config.env
  set +a
fi

EV_URL="${DRIVE_EV_URL:-http://10.91.1.51:8091}"
LB2_URL="${DRIVE_LB2_URL:-http://10.91.1.66:8080}"
OPERATOR="${DRIVE_OPERATOR:-Automation}"
EV_PASS="${DRIVE_EV_PASSWORD:-${DRIVE_PASSWORD:-}}"
LB2_PASS="${DRIVE_LB2_PASSWORD:-${DRIVE_PASSWORD:-}}"
LB1_SSH="${LOADBANK1_PI_HOST:-loadbank1@10.91.1.51}"
LB2_SSH="${LOADBANK2_PI_HOST:-loadbank2@10.91.1.66}"

DO_EV=1
DO_LB2=1
EMERGENCY=0
for arg in "$@"; do
  case "$arg" in
    --ev) DO_LB2=0 ;;
    --lb2) DO_EV=0 ;;
    --emergency) EMERGENCY=1 ;;
    -h|--help)
      sed -n '2,10p' "$0"
      exit 0
      ;;
  esac
done

show_status() {
  echo "--- live status ---"
  echo -n "EV:  "
  curl -sf --max-time 5 "$EV_URL/api/public/live-status" 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"in_use={d.get('in_use')} operator={d.get('operator')!r}\")" \
    || echo "unreachable"
  echo -n "LB2: "
  curl -sf --max-time 5 "$LB2_URL/api/public/live-status" 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"in_use={d.get('in_use')} operator={d.get('operator')!r}\")" \
    || echo "unreachable"
}

login_json() {
  # args: url password cookie_file
  local url="$1" pass="$2" cookie="$3"
  local body resp code
  body="$(python3 -c "import json; print(json.dumps({'operator_name':'''$OPERATOR''','password':'''$pass'''}))")"
  resp="$(mktemp)"
  code="$(curl -s -o "$resp" -w '%{http_code}' -c "$cookie" -X POST "$url/login" \
    -H 'Content-Type: application/json' -d "$body" || true)"
  echo "  login HTTP $code: $(cat "$resp")"
  if [[ "$code" != "200" ]]; then
    rm -f "$resp"
    return 1
  fi
  rm -f "$resp"
  return 0
}

release_ev_api() {
  local cookie
  cookie="$(mktemp)"
  echo "=== EV Simulator safe exit (${EV_URL}) ==="
  if [[ -z "$EV_PASS" ]]; then
    echo "Missing DRIVE_PASSWORD / DRIVE_EV_PASSWORD in config.env" >&2
    return 1
  fi
  if ! login_json "$EV_URL" "$EV_PASS" "$cookie"; then
    echo "EV API login failed. Use: ./release_units.sh --emergency --ev" >&2
    rm -f "$cookie"
    return 1
  fi
  curl -sf -b "$cookie" -X POST "$EV_URL/api/ev/stop" || true
  for _ in $(seq 1 20); do
    status="$(curl -sf -b "$cookie" "$EV_URL/api/state" \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('shutdown_status',''))" 2>/dev/null || true)"
    echo "  shutdown_status=$status"
    [[ "$status" == "done" || "$status" == "error" ]] && break
    sleep 2
  done
  curl -sf -b "$cookie" -X POST "$EV_URL/logout" || true
  rm -f "$cookie"
}

release_lb2_api() {
  local cookie
  cookie="$(mktemp)"
  echo "=== Load Bank-2 safe exit (${LB2_URL}) ==="
  if [[ -z "$LB2_PASS" ]]; then
    echo "Missing DRIVE_PASSWORD / DRIVE_LB2_PASSWORD in config.env" >&2
    return 1
  fi
  if ! login_json "$LB2_URL" "$LB2_PASS" "$cookie"; then
    echo ""
    echo "LB2 API login failed."
    echo "  - Wrong password? Set DRIVE_LB2_PASSWORD to loadbank2 Pi SSH password."
    echo "  - Session stuck (same operator, lost cookie)? Use emergency:"
    echo "      ./release_units.sh --emergency --lb2"
    rm -f "$cookie"
    return 1
  fi
  curl -sf -b "$cookie" -X POST "$LB2_URL/api/shutdown" \
    -H 'Content-Type: application/json' \
    -d '{"reason":"Manual release from release_units.sh"}' || true
  for _ in $(seq 1 20); do
    status="$(curl -sf -b "$cookie" "$LB2_URL/api/state" \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('shutdown_status',''))" 2>/dev/null || true)"
    echo "  shutdown_status=$status"
    [[ "$status" == "done" || "$status" == "error" ]] && break
    sleep 2
  done
  curl -sf -b "$cookie" -X POST "$LB2_URL/logout" || true
  rm -f "$cookie"
}

release_emergency() {
  echo "=== Emergency Pi scripts (SSH) ==="
  echo "You will be prompted for each Pi SSH password if needed."
  if [[ "$DO_EV" == "1" ]]; then
    echo "LB1 emergency shutdown + restart EV web..."
    ssh -t "$LB1_SSH" 'bash ~/loadbank_project/scripts/emergency_shutdown_lb1.sh; sudo systemctl restart ev-simulator1-web loadbank1-web 2>/dev/null || sudo systemctl restart ev-simulator1-web'
  fi
  if [[ "$DO_LB2" == "1" ]]; then
    echo "LB2 emergency shutdown + restart web..."
    ssh -t "$LB2_SSH" 'bash ~/loadbank_project/scripts/emergency_shutdown_lb2.sh; sudo systemctl restart loadbank2-web'
  fi
}

show_status
echo ""

if [[ "$EMERGENCY" == "1" ]]; then
  release_emergency
else
  [[ "$DO_EV" == "1" ]] && release_ev_api || true
  [[ "$DO_LB2" == "1" ]] && release_lb2_api || true
fi

echo ""
show_status
echo ""
echo "If LB2 still in_use:  ./release_units.sh --emergency --lb2"
