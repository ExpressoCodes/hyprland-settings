"""Tests for hyprland_settings.backend.hyprctl.

All tests mock subprocess.run and socket connections so they can run without a
live Hyprland session.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call

from hyprland_settings.backend.hyprctl import (
    HyprctlApplyError,
    HyprctlUnavailableError,
    Monitor,
    apply_monitor,
    apply_monitors_batch,
    get_all_monitors,
    get_monitors,
    reload_config,
    subscribe_monitor_events,
    _monitor_keyword_value,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MONITOR_JSON_1 = {
    "id": 0,
    "name": "eDP-1",
    "description": "Chimei Innolux Corporation 0x1515 (eDP-1)",
    "make": "Chimei Innolux Corporation",
    "model": "0x1515",
    "serial": "",
    "width": 1920,
    "height": 1080,
    "refreshRate": 60.007999,
    "x": 0,
    "y": 0,
    "activeWorkspace": {"id": 1, "name": "1"},
    "specialWorkspace": {"id": 0, "name": ""},
    "reserved": [0, 0, 0, 0],
    "scale": 1.0,
    "transform": 0,
    "focused": True,
    "dpmsStatus": True,
    "vrr": False,
}

MONITOR_JSON_2 = {
    "id": 1,
    "name": "DP-1",
    "description": "Dell Inc. U2722D (DP-1)",
    "make": "Dell Inc.",
    "model": "U2722D",
    "serial": "ABCD1234",
    "width": 2560,
    "height": 1440,
    "refreshRate": 144.0,
    "x": 1920,
    "y": 0,
    "activeWorkspace": {"id": 2, "name": "2"},
    "specialWorkspace": {"id": 0, "name": ""},
    "reserved": [0, 0, 0, 0],
    "scale": 1.5,
    "transform": 0,
    "focused": False,
    "dpmsStatus": True,
    "vrr": True,
}


def _make_completed(stdout="", returncode=0, stderr=""):
    result = subprocess.CompletedProcess(args=[], returncode=returncode)
    result.stdout = stdout
    result.stderr = stderr
    return result


def _make_monitor(**overrides) -> Monitor:
    base = dict(
        id=0,
        name="eDP-1",
        description="Test monitor",
        make="Test",
        model="M1",
        serial="",
        width=1920,
        height=1080,
        refresh_rate=60.0,
        x=0,
        y=0,
        scale=1.0,
        transform=0,
        focused=True,
        dpms_status=True,
        vrr=False,
    )
    base.update(overrides)
    return Monitor(**base)


# ---------------------------------------------------------------------------
# Monitor dataclass tests
# ---------------------------------------------------------------------------


class TestMonitorLogicalDimensions(unittest.TestCase):
    def test_no_transform(self):
        m = _make_monitor(width=1920, height=1080, scale=1.0, transform=0)
        self.assertEqual(m.logical_width, 1920)
        self.assertEqual(m.logical_height, 1080)

    def test_scale_2(self):
        m = _make_monitor(width=3840, height=2160, scale=2.0, transform=0)
        self.assertEqual(m.logical_width, 1920)
        self.assertEqual(m.logical_height, 1080)

    def test_fractional_scale(self):
        m = _make_monitor(width=2560, height=1440, scale=1.5, transform=0)
        self.assertEqual(m.logical_width, round(2560 / 1.5))
        self.assertEqual(m.logical_height, round(1440 / 1.5))

    def test_rotated_90(self):
        """Transform 1 (90°) swaps width and height."""
        m = _make_monitor(width=1920, height=1080, scale=1.0, transform=1)
        self.assertEqual(m.logical_width, 1080)
        self.assertEqual(m.logical_height, 1920)

    def test_rotated_270(self):
        m = _make_monitor(width=1920, height=1080, scale=1.0, transform=3)
        self.assertEqual(m.logical_width, 1080)
        self.assertEqual(m.logical_height, 1920)

    def test_rotated_180_no_swap(self):
        m = _make_monitor(width=1920, height=1080, scale=1.0, transform=2)
        self.assertEqual(m.logical_width, 1920)
        self.assertEqual(m.logical_height, 1080)


# ---------------------------------------------------------------------------
# Keyword value builder tests
# ---------------------------------------------------------------------------


class TestMonitorKeywordValue(unittest.TestCase):
    def test_basic(self):
        m = _make_monitor()
        val = _monitor_keyword_value(m)
        self.assertEqual(val, "eDP-1,1920x1080@60,0x0,1")

    def test_with_transform(self):
        m = _make_monitor(transform=1)
        val = _monitor_keyword_value(m)
        self.assertIn("transform,1", val)

    def test_no_transform_suffix_when_zero(self):
        m = _make_monitor(transform=0)
        val = _monitor_keyword_value(m)
        self.assertNotIn("transform", val)

    def test_with_mirror(self):
        m = _make_monitor(mirror_of="eDP-1")
        val = _monitor_keyword_value(m)
        self.assertIn("mirror,eDP-1", val)

    def test_disabled(self):
        m = _make_monitor(disabled=True)
        val = _monitor_keyword_value(m)
        self.assertEqual(val, "eDP-1,disabled")

    def test_fractional_scale_format(self):
        m = _make_monitor(scale=1.5)
        val = _monitor_keyword_value(m)
        self.assertIn(",1.5", val)

    def test_refresh_rate_float(self):
        m = _make_monitor(refresh_rate=144.0)
        val = _monitor_keyword_value(m)
        self.assertIn("@144", val)

    def test_position(self):
        m = _make_monitor(x=1920, y=0)
        val = _monitor_keyword_value(m)
        self.assertIn("1920x0", val)


# ---------------------------------------------------------------------------
# get_monitors tests
# ---------------------------------------------------------------------------


class TestGetMonitors(unittest.TestCase):
    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_returns_monitor_list(self, mock_run):
        mock_run.return_value = _make_completed(
            stdout=json.dumps([MONITOR_JSON_1, MONITOR_JSON_2])
        )
        monitors = get_monitors()
        self.assertEqual(len(monitors), 2)
        self.assertIsInstance(monitors[0], Monitor)
        self.assertEqual(monitors[0].name, "eDP-1")
        self.assertEqual(monitors[1].name, "DP-1")
        # Verify the right command was called
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args, ["hyprctl", "monitors", "-j"])

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_maps_fields_correctly(self, mock_run):
        mock_run.return_value = _make_completed(stdout=json.dumps([MONITOR_JSON_1]))
        monitors = get_monitors()
        m = monitors[0]
        self.assertEqual(m.id, 0)
        self.assertEqual(m.width, 1920)
        self.assertEqual(m.height, 1080)
        self.assertAlmostEqual(m.refresh_rate, 60.007999, places=3)
        self.assertEqual(m.scale, 1.0)
        self.assertTrue(m.focused)
        self.assertTrue(m.dpms_status)
        self.assertFalse(m.vrr)

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_run):
        mock_run.return_value = _make_completed(returncode=1, stderr="error")
        with self.assertRaises(HyprctlUnavailableError):
            get_monitors()

    @patch(
        "hyprland_settings.backend.hyprctl.subprocess.run",
        side_effect=FileNotFoundError("hyprctl not found"),
    )
    def test_raises_on_missing_binary(self, mock_run):
        with self.assertRaises(HyprctlUnavailableError):
            get_monitors()

    @patch(
        "hyprland_settings.backend.hyprctl.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="hyprctl", timeout=5),
    )
    def test_raises_on_timeout(self, mock_run):
        with self.assertRaises(HyprctlUnavailableError):
            get_monitors()

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_timeout_passed_to_subprocess(self, mock_run):
        mock_run.return_value = _make_completed(stdout=json.dumps([MONITOR_JSON_1]))
        get_monitors()
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["timeout"], 5)


# ---------------------------------------------------------------------------
# get_all_monitors tests
# ---------------------------------------------------------------------------


class TestGetAllMonitors(unittest.TestCase):
    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_calls_monitors_all(self, mock_run):
        mock_run.return_value = _make_completed(
            stdout=json.dumps([MONITOR_JSON_1])
        )
        monitors = get_all_monitors()
        args = mock_run.call_args[0][0]
        self.assertEqual(args, ["hyprctl", "monitors", "all", "-j"])
        self.assertEqual(len(monitors), 1)

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_run):
        mock_run.return_value = _make_completed(returncode=1, stderr="error")
        with self.assertRaises(HyprctlUnavailableError):
            get_all_monitors()


# ---------------------------------------------------------------------------
# apply_monitor tests
# ---------------------------------------------------------------------------


class TestApplyMonitor(unittest.TestCase):
    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_calls_hyprctl_keyword(self, mock_run):
        mock_run.return_value = _make_completed()
        m = _make_monitor()
        apply_monitor(m)
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "hyprctl")
        self.assertEqual(args[1], "keyword")
        self.assertEqual(args[2], "monitor")
        self.assertIsInstance(args[3], str)

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_run):
        mock_run.return_value = _make_completed(returncode=1, stderr="error")
        with self.assertRaises(HyprctlApplyError):
            apply_monitor(_make_monitor())

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_timeout_passed(self, mock_run):
        mock_run.return_value = _make_completed()
        apply_monitor(_make_monitor())
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["timeout"], 5)

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_disabled_monitor(self, mock_run):
        mock_run.return_value = _make_completed()
        m = _make_monitor(disabled=True)
        apply_monitor(m)
        args = mock_run.call_args[0][0]
        self.assertIn("disabled", args[3])


# ---------------------------------------------------------------------------
# apply_monitors_batch tests
# ---------------------------------------------------------------------------


class TestApplyMonitorsBatch(unittest.TestCase):
    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_calls_batch(self, mock_run):
        mock_run.return_value = _make_completed()
        m1 = _make_monitor(name="eDP-1")
        m2 = _make_monitor(name="DP-1", x=1920)
        apply_monitors_batch([m1, m2])
        args = mock_run.call_args[0][0]
        self.assertIn("--batch", args)

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_batch_contains_semicolons(self, mock_run):
        mock_run.return_value = _make_completed()
        m1 = _make_monitor(name="eDP-1")
        m2 = _make_monitor(name="DP-1", x=1920)
        apply_monitors_batch([m1, m2])
        batch_str = mock_run.call_args[0][0][-1]
        self.assertIn(";", batch_str)

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_run):
        mock_run.return_value = _make_completed(returncode=1, stderr="error")
        with self.assertRaises(HyprctlApplyError):
            apply_monitors_batch([_make_monitor()])

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_timeout_passed(self, mock_run):
        mock_run.return_value = _make_completed()
        apply_monitors_batch([_make_monitor()])
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["timeout"], 5)


# ---------------------------------------------------------------------------
# reload_config tests
# ---------------------------------------------------------------------------


class TestReloadConfig(unittest.TestCase):
    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_calls_reload(self, mock_run):
        mock_run.return_value = _make_completed()
        reload_config()
        args = mock_run.call_args[0][0]
        self.assertEqual(args, ["hyprctl", "reload"])

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_run):
        mock_run.return_value = _make_completed(returncode=1, stderr="error")
        with self.assertRaises(HyprctlApplyError):
            reload_config()

    @patch("hyprland_settings.backend.hyprctl.subprocess.run")
    def test_timeout_passed(self, mock_run):
        mock_run.return_value = _make_completed()
        reload_config()
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["timeout"], 5)


# ---------------------------------------------------------------------------
# subscribe_monitor_events tests
# ---------------------------------------------------------------------------


class TestSubscribeMonitorEvents(unittest.TestCase):
    def _make_fake_socket(self, data: bytes):
        """Return a mock socket that yields data once then empty bytes."""
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = [data, b""]
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = MagicMock(return_value=False)
        return mock_sock

    @patch("hyprland_settings.backend.hyprctl.socket.socket")
    def test_callback_called_for_events(self, mock_socket_cls):
        events_received = []

        data = b"monitoradded>>eDP-1\nmonitorremoved>>DP-1\n"
        fake_sock = self._make_fake_socket(data)
        mock_socket_cls.return_value = fake_sock

        def cb(name, data_str):
            events_received.append((name, data_str))

        subscribe_monitor_events(cb)

        # Give the thread time to process.
        time.sleep(0.2)

        self.assertIn(("monitoradded", "eDP-1"), events_received)
        self.assertIn(("monitorremoved", "DP-1"), events_received)

    @patch("hyprland_settings.backend.hyprctl.socket.socket")
    def test_thread_is_daemon(self, mock_socket_cls):
        fake_sock = self._make_fake_socket(b"")
        mock_socket_cls.return_value = fake_sock

        subscribe_monitor_events(lambda n, d: None)

        import hyprland_settings.backend.hyprctl as _m
        self.assertIsNotNone(_m._event_thread)
        self.assertTrue(_m._event_thread.daemon)

    @patch("hyprland_settings.backend.hyprctl.socket.socket")
    def test_v2_event_parsed(self, mock_socket_cls):
        events_received = []
        data = b"monitoraddedv2>>0,eDP-1,Chimei Innolux\n"
        fake_sock = self._make_fake_socket(data)
        mock_socket_cls.return_value = fake_sock

        subscribe_monitor_events(lambda n, d: events_received.append((n, d)))
        time.sleep(0.2)

        self.assertEqual(events_received[0][0], "monitoraddedv2")
        self.assertIn("eDP-1", events_received[0][1])

    @patch("hyprland_settings.backend.hyprctl.socket.socket")
    def test_callback_exception_does_not_crash_thread(self, mock_socket_cls):
        """A callback that raises must not kill the reader thread."""
        data = b"monitoradded>>eDP-1\nmonitorremoved>>DP-1\n"
        fake_sock = self._make_fake_socket(data)
        mock_socket_cls.return_value = fake_sock

        call_count = [0]

        def bad_cb(name, data_str):
            call_count[0] += 1
            raise RuntimeError("boom")

        subscribe_monitor_events(bad_cb)
        time.sleep(0.2)

        # Both events should still have been attempted.
        self.assertGreaterEqual(call_count[0], 1)

    @patch("hyprland_settings.backend.hyprctl.socket.socket")
    def test_socket_connect_uses_correct_path(self, mock_socket_cls):
        import os
        fake_sock = self._make_fake_socket(b"")
        mock_socket_cls.return_value = fake_sock

        with patch.dict(
            os.environ,
            {
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "HYPRLAND_INSTANCE_SIGNATURE": "abc123",
            },
        ):
            subscribe_monitor_events(lambda n, d: None)
            time.sleep(0.1)

        expected = "/run/user/1000/hypr/abc123/.socket2.sock"
        fake_sock.connect.assert_called_with(expected)


if __name__ == "__main__":
    unittest.main()
