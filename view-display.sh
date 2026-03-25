#!/bin/bash
# Connect a VNC viewer to the gui-user Xvfb display.
#
# Usage:
#   ./view-display.sh           # auto-detect running x11vnc
#   ./view-display.sh 5902      # connect to specific port
#
# If x11vnc isn't running yet, starts it on the first Xvfb display found.

set -euo pipefail

# Find a VNC viewer
VIEWER=""
for cmd in vncviewer xtigervncviewer; do
    if command -v "$cmd" &>/dev/null; then
        VIEWER="$cmd"
        break
    fi
done

if [ -z "$VIEWER" ]; then
    echo "No VNC viewer found. Install one:"
    echo "  sudo apt install tigervnc-viewer"
    exit 1
fi

if [ "${1:-}" != "" ]; then
    PORT="$1"
else
    # Auto-detect: find a running x11vnc and its port
    VNC_PID=$(pgrep -f "x11vnc.*-viewonly" 2>/dev/null | head -1 || true)

    if [ -n "$VNC_PID" ]; then
        # Extract port from /proc/pid/cmdline or listening sockets
        PORT=$(ss -tlnp 2>/dev/null | grep "pid=$VNC_PID" | grep -oP ':\K[0-9]+' | head -1 || true)
        if [ -z "$PORT" ]; then
            PORT=5900
        fi
        echo "Found running x11vnc (pid=$VNC_PID) on port $PORT"
    else
        # No x11vnc running — find an Xvfb display and start one
        XVFB_DISPLAY=$(ps aux | grep '[X]vfb' | grep -oP ':\d+' | head -1 || true)
        if [ -z "$XVFB_DISPLAY" ]; then
            echo "No Xvfb display found. Launch an app first via the MCP server."
            exit 1
        fi
        echo "No x11vnc running. Starting one on display $XVFB_DISPLAY..."
        x11vnc -display "$XVFB_DISPLAY" -viewonly -shared -nopw -forever -noxdamage -q -autoport 5900 &
        sleep 1
        VNC_PID=$!
        PORT=$(ss -tlnp 2>/dev/null | grep "pid=$VNC_PID" | grep -oP ':\K[0-9]+' | head -1 || echo 5900)
        echo "x11vnc started on port $PORT"
    fi
fi

echo "Connecting $VIEWER to localhost:$PORT ..."
exec "$VIEWER" "localhost:$PORT"
