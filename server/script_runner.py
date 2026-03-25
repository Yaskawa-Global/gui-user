"""Console entry points for helper shell scripts."""

import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def view_display():
    script = _SCRIPTS_DIR / "view-display.sh"
    sys.exit(subprocess.call([str(script)] + sys.argv[1:]))


def stop_display():
    script = _SCRIPTS_DIR / "stop-display.sh"
    sys.exit(subprocess.call([str(script)] + sys.argv[1:]))
