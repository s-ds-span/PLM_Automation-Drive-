#!/usr/bin/env bash
# Start the dashboard — same pattern as lucid_automation/run.sh
# (source env, then exec a production server).
#
# Lucid:  python3 -m uvicorn proxy_update:app --host 0.0.0.0 --port 8000
# Drive:  python3 -m gunicorn dashboard.app:app  (Flask + SSE)
#
# systemd:  sudo systemctl start|stop|restart drive-dashboard
# logs:     sudo journalctl -u drive-dashboard -f
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ -f config.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source config.env
  set +a
fi

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

export DRIVE_DASHBOARD_PORT="${DRIVE_DASHBOARD_PORT:-8765}"
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Dashboard: http://${HOST_IP:-0.0.0.0}:${DRIVE_DASHBOARD_PORT}/"

if python3 -c "import gunicorn" >/dev/null 2>&1; then
  echo "gunicorn (gthread) on 0.0.0.0:${DRIVE_DASHBOARD_PORT}"
  exec python3 -m gunicorn \
    --bind "0.0.0.0:${DRIVE_DASHBOARD_PORT}" \
    --worker-class gthread \
    --workers 1 \
    --threads 16 \
    --timeout 0 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    "dashboard.app:app"
fi

echo "gunicorn not installed — pip install gunicorn (Flask fallback)"
exec python3 dashboard/app.py
