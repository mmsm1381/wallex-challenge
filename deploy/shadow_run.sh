#!/usr/bin/env bash
# Runs the shadow test on a shared server for a fixed time, as a transient
# systemd unit. The unit is not started at boot, has low CPU and IO
# priority, and has hard CPU and memory limits, so other programs keep
# their resources.
# Usage: deploy/shadow_run.sh start HOURS | status | stop | report
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT=lagcatcher-shadow
CONFIG="$APP_DIR/config.server.toml"

case "${1:-}" in
  start)
    RUN_S=$(( ${2:?usage: start HOURS} * 3600 ))
    systemd-run --unit="$UNIT" --description="lagcatcher shadow test (paper only)" \
      --property=CPUQuota=50% --property=MemoryMax=400M --property=IOWeight=10 \
      --property=RuntimeMaxSec=$(( RUN_S + 900 )) --nice=10 \
      --working-directory="$APP_DIR" --setenv=PYTHONUNBUFFERED=1 \
      "$APP_DIR/.venv/bin/python" -m lagcatcher run --config "$CONFIG" --duration "$RUN_S"
    ;;
  status)
    systemctl is-active "$UNIT" || true
    journalctl -u "$UNIT" --no-pager -n 3 -o cat
    ;;
  stop)
    systemctl stop "$UNIT"
    ;;
  report)
    cd "$APP_DIR" && .venv/bin/python -m lagcatcher report --config "$CONFIG"
    ;;
  *)
    echo "usage: $0 start HOURS | status | stop | report"
    exit 1
    ;;
esac
