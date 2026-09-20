"""Cursor settings page.

Implements CursorPage — an Adw.PreferencesPage subclass that manages
cursor theme and size settings for Hyprland via XCURSOR_* and HYPRCURSOR_*
environment variables written to cursor.lua.

Popular themes are downloaded directly from GitHub releases and extracted
to ~/.local/share/icons/ with a live progress indicator.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk

from hyprland_settings.backend.hyprctl import HyprctlUnavailableError

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

_INSTALL_DIR = Path("~/.local/share/icons").expanduser()


@dataclass
class ThemeInfo:
    display_name: str       # shown in the UI row
    description: str
    theme_name: str         # value written to XCURSOR_THEME / set as entry text
    download_url: str       # empty string = built-in, no download needed
    extracted_dir: str      # folder name that appears under icons/ after extraction


_POPULAR_THEMES: list[ThemeInfo] = [
    ThemeInfo(
        "Adwaita", "Built-in GNOME default — already available",
        "Adwaita", "", "Adwaita",
    ),
    ThemeInfo(
        "Bibata-Modern-Ice", "macOS-style, clean and sharp",
        "Bibata-Modern-Ice",
        "https://github.com/ful1e5/Bibata_Cursor/releases/latest/download/Bibata-Modern-Ice.tar.xz",
        "Bibata-Modern-Ice",
    ),
    ThemeInfo(
        "macOS", "Apple macOS style",
        "macOS",
        "https://github.com/ful1e5/apple_cursor/releases/latest/download/macOS.tar.xz",
        "macOS",
    ),
    ThemeInfo(
        "BreezeX-RosePine", "Aesthetic rose pine colour scheme",
        "BreezeX-RoséPine",
        "https://github.com/rose-pine/cursors/releases/latest/download/BreezeX-RosePine-Linux.tar.xz",
        "BreezeX-RoséPine",
    ),
    ThemeInfo(
        "Phinger Cursors", "Flat and minimal (light variant)",
        "phinger-cursors-light",
        "https://github.com/phisch/phinger-cursors/releases/latest/download/phinger-cursors-variants.tar.bz2",
        "phinger-cursors-light",
    ),
    ThemeInfo(
        "Bibata-Modern-Amber", "Warm amber tinted cursors",
        "Bibata-Modern-Amber",
        "https://github.com/ful1e5/Bibata_Cursor/releases/latest/download/Bibata-Modern-Amber.tar.xz",
        "Bibata-Modern-Amber",
    ),
]


# ---------------------------------------------------------------------------
# Discovery helper
# ---------------------------------------------------------------------------


def discover_cursor_themes() -> list[str]:
    """Return sorted cursor theme names found in the standard icon directories."""
    seen: set[str] = set()
    for base_dir in _CURSOR_THEME_DIRS:
        if not base_dir.exists():
            continue
        try:
            for entry in sorted(base_dir.iterdir()):
                if entry.is_dir() and (entry / "cursors").is_dir():
                    seen.add(entry.name)
        except OSError as exc:
            log.warning("Could not scan cursor theme directory %s: %s", base_dir, exc)
    themes = sorted(seen - {"default"})
    themes.insert(0, "default")
    return themes


def _is_installed(theme: ThemeInfo) -> bool:
    """Return True if the theme's extracted directory already exists."""
    if not theme.download_url:
        return True  # built-in
    return (_INSTALL_DIR / theme.extracted_dir).is_dir()


# ---------------------------------------------------------------------------
# Per-row download widget
# ---------------------------------------------------------------------------


class _ThemeRow(Adw.ActionRow):
    """ActionRow with a built-in download + progress suffix stack."""

    def __init__(self, theme: ThemeInfo, on_installed: Callable[[str], None]) -> None:
        super().__init__()
        self._theme = theme
        self._on_installed = on_installed

        self.set_title(theme.display_name)
        self.set_subtitle(theme.description)
        self.set_activatable(True)
        self.connect("activated", self._on_activate)

        self._build_suffix()
        self._refresh_state()

    # ------------------------------------------------------------------
    # Suffix: a Gtk.Stack with three pages
    # ------------------------------------------------------------------

    def _build_suffix(self) -> None:
        self._suffix_stack = Gtk.Stack()
        self._suffix_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._suffix_stack.set_transition_duration(150)
        self._suffix_stack.set_valign(Gtk.Align.CENTER)

        # Page: "use" — just a forward arrow / select indicator (built-in or installed)
        use_icon = Gtk.Image.new_from_icon_name("object-select-symbolic")
        use_icon.add_css_class("success")
        self._suffix_stack.add_named(use_icon, "installed")

        # Page: "download" button
        dl_btn = Gtk.Button()
        dl_btn.set_icon_name("folder-download-symbolic")
        dl_btn.set_tooltip_text("Download and install")
        dl_btn.add_css_class("flat")
        dl_btn.connect("clicked", self._on_download_clicked)
        self._dl_btn = dl_btn
        self._suffix_stack.add_named(dl_btn, "download")

        # Page: progress (spinner + label)
        progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        progress_box.set_valign(Gtk.Align.CENTER)

        self._progress_label = Gtk.Label(label="0%")
        self._progress_label.add_css_class("dim-label")
        self._progress_label.set_width_chars(4)

        spinner = Gtk.Spinner()
        spinner.set_spinning(True)
        self._spinner = spinner

        progress_box.append(self._progress_label)
        progress_box.append(spinner)
        self._suffix_stack.add_named(progress_box, "progress")

        self.add_suffix(self._suffix_stack)

    def _refresh_state(self) -> None:
        if _is_installed(self._theme):
            self._suffix_stack.set_visible_child_name("installed")
        else:
            self._suffix_stack.set_visible_child_name("download")

    # ------------------------------------------------------------------
    # Activation: always sets the theme entry
    # ------------------------------------------------------------------

    def _on_activate(self, _row: Adw.ActionRow) -> None:
        self._on_installed(self._theme.theme_name)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _on_download_clicked(self, _btn: Gtk.Button) -> None:
        self._suffix_stack.set_visible_child_name("progress")
        self._progress_label.set_text("0%")
        self.set_activatable(False)

        thread = threading.Thread(target=self._download_worker, daemon=True)
        thread.start()

    def _download_worker(self) -> None:
        try:
            self._do_download()
            GLib.idle_add(self._on_download_done)
        except Exception as exc:
            log.error("Download failed for %s: %s", self._theme.display_name, exc)
            GLib.idle_add(self._on_download_error, str(exc))

    def _do_download(self) -> None:
        url = self._theme.download_url
        with urllib.request.urlopen(url, timeout=60) as response:
            total = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 65536
            last_pct = -1

            with tempfile.NamedTemporaryFile(delete=False, suffix=Path(url).suffix) as tmp:
                tmp_path = Path(tmp.name)
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / total)
                        if pct != last_pct:  # only queue when integer % changes
                            last_pct = pct
                            GLib.idle_add(self._set_progress, pct)

        # Wait for the UI to actually render "Extracting…" before blocking the
        # thread with tarfile work — otherwise the label never shows.
        ready = threading.Event()

        def _show_extracting() -> bool:
            self._progress_label.set_text("Extracting…")
            ready.set()
            return False  # remove from idle queue

        GLib.idle_add(_show_extracting)
        ready.wait(timeout=10)

        _INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(tmp_path) as tar:
                try:
                    tar.extractall(path=_INSTALL_DIR, filter="data")
                except TypeError:
                    tar.extractall(path=_INSTALL_DIR)  # Python < 3.12
        finally:
            tmp_path.unlink(missing_ok=True)

    def _set_progress(self, pct: int) -> None:
        self._progress_label.set_text(f"{pct}%")

    def _on_download_done(self) -> None:
        self._suffix_stack.set_visible_child_name("installed")
        self.set_activatable(True)
        self._on_installed(self._theme.theme_name)
        log.info("Installed cursor theme: %s", self._theme.display_name)

    def _on_download_error(self, msg: str) -> None:
        self._suffix_stack.set_visible_child_name("download")
        self.set_activatable(True)
        self.set_subtitle(f"Download failed: {msg}")
        log.error("Cursor theme download error: %s", msg)


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
        Apply cursor immediately via hyprctl setcursor.
    """

    __gtype_name__ = "CursorPage"

    __gsignals__ = {
        "settings-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

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
            "Type a theme name, or click a popular theme below to select and apply it."
        )
        self.add(group)

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
            value=24, lower=12, upper=96,
            step_increment=4, page_increment=16, page_size=0.0,
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
            "Click a row to select a theme. "
            "Use the download button to install it directly to ~/.local/share/icons/."
        )
        self.add(group)

        for theme in _POPULAR_THEMES:
            row = _ThemeRow(theme, on_installed=self._on_theme_selected)
            group.add(row)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_changed(self, _widget: GObject.Object, _param: GObject.ParamSpec) -> None:
        if self._suppress_signals:
            return
        self.emit("settings-changed")

    def _on_theme_selected(self, theme_name: str) -> None:
        """Called by _ThemeRow when a theme is clicked or just installed."""
        self._suppress_signals = True
        try:
            self._theme_row.set_text(theme_name)
        finally:
            self._suppress_signals = False
        self.emit("settings-changed")

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
        self._loaded_snapshot = self._snapshot_values()

    def revert_to_loaded(self) -> None:
        snap = self._loaded_snapshot
        if snap is None:
            return
        self._suppress_signals = True
        try:
            self._apply_values(snap["theme"], snap["size"])
        finally:
            self._suppress_signals = False

    def load(self, config_path: Path, hyprctl_available: bool) -> None:
        self._hyprctl_available = hyprctl_available

        theme = str(DEFAULTS["xcursor_theme"])
        size = int(DEFAULTS["xcursor_size"])

        cursor_lua = (
            Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
            / "hypr" / "cursor.lua"
        )
        if cursor_lua.exists():
            parsed = self._parse_cursor_lua(cursor_lua)
            theme = parsed.get("XCURSOR_THEME", theme)
            try:
                size = int(parsed.get("XCURSOR_SIZE", size))
            except (ValueError, TypeError):
                pass
        else:
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
        import subprocess

        theme = self._theme_row.get_text().strip() or "default"
        size = str(round(self._size_row.get_value()))
        try:
            result = subprocess.run(
                ["hyprctl", "setcursor", theme, size],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                raise HyprctlUnavailableError(
                    f"hyprctl setcursor failed: {result.stderr.strip()}"
                )
            log.info("Applied cursor: theme=%s size=%s", theme, size)
        except FileNotFoundError as exc:
            raise HyprctlUnavailableError("hyprctl not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise HyprctlUnavailableError("hyprctl setcursor timed out") from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_cursor_lua(path: Path) -> dict[str, str]:
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
