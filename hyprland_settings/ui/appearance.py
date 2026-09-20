"""Appearance (Look & Feel) settings page.

Implements AppearancePage — an Adw.PreferencesPage subclass that manages
window gaps, borders, opacity, shadow, and blur settings for Hyprland.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk

from hyprland_settings.backend.config_writer import (
    read_section_from_config,
    write_section_to_config,
)
from hyprland_settings.backend.hyprctl import HyprctlApplyError, apply_keyword, get_option

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, int | float | bool] = {
    "gaps_in": 3,
    "gaps_out": 7,
    "border_size": 1,
    "rounding": 10,
    "active_opacity": 1.0,
    "inactive_opacity": 1.0,
    "shadow_enabled": True,
    "shadow_range": 4,
    "blur_enabled": True,
    "blur_size": 3,
    "blur_passes": 1,
}


# ---------------------------------------------------------------------------
# AppearancePage
# ---------------------------------------------------------------------------


class AppearancePage(Adw.PreferencesPage):
    """Appearance settings page for Hyprland.

    Signals
    -------
    settings-changed
        Emitted when any row value changes from the state that was loaded.

    Public API
    ----------
    load(config_path, hyprctl_available)
        Populate widgets from live hyprctl values (or defaults).

    collect_lines() -> list[str]
        Return Lua lines representing the current widget state.

    apply_live()
        Push current values to a running Hyprland session via hyprctl keyword.
    """

    __gtype_name__ = "AppearancePage"

    __gsignals__ = {
        "settings-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()

        self._suppress_signals: bool = False
        self._hyprctl_available: bool = False
        self._loaded_values: dict[str, int | float | bool] = {}

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_gaps_group()
        self._build_borders_group()
        self._build_opacity_group()
        self._build_shadow_group()
        self._build_blur_group()

    def _build_gaps_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Window Gaps")
        self.add(group)

        self._gaps_in = self._make_spin_row(
            title="Inner Gaps",
            lower=0,
            upper=100,
            step=1,
        )
        group.add(self._gaps_in)

        self._gaps_out = self._make_spin_row(
            title="Outer Gaps",
            lower=0,
            upper=100,
            step=1,
        )
        group.add(self._gaps_out)

    def _build_borders_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Borders")
        self.add(group)

        self._border_size = self._make_spin_row(
            title="Border Size",
            lower=0,
            upper=20,
            step=1,
        )
        group.add(self._border_size)

        self._rounding = self._make_spin_row(
            title="Corner Rounding",
            lower=0,
            upper=30,
            step=1,
        )
        group.add(self._rounding)

    def _build_opacity_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Opacity")
        self.add(group)

        self._active_opacity = self._make_spin_row(
            title="Active Window",
            lower=0.0,
            upper=1.0,
            step=0.05,
            digits=2,
        )
        group.add(self._active_opacity)

        self._inactive_opacity = self._make_spin_row(
            title="Inactive Windows",
            lower=0.0,
            upper=1.0,
            step=0.05,
            digits=2,
        )
        group.add(self._inactive_opacity)

    def _build_shadow_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Shadow")
        self.add(group)

        self._shadow_enabled = Adw.SwitchRow()
        self._shadow_enabled.set_title("Enable Shadow")
        group.add(self._shadow_enabled)

        self._shadow_range = self._make_spin_row(
            title="Shadow Range",
            lower=0,
            upper=50,
            step=1,
        )
        group.add(self._shadow_range)

    def _build_blur_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Blur")
        self.add(group)

        self._blur_enabled = Adw.SwitchRow()
        self._blur_enabled.set_title("Enable Blur")
        group.add(self._blur_enabled)

        self._blur_size = self._make_spin_row(
            title="Blur Size",
            lower=1,
            upper=20,
            step=1,
        )
        group.add(self._blur_size)

        self._blur_passes = self._make_spin_row(
            title="Blur Passes",
            lower=1,
            upper=10,
            step=1,
        )
        group.add(self._blur_passes)

    @staticmethod
    def _make_spin_row(
        title: str,
        lower: float,
        upper: float,
        step: float,
        digits: int = 0,
    ) -> Adw.SpinRow:
        adj = Gtk.Adjustment(
            value=lower,
            lower=lower,
            upper=upper,
            step_increment=step,
            page_increment=step * 10,
            page_size=0.0,
        )
        row = Adw.SpinRow()
        row.set_title(title)
        row.set_adjustment(adj)
        row.set_digits(digits)
        row.set_snap_to_ticks(False)
        return row

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        spin_rows = [
            self._gaps_in,
            self._gaps_out,
            self._border_size,
            self._rounding,
            self._active_opacity,
            self._inactive_opacity,
            self._shadow_range,
            self._blur_size,
            self._blur_passes,
        ]
        for row in spin_rows:
            row.connect("notify::value", self._on_value_changed)

        self._shadow_enabled.connect("notify::active", self._on_switch_changed)
        self._blur_enabled.connect("notify::active", self._on_switch_changed)

    def _on_value_changed(self, row: Adw.SpinRow, _param: object) -> None:
        if self._suppress_signals:
            return
        if self._hyprctl_available:
            self.apply_live()
        if self._has_changed():
            self.emit("settings-changed")

    def _on_switch_changed(self, row: Adw.SwitchRow, _param: object) -> None:
        if self._suppress_signals:
            return
        if self._hyprctl_available:
            self.apply_live()
        if self._has_changed():
            self.emit("settings-changed")

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    def _current_values(self) -> dict[str, int | float | bool]:
        return {
            "gaps_in": round(self._gaps_in.get_value()),
            "gaps_out": round(self._gaps_out.get_value()),
            "border_size": round(self._border_size.get_value()),
            "rounding": round(self._rounding.get_value()),
            "active_opacity": self._active_opacity.get_value(),
            "inactive_opacity": self._inactive_opacity.get_value(),
            "shadow_enabled": self._shadow_enabled.get_active(),
            "shadow_range": round(self._shadow_range.get_value()),
            "blur_enabled": self._blur_enabled.get_active(),
            "blur_size": round(self._blur_size.get_value()),
            "blur_passes": round(self._blur_passes.get_value()),
        }

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
        """Reset widgets to last-saved state and re-apply to Hyprland."""
        vals = self._loaded_values
        if not vals:
            return
        self._suppress_signals = True
        try:
            self._gaps_in.set_value(float(vals["gaps_in"]))
            self._gaps_out.set_value(float(vals["gaps_out"]))
            self._border_size.set_value(float(vals["border_size"]))
            self._rounding.set_value(float(vals["rounding"]))
            self._active_opacity.set_value(float(vals["active_opacity"]))
            self._inactive_opacity.set_value(float(vals["inactive_opacity"]))
            self._shadow_enabled.set_active(bool(vals["shadow_enabled"]))
            self._shadow_range.set_value(float(vals["shadow_range"]))
            self._blur_enabled.set_active(bool(vals["blur_enabled"]))
            self._blur_size.set_value(float(vals["blur_size"]))
            self._blur_passes.set_value(float(vals["blur_passes"]))
        finally:
            self._suppress_signals = False
        if self._hyprctl_available:
            self.apply_live()

    def load(self, config_path: Path, hyprctl_available: bool) -> None:
        """Populate widgets from live hyprctl values or fall back to defaults.

        Parameters
        ----------
        config_path:
            Path to the Hyprland Lua config file (not parsed here; reserved
            for future use or the config_writer layer).
        hyprctl_available:
            When False, skip hyprctl entirely and use DEFAULTS.
        """
        self._hyprctl_available = hyprctl_available
        values: dict[str, int | float | bool] = dict(DEFAULTS)

        if hyprctl_available:
            _queries: list[tuple[str, str, str]] = [
                # (key_in_values, hyprctl_option_path, json_type_key)
                ("gaps_in",          "general:gaps_in",               "int"),
                ("gaps_out",         "general:gaps_out",              "int"),
                ("border_size",      "general:border_size",           "int"),
                ("rounding",         "decoration:rounding",           "int"),
                ("active_opacity",   "decoration:active_opacity",     "float"),
                ("inactive_opacity", "decoration:inactive_opacity",   "float"),
                ("shadow_enabled",   "decoration:shadow:enabled",     "int"),
                ("shadow_range",     "decoration:shadow:range",       "int"),
                ("blur_enabled",     "decoration:blur:enabled",       "int"),
                ("blur_size",        "decoration:blur:size",          "int"),
                ("blur_passes",      "decoration:blur:passes",        "int"),
            ]
            for key, option_path, type_key in _queries:
                result = get_option(option_path)
                if not result:
                    continue
                raw = result.get(type_key)
                if raw is None:
                    continue
                if key in ("shadow_enabled", "blur_enabled"):
                    values[key] = bool(raw)
                elif type_key == "float":
                    values[key] = float(raw)
                else:
                    values[key] = int(raw)

        self._suppress_signals = True
        try:
            self._gaps_in.set_value(float(values["gaps_in"]))
            self._gaps_out.set_value(float(values["gaps_out"]))
            self._border_size.set_value(float(values["border_size"]))
            self._rounding.set_value(float(values["rounding"]))
            self._active_opacity.set_value(float(values["active_opacity"]))
            self._inactive_opacity.set_value(float(values["inactive_opacity"]))
            self._shadow_enabled.set_active(bool(values["shadow_enabled"]))
            self._shadow_range.set_value(float(values["shadow_range"]))
            self._blur_enabled.set_active(bool(values["blur_enabled"]))
            self._blur_size.set_value(float(values["blur_size"]))
            self._blur_passes.set_value(float(values["blur_passes"]))
        finally:
            self._suppress_signals = False

        self._loaded_values = self._current_values()

    def collect_lines(self) -> list[str]:
        """Return Lua lines representing the current widget state.

        The returned lines form a complete ``hl.config({...})`` call that can
        be written to the config file via write_section_to_config.
        """
        gaps_in       = round(self._gaps_in.get_value())
        gaps_out      = round(self._gaps_out.get_value())
        border_size   = round(self._border_size.get_value())
        rounding      = round(self._rounding.get_value())
        active_op     = self._active_opacity.get_value()
        inactive_op   = self._inactive_opacity.get_value()
        shadow_en     = "true" if self._shadow_enabled.get_active() else "false"
        shadow_range  = round(self._shadow_range.get_value())
        blur_en       = "true" if self._blur_enabled.get_active() else "false"
        blur_size     = round(self._blur_size.get_value())
        blur_passes   = round(self._blur_passes.get_value())

        return [
            "hl.config({",
            "    general = {",
            f"        gaps_in = {gaps_in},",
            f"        gaps_out = {gaps_out},",
            f"        border_size = {border_size},",
            "    },",
            "    decoration = {",
            f"        rounding = {rounding},",
            f"        active_opacity = {active_op:.2f},",
            f"        inactive_opacity = {inactive_op:.2f},",
            "        shadow = {",
            f"            enabled = {shadow_en},",
            f"            range = {shadow_range},",
            "        },",
            "        blur = {",
            f"            enabled = {blur_en},",
            f"            size = {blur_size},",
            f"            passes = {blur_passes},",
            "        },",
            "    },",
            "})",
        ]

    def apply_live(self) -> None:
        """Push current widget values to a running Hyprland session.

        Calls ``hyprctl keyword`` for each setting.  Logs a warning for each
        setting that fails rather than aborting the whole batch.
        """
        shadow_val = "true" if self._shadow_enabled.get_active() else "false"
        blur_val   = "true" if self._blur_enabled.get_active() else "false"

        keywords: list[tuple[str, str]] = [
            ("general:gaps_in",              str(round(self._gaps_in.get_value()))),
            ("general:gaps_out",             str(round(self._gaps_out.get_value()))),
            ("general:border_size",          str(round(self._border_size.get_value()))),
            ("decoration:rounding",          str(round(self._rounding.get_value()))),
            ("decoration:active_opacity",    f"{self._active_opacity.get_value():.2f}"),
            ("decoration:inactive_opacity",  f"{self._inactive_opacity.get_value():.2f}"),
            ("decoration:shadow:enabled",    shadow_val),
            ("decoration:shadow:range",      str(round(self._shadow_range.get_value()))),
            ("decoration:blur:enabled",      blur_val),
            ("decoration:blur:size",         str(round(self._blur_size.get_value()))),
            ("decoration:blur:passes",       str(round(self._blur_passes.get_value()))),
        ]

        for key, value in keywords:
            try:
                apply_keyword(key, value)
            except HyprctlApplyError:
                log.warning("Failed to apply %s = %s", key, value, exc_info=True)
