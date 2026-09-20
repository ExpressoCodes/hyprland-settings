"""Input settings page.

Implements InputPage — an Adw.PreferencesPage subclass for editing Hyprland
input settings (keyboard, mouse, touchpad).  It reads live values via hyprctl
and persists them through config_writer.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402

from hyprland_settings.backend.config_writer import (  # noqa: E402
    read_section_from_config,
    write_section_to_config,
)
from hyprland_settings.backend.hyprctl import (  # noqa: E402
    HyprctlApplyError,
    apply_keyword,
    get_option,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    "kb_layout": "us",
    "kb_variant": "",
    "kb_options": "",
    "sensitivity": 0.0,
    "follow_mouse": 1,
    "natural_scroll": True,
}

# Maps ComboRow selected index → follow_mouse int value
_FOLLOW_MOUSE_OPTIONS = ["Disabled (0)", "Full (1)", "Always (2)", "Window (3)"]


# ---------------------------------------------------------------------------
# InputPage
# ---------------------------------------------------------------------------


class InputPage(Adw.PreferencesPage):
    """Preferences page for Hyprland input settings.

    Signals
    -------
    settings-changed()
        Emitted when any widget value changes from the loaded state.

    Public API
    ----------
    load(config_path, hyprctl_available)
        Populate widgets from live hyprctl values (preferred) or defaults.

    collect_lines() -> list[str]
        Return Lua config lines for the input section.

    apply_live()
        Push current widget values to Hyprland at runtime via hyprctl keyword.
    """

    __gtype_name__ = "InputPage"

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

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_keyboard_group()
        self._build_mouse_group()
        self._build_touchpad_group()

    def _build_keyboard_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Keyboard")
        self.add(group)

        self._layout_row = Adw.EntryRow()
        self._layout_row.set_title("Layout")
        self._layout_row.set_show_apply_button(False)
        group.add(self._layout_row)

        self._variant_row = Adw.EntryRow()
        self._variant_row.set_title("Variant")
        self._variant_row.set_show_apply_button(False)
        group.add(self._variant_row)

        self._options_row = Adw.EntryRow()
        self._options_row.set_title("Options")
        self._options_row.set_show_apply_button(False)
        group.add(self._options_row)

    def _build_mouse_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Mouse")
        self.add(group)

        self._sens_adj = Gtk.Adjustment(
            value=DEFAULTS["sensitivity"],
            lower=-1.0,
            upper=1.0,
            step_increment=0.05,
            page_increment=0.1,
            page_size=0.0,
        )
        self._sens_row = Adw.SpinRow()
        self._sens_row.set_title("Sensitivity")
        self._sens_row.set_subtitle("Pointer acceleration (-1 = min, 0 = default, 1 = max)")
        self._sens_row.set_adjustment(self._sens_adj)
        self._sens_row.set_digits(2)
        group.add(self._sens_row)

        self._follow_model = Gtk.StringList.new(_FOLLOW_MOUSE_OPTIONS)
        self._follow_row = Adw.ComboRow()
        self._follow_row.set_title("Follow Mouse Focus")
        self._follow_row.set_model(self._follow_model)
        group.add(self._follow_row)

    def _build_touchpad_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Touchpad")
        self.add(group)

        self._natural_scroll_row = Adw.SwitchRow()
        self._natural_scroll_row.set_title("Natural Scrolling")
        self._natural_scroll_row.set_subtitle("Reverse scroll direction (touchpad)")
        group.add(self._natural_scroll_row)

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        # Keyboard: dirty-flag only (applying mid-type would send partial layout)
        for row in (self._layout_row, self._variant_row, self._options_row):
            row.connect("notify::text", self._on_changed_dirty)

        # Mouse / touchpad: apply the individual setting live on each change
        self._sens_row.connect("notify::value", self._on_sens_changed)
        self._follow_row.connect("notify::selected", self._on_follow_changed)
        self._natural_scroll_row.connect("notify::active", self._on_natural_scroll_changed)

    def _on_changed_dirty(self, _widget: GObject.Object, _param: GObject.ParamSpec) -> None:
        if self._suppress_signals:
            return
        self.emit("settings-changed")

    def _on_sens_changed(self, _widget: GObject.Object, _param: GObject.ParamSpec) -> None:
        if self._suppress_signals:
            return
        if self._hyprctl_available:
            try:
                apply_keyword("input:sensitivity", f"{self._sens_adj.get_value():.2f}")
            except HyprctlApplyError:
                log.warning("Failed to apply sensitivity live", exc_info=True)
        self.emit("settings-changed")

    def _on_follow_changed(self, _widget: GObject.Object, _param: GObject.ParamSpec) -> None:
        if self._suppress_signals:
            return
        if self._hyprctl_available:
            try:
                apply_keyword("input:follow_mouse", str(int(self._follow_row.get_selected())))
            except HyprctlApplyError:
                log.warning("Failed to apply follow_mouse live", exc_info=True)
        self.emit("settings-changed")

    def _on_natural_scroll_changed(self, _widget: GObject.Object, _param: GObject.ParamSpec) -> None:
        if self._suppress_signals:
            return
        if self._hyprctl_available:
            try:
                apply_keyword(
                    "input:touchpad:natural_scroll",
                    str(self._natural_scroll_row.get_active()).lower(),
                )
            except HyprctlApplyError:
                log.warning("Failed to apply natural_scroll live", exc_info=True)
        self.emit("settings-changed")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _snapshot_values(self) -> dict:
        return {
            "kb_layout":      self._layout_row.get_text(),
            "kb_variant":     self._variant_row.get_text(),
            "kb_options":     self._options_row.get_text(),
            "sensitivity":    self._sens_adj.get_value(),
            "follow_mouse":   int(self._follow_row.get_selected()),
            "natural_scroll": self._natural_scroll_row.get_active(),
        }

    def mark_saved(self) -> None:
        """Record current widget values as the last-saved state (for revert)."""
        self._loaded_snapshot = self._snapshot_values()

    def revert_to_loaded(self) -> None:
        """Reset widgets to last-saved state and re-apply mouse/touchpad settings live."""
        snap = getattr(self, "_loaded_snapshot", None)
        if snap is None:
            return
        self._suppress_signals = True
        try:
            self._apply_values(
                kb_layout=snap["kb_layout"],
                kb_variant=snap["kb_variant"],
                kb_options=snap["kb_options"],
                sensitivity=snap["sensitivity"],
                follow_mouse=snap["follow_mouse"],
                natural_scroll=snap["natural_scroll"],
            )
        finally:
            self._suppress_signals = False
        if self._hyprctl_available:
            self.apply_live()

    def load(self, config_path: Path, hyprctl_available: bool) -> None:
        """Populate widgets from live hyprctl values or defaults."""
        self._hyprctl_available = hyprctl_available

        self._suppress_signals = True
        try:
            if hyprctl_available:
                self._load_from_hyprctl()
            else:
                self._load_defaults()
        finally:
            self._suppress_signals = False
        self._loaded_snapshot = self._snapshot_values()

    def _load_from_hyprctl(self) -> None:
        """Fetch values from hyprctl getoption and populate widgets."""
        # Keyboard
        kb_layout = self._str_option("input:kb_layout", DEFAULTS["kb_layout"])
        kb_variant = self._str_option("input:kb_variant", DEFAULTS["kb_variant"])
        kb_options = self._str_option("input:kb_options", DEFAULTS["kb_options"])

        # Mouse
        sensitivity = self._float_option("input:sensitivity", DEFAULTS["sensitivity"])
        follow_mouse = self._int_option("input:follow_mouse", DEFAULTS["follow_mouse"])

        # Touchpad
        natural_scroll = self._int_option(
            "input:touchpad:natural_scroll", int(DEFAULTS["natural_scroll"])
        )

        self._apply_values(
            kb_layout=kb_layout,
            kb_variant=kb_variant,
            kb_options=kb_options,
            sensitivity=sensitivity,
            follow_mouse=follow_mouse,
            natural_scroll=bool(natural_scroll),
        )

    def _load_defaults(self) -> None:
        self._apply_values(
            kb_layout=DEFAULTS["kb_layout"],
            kb_variant=DEFAULTS["kb_variant"],
            kb_options=DEFAULTS["kb_options"],
            sensitivity=float(DEFAULTS["sensitivity"]),
            follow_mouse=int(DEFAULTS["follow_mouse"]),
            natural_scroll=bool(DEFAULTS["natural_scroll"]),
        )

    def _apply_values(
        self,
        *,
        kb_layout: str,
        kb_variant: str,
        kb_options: str,
        sensitivity: float,
        follow_mouse: int,
        natural_scroll: bool,
    ) -> None:
        self._layout_row.set_text(kb_layout)
        self._variant_row.set_text(kb_variant)
        self._options_row.set_text(kb_options)
        self._sens_adj.set_value(sensitivity)
        # Clamp follow_mouse to valid combo index
        idx = max(0, min(follow_mouse, len(_FOLLOW_MOUSE_OPTIONS) - 1))
        self._follow_row.set_selected(idx)
        self._natural_scroll_row.set_active(natural_scroll)

    def collect_lines(self) -> list[str]:
        """Return Lua config lines for the input section."""
        kb_layout = self._layout_row.get_text()
        kb_variant = self._variant_row.get_text()
        kb_options = self._options_row.get_text()
        sensitivity = self._sens_adj.get_value()
        follow_mouse = int(self._follow_row.get_selected())
        natural_scroll = self._natural_scroll_row.get_active()

        return [
            "hl.config({",
            "    input = {",
            f'        kb_layout = "{kb_layout}",',
            f'        kb_variant = "{kb_variant}",',
            f'        kb_options = "{kb_options}",',
            f"        sensitivity = {sensitivity:.2f},",
            f"        follow_mouse = {follow_mouse},",
            "        touchpad = {",
            f"            natural_scroll = {str(natural_scroll).lower()},",
            "        },",
            "    },",
            "})",
        ]

    def apply_live(self) -> None:
        """Push current widget values to Hyprland via hyprctl keyword."""
        kb_layout = self._layout_row.get_text()
        kb_variant = self._variant_row.get_text()
        kb_options = self._options_row.get_text()
        sensitivity = self._sens_adj.get_value()
        follow_mouse = int(self._follow_row.get_selected())
        natural_scroll = self._natural_scroll_row.get_active()

        pairs = [
            ("input:kb_layout", kb_layout),
            ("input:kb_variant", kb_variant),
            ("input:kb_options", kb_options),
            ("input:sensitivity", f"{sensitivity:.2f}"),
            ("input:follow_mouse", str(follow_mouse)),
            ("input:touchpad:natural_scroll", str(natural_scroll).lower()),
        ]

        for key, value in pairs:
            try:
                apply_keyword(key, value)
            except HyprctlApplyError:
                log.warning("Failed to apply %s = %r", key, value, exc_info=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _str_option(path: str, default: str) -> str:
        data = get_option(path)
        return data.get("str", default) if data else default

    @staticmethod
    def _float_option(path: str, default: float) -> float:
        data = get_option(path)
        return float(data.get("float", default)) if data else default

    @staticmethod
    def _int_option(path: str, default: int) -> int:
        data = get_option(path)
        return int(data.get("int", default)) if data else default
