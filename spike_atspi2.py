#!/usr/bin/env python3
"""Spike v2: try dbus-run-session wrapper approach for AT-SPI inside Xvfb."""

import os
import subprocess
import time
import signal
import sys
import tempfile

# This script uses dbus-run-session to wrap everything in a clean D-Bus session.
# The inner script does the AT-SPI setup and app launch.

INNER_SCRIPT = r'''
import os, subprocess, time, sys, shutil

display = os.environ["DISPLAY"]
print(f"[inner] DISPLAY={display}")
print(f"[inner] DBUS_SESSION_BUS_ADDRESS={os.environ.get('DBUS_SESSION_BUS_ADDRESS', 'NOT SET')}")

# Start AT-SPI registry daemon
atspi_path = shutil.which("at-spi2-registryd") or "/usr/libexec/at-spi2-registryd"
atspi = subprocess.Popen([atspi_path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
time.sleep(1)
if atspi.poll() is not None:
    print(f"[inner] WARN: at-spi2-registryd exited: {atspi.stderr.read().decode()[:200]}")
else:
    print(f"[inner] at-spi2-registryd running (pid={atspi.pid})")

# Enable accessibility
subprocess.run(
    ["dbus-send", "--session", "--dest=org.a11y.Status",
     "--type=method_call",
     "/org/a11y/bus", "org.freedesktop.DBus.Properties.Set",
     "string:org.a11y.Status", "string:IsEnabled",
     "variant:boolean:true"],
    capture_output=True, text=True, timeout=5,
)
print("[inner] Set IsEnabled=true")

# Verify
result = subprocess.run(
    ["dbus-send", "--session", "--dest=org.a11y.Status",
     "--type=method_call", "--print-reply",
     "/org/a11y/bus", "org.freedesktop.DBus.Properties.Get",
     "string:org.a11y.Status", "string:IsEnabled"],
    capture_output=True, text=True, timeout=5,
)
print(f"[inner] IsEnabled check: {result.stdout.strip()}")

# Set env vars for child apps
os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"
os.environ["QT_ACCESSIBILITY"] = "1"
os.environ["GTK_MODULES"] = "gail:atk-bridge"
os.environ["GTK_A11Y"] = "atspi"
os.environ["ACCESSIBILITY_ENABLED"] = "1"

# Launch apps
apps = []
for name in ["gnome-calculator", "xeyes"]:
    p = subprocess.Popen([name], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    apps.append((name, p))
    print(f"[inner] Launched {name} (pid={p.pid})")

print("[inner] Waiting 4s...")
time.sleep(4)

for name, p in apps:
    status = "running" if p.poll() is None else f"exited({p.returncode})"
    print(f"[inner] {name}: {status}")

# Now query AT-SPI
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

# Try event listeners approach
Atspi.init()

desktop = Atspi.get_desktop(0)
n = desktop.get_child_count()
print(f"\n[inner] AT-SPI Desktop has {n} application(s):")
for i in range(n):
    app = desktop.get_child_at_index(i)
    if app:
        name = app.get_name()
        pid = app.get_process_id()
        nc = app.get_child_count()
        print(f"  [{i}] name={name!r}, pid={pid}, children={nc}")
        for j in range(min(nc, 5)):
            child = app.get_child_at_index(j)
            if child:
                role = child.get_role_name()
                cname = child.get_name()
                print(f"    [{j}] role={role!r}, name={cname!r}")

if n == 0:
    print("[inner] FAIL: no apps in tree")
else:
    print("[inner] SUCCESS")

# Cleanup
for _, p in apps:
    p.terminate()
atspi.terminate()
'''

procs = []

def cleanup():
    for p in reversed(procs):
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try: p.kill()
            except: pass

signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(1)))

try:
    # Find free display
    display_num = 99
    while os.path.exists(f"/tmp/.X{display_num}-lock"):
        display_num += 1
    display = f":{display_num}"
    print(f"[outer] Using display {display}")

    # Start Xvfb
    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x1024x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    procs.append(xvfb)
    time.sleep(0.5)
    print(f"[outer] Xvfb started")

    # Write inner script to temp file
    script_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    script_file.write(INNER_SCRIPT)
    script_file.close()

    # Run inner script inside dbus-run-session
    env = {**os.environ, "DISPLAY": display}
    result = subprocess.run(
        ["dbus-run-session", "--", sys.executable, script_file.name],
        env=env, capture_output=True, text=True, timeout=30,
    )
    print(result.stdout)
    if result.stderr:
        print(f"[outer] stderr: {result.stderr[:500]}")
    print(f"[outer] Exit code: {result.returncode}")

    os.unlink(script_file.name)

finally:
    cleanup()
