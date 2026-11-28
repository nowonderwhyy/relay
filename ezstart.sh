#!/usr/bin/env bash
# One-click / one-command launcher for macOS and Linux.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${RELAY_PORT:-8080}"

echo ""
echo "  Relay - Easy Start"
echo ""

# If already running on this port, just open the browser.
if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "Relay is already running at http://127.0.0.1:${PORT}/"
    if [ "${RELAY_NO_OPEN:-0}" != "1" ]; then
        if command -v open >/dev/null 2>&1; then
            open "http://127.0.0.1:${PORT}/"
        elif command -v xdg-open >/dev/null 2>&1; then
            xdg-open "http://127.0.0.1:${PORT}/" >/dev/null 2>&1 &
        fi
    fi
    exit 0
fi

if ! command -v aria2c >/dev/null 2>&1; then
    echo "[!] aria2c was not found on PATH."
    echo "    macOS:         brew install aria2"
    echo "    Debian/Ubuntu: sudo apt install aria2"
    echo "    Arch:          sudo pacman -S aria2"
    exit 1
fi

if [ ! -x ".venv/bin/relay" ]; then
    echo "[1/2] First-time setup: creating local environment..."
    if command -v uv >/dev/null 2>&1; then
        uv venv .venv
        echo "[2/2] Installing Relay..."
        uv pip install --python .venv/bin/python -e .
    else
        python3 -m venv .venv
        echo "[2/2] Installing Relay..."
        .venv/bin/pip install --upgrade pip >/dev/null 2>&1 || true
        .venv/bin/pip install -e .
    fi
fi

echo "Starting Relay on http://127.0.0.1:${PORT}/"
echo "Keep this terminal open while downloading. Press Ctrl+C to stop."
echo ""
if [ "${RELAY_NO_OPEN:-0}" = "1" ]; then
    exec .venv/bin/relay serve --portable --port "$PORT" "$@"
else
    exec .venv/bin/relay serve --portable --port "$PORT" --open "$@"
fi
