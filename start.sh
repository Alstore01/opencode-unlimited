#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

mkdir -p "${OC_STATE_DIR:-./state}" /tmp/oc_empty /tmp/oc_daemon

echo "[1/3] Starting rotating_proxy_relay on 127.0.0.1:${LISTEN_PORT:-20988}..."
python3 -u scripts/rotating_proxy_relay.py >/tmp/oc-relay.log 2>&1 &
RELAY_PID=$!

echo "[2/3] Starting opencode hot daemon on 127.0.0.1:20999..."
HOME=/tmp/oc_daemon \
OPENCODE_PASSWORD="${OPENCODE_SERVER_PASSWORD:-opencode-local-daemon}" \
OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-opencode-local-daemon}" \
OPENCODE_DISABLE_AUTOUPDATE=1 \
HTTP_PROXY="http://127.0.0.1:${LISTEN_PORT:-20988}" \
HTTPS_PROXY="http://127.0.0.1:${LISTEN_PORT:-20988}" \
NO_PROXY="localhost,127.0.0.1,::1" \
"${OPENCODE_BIN:-opencode}" serve --port 20999 --hostname 127.0.0.1 >/tmp/opencode-daemon.log 2>&1 &
DAEMON_PID=$!

cleanup() {
  kill "$RELAY_PID" "$DAEMON_PID" 2>/dev/null || true
}
trap cleanup EXIT

sleep 2
echo "[3/3] Starting oc_bridge on 127.0.0.1:${BRIDGE_PORT:-20900}..."
exec python3 -u scripts/oc_bridge.py
