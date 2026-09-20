"""Hyprland IPC backend — the single module that talks to Hyprland.

All subprocess calls and socket connections live here.  No other module should
call subprocess or touch IPC sockets directly.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HyprctlUnavailableError(Exception):
    """Raised when hyprctl is not found or exits with a non-zero status."""


class HyprctlApplyError(Exception):
    """Raised when a hyprctl apply command exits with a non-zero status."""


# ---------------------------------------------------------------------------
# Monitor dataclass
# ---------------------------------------------------------------------------


@dataclass
class Monitor:
    id: int
    name: str
    description: str
    make: str
    model: str
    serial: str
    width: int           # physical pixels
    height: int          # physical pixels
    refresh_rate: float  # Hz
    x: int               # logical position
    y: int               # logical position
    scale: float
    transform: int       # 0-7
    focused: bool
    dpms_status: bool
    vrr: bool
    disabled: bool = False
    mirror_of: str = ""  # name of source monitor if mirroring

    @property
    def logical_width(self) -> int:
        """Width in logical pixels after scale."""
        if self.transform in (1, 3, 5, 7):
            return round(self.height / self.scale)
        return round(self.width / self.scale)

    @property
    def logical_height(self) -> int:
        """Height in logical pixels after scale."""
        if self.transform in (1, 3, 5, 7):
            return round(self.width / self.scale)
        return round(self.height / self.scale)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """Run a hyprctl command with a 5-second timeout and return the result."""
    log.debug("hyprctl command: %s", " ".join(args))
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise HyprctlUnavailableError(f"hyprctl not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HyprctlUnavailableError(f"hyprctl timed out: {exc}") from exc
    return result


def _monitor_from_dict(data: dict) -> Monitor:
    """Build a Monitor from a hyprctl JSON monitor object."""
    return Monitor(
        id=data["id"],
        name=data["name"],
        description=data.get("description", ""),
        make=data.get("make", ""),
        model=data.get("model", ""),
        serial=data.get("serial", ""),
        width=data["width"],
        height=data["height"],
        refresh_rate=data["refreshRate"],
        x=data["x"],
        y=data["y"],
        scale=data["scale"],
        transform=data["transform"],
        focused=data["focused"],
        dpms_status=data["dpmsStatus"],
        vrr=data["vrr"],
    )


def _monitor_keyword_value(monitor: Monitor) -> str:
    """Build the value string for `hyprctl keyword monitor <value>`."""
    if monitor.disabled:
        return f"{monitor.name},disabled"

    hz = f"{monitor.refresh_rate:g}"
    base = (
        f"{monitor.name},"
        f"{monitor.width}x{monitor.height}@{hz},"
        f"{monitor.x}x{monitor.y},"
        f"{monitor.scale:g}"
    )

    if monitor.transform != 0:
        base += f",transform,{monitor.transform}"

    if monitor.mirror_of:
        base += f",mirror,{monitor.mirror_of}"

    return base


def _event_socket_path() -> str:
    """Return the path to Hyprland's event socket (.socket2.sock)."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    return os.path.join(runtime_dir, "hypr", sig, ".socket2.sock")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_monitors() -> list[Monitor]:
    """Return the list of currently active monitors.

    Raises HyprctlUnavailableError if hyprctl is unavailable or fails.
    """
    result = _run(["hyprctl", "monitors", "-j"])
    if result.returncode != 0:
        raise HyprctlUnavailableError(
            f"hyprctl monitors -j failed (rc={result.returncode}): {result.stderr}"
        )
    data = json.loads(result.stdout)
    return [_monitor_from_dict(m) for m in data]


def get_all_monitors() -> list[Monitor]:
    """Return all monitors including disconnected ones.

    Raises HyprctlUnavailableError if hyprctl is unavailable or fails.
    """
    result = _run(["hyprctl", "monitors", "all", "-j"])
    if result.returncode != 0:
        raise HyprctlUnavailableError(
            f"hyprctl monitors all -j failed (rc={result.returncode}): {result.stderr}"
        )
    data = json.loads(result.stdout)
    return [_monitor_from_dict(m) for m in data]


def apply_monitor(monitor: Monitor) -> None:
    """Apply a single monitor configuration at runtime via hyprctl keyword.

    Raises HyprctlApplyError on non-zero exit.
    """
    value = _monitor_keyword_value(monitor)
    result = _run(["hyprctl", "keyword", "monitor", value])
    if result.returncode != 0:
        raise HyprctlApplyError(
            f"hyprctl keyword monitor failed (rc={result.returncode}): {result.stderr}"
        )


def apply_monitors_batch(monitors: list[Monitor]) -> None:
    """Apply multiple monitor configurations in a single hyprctl --batch call.

    Preferred over calling apply_monitor in a loop.
    Raises HyprctlApplyError on non-zero exit.
    """
    commands = " ; ".join(
        f"keyword monitor {_monitor_keyword_value(m)}" for m in monitors
    )
    result = _run(["hyprctl", "--batch", commands])
    if result.returncode != 0:
        raise HyprctlApplyError(
            f"hyprctl --batch failed (rc={result.returncode}): {result.stderr}"
        )


def get_available_modes(monitor_name: str) -> list[str]:
    """Return available mode strings for *monitor_name*.

    Queries ``hyprctl monitors all -j`` and returns the ``availableModes``
    list for the named monitor.  Strings are in Hyprland native format,
    e.g. ``"1920x1080@144.00Hz"``.  Returns an empty list on any failure.
    """
    try:
        result = _run(["hyprctl", "monitors", "all", "-j"])
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        for mon in data:
            if mon.get("name") == monitor_name:
                return mon.get("availableModes", [])
    except Exception:
        log.debug("Could not get available modes for %s", monitor_name, exc_info=True)
    return []


def get_option(option_path: str) -> dict:
    """Call `hyprctl getoption {option_path} -j` and return parsed JSON.

    Example: get_option("general:gaps_in") → {"option": "general:gaps_in", "int": 3, "set": True}
    Returns {} on any error (hyprctl unavailable, parse error, etc.).
    Never raises — always returns a dict.
    """
    try:
        result = _run(["hyprctl", "getoption", option_path, "-j"])
        if result.returncode != 0:
            log.debug("hyprctl getoption %s failed (rc=%d): %s", option_path, result.returncode, result.stderr)
            return {}
        return json.loads(result.stdout)
    except Exception:
        log.debug("get_option(%r) failed", option_path, exc_info=True)
        return {}


def apply_keyword(key: str, value: str) -> None:
    """Call `hyprctl keyword {key} {value}`.

    Example: apply_keyword("general:gaps_in", "5")
    Raises HyprctlApplyError on non-zero exit.
    Raises HyprctlUnavailableError if hyprctl not found.
    """
    result = _run(["hyprctl", "keyword", key, value])
    if result.returncode != 0:
        raise HyprctlApplyError(
            f"hyprctl keyword {key} {value!r} failed (rc={result.returncode}): {result.stderr}"
        )


def reload_config() -> None:
    """Tell Hyprland to reload its configuration file."""
    result = _run(["hyprctl", "reload"])
    if result.returncode != 0:
        raise HyprctlApplyError(
            f"hyprctl reload failed (rc={result.returncode}): {result.stderr}"
        )


# The single global for the event thread (intentional).
_event_thread: threading.Thread | None = None


def subscribe_monitor_events(callback: Callable[[str, str], None]) -> None:
    """Connect to Hyprland's event socket and call *callback* for each event.

    The connection runs in a background daemon thread so it does not block the
    caller.  The callback receives ``(event_name, event_data)`` for every line
    received from the socket.  Relevant monitor events:

    - ``monitoradded`` / ``monitorremoved``
    - ``monitoraddedv2`` / ``monitorremovedv2``
    """
    global _event_thread

    socket_path = _event_socket_path()

    def _reader() -> None:
        log.debug("Connecting to Hyprland event socket: %s", socket_path)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(socket_path)
                buf = ""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        log.debug("Hyprland event socket closed.")
                        break
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        if ">>" in line:
                            event_name, _, event_data = line.partition(">>")
                        else:
                            event_name, event_data = line, ""
                        log.debug("Hyprland event: %s >> %s", event_name, event_data)
                        try:
                            callback(event_name, event_data)
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "Error in monitor event callback for event %s",
                                event_name,
                            )
        except OSError as exc:
            log.error("Hyprland event socket error: %s", exc)

    _event_thread = threading.Thread(target=_reader, name="hyprland-events", daemon=True)
    _event_thread.start()
