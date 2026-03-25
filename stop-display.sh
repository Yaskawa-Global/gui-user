#!/bin/bash
# Kill any gui-user Xvfb display sessions and associated processes.
#
# Usage:
#   ./stop-display.sh

set -euo pipefail

killed=0

for proc in "x11vnc" "at-spi2-registryd" "Xvfb"; do
    pids=$(pgrep -f "$proc" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "Stopping $proc (pids: $(echo $pids | tr '\n' ' '))"
        kill $pids 2>/dev/null || true
        killed=1
    fi
done

if [ "$killed" -eq 0 ]; then
    echo "No display processes found."
else
    echo "Done."
fi
