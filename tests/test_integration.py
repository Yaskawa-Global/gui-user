"""Integration tests for gui-user MCP server.

Launches a GTK3 test app and exercises the full observe -> act -> verify cycle.
Tests are numbered to enforce execution order (shared app session).

Run with: python3 -m pytest tests/test_integration.py -v
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.main import (
    launch_app, close_app, get_app_status,
    screenshot, list_ui_elements, find_element, get_element_info,
    click, click_element, double_click, hover, hover_element,
    type_text, press_key,
    wait_for_idle, wait_for_element,
)

GTK_TEST_APP = '''
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

win = Gtk.Window(title="IntegrationTest")
win.set_default_size(300, 200)
win.connect("destroy", Gtk.main_quit)

box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
win.add(box)

lbl = Gtk.Label(label="Status: idle")
box.pack_start(lbl, True, True, 0)

btn = Gtk.Button(label="Click Me")
def on_click(b):
    lbl.set_text("Status: clicked!")
btn.connect("clicked", on_click)
box.pack_start(btn, True, True, 0)

entry = Gtk.Entry()
entry.set_text("Original text")
box.pack_start(entry, True, True, 0)

win.show_all()
Gtk.main()
'''


class TestMCPWorkflow(unittest.TestCase):
    """Full workflow: launch -> observe -> interact -> verify -> close."""

    _app_file = None

    @classmethod
    def setUpClass(cls):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
        f.write(GTK_TEST_APP)
        f.close()
        cls._app_file = f.name

        result = asyncio.run(launch_app(binary="python3", args=[f.name], timeout=5.0))
        if not result["success"]:
            raise unittest.SkipTest(f"launch_app failed in test environment: {result['message']}")

    @classmethod
    def tearDownClass(cls):
        close_app()
        if cls._app_file:
            os.unlink(cls._app_file)

    def test_01_app_running(self):
        result = get_app_status()
        self.assertTrue(result["running"])
        self.assertIsNotNone(result["pid"])

    def test_02_list_elements(self):
        result = list_ui_elements()
        self.assertTrue(result["success"])
        self.assertGreater(result["count"], 0)
        roles = [e["role"] for e in result["elements"]]
        self.assertIn("push button", roles)
        self.assertIn("label", roles)

    def test_03_find_element(self):
        result = find_element(text="Click Me", role="button")
        self.assertTrue(result["success"])
        self.assertGreater(result["element"]["bounds"][2], 0)

    def test_04_get_element_info_by_coords(self):
        wait_for_element(text="Click Me", role="button", timeout=5.0)
        btn = find_element(text="Click Me", role="button")
        self.assertTrue(btn["success"], btn)
        cx, cy = btn["element"]["center"]
        result = get_element_info(at_x=cx, at_y=cy)
        self.assertTrue(result["success"])

    def test_05_click_element_changes_label(self):
        result = click_element(text="Click Me", role="button")
        self.assertTrue(result["success"])
        time.sleep(0.5)
        result = find_element(role="label")
        self.assertIn("clicked", result["element"]["name"].lower())

    def test_06_type_text(self):
        click_element(role="text")
        time.sleep(0.2)
        press_key("a", modifiers=["Ctrl"])
        type_text(text="Integration test")
        time.sleep(0.3)
        result = find_element(role="text")
        self.assertIn("Integration test", result["element"]["text"])

    def test_07_screenshot(self):
        path = "/tmp/test_integration.png"
        result = screenshot(output_path=path)
        self.assertTrue(result["success"])
        self.assertTrue(os.path.exists(path))
        self.assertGreater(len(result["image_base64"]), 100)
        os.unlink(path)

    def test_08_wait_for_idle(self):
        result = wait_for_idle(timeout=5.0)
        self.assertTrue(result["success"])

    def test_09_hover(self):
        result = hover(x=150, y=100)
        self.assertTrue(result["success"])

    def test_10_hover_element(self):
        result = hover_element(text="Click Me", role="button")
        self.assertTrue(result["success"])

    def test_11_press_key(self):
        for key in ["Escape", "F1", "Tab"]:
            result = press_key(key=key)
            self.assertTrue(result["success"], f"press_key({key}) failed")

    def test_12_double_click(self):
        result = double_click(x=150, y=100)
        self.assertTrue(result["success"])

    def test_13_list_elements_filtered(self):
        result = list_ui_elements(role="button")
        self.assertTrue(result["success"])
        self.assertGreaterEqual(result["count"], 1)
        for e in result["elements"]:
            self.assertIn("button", e["role"].lower())


if __name__ == "__main__":
    unittest.main()
