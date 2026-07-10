#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="sakamichi-blog-watcher"
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

sudo systemctl status "$SERVICE_NAME.service" --no-pager
echo
echo "Recent service logs:"
journalctl -u "$SERVICE_NAME.service" -n 30 --no-pager
echo
echo "Application storage:"
du -sh "$PROJECT_DIR/data" "$PROJECT_DIR/images" "$PROJECT_DIR/logs"
