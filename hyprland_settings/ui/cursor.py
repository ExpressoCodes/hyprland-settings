"""Cursor settings page.

Implements CursorPage — an Adw.PreferencesPage subclass that manages
cursor theme and size settings for Hyprland via XCURSOR_* and HYPRCURSOR_*
environment variables written to cursor.lua.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, str | int] = {
    "xcursor_theme": "default",
    "xcursor_size": 24,
}

_CURSOR_THEME_DIRS: list[Path] = [
    Path("/usr/share/icons"),
    Path("~/.local/share/icons").expanduser(),
    Path("~/.icons").expanduser(),
]

# (theme_name, style_description, aur_install_command)
_POPULAR_THEMES: list[tuple[str, str, str]] = [
    ("Adwaita",            "Built-in GNOME default — no install needed",      ""),
    ("Bibata-Modern-Ice",  "macOS-style, clean and sharp",                    "paru -S bibata-cursor-theme"),
    ("macOS-Monterey",     "macOS Monterey style",                            "paru -S macos-cursor"),
    ("BreezeX-RosePine",   "Aesthetic rose pine colour scheme",               "paru -S breeze-x-cursor"),
    ("Phinger-Cursors",    "Flat and minimal",                                "paru -S phinger-cursors"),
    ("Windows-10-Edge",    "Windows 10 Edge style",                           "paru -S windows-10-edge-cursor-theme-bin"),
]


# ---------------------------------------------------------------------------
# Discovery helper
# ---------------------------------------------------------------------------


def discover_cursor_themes() -> list[str]:
    """Return sorted cursor theme names found in the standard icon directories.

    A directory is considered a cursor theme when it contains a ``cursors/``
    subdirectory.  "default" is always present as the first entry.
    """
    seen: set[str] = set()

    for base_dir in _CURSOR_THEME_DIRS:
        if not base_dir.exists():
            continue
        try:
            for entry in sorted(base_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if (entry / "cursors").is_dir() and entry.name not in seen:
                    seen.add(entry.name)
        except OSError as exc:
            log.warning("Could not scan cursor theme directory %s: %s", base_dir, exc)

    themes = sorted(seen - {"default"})
    themes.insert(0, "default")
    return themes


# ---------------------------------------------------------------------------
# CursorPage
# ---------------------------------------------------------------------------


class CursorPage(Adw.PreferencesPage):
    """Cursor settings page for Hyprland.

    Signals
    -------
    settings-changed
        Emitted when any row value changes.

    Public API
    ----------
    load(config_path, hyprctl_available)
        Populate widgets from cursor.lua, env vars, or defaults.

    collect_lines() -> list[str]
        Return Lua lines for cursor.lua.

    apply_live()
        Cursor changes require logout; this method logs that fact.
    """

    __gtype_name__ = "CursorPage"

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
        self._loaded_snapshot: dict | None = None

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_theme_group()
        self._build_size_group()
        self._build_popular_group()

    def _build_theme_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Theme")
        group.set_description(
            "Sets both XCURSOR_THEME (X11/XWayland) and HYPRCURSOR_THEME (Wayland native). "
            "Type a theme name or click a popular theme below to fill this field."
        )
        self.add(group)

        self._theme_names = discover_cursor_themes()

        self._theme_row = Adw.EntryRow()
        self._theme_row.set_title("Cursor Theme")
        self._theme_row.set_text("default")
        group.add(self._theme_row)

        self._theme_row.connect("notify::text", self._on_changed)

    def _build_size_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Size")
        self.add(group)

        adj = Gtk.Adjustment(
            value=24,
            lower=12,
            upper=96,
            step_increment=4,
            page_increment=16,
            page_size=0.0,
        )
        self._size_row = Adw.SpinRow()
        self._size_row.set_title("Cursor Size")
        self._size_row.set_subtitle("Sets both XCURSOR_SIZE and HYPRCURSOR_SIZE")
        self._size_row.set_adjustment(adj)
        self._size_row.set_digits(0)
        self._size_row.set_snap_to_ticks(False)
        group.add(self._size_row)

        self._size_row.connect("notify::value", self._on_changed)

    def _build_popular_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Popular Themes")
        group.set_description(
            "These themes must be installed separately. "
            "Activate a row to copy the AUR install command to the clipboard."
        )
        self.add(group)

        for name, description, cmd in _POPULAR_THEMES:
            row = Adw.ActionRow()
            row.set_title(name)
            row.set_subtitle(description)
            row.set_activatable(True)
            if cmd:
                icon = Gtk.Image.new_from_icon_name("edit-copy-symbolic")
                row.add_suffix(icon)
            row.connect("activated", self._on_popular_row_activated, name, cmd)
            group.add(row)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_changed(self, _widget: GObject.Object, _param: GObject.ParamSpec) -> None:
        if self._suppress_signals:
            return
        self.emit("settings-changed")

    def _on_popular_row_activated(self, _row: Adw.ActionRow, name: str, cmd: str) -> None:
        self._suppress_signals = True
        try:
            self._theme_row.set_text(name)
        finally:
            self._suppress_signals = False
        self.emit("settings-changed")
        if cmd:
            clipboard = self.get_clipboard()
            clipboard.set(cmd)
            log.info("Set theme to %s and copied install command: %s", name, cmd)

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    def _snapshot_values(self) -> dict:
        return {
            "theme": self._theme_row.get_text().strip() or "default",
            "size": round(self._size_row.get_value()),
        }

    def _apply_values(self, theme: str, size: int) -> None:
        self._theme_row.set_text(theme)
        self._size_row.set_value(float(size))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_saved(self) -> None:
        """Record current widget values as the last-saved state (for revert)."""
        self._loaded_snapshot = self._snapshot_values()

    def revert_to_loaded(self) -> None:
        """Reset widgets to last-saved state."""
        snap = self._loaded_snapshot
        if snap is None:
            return
        self._suppress_signals = True
        try:
            self._apply_values(snap["theme"], snap["size"])
        finally:
            self._suppress_signals = False

    def load(self, config_path: Path, hyprctl_available: bool) -> None:
        """Populate widgets from cursor.lua, falling back to env vars then defaults.

        Parameters
        ----------
        config_path:
            Path to the main Hyprland config file (not used directly here;
            cursor.lua is looked up as a sibling).
        hyprctl_available:
            Reserved for future use; cursor values are not exposed via
            hyprctl getoption and are read from cursor.lua instead.
        """
        self._hyprctl_available = hyprctl_available

        theme = str(DEFAULTS["xcursor_theme"])
        size = int(DEFAULTS["xcursor_size"])

        # Prefer cursor.lua
        cursor_lua = (
            Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
            / "hypr"
            / "cursor.lua"
        )
        if cursor_lua.exists():
            parsed = self._parse_cursor_lua(cursor_lua)
            theme = parsed.get("XCURSOR_THEME", theme)
            try:
                size = int(parsed.get("XCURSOR_SIZE", size))
            except (ValueError, TypeError):
                pass
        else:
            # Fall back to process env vars
            env_theme = os.environ.get("XCURSOR_THEME", "")
            env_size = os.environ.get("XCURSOR_SIZE", "")
            if env_theme:
                theme = env_theme
            if env_size:
                try:
                    size = int(env_size)
                except ValueError:
                    pass

        self._suppress_signals = True
        try:
            self._apply_values(theme, size)
        finally:
            self._suppress_signals = False

        self._loaded_snapshot = self._snapshot_values()

    def collect_lines(self) -> list[str]:
        """Return Lua lines for cursor.lua representing current widget state."""
        theme = self._theme_row.get_text().strip() or "default"
        size = round(self._size_row.get_value())
        return [
            "-- Managed by hyprland-settings (or edit directly)",
            f'hl.env("XCURSOR_THEME", "{theme}")',
            f'hl.env("XCURSOR_SIZE", "{size}")',
            f'hl.env("HYPRCURSOR_THEME", "{theme}")',
            f'hl.env("HYPRCURSOR_SIZE", "{size}")',
        ]

    def apply_live(self) -> None:
        """Cursor theme/size changes take effect after log out on Wayland.

        The config has already been written by the time this is called.
        Nothing is pushed via hyprctl since XCURSOR_* are env-var-level
        settings that require a new login session to be picked up.
        """
        log.info(
            "Cursor settings written. Changes will take effect after logging out."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_cursor_lua(path: Path) -> dict[str, str]:
        """Parse hl.env("KEY", "VALUE") pairs from a Lua file."""
        result: dict[str, str] = {}
        pattern = re.compile(r'hl\.env\s*\(\s*"([^"]+)"\s*,\s*"([^"]*)"\s*\)')
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Could not read cursor.lua: %s", exc)
            return result
        for m in pattern.finditer(text):
            result[m.group(1)] = m.group(2)
        return result
