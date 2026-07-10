#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="sakamichi-blog-watcher"
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

cd "$PROJECT_DIR"
git pull --ff-only
"$PROJECT_DIR/.venv/bin/python" -m pip install -r requirements.txt
"$PROJECT_DIR/.venv/bin/python" -m unittest discover -s tests -v
sudo systemctl restart "$SERVICE_NAME.service"
sudo systemctl status "$SERVICE_NAME.service" --no-pager
