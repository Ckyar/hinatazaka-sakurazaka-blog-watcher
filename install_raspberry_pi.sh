#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="sakamichi-blog-watcher"
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_TEMPLATE="$PROJECT_DIR/deploy/$SERVICE_NAME.service"
SERVICE_TARGET="/etc/systemd/system/$SERVICE_NAME.service"
cd "$PROJECT_DIR"

if [[ "${EUID}" -eq 0 ]]; then
  echo "Please run this script as your normal Raspberry Pi user, not with sudo."
  exit 1
fi

if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
  echo "Missing service template: $SERVICE_TEMPLATE"
  exit 1
fi

echo "Installing Raspberry Pi OS packages..."
sudo apt-get update
sudo apt-get install -y git python3 python3-venv

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$PROJECT_DIR/.venv"
fi

"$PROJECT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$PROJECT_DIR/.venv/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/images/日向坂46" \
  "$PROJECT_DIR/images/櫻坂46" "$PROJECT_DIR/logs"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  chmod 600 "$PROJECT_DIR/.env"
  echo
  echo "Created $PROJECT_DIR/.env"
  echo "Fill in the Discord token and channel IDs, then run this installer again."
  exit 2
fi
chmod 600 "$PROJECT_DIR/.env"

echo "Validating configuration..."
PROJECT_DIR="$PROJECT_DIR" "$PROJECT_DIR/.venv/bin/python" -c \
  "import os; from pathlib import Path; from dotenv import load_dotenv; root=Path(os.environ['PROJECT_DIR']); load_dotenv(root/'.env'); from app import Config; Config.from_env(); print('Configuration OK')"

CURRENT_USER="$(id -un)"
CURRENT_GROUP="$(id -gn)"
TEMP_SERVICE="$(mktemp)"
trap 'rm -f "$TEMP_SERVICE"' EXIT
sed \
  -e "s|@@USER@@|$CURRENT_USER|g" \
  -e "s|@@GROUP@@|$CURRENT_GROUP|g" \
  -e "s|@@PROJECT_DIR@@|$PROJECT_DIR|g" \
  "$SERVICE_TEMPLATE" > "$TEMP_SERVICE"

sudo install -m 0644 "$TEMP_SERVICE" "$SERVICE_TARGET"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME.service"

echo
echo "Installation complete."
echo "Status:  sudo systemctl status $SERVICE_NAME --no-pager"
echo "Logs:    journalctl -u $SERVICE_NAME -f"
echo "App log: tail -f '$PROJECT_DIR/logs/watcher.log'"
