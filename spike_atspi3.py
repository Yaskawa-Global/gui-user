#!/usr/bin/env python3
"""Spike v3: Use a minimal GTK3 test app to validate AT-SPI registration."""

import os
import subprocess
import time
import signal
import sys
import tempfile

# Minimal GTK3 app with a button
GTK_APP = r'''
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

win = Gtk.Window(title="Test App")
win.set_default_size(300, 200)
win.connect("destroy", Gtk.main_quit)

box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
win.add(box)

btn = Gtk.Button(label="Click Me")
box.pack_start(btn, True, True, 0)

entry = Gtk.Entry()
entry.set_text("Hello AT-SPI")
box.pack_start(entry, True, True, 0)

lbl = Gtk.Label(label="Status: idle")
box.pack_start(lbl, True, True, 0)

win.show_all()
Gtk.main()
'''

# Inner script that sets up AT-SPI and queries the tree
INNER_SCRIPT = r'''
import os, subprocess, time, sys, shutil, tempfile

display = os.environ["DISPLAY"]
print(f"[inner] DISPLAY={display}")

# Start AT-SPI registry
atspi_path = shutil.which("at-spi2-registryd") or "/usr/libexec/at-spi2-registryd"
atspi = subprocess.Popen([atspi_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
print(f"[inner] at-spi2-registryd: {'running' if atspi.poll() is None else 'exited'}")

# Set env
os.environ["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"
os.environ["QT_ACCESSIBILITY"] = "1"
os.environ["GTK_MODULES"] = "gail:atk-bridge"
os.environ["ACCESSIBILITY_ENABLED"] = "1"

# Write and launch GTK app
app_script = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
app_script.write(APP_CODE)
app_script.close()

app = subprocess.Popen(
    [sys.executable, app_script.name],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    env=os.environ,
)
print(f"[inner] Launched GTK test app (pid={app.pid})")
print("[inner] Waiting 3s for app startup...")
time.sleep(3)

if app.poll() is not None:
    print(f"[inner] App exited! stderr: {app.stderr.read().decode()[:500]}")
    sys.exit(1)

# Query AT-SPI
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
Atspi.init()

desktop = Atspi.get_desktop(0)
n = desktop.get_child_count()
print(f"\n[inner] AT-SPI Desktop: {n} app(s)")

def dump_tree(node, indent=0):
    if node is None:
        return
    prefix = "  " * indent
    try:
        role = node.get_role_name()
        name = node.get_name()
        nc = node.get_child_count()
        # Get bounds if available
        try:
            comp = node.get_component_iface()
            if comp:
                ext = comp.get_extents(Atspi.CoordType.SCREEN)
                bounds = f" @ ({ext.x},{ext.y},{ext.width},{ext.height})"
            else:
                bounds = ""
        except:
            bounds = ""
        print(f"{prefix}[{role}] {name!r}{bounds} ({nc} children)")
        for j in range(min(nc, 10)):
            dump_tree(node.get_child_at_index(j), indent + 1)
    except Exception as e:
        print(f"{prefix}ERROR: {e}")

for i in range(n):
    app_node = desktop.get_child_at_index(i)
    dump_tree(app_node)

print()
app.terminate()
atspi.terminate()
os.unlink(app_script.name)

if n > 0:
    print("[inner] SUCCESS")
else:
    print("[inner] FAIL: no apps registered")
'''.replace("APP_CODE", "APP_CODE_PLACEHOLDER")

procs = []
def cleanup():
    for p in reversed(procs):
        try: p.terminate(); p.wait(timeout=3)
        except: pass

signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(1)))

try:
    # Find free display
    display_num = 99
    while os.path.exists(f"/tmp/.X{display_num}-lock"):
        display_num += 1
    display = f":{display_num}"
    print(f"[outer] Display: {display}")

    # Start Xvfb
    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x1024x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    procs.append(xvfb)
    time.sleep(0.5)
    print("[outer] Xvfb started")

    # Write inner script with app code embedded
    inner_code = INNER_SCRIPT.replace("APP_CODE_PLACEHOLDER", repr(GTK_APP))
    script_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    script_file.write(inner_code)
    script_file.close()

    env = {**os.environ, "DISPLAY": display}
    result = subprocess.run(
        ["dbus-run-session", "--", sys.executable, script_file.name],
        env=env, capture_output=True, text=True, timeout=30,
    )
    print(result.stdout)
    if result.stderr:
        # Filter out dbus noise
        for line in result.stderr.split("\n"):
            if line.strip() and "dbus-daemon" not in line:
                print(f"[outer] stderr: {line}")

    os.unlink(script_file.name)

finally:
    cleanup()
