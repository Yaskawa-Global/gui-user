#!/usr/bin/env python3
"""Spike: validate AT-SPI2 works inside Xvfb with a D-Bus session."""

import os
import subprocess
import time
import signal
import sys

procs = []

def cleanup():
    for p in reversed(procs):
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(1)))

try:
    # 1. Find free display
    display_num = 99
    while os.path.exists(f"/tmp/.X{display_num}-lock"):
        display_num += 1
    display = f":{display_num}"
    print(f"[1] Using display {display}")

    # 2. Start Xvfb
    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x1024x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    procs.append(xvfb)
    time.sleep(0.5)
    if xvfb.poll() is not None:
        print("[FAIL] Xvfb exited immediately")
        sys.exit(1)
    print("[2] Xvfb started")

    # 3. Start D-Bus session daemon
    dbus = subprocess.Popen(
        ["dbus-daemon", "--session", "--print-address", "--nofork"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, "DISPLAY": display},
    )
    procs.append(dbus)
    dbus_address = dbus.stdout.readline().decode().strip()
    if not dbus_address:
        print("[FAIL] dbus-daemon did not print an address")
        cleanup()
        sys.exit(1)
    print(f"[3] D-Bus session: {dbus_address}")

    # Build environment for child processes
    child_env = {
        **os.environ,
        "DISPLAY": display,
        "DBUS_SESSION_BUS_ADDRESS": dbus_address,
        "QT_QPA_PLATFORM": "xcb",
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
        "QT_ACCESSIBILITY": "1",
        "GTK_MODULES": "gail:atk-bridge",
    }

    # 4. Start AT-SPI registry daemon
    atspi_path = "/usr/libexec/at-spi2-registryd"
    atspi = subprocess.Popen(
        [atspi_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=child_env,
    )
    procs.append(atspi)
    time.sleep(0.5)
    if atspi.poll() is not None:
        print(f"[WARN] at-spi2-registryd exited (code {atspi.returncode}), continuing anyway")
    else:
        print("[4] at-spi2-registryd started")

    # 4b. Enable accessibility BEFORE launching apps
    #     GTK apps check org.a11y.Status.IsEnabled at startup
    child_env["GTK_A11Y"] = "atspi"
    time.sleep(0.5)  # let registryd settle
    subprocess.run(
        ["dbus-send", "--session", "--dest=org.a11y.Status",
         "--type=method_call",
         "/org/a11y/bus", "org.freedesktop.DBus.Properties.Set",
         "string:org.a11y.Status", "string:IsEnabled",
         "variant:boolean:true"],
        env=child_env, capture_output=True, text=True, timeout=5,
    )
    print("[4b] Set org.a11y.Status.IsEnabled = true")

    # 5. Launch multiple test apps
    test_apps = ["gnome-calculator", "xclock", "xeyes"]
    for app_name in test_apps:
        p = subprocess.Popen(
            [app_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=child_env,
        )
        procs.append(p)
        print(f"[5] Launched {app_name} (pid={p.pid})")
    print("    Waiting 4s for apps to start and register with AT-SPI...")
    time.sleep(4)

    for p, name in zip(procs[3:], test_apps):  # skip xvfb, dbus, atspi
        status = "running" if p.poll() is None else f"exited({p.returncode})"
        print(f"    {name} (pid={p.pid}): {status}")
        if p.poll() is not None and p.stderr:
            stderr = p.stderr.read().decode(errors="replace")[:300]
            if stderr:
                print(f"      stderr: {stderr}")

    # Check D-Bus names and AT-SPI bus address
    print("\n[5b] Checking D-Bus...")
    dbus_check = subprocess.run(
        ["dbus-send", "--session", "--dest=org.freedesktop.DBus",
         "--type=method_call", "--print-reply",
         "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames"],
        env=child_env, capture_output=True, text=True, timeout=5,
    )
    for line in dbus_check.stdout.split("\n"):
        if "atspi" in line.lower() or "a11y" in line.lower() or "registr" in line.lower():
            print(f"    D-Bus name: {line.strip()}")

    # Get the AT-SPI bus address
    atspi_bus = subprocess.run(
        ["dbus-send", "--session", "--dest=org.a11y.Bus",
         "--type=method_call", "--print-reply",
         "/org/a11y/bus", "org.a11y.Bus.GetAddress"],
        env=child_env, capture_output=True, text=True, timeout=5,
    )
    print(f"    AT-SPI bus address response:\n{atspi_bus.stdout.strip()}")
    if atspi_bus.stderr.strip():
        print(f"    AT-SPI bus stderr: {atspi_bus.stderr.strip()}")

    # Also try: is the AT-SPI enabled?
    atspi_status = subprocess.run(
        ["dbus-send", "--session", "--dest=org.a11y.Status",
         "--type=method_call", "--print-reply",
         "/org/a11y/bus", "org.freedesktop.DBus.Properties.Get",
         "string:org.a11y.Status", "string:IsEnabled"],
        env=child_env, capture_output=True, text=True, timeout=5,
    )
    print(f"    a11y IsEnabled: {atspi_status.stdout.strip()}")

    # Try setting it to enabled
    subprocess.run(
        ["dbus-send", "--session", "--dest=org.a11y.Status",
         "--type=method_call",
         "/org/a11y/bus", "org.freedesktop.DBus.Properties.Set",
         "string:org.a11y.Status", "string:IsEnabled",
         "variant:boolean:true"],
        env=child_env, capture_output=True, text=True, timeout=5,
    )
    print("    Set IsEnabled=true, waiting 2s for apps to re-register...")
    time.sleep(2)

    # 6. Query AT-SPI from within the same D-Bus session
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = dbus_address
    os.environ["DISPLAY"] = display

    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    desktop = Atspi.get_desktop(0)
    child_count = desktop.get_child_count()
    print(f"\n[6] AT-SPI Desktop has {child_count} application(s):")

    if child_count == 0:
        print("[FAIL] No applications visible in AT-SPI tree")
        cleanup()
        sys.exit(1)

    for i in range(child_count):
        app_node = desktop.get_child_at_index(i)
        name = app_node.get_name() if app_node else "<null>"
        pid = app_node.get_process_id() if app_node else -1
        n_children = app_node.get_child_count() if app_node else 0
        print(f"  [{i}] name={name!r}, pid={pid}, children={n_children}")

        # Try to enumerate first level of children (windows)
        for j in range(min(n_children, 3)):
            child = app_node.get_child_at_index(j)
            if child:
                role = child.get_role_name()
                cname = child.get_name()
                print(f"    [{j}] role={role!r}, name={cname!r}")

    print("\n[SUCCESS] AT-SPI works inside Xvfb!")

finally:
    cleanup()
