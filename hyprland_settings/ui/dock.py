"""Dock settings page (hybrid).

Implements DockPage — an Adw.PreferencesPage subclass that manages a curated
subset of qs-dock's persisted settings inline (appearance + behaviour), plus an
"Advanced" row that launches qs-dock's own settings window for everything else
(position, pinned apps, exclusions, ...).

Unlike the other settings pages these options have NO Hyprland key: they are
written straight into ``~/.config/qs-dock/settings.json`` by the qs_dock_writer
backend, so ``collect_lines()`` returns no Hyprland lines and there is no
``apply_live()`` hyprctl path.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk

from hyprland_settings.backend.qs_dock_writer import (
    DOCK_DEFAULTS,
    open_advanced_settings,
    read_dock_settings,
)
from hyprland_settings.ui.appearance import AppearancePage

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default values (sourced from qs-dock Settings.qml, via the backend)
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, int | bool | str] = dict(DOCK_DEFAULTS)

# Visibility mode: ComboRow index <-> stored value.  Order matches the labels
# in _build_behaviour_group().
_VIS_MODES: list[str] = ["reserve", "overlap", "autohide", "intelligent"]
_VIS_LABELS: list[str] = [
    "Reserve space",
    "Overlap windows",
    "Auto-hide",
    "Intelligent",
]
# Delays only matter when the dock auto-hides.
_DELAY_MODES: frozenset[str] = frozenset({"autohide", "intelligent"})


# ---------------------------------------------------------------------------
# DockPage
# ---------------------------------------------------------------------------


class DockPage(Adw.PreferencesPage):
    """Hybrid Dock settings page backed by qs-dock's settings.json.

    Signals
    -------
    settings-changed
        Emitted when any managed value changes from the state that was loaded.
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
        self._loaded_values: dict[str, int | bool | str] = {}

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_general_group()
        self._build_appearance_group()
        self._build_behaviour_group()
        self._build_advanced_group()

    def _build_general_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("General")
        self.add(group)

        self._dock_enabled = Adw.SwitchRow()
        self._dock_enabled.set_title("Enable Dock")
        self._dock_enabled.set_subtitle("Show the qs-dock dock window")
        group.add(self._dock_enabled)

    def _build_appearance_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Appearance")
        self.add(group)
        self._appearance_group = group

        self._icon_size = AppearancePage._make_spin_row(
            title="Icon Size", lower=24, upper=128, step=1, digits=0,
        )
        self._icon_size.set_subtitle("Dock icon size (pixels)")
        group.add(self._icon_size)

        self._spacing = AppearancePage._make_spin_row(
            title="Icon Spacing", lower=0, upper=24, step=1, digits=0,
        )
        self._spacing.set_subtitle("Space between icons (pixels)")
        group.add(self._spacing)

        self._magnification = Adw.SwitchRow()
        self._magnification.set_title("Hover Magnification")
        group.add(self._magnification)

        self._max_magnified_size = AppearancePage._make_spin_row(
            title="Maximum Magnified Size", lower=24, upper=192, step=1, digits=0,
        )
        self._max_magnified_size.set_subtitle("Largest icon size when magnified (pixels)")
        group.add(self._max_magnified_size)

    def _build_behaviour_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Behaviour")
        self.add(group)
        self._behaviour_group = group

        self._visibility_mode = Adw.ComboRow()
        self._visibility_mode.set_title("Visibility")
        self._visibility_mode.set_model(Gtk.StringList.new(_VIS_LABELS))
        group.add(self._visibility_mode)

        self._hide_on_fullscreen = Adw.SwitchRow()
        self._hide_on_fullscreen.set_title("Hide on Fullscreen")
        group.add(self._hide_on_fullscreen)

        self._show_delay = AppearancePage._make_spin_row(
            title="Show Delay", lower=0, upper=1000, step=50, digits=0,
        )
        self._show_delay.set_subtitle("Delay before showing (milliseconds)")
        group.add(self._show_delay)

        self._hide_delay = AppearancePage._make_spin_row(
            title="Hide Delay", lower=0, upper=2000, step=50, digits=0,
        )
        self._hide_delay.set_subtitle("Delay before hiding (milliseconds)")
        group.add(self._hide_delay)

    def _build_advanced_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Advanced")
        self.add(group)

        row = Adw.ActionRow()
        row.set_title("Advanced Dock Settings")
        row.set_subtitle(
            "Position, pinned apps, exclusions and more — opens in a separate "
            "qs-dock window"
        )
        self._advanced_button = Gtk.Button(label="Open…")
        self._advanced_button.add_css_class("flat")
        self._advanced_button.set_valign(Gtk.Align.CENTER)
        row.add_suffix(self._advanced_button)
        row.set_activatable_widget(self._advanced_button)
        group.add(row)

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        for spin in (
            self._icon_size,
            self._spacing,
            self._max_magnified_size,
            self._show_delay,
            self._hide_delay,
        ):
            spin.connect("notify::value", self._on_value_changed)

        self._dock_enabled.connect("notify::active", self._on_dock_enabled_changed)
        self._magnification.connect("notify::active", self._on_magnification_changed)
        self._hide_on_fullscreen.connect("notify::active", self._on_switch_changed)
        self._visibility_mode.connect("notify::selected", self._on_visibility_changed)

        # Advanced launcher is an independent action: NOT change detection and
        # NOT a reload trigger (qs-restart would kill the settings window).
        self._advanced_button.connect("clicked", self._on_advanced_clicked)

    def _emit_if_changed(self) -> None:
        if self._suppress_signals:
            return
        if self._has_changed():
            self.emit("settings-changed")

    def _on_value_changed(self, _row: Adw.SpinRow, _param: object) -> None:
        self._emit_if_changed()

    def _on_switch_changed(self, _row: Adw.SwitchRow, _param: object) -> None:
        self._emit_if_changed()

    def _on_dock_enabled_changed(self, _row: Adw.SwitchRow, _param: object) -> None:
        self._update_enabled_sensitivity()
        self._emit_if_changed()

    def _on_magnification_changed(self, _row: Adw.SwitchRow, _param: object) -> None:
        self._update_magnification_sensitivity()
        self._emit_if_changed()

    def _on_visibility_changed(self, _row: Adw.ComboRow, _param: object) -> None:
        self._update_delay_sensitivity()
        self._emit_if_changed()

    def _on_advanced_clicked(self, _button: Gtk.Button) -> None:
        try:
            open_advanced_settings()
        except Exception as exc:  # noqa: BLE001 — launch is best-effort
            log.warning("Could not open qs-dock advanced settings: %s", exc)

    # ------------------------------------------------------------------
    # Sensitivity rules
    # ------------------------------------------------------------------

    def _update_enabled_sensitivity(self) -> None:
        on = self._dock_enabled.get_active()
        self._appearance_group.set_sensitive(on)
        self._behaviour_group.set_sensitive(on)

    def _update_magnification_sensitivity(self) -> None:
        self._max_magnified_size.set_sensitive(self._magnification.get_active())

    def _update_delay_sensitivity(self) -> None:
        mode = _VIS_MODES[self._visibility_index()]
        sensitive = mode in _DELAY_MODES
        self._show_delay.set_sensitive(sensitive)
        self._hide_delay.set_sensitive(sensitive)

    def _apply_all_sensitivity(self) -> None:
        self._update_enabled_sensitivity()
        self._update_magnification_sensitivity()
        self._update_delay_sensitivity()

    # ------------------------------------------------------------------
    # Combo helpers
    # ------------------------------------------------------------------

    def _visibility_index(self) -> int:
        idx = self._visibility_mode.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(_VIS_MODES):
            return 0
        return idx

    @staticmethod
    def _visibility_value_to_index(value: object) -> int:
        try:
            return _VIS_MODES.index(str(value))
        except ValueError:
            return 0

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    def _current_values(self) -> dict[str, int | bool | str]:
        return {
            "dock_enabled":       self._dock_enabled.get_active(),
            "icon_size":          round(self._icon_size.get_value()),
            "spacing":            round(self._spacing.get_value()),
            "magnification":      self._magnification.get_active(),
            "max_magnified_size": round(self._max_magnified_size.get_value()),
            "visibility_mode":    _VIS_MODES[self._visibility_index()],
            "hide_on_fullscreen": self._hide_on_fullscreen.get_active(),
            "show_delay":         round(self._show_delay.get_value()),
            "hide_delay":         round(self._hide_delay.get_value()),
        }

    def _snapshot_values(self) -> dict[str, int | bool | str]:
        return self._current_values()

    def _has_changed(self) -> bool:
        if not self._loaded_values:
            return False
        return self._current_values() != self._loaded_values

    # ------------------------------------------------------------------
    # Apply widget state (shared by load + revert)
    # ------------------------------------------------------------------

    def _apply_values(self, values: dict[str, int | bool | str]) -> None:
        self._suppress_signals = True
        try:
            self._dock_enabled.set_active(bool(values["dock_enabled"]))
            self._icon_size.set_value(float(values["icon_size"]))
            self._spacing.set_value(float(values["spacing"]))
            self._magnification.set_active(bool(values["magnification"]))
            self._max_magnified_size.set_value(float(values["max_magnified_size"]))
            self._visibility_mode.set_selected(
                self._visibility_value_to_index(values["visibility_mode"])
            )
            self._hide_on_fullscreen.set_active(bool(values["hide_on_fullscreen"]))
            self._show_delay.set_value(float(values["show_delay"]))
            self._hide_delay.set_value(float(values["hide_delay"]))
        finally:
            self._suppress_signals = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_saved(self) -> None:
        """Record current widget values as the last-saved state (for revert)."""
        self._loaded_values = self._current_values()

    def revert_to_loaded(self) -> None:
        """Reset widgets to the last-saved state."""
        vals = self._loaded_values
        if not vals:
            return
        self._apply_values(vals)
        # Sensitivity reflects the reverted values; it must not emit a change.
        self._apply_all_sensitivity()

    def load(self, config_path: Path | None, hyprctl_available: bool) -> None:
        """Populate widgets from qs-dock's settings.json (or defaults).

        The ``config_path`` / ``hyprctl_available`` arguments are accepted to
        match the common page-load contract but are unused here — these options
        are not Hyprland keys, so no hyprctl query is performed.
        """
        values: dict[str, int | bool | str] = dict(DEFAULTS)
        try:
            values.update(read_dock_settings())
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read qs-dock settings: %s", exc)

        self._apply_values(values)
        # Snapshot BEFORE applying sensitivity so change detection is clean.
        self._loaded_values = self._current_values()
        self._apply_all_sensitivity()

    def get_settings(self) -> dict[str, int | bool | str]:
        """Return the current flat settings dict (used by the qs-dock save path)."""
        return self._current_values()

    def get_enabled(self) -> bool:
        """Return the master switch state."""
        return self._dock_enabled.get_active()

    def collect_lines(self) -> list[str]:
        """Return no Hyprland lines — this page has no Hyprland config block."""
        return []
