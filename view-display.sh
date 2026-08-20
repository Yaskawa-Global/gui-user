#!/bin/bash
# Connect a VNC viewer to the gui-user Xvfb display.
#
# Usage:
#   ./view-display.sh              # auto-detect running x11vnc
#   ./view-display.sh 5902         # connect to specific port
#   ./view-display.sh --scale 0.8  # shrink the view to 80% (or 4/5, or 0.5, ...)
#
# If x11vnc isn't running yet, starts it on the first Xvfb display found.
#
# --scale shrinks what the *server sends*, which is the answer for a monitor shorter than the
# display being tested: the Xvfb display keeps its real pixel size, the app under test is
# unaffected, and only the picture you watch is smaller. It applies when this script starts
# x11vnc; if one is already running, stop it first so a scaled one can take its place.

set -euo pipefail

SCALE=""
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --scale) SCALE="${2:-}"; shift 2 ;;
        --scale=*) SCALE="${1#*=}"; shift ;;
        *) ARGS+=("$1"); shift ;;
    esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

SCALE_OPT=()
if [ -n "$SCALE" ]; then
    SCALE_OPT=(-scale "$SCALE")
fi

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
        if [ -n "$SCALE" ]; then
            echo "  note: --scale only applies when this script starts x11vnc, and one was"
            echo "        already running. Stop it first to watch at a different scale."
        fi
    else
        # No x11vnc running — find an Xvfb display and start one.
        # Only match actual Xvfb processes (not Xwayland, Xorg, etc.)
        XVFB_DISPLAY=$(pgrep -a -x Xvfb 2>/dev/null | grep -oP ' :\K\d+' | head -1 || true)
        if [ -z "$XVFB_DISPLAY" ]; then
            echo "No Xvfb display found. Launch an app first via the MCP server"
            echo "(use vnc=True in launch_app, or run this script after launch)."
            exit 1
        fi
        XVFB_DISPLAY=":$XVFB_DISPLAY"
        echo "No x11vnc running. Starting one on Xvfb display $XVFB_DISPLAY..."
        # env -u: x11vnc bails out if it sees a Wayland session, even for an Xvfb target
        env -u WAYLAND_DISPLAY -u XDG_SESSION_TYPE \
            x11vnc -display "$XVFB_DISPLAY" "${SCALE_OPT[@]+"${SCALE_OPT[@]}"}" \
                   -viewonly -shared -nopw -forever -noxdamage -q -autoport 5900 &
        sleep 1
        VNC_PID=$!
        if ! kill -0 "$VNC_PID" 2>/dev/null; then
            echo "x11vnc failed to start. Is the Xvfb display still running?"
            exit 1
        fi
        PORT=$(ss -tlnp 2>/dev/null | grep "pid=$VNC_PID" | grep -oP ':\K[0-9]+' | head -1 || echo 5900)
        echo "x11vnc started on port $PORT"
    fi
fi

echo "Connecting $VIEWER to localhost:$PORT ..."
exec "$VIEWER" "localhost:$PORT"
