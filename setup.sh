#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${SERVICE_NAME:-jungle-guardian}"
PYTHON="${PYTHON:-python3}"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
LOG_FILE="${LOG_FILE:-$SCRIPT_DIR/guardian.log}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ first."
  exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
DISCORD_TOKEN=
LOG_LEVEL=info
LOG_STDOUT=false
DISCORD_GUILD_ID=
EOF
  echo "Created $ENV_FILE. Set DISCORD_TOKEN before starting the service."
fi

if [ ! -f "$SCRIPT_DIR/score.json" ]; then
  echo "{}" > "$SCRIPT_DIR/score.json"
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    echo "sudo is required to install the systemd service."
    exit 1
  fi
fi

$SUDO tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Jungle Guardian Discord Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python $SCRIPT_DIR/bot.py
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload

if ! grep -q '^DISCORD_TOKEN=' "$ENV_FILE" || grep -q '^DISCORD_TOKEN=$' "$ENV_FILE"; then
  echo "DISCORD_TOKEN is not set in $ENV_FILE."
  echo "After updating it, run:"
  echo "  $SUDO systemctl enable --now $SERVICE_NAME"
  exit 0
fi

$SUDO systemctl enable --now "$SERVICE_NAME"
$SUDO systemctl status "$SERVICE_NAME" --no-pager
