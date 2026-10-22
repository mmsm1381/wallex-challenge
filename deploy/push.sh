#!/usr/bin/env bash
# Copies the project to a server over SSH and runs the installer there.
# Usage: deploy/push.sh user@host [remote_folder]
set -euo pipefail

TARGET="${1:?usage: deploy/push.sh user@host [remote_folder]}"
REMOTE_DIR="${2:-lagcatcher}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

rsync -az --delete --exclude .venv --exclude data --exclude __pycache__ \
  --exclude .pytest_cache --exclude .superpowers --exclude .git "$LOCAL_DIR/" "$TARGET:$REMOTE_DIR/"
ssh -t "$TARGET" "cd $REMOTE_DIR && bash deploy/install.sh"
