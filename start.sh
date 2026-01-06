#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-jungle-guardian}"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    echo "sudo is required to start the service."
    exit 1
  fi
fi

$SUDO systemctl start "$SERVICE_NAME"
$SUDO systemctl status "$SERVICE_NAME" --no-pager
