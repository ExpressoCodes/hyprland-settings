"""Dock settings page.

Implements DockPage — an Adw.PreferencesPage subclass that manages qs-dock's
persisted ``behaviour.enabled`` flag.  Unlike the other settings pages this
option has NO Hyprland key: it is written straight into
``~/.config/qs-dock/settings.json`` by the qs_dock_writer backend, so
``collect_lines()`` returns no Hyprland lines and there is no ``apply_live()``
hyprctl path.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject

from hyprland_settings.backend.qs_dock_writer import (
    DEFAULT_DOCK_ENABLED,
    read_dock_enabled,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, bool] = {
    "dock_enabled": DEFAULT_DOCK_ENABLED,
}


# ---------------------------------------------------------------------------
# DockPage
# ---------------------------------------------------------------------------


class DockPage(Adw.PreferencesPage):
    """Dock settings page backed by qs-dock's settings.json.

    Signals
    -------
    settings-changed
        Emitted when the switch value changes from the state that was loaded.
    """

    __gtype_name__ = "DockPage"

    __gsignals__ = {
        "settings-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()

        self._suppress_signals: bool = False
        self._loaded_values: dict[str, bool] = {}

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_dock_group()

    def _build_dock_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Dock")
        self.add(group)

        self._dock_enabled = Adw.SwitchRow()
        self._dock_enabled.set_title("Enable Dock")
        self._dock_enabled.set_subtitle("Show the qs-dock dock window")
        group.add(self._dock_enabled)

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._dock_enabled.connect("notify::active", self._on_switch_changed)

    def _on_switch_changed(self, _row: Adw.SwitchRow, _param: object) -> None:
        if self._suppress_signals:
            return
        if self._has_changed():
            self.emit("settings-changed")

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    def _current_values(self) -> dict[str, bool]:
        return {
            "dock_enabled": self._dock_enabled.get_active(),
        }

    def _snapshot_values(self) -> dict[str, bool]:
        return self._current_values()

    def _has_changed(self) -> bool:
        if not self._loaded_values:
            return False
        return self._current_values() != self._loaded_values

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_saved(self) -> None:
        """Record current widget values as the last-saved state (for revert)."""
        self._loaded_values = self._current_values()

    def revert_to_loaded(self) -> None:
        """Reset the switch to the last-saved state."""
        vals = self._loaded_values
        if not vals:
            return
        self._suppress_signals = True
        try:
            self._dock_enabled.set_active(bool(vals["dock_enabled"]))
        finally:
            self._suppress_signals = False

    def load(self, config_path: Path | None, hyprctl_available: bool) -> None:
        """Populate the switch from qs-dock's settings.json (or the default).

        The ``config_path``/``hyprctl_available`` arguments are accepted to match
        the common page-load contract but are unused here — this option is not a
        Hyprland key, so no hyprctl query is performed.
        """
        values: dict[str, bool] = dict(DEFAULTS)
        try:
            values["dock_enabled"] = read_dock_enabled()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read qs-dock enabled state: %s", exc)

        self._suppress_signals = True
        try:
            self._dock_enabled.set_active(bool(values["dock_enabled"]))
        finally:
            self._suppress_signals = False

        self._loaded_values = self._current_values()

    def get_enabled(self) -> bool:
        """Return the current switch state (used by the qs-dock save path)."""
        return self._dock_enabled.get_active()

    def collect_lines(self) -> list[str]:
        """Return no Hyprland lines — this option has no Hyprland config block."""
        return []
