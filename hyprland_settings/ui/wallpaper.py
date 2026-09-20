"""Wallpaper settings page.

Implements WallpaperPage — an Adw.PreferencesPage subclass that lets users
browse available wallpapers as thumbnails and apply them to a specific monitor
(or all monitors) via hyprpaper IPC.

Unlike other settings pages this one does NOT use the Lua section-file system.
It reads/writes ~/.config/hypr/hyprpaper.conf directly via the hyprpaper
backend and returns an empty list from collect_lines().
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, GdkPixbuf, GLib, GObject, Gtk

from hyprland_settings.backend.hyprctl import HyprctlUnavailableError, get_monitors
from hyprland_settings.backend.hyprpaper import (
    HyprpaperUnavailableError,
    find_hyprpaper_conf,
    is_hyprpaper_running,
    preload_wallpaper,
    read_hyprpaper_conf,
    set_wallpaper,
    write_hyprpaper_conf,
)

log = logging.getLogger(__name__)

_BUNDLED_WALLPAPERS_DIR = Path(__file__).parent.parent / "data" / "wallpapers"

# ---------------------------------------------------------------------------
# Wallpaper discovery
# ---------------------------------------------------------------------------

WALLPAPER_DIRS: list[Path] = [
    Path("~/Pictures/Wallpapers").expanduser(),
    Path("~/Pictures").expanduser(),
]

_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})

# Thumbnail dimensions (2× for HiDPI)
_THUMB_W = 384
_THUMB_H = 216

# Display dimensions
_TILE_W = 192
_TILE_H = 108


def discover_wallpapers() -> list[Path]:
    """Return a flat deduplicated list of image files from WALLPAPER_DIRS."""
    seen: set[Path] = set()
    results: list[Path] = []
    for directory in WALLPAPER_DIRS:
        if not directory.exists():
            continue
        try:
            for entry in sorted(directory.iterdir()):
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in _IMAGE_EXTENSIONS:
                    continue
                resolved = entry.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    results.append(entry)
        except OSError as exc:
            log.warning("Could not scan wallpaper directory %s: %s", directory, exc)
    return results


# ---------------------------------------------------------------------------
# WallpaperTile widget
# ---------------------------------------------------------------------------


class WallpaperTile(Gtk.Button):
    """A thumbnail button representing one wallpaper image."""

    def __init__(self, path: Path, on_selected: Callable[[Path], None]) -> None:
        super().__init__()
        self._path = path
        self._on_selected = on_selected
        self._is_selected = False

        self.add_css_class("card")
        self.set_tooltip_text(path.name)

        # Overlay: picture + checkmark
        overlay = Gtk.Overlay()

        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.COVER)
        self._picture.set_size_request(_TILE_W, _TILE_H)
        self._picture.set_paintable(None)
        overlay.set_child(self._picture)

        self._checkmark = Gtk.Image.new_from_icon_name("object-select-symbolic")
        self._checkmark.set_pixel_size(32)
        self._checkmark.set_valign(Gtk.Align.END)
        self._checkmark.set_halign(Gtk.Align.END)
        self._checkmark.set_margin_bottom(8)
        self._checkmark.set_margin_end(8)
        self._checkmark.add_css_class("success")
        self._checkmark.set_visible(False)
        overlay.add_overlay(self._checkmark)

        self.set_child(overlay)
        self.connect("clicked", self._on_clicked)

        # Start async thumbnail load
        thread = threading.Thread(target=self._load_thumbnail, daemon=True)
        thread.start()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_selected(self) -> bool:
        return self._is_selected

    # ------------------------------------------------------------------
    # Selection state
    # ------------------------------------------------------------------

    def set_selected(self, selected: bool) -> None:
        self._is_selected = selected
        self._checkmark.set_visible(selected)

    # ------------------------------------------------------------------
    # Thumbnail loading
    # ------------------------------------------------------------------

    def _load_thumbnail(self) -> None:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(self._path), _THUMB_W, _THUMB_H, False
            )
        except Exception as exc:
            log.debug("Could not load thumbnail for %s: %s", self._path, exc)
            return
        GLib.idle_add(self._set_pixbuf, pixbuf)

    def _set_pixbuf(self, pixbuf: GdkPixbuf.Pixbuf) -> bool:
        from gi.repository import Gdk

        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        self._picture.set_paintable(texture)
        return False

    # ------------------------------------------------------------------
    # Click handler
    # ------------------------------------------------------------------

    def _on_clicked(self, _btn: Gtk.Button) -> None:
        self._on_selected(self._path)


# ---------------------------------------------------------------------------
# WallpaperPage
# ---------------------------------------------------------------------------


class WallpaperPage(Adw.PreferencesPage):
    """Wallpaper settings page for Hyprland.

    Signals
    -------
    settings-changed
        Emitted when the active wallpaper assignment changes.

    Public API
    ----------
    load(config_path, hyprctl_available)
        Read hyprpaper.conf, populate monitor selector, discover wallpapers,
        and mark the active tile.

    collect_lines() -> list[str]
        Always returns [] — this page manages its own conf file.

    apply_live()
        Apply assignments via hyprpaper IPC and write hyprpaper.conf.

    mark_saved()
        Snapshot the current assignments as the saved state.

    revert_to_loaded()
        Restore assignments from the last snapshot and re-apply.
    """

    __gtype_name__ = "WallpaperPage"

    __gsignals__ = {
        "settings-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()

        self._suppress_signals: bool = False
        self._hyprctl_available: bool = False

        # monitor_key -> wallpaper path string  ("" key = all monitors)
        self._assignments: dict[str, str] = {}
        self._available_wallpapers: list[Path] = []
        self._selected_tile: WallpaperTile | None = None
        self._loaded_snapshot: dict[str, str] = {}

        # Monitor names list (populated in load())
        self._monitor_names: list[str] = []

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_monitor_group()
        self._build_wallpaper_group()

    def _build_monitor_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Monitor")
        self.add(group)

        self._monitor_row = Adw.ComboRow()
        self._monitor_row.set_title("Apply to")
        self._set_monitor_model(["All Monitors"])
        group.add(self._monitor_row)

        self._monitor_row.connect("notify::selected", self._on_monitor_changed)

    def _build_wallpaper_group(self) -> None:
        self._wallpaper_group = Adw.PreferencesGroup()
        self._wallpaper_group.set_title("Wallpapers")
        self._wallpaper_group.set_description("Click to apply")
        self.add(self._wallpaper_group)

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scrolled.set_min_content_height(300)

        self._flowbox = Gtk.FlowBox()
        self._flowbox.set_homogeneous(True)
        self._flowbox.set_column_spacing(8)
        self._flowbox.set_row_spacing(8)
        self._flowbox.set_margin_top(12)
        self._flowbox.set_margin_bottom(12)
        self._flowbox.set_margin_start(12)
        self._flowbox.set_margin_end(12)
        self._flowbox.set_selection_mode(Gtk.SelectionMode.NONE)

        self._scrolled.set_child(self._flowbox)
        self._wallpaper_group.add(self._scrolled)

    # ------------------------------------------------------------------
    # Monitor selector helpers
    # ------------------------------------------------------------------

    def _set_monitor_model(self, names: list[str]) -> None:
        string_list = Gtk.StringList()
        for name in names:
            string_list.append(name)
        self._monitor_row.set_model(string_list)

    def _selected_monitor_key(self) -> str:
        """Return "" for "All Monitors", or the monitor name string."""
        idx = self._monitor_row.get_selected()
        if idx == 0:
            return ""
        # idx 1 = first real monitor name
        if idx - 1 < len(self._monitor_names):
            return self._monitor_names[idx - 1]
        return ""

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_monitor_changed(
        self, _row: Adw.ComboRow, _param: GObject.ParamSpec
    ) -> None:
        if self._suppress_signals:
            return
        # Refresh which tile appears selected for the newly chosen monitor
        self._refresh_selection()

    def _on_wallpaper_selected(self, path: Path) -> None:
        """Called when the user clicks a WallpaperTile."""
        monitor_key = self._selected_monitor_key()
        self._assignments[monitor_key] = str(path)

        # Update visual selection
        if self._selected_tile is not None:
            self._selected_tile.set_selected(False)
        self._selected_tile = self._find_tile_for_path(path)
        if self._selected_tile is not None:
            self._selected_tile.set_selected(True)

        self.apply_live()
        if not self._suppress_signals:
            self.emit("settings-changed")

    # ------------------------------------------------------------------
    # Grid management
    # ------------------------------------------------------------------

    def _populate_grid(self, wallpapers: list[Path]) -> None:
        """Remove old tiles and add new ones for *wallpapers*."""
        # Remove all existing children
        while True:
            child = self._flowbox.get_first_child()
            if child is None:
                break
            self._flowbox.remove(child)

        self._selected_tile = None

        if not wallpapers:
            self._show_empty_state()
            return

        # Ensure scrolled window is visible (may have been replaced by status page)
        self._scrolled.set_visible(True)

        for path in wallpapers:
            tile = WallpaperTile(path, on_selected=self._on_wallpaper_selected)
            self._flowbox.append(tile)

    def _show_empty_state(self) -> None:
        """Replace the scrolled window contents with an empty-state status page."""
        status = Adw.StatusPage()
        status.set_title("No wallpapers found")
        status.set_description("Add images to ~/Pictures/Wallpapers/ to get started")
        status.set_icon_name("image-missing-symbolic")
        # Hide the scrolled window and show a status page in the group instead
        self._scrolled.set_visible(False)
        self._wallpaper_group.add(status)

    def _find_tile_for_path(self, path: Path) -> WallpaperTile | None:
        child = self._flowbox.get_first_child()
        while child is not None:
            # FlowBox wraps each item in a FlowBoxChild
            inner = child.get_child() if isinstance(child, Gtk.FlowBoxChild) else child
            if isinstance(inner, WallpaperTile) and inner.path.resolve() == path.resolve():
                return inner
            child = child.get_next_sibling()
        return None

    def _refresh_selection(self) -> None:
        """Update tile checkmarks to match the current monitor's assignment."""
        monitor_key = self._selected_monitor_key()
        active_path = self._assignments.get(monitor_key, "")

        # Clear all selections
        child = self._flowbox.get_first_child()
        while child is not None:
            inner = child.get_child() if isinstance(child, Gtk.FlowBoxChild) else child
            if isinstance(inner, WallpaperTile):
                is_active = active_path and str(inner.path) == active_path
                inner.set_selected(bool(is_active))
                if is_active:
                    self._selected_tile = inner
            child = child.get_next_sibling()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _seed_bundled_wallpapers(self) -> None:
        """Copy bundled wallpapers to ~/Pictures/Wallpapers/ on first use (no-clobber)."""
        if not _BUNDLED_WALLPAPERS_DIR.exists():
            return
        dest = Path("~/Pictures/Wallpapers").expanduser()
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for src in _BUNDLED_WALLPAPERS_DIR.iterdir():
                if src.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    target = dest / src.name
                    if not target.exists():
                        import shutil
                        shutil.copy2(src, target)
        except OSError as exc:
            log.warning("Could not seed bundled wallpapers: %s", exc)

    def load(self, config_path: Path, hyprctl_available: bool) -> None:
        """Read hyprpaper.conf, populate monitor selector, discover wallpapers."""
        self._hyprctl_available = hyprctl_available
        self._seed_bundled_wallpapers()

        # Read current assignments from hyprpaper.conf
        conf_path = find_hyprpaper_conf()
        hyprpaper_config = read_hyprpaper_conf(conf_path)
        self._assignments = dict(hyprpaper_config.wallpapers)

        # Populate monitor selector
        monitor_names: list[str] = []
        try:
            monitors = get_monitors()
            monitor_names = [m.name for m in monitors]
        except HyprctlUnavailableError as exc:
            log.warning("Could not get monitor list: %s", exc)

        self._monitor_names = monitor_names

        self._suppress_signals = True
        try:
            self._set_monitor_model(["All Monitors"] + monitor_names)
            self._monitor_row.set_selected(0)
        finally:
            self._suppress_signals = False

        # Discover wallpapers and build grid
        self._available_wallpapers = discover_wallpapers()
        self._populate_grid(self._available_wallpapers)
        self._refresh_selection()

        # Snapshot
        self._loaded_snapshot = dict(self._assignments)

    def collect_lines(self) -> list[str]:
        """Return empty list — wallpaper uses its own conf file."""
        return []

    def apply_live(self) -> None:
        """Apply current assignments via hyprpaper IPC and write hyprpaper.conf."""
        if not is_hyprpaper_running():
            log.warning(
                "hyprpaper is not running — skipping IPC, still writing conf file"
            )
        else:
            for monitor_key, path_str in self._assignments.items():
                if not path_str:
                    continue
                try:
                    preload_wallpaper(path_str)
                except Exception as exc:
                    log.warning("Failed to preload wallpaper %s: %s", path_str, exc)
                try:
                    set_wallpaper(monitor_key, path_str)
                except Exception as exc:
                    log.warning(
                        "Failed to set wallpaper %s on monitor %r: %s",
                        path_str,
                        monitor_key,
                        exc,
                    )

        # Always write the conf file
        conf_path = find_hyprpaper_conf()
        conf = read_hyprpaper_conf(conf_path)
        conf.wallpapers = dict(self._assignments)
        # Ensure every assigned path is in the preload list
        preload_set = set(conf.preload)
        for path_str in self._assignments.values():
            if path_str and path_str not in preload_set:
                conf.preload.append(path_str)
                preload_set.add(path_str)
        try:
            write_hyprpaper_conf(conf, conf_path)
        except OSError as exc:
            log.error("Failed to write hyprpaper.conf: %s", exc)

    def mark_saved(self) -> None:
        """Snapshot the current assignments as the saved/loaded state."""
        self._loaded_snapshot = dict(self._assignments)

    def revert_to_loaded(self) -> None:
        """Restore assignments from the last snapshot and re-apply."""
        self._assignments = dict(self._loaded_snapshot)
        self._refresh_selection()
        self.apply_live()
