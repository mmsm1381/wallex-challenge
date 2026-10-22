#!/usr/bin/env bash
# Installs lagcatcher on a Linux server and starts it as a systemd service.
# Run it as the normal user from any folder. It uses sudo only for systemd.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(id -un)"
PYTHON="${PYTHON:-python3}"

"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11 or newer is necessary."; exit 1; }
"$PYTHON" -m venv "$APP_DIR/.venv" \
  || { echo "venv failed. On Ubuntu run: sudo apt-get install -y python3-venv"; exit 1; }
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"

echo "== Feed check (20 s)"
(cd "$APP_DIR" && .venv/bin/python -m lagcatcher check --seconds 20) \
  || echo "WARNING: the check failed. Read the lines above before you continue."

sed -e "s#@APP_DIR@#$APP_DIR#g" -e "s#@RUN_USER@#$RUN_USER#g" \
  "$APP_DIR/deploy/lagcatcher.service" | sudo tee /etc/systemd/system/lagcatcher.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now lagcatcher
echo "Started. Live log: journalctl -u lagcatcher -f"
echo "Report: cd $APP_DIR && .venv/bin/python -m lagcatcher report"
