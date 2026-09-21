"""Main application window for Hyprland-Settings.

Lays out a multi-page ViewStack with MonitorCanvas + MonitorSidebar on the
monitors page, owns the Apply+Save split button, manages the unsaved-changes
banner, and maintains the undo stack.
"""

from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from hyprland_settings.backend.hyprctl import (
    Monitor,
    HyprctlUnavailableError,
    HyprctlApplyError,
    apply_monitors_batch,
    get_available_modes,
    get_monitors,
    reload_config,
)
from hyprland_settings.backend.config_writer import (
    ConfigNotFoundError,
    find_config_path,
    get_section_file_path,
    read_monitors_from_config,
    write_monitors_to_config,
    write_section_file,
)
from hyprland_settings.ui.canvas import MonitorCanvas
from hyprland_settings.ui.sidebar import MonitorSidebar

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional page imports (graceful fallback to None)
# ---------------------------------------------------------------------------

try:
    from hyprland_settings.ui.appearance import AppearancePage
except ImportError:
    AppearancePage = None

try:
    from hyprland_settings.ui.animations import AnimationsPage
except ImportError:
    AnimationsPage = None

try:
    from hyprland_settings.ui.input_settings import InputPage
except ImportError:
    InputPage = None

try:
    from hyprland_settings.ui.keybindings import KeybindingsPage
except ImportError:
    KeybindingsPage = None

try:
    from hyprland_settings.ui.cursor import CursorPage
except ImportError:
    CursorPage = None

try:
    from hyprland_settings.ui.wallpaper import WallpaperPage
except ImportError:
    WallpaperPage = None


# ---------------------------------------------------------------------------
# Undo stack
# ---------------------------------------------------------------------------


class UndoStack:
    """Fixed-depth undo stack that stores snapshots of the monitor list."""

    MAX_DEPTH = 50

    def __init__(self) -> None:
        self._stack: list[list[Monitor]] = []

    def push(self, state: list[Monitor]) -> None:
        """Push a deep copy of *state* onto the stack."""
        self._stack.append(copy.deepcopy(state))
        if len(self._stack) > self.MAX_DEPTH:
            self._stack.pop(0)

    def undo(self) -> list[Monitor] | None:
        """Pop and return the most recent saved state, or None if empty."""
        if not self._stack:
            return None
        return self._stack.pop()

    def can_undo(self) -> bool:
        return bool(self._stack)

    def clear(self) -> None:
        self._stack.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _monitor_config_to_monitor(cfg, idx: int) -> Monitor:
    """Build a synthetic Monitor dataclass from a MonitorConfig (config-only mode)."""
    width, height = 1920, 1080
    res = cfg.resolution.lower()
    if res not in ("preferred", "highres", "highrr", "disabled") and "x" in res:
        try:
            parts = res.split("x", 1)
            width, height = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass

    x, y = 0, 0
    pos = cfg.position.lower()
    if pos not in ("auto", "auto-right", "auto-left", "auto-up", "auto-down", "") and "x" in pos:
        try:
            parts = pos.split("x", 1)
            x, y = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass

    scale = float(cfg.scale) if isinstance(cfg.scale, (int, float)) else 1.0
    refresh = cfg.refresh if cfg.refresh is not None else 60.0
    transform = cfg.transform if cfg.transform is not None else 0
    disabled = cfg.resolution == "disabled"

    return Monitor(
        id=idx,
        name=cfg.name,
        description=cfg.name,
        make="",
        model="",
        serial="",
        width=width,
        height=height,
        refresh_rate=refresh,
        x=x,
        y=y,
        scale=scale,
        transform=transform,
        focused=False,
        dpms_status=True,
        vrr=False,
        disabled=disabled,
        mirror_of=cfg.mirror or "",
    )


def _monitors_overlap(monitors: list[Monitor]) -> list[tuple[str, str]]:
    """Return pairs of monitor names whose logical bounding boxes overlap."""
    overlaps: list[tuple[str, str]] = []
    active = [m for m in monitors if not m.disabled and not m.mirror_of]
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if (
                a.x < b.x + b.logical_width
                and a.x + a.logical_width > b.x
                and a.y < b.y + b.logical_height
                and a.y + a.logical_height > b.y
            ):
                overlaps.append((a.name, b.name))
    return overlaps


def _states_equal(a: list[Monitor], b: list[Monitor]) -> bool:
    """Compare two monitor lists by config-relevant fields only."""
    if len(a) != len(b):
        return False
    # Sort by name for stable comparison
    sa = sorted(a, key=lambda m: m.name)
    sb = sorted(b, key=lambda m: m.name)
    for ma, mb in zip(sa, sb):
        if (
            ma.name != mb.name
            or ma.width != mb.width
            or ma.height != mb.height
            or ma.refresh_rate != mb.refresh_rate
            or ma.x != mb.x
            or ma.y != mb.y
            or ma.scale != mb.scale
            or ma.transform != mb.transform
            or ma.disabled != mb.disabled
            or ma.mirror_of != mb.mirror_of
        ):
            return False
    return True


def _make_stub_page(title: str, description: str) -> Adw.StatusPage:
    """Return a placeholder StatusPage for pages not yet implemented."""
    page = Adw.StatusPage()
    page.set_title(title)
    page.set_description(description)
    page.set_icon_name("preferences-system-symbolic")
    page.set_vexpand(True)
    return page


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(Adw.ApplicationWindow):
    """Hyprland Settings main application window."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        self._undo_stack = UndoStack()
        self._applied_state: list[Monitor] = []
        self._current_state: list[Monitor] = []
        self._monitor_modes: dict[str, list[str]] = {}
        self._config_path: Optional[Path] = None
        self._hyprctl_available: bool = True
        self._apply_in_progress: bool = False
        self._live_timer_ids: dict[str, int] = {}
        self._wallpaper_page: object = None
        self._wallpaper_has_changes: bool = False

        self.set_title("Hyprland Settings")
        self.set_default_size(1100, 650)

        self._build_ui()
        self._setup_shortcuts()

        # Defer actual I/O until after the window is shown
        GLib.idle_add(self._startup)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ---- Root: Adw.ToolbarView ----
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # ---- Header bar ----
        self._header_bar = Adw.HeaderBar()
        self._header_bar.set_title_widget(Gtk.Label(label="Hyprland Settings"))

        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)
        self._header_bar.pack_end(self._spinner)

        self._apply_save_btn = Adw.SplitButton()
        self._apply_save_btn.set_label("Apply + Save")
        self._apply_save_btn.connect("clicked", self._on_apply_save_clicked)

        menu = Gio.Menu()
        menu.append("Apply only", "win.apply-only")
        menu.append("Save only", "win.save-only")
        section = Gio.Menu()
        section.append("Revert to saved", "win.revert")
        menu.append_section(None, section)
        self._apply_save_btn.set_menu_model(menu)
        self._header_bar.pack_end(self._apply_save_btn)
        toolbar_view.add_top_bar(self._header_bar)

        # ---- Main horizontal layout: sidebar | separator | content ----
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        toolbar_view.set_content(main_box)

        # Left navigation sidebar
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.set_size_request(200, -1)
        main_box.append(sidebar_box)

        self._nav_list = Gtk.ListBox()
        self._nav_list.add_css_class("navigation-sidebar")
        self._nav_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._nav_list.set_vexpand(True)
        self._nav_list.connect("row-selected", self._on_nav_row_selected)
        sidebar_box.append(self._nav_list)

        # Vertical separator between sidebar and content
        main_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # Right content area
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.set_hexpand(True)
        main_box.append(content_box)

        # Banners
        self._changes_banner = Adw.Banner()
        self._changes_banner.set_title("Unsaved changes")
        self._changes_banner.set_button_label("Revert")
        self._changes_banner.set_revealed(False)
        self._changes_banner.connect("button-clicked", self._on_revert_clicked)
        content_box.append(self._changes_banner)

        self._offline_banner = Adw.Banner()
        self._offline_banner.set_title(
            "Running outside Hyprland — Apply is unavailable"
        )
        self._offline_banner.set_revealed(False)
        content_box.append(self._offline_banner)

        # Page stack
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_vexpand(True)
        self._stack.set_hexpand(True)
        content_box.append(self._stack)

        # Instantiate settings pages (store refs for apply/save wiring)
        _appearance  = AppearancePage()  if AppearancePage  else _make_stub_page("Appearance",  "Appearance settings — coming soon")
        _animations  = AnimationsPage()  if AnimationsPage  else _make_stub_page("Animations",  "Animation settings — coming soon")
        _input       = InputPage()       if InputPage       else _make_stub_page("Input",        "Input device settings — coming soon")
        _keybindings = KeybindingsPage() if KeybindingsPage else _make_stub_page("Keybindings", "Keyboard shortcuts — coming soon")
        _cursor      = CursorPage()      if CursorPage      else _make_stub_page("Cursor",       "Cursor settings — coming soon")
        _wallpaper = WallpaperPage() if WallpaperPage else _make_stub_page("Wallpaper", "Wallpaper settings — coming soon")
        self._wallpaper_page = _wallpaper

        # Map page name → (widget, config section name) for apply/save dispatch
        self._settings_pages: dict[str, tuple] = {
            "appearance":  (_appearance,  "appearance"),
            "animations":  (_animations,  "animations"),
            "input":       (_input,       "input"),
            "cursor":      (_cursor,      "cursor"),
            "keybindings": (_keybindings, "keybinds"),
        }

        # Register pages: (name, label, icon, widget)
        self._nav_page_names: list[str] = []
        pages = [
            ("monitors",    "Monitors",    "preferences-desktop-display-symbolic",    self._build_monitors_page()),
            ("appearance",  "Appearance",  "preferences-desktop-appearance-symbolic", _appearance),
            ("animations",  "Animations",  "media-playback-start-symbolic",           _animations),
            ("input",       "Input",       "input-keyboard-symbolic",                 _input),
            ("cursor",      "Cursor",      "preferences-peripherals-symbolic",        _cursor),
            ("wallpaper",   "Wallpaper",   "preferences-desktop-wallpaper-symbolic",  _wallpaper),
            ("keybindings", "Keybindings", "key-symbolic",                            _keybindings),
        ]

        for name, label, icon_name, widget in pages:
            self._stack.add_named(widget, name)
            self._nav_page_names.append(name)

            row = Gtk.ListBoxRow()
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)
            row_box.set_margin_top(10)
            row_box.set_margin_bottom(10)
            row_box.append(Gtk.Image.new_from_icon_name(icon_name))
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_hexpand(True)
            row_box.append(lbl)
            row.set_child(row_box)
            self._nav_list.append(row)

        # Select Monitors by default
        self._nav_list.select_row(self._nav_list.get_row_at_index(0))

        # Connect settings-changed signals so the banner stays in sync
        self._page_has_changes: dict[str, bool] = {}
        for page_name, (widget, _) in self._settings_pages.items():
            widget.connect(
                "settings-changed",
                lambda _w, name=page_name: self._on_settings_page_changed(name),
            )

        if WallpaperPage and isinstance(_wallpaper, WallpaperPage):
            _wallpaper.connect("settings-changed", self._on_wallpaper_changed)

        # Update banner when switching pages
        self._stack.connect("notify::visible-child", lambda *_: self._update_changes_banner())

        self._setup_actions()

    def _on_nav_row_selected(self, _listbox, row) -> None:
        if row is None:
            return
        idx = row.get_index()
        if 0 <= idx < len(self._nav_page_names):
            self._stack.set_visible_child_name(self._nav_page_names[idx])

    def _build_monitors_page(self) -> Gtk.Widget:
        """Construct and return the monitor canvas + sidebar paned layout."""
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_vexpand(True)

        self._canvas = MonitorCanvas()
        self._canvas.set_hexpand(True)
        self._canvas.set_vexpand(True)
        paned.set_start_child(self._canvas)
        paned.set_resize_start_child(True)
        paned.set_shrink_start_child(False)

        self._sidebar = MonitorSidebar()
        self._sidebar.set_size_request(320, -1)
        self._sidebar.set_hexpand(False)
        paned.set_end_child(self._sidebar)
        paned.set_resize_end_child(False)
        paned.set_shrink_end_child(False)

        # Wire signals
        self._canvas.connect("monitor-moved", self._on_monitor_moved)
        self._canvas.connect("monitor-selected", self._on_monitor_selected)
        self._sidebar.connect("monitor-changed", self._on_monitor_changed)

        return paned

    def _setup_actions(self) -> None:
        apply_only = Gio.SimpleAction.new("apply-only", None)
        apply_only.connect("activate", lambda a, p: self._on_action_apply_only())
        self.add_action(apply_only)

        save_only = Gio.SimpleAction.new("save-only", None)
        save_only.connect("activate", lambda a, p: self._on_action_save_only())
        self.add_action(save_only)

        revert = Gio.SimpleAction.new("revert", None)
        revert.connect("activate", lambda a, p: self._on_revert_clicked(None))
        self.add_action(revert)

    def _on_action_apply_only(self) -> None:
        page = self._stack.get_visible_child_name()
        if page == "monitors":
            self._do_apply(save=False)
        elif page == "wallpaper":
            self._do_apply_wallpaper(apply=self._hyprctl_available, save=False)
        elif page in self._settings_pages:
            self._do_apply_settings_page(page, apply=self._hyprctl_available, save=False)

    def _on_action_save_only(self) -> None:
        page = self._stack.get_visible_child_name()
        if page == "monitors":
            self._do_save()
        elif page == "wallpaper":
            self._do_apply_wallpaper(apply=False, save=True)
        elif page in self._settings_pages:
            self._do_apply_settings_page(page, apply=False, save=True)

    def _setup_shortcuts(self) -> None:
        ctrl = Gtk.ShortcutController.new()
        ctrl.set_scope(Gtk.ShortcutScope.MANAGED)
        self.add_controller(ctrl)

        def _add(accel: str, fn) -> None:
            trigger = Gtk.ShortcutTrigger.parse_string(accel)
            action = Gtk.CallbackAction.new(fn)
            ctrl.add_shortcut(Gtk.Shortcut.new(trigger, action))

        _add("<ctrl>z", lambda w, a: self._on_undo() or True)
        _add("<ctrl>s", lambda w, a: self._on_apply_save_clicked(None) or True)
        _add("<ctrl><shift>s", lambda w, a: self._do_save() or True)
        _add("<ctrl>r", lambda w, a: self._on_revert_clicked(None) or True)
        _add("<ctrl>equal", lambda w, a: self._zoom_canvas(1) or True)
        _add("<ctrl>minus", lambda w, a: self._zoom_canvas(-1) or True)
        _add("<ctrl>0", lambda w, a: self._canvas.reset_view() or True)

    # ------------------------------------------------------------------
    # Startup (deferred via GLib.idle_add)
    # ------------------------------------------------------------------

    def _startup(self) -> bool:
        # Locate hyprland.conf
        try:
            self._config_path = find_config_path()
        except ConfigNotFoundError as exc:
            log.warning("hyprland.conf not found: %s", exc)
            self._config_path = None

        monitors: list[Monitor] = []

        # Try live state via hyprctl
        try:
            monitors = get_monitors()
            self._hyprctl_available = True
            # Fetch available modes for each monitor (best-effort)
            for m in monitors:
                self._monitor_modes[m.name] = get_available_modes(m.name)
        except HyprctlUnavailableError as exc:
            log.info("hyprctl unavailable, falling back to config: %s", exc)
            self._hyprctl_available = False
            self._offline_banner.set_revealed(True)
            self._apply_save_btn.set_sensitive(False)

            # Fall back to config file
            if self._config_path is not None:
                try:
                    cfg_list = read_monitors_from_config(self._config_path)
                    monitors = [
                        _monitor_config_to_monitor(c, i)
                        for i, c in enumerate(cfg_list)
                    ]
                except Exception as exc2:
                    log.warning("Could not parse config: %s", exc2)

        self._applied_state = copy.deepcopy(monitors)
        self._current_state = copy.deepcopy(monitors)

        self._canvas.set_monitors(copy.deepcopy(monitors))
        self._sidebar.set_other_monitors(monitors)
        self._undo_stack.clear()
        self._update_changes_banner()

        # Load all settings pages now that we know config_path and hyprctl state
        for page_name, (widget, section_name) in self._settings_pages.items():
            if hasattr(widget, "load"):
                try:
                    widget.load(self._config_path, self._hyprctl_available)
                except Exception as exc:
                    log.warning("Could not load settings page %s: %s", page_name, exc)

        # Load wallpaper page
        if self._wallpaper_page is not None and hasattr(self._wallpaper_page, "load"):
            try:
                self._wallpaper_page.load(self._config_path, self._hyprctl_available)
            except Exception as exc:
                log.warning("Could not load wallpaper page: %s", exc)

        return False  # do not repeat

    # ------------------------------------------------------------------
    # Canvas signal handlers
    # ------------------------------------------------------------------

    def _on_monitor_moved(
        self, canvas: MonitorCanvas, name: str, new_x: int, new_y: int
    ) -> None:
        """Drag complete — push undo, sync current state, update sidebar."""
        # Pre-change state is still in self._current_state
        self._undo_stack.push(self._current_state)

        # Sync from canvas (already updated internally)
        self._current_state = copy.deepcopy(canvas.get_monitors())
        self._update_changes_banner()

        # Refresh sidebar if the moved monitor is currently shown
        mon = self._find_monitor(name)
        if mon is not None:
            others = [m for m in self._current_state if m.name != name]
            self._sidebar.set_other_monitors(others)
            self._sidebar.set_modes(self._monitor_modes.get(name, []))
            # Pass a deepcopy so sidebar has its own object to mutate
            self._sidebar.set_monitor(copy.deepcopy(mon))

    def _on_monitor_selected(self, canvas: MonitorCanvas, name) -> None:
        """User clicked a monitor — populate sidebar."""
        if name is None:
            self._sidebar.set_monitor(None)
            return

        mon = self._find_monitor(name)
        if mon is not None:
            others = [m for m in self._current_state if m.name != name]
            self._sidebar.set_other_monitors(others)
            self._sidebar.set_modes(self._monitor_modes.get(name, []))
            self._sidebar.set_monitor(copy.deepcopy(mon))

    # ------------------------------------------------------------------
    # Sidebar signal handlers
    # ------------------------------------------------------------------

    def _on_monitor_changed(self, sidebar: MonitorSidebar, monitor: Monitor) -> None:
        """Sidebar edited a field — push undo, update state, refresh canvas."""
        # Push pre-change snapshot (self._current_state is still the old one
        # because we gave sidebar a deepcopy in _on_monitor_selected)
        self._undo_stack.push(self._current_state)

        # Replace just this monitor in current state
        self._current_state = [
            monitor if m.name == monitor.name else m
            for m in self._current_state
        ]

        # Reflect changes on canvas without resetting pan/zoom
        self._canvas.set_monitors(copy.deepcopy(self._current_state))
        self._update_changes_banner()

    # ------------------------------------------------------------------
    # Apply / Save / Revert
    # ------------------------------------------------------------------

    def _on_apply_save_clicked(self, _btn) -> None:
        page = self._stack.get_visible_child_name()
        if page == "monitors":
            self._do_apply(save=True)
        elif page == "wallpaper":
            self._do_apply_wallpaper(apply=self._hyprctl_available, save=True)
        elif page in self._settings_pages:
            self._do_apply_settings_page(page, apply=self._hyprctl_available, save=True)

    def _do_apply(self, *, save: bool) -> None:
        """Run apply sequence; write config afterward when *save* is True."""
        if not self._hyprctl_available or self._apply_in_progress:
            return

        monitors = copy.deepcopy(self._current_state)

        # Overlap check — warn but never block
        overlaps = _monitors_overlap(monitors)
        if overlaps:
            pairs = ", ".join(f"{a}/{b}" for a, b in overlaps)
            log.warning("Overlapping monitors: %s", pairs)
            # Best-effort toast; the real UI path handles toasts via ToastOverlay
            self._log_warning(f"Warning: monitors overlap — {pairs}")

        self._apply_in_progress = True
        self._spinner.set_spinning(True)
        self._spinner.set_visible(True)
        self._apply_save_btn.set_sensitive(False)

        # Snapshot of state at apply time so callbacks can capture it
        applied_snapshot = copy.deepcopy(self._applied_state)
        config_path = self._config_path

        def _worker() -> None:
            try:
                apply_monitors_batch(monitors)
                GLib.idle_add(_on_success)
            except (HyprctlApplyError, HyprctlUnavailableError) as exc:
                GLib.idle_add(_on_error, str(exc), applied_snapshot)
            except Exception as exc:
                log.exception("Unexpected error in apply worker")
                GLib.idle_add(_on_error, str(exc), applied_snapshot)

        def _on_success() -> None:
            if save and config_path is not None:
                try:
                    write_monitors_to_config(monitors, config_path)
                except Exception as exc:
                    log.warning("Config write failed: %s", exc)
            self._applied_state = copy.deepcopy(monitors)
            self._update_changes_banner()
            _cleanup()

        def _on_error(err: str, revert_to: list[Monitor]) -> None:
            # Auto-revert the runtime state
            try:
                apply_monitors_batch(revert_to)
            except Exception:
                pass
            _cleanup()
            self._show_apply_error(err)

        def _cleanup() -> None:
            self._apply_in_progress = False
            self._spinner.set_spinning(False)
            self._spinner.set_visible(False)
            self._apply_save_btn.set_sensitive(True)

        threading.Thread(target=_worker, daemon=True, name="hyprctl-apply").start()

    def _do_save(self) -> None:
        """Write current state to config without calling hyprctl."""
        if self._config_path is None:
            log.warning("Cannot save: config path unknown")
            return

        monitors = copy.deepcopy(self._current_state)
        try:
            write_monitors_to_config(monitors, self._config_path)
            self._applied_state = copy.deepcopy(monitors)
            self._update_changes_banner()
        except Exception as exc:
            log.error("Save failed: %s", exc)
            self._show_apply_error(str(exc))

    def _do_apply_settings_page(self, page_name: str, *, apply: bool, save: bool) -> None:
        """Write section file and optionally apply live via hyprctl."""
        widget, section_name = self._settings_pages[page_name]
        # Cancel pending debounce — we're writing now
        existing = self._live_timer_ids.pop(page_name, None)
        if existing is not None:
            GLib.source_remove(existing)
        try:
            write_section_file(section_name, widget.collect_lines())
        except Exception as exc:
            log.error("Section file write failed for %s: %s", page_name, exc)
            self._show_apply_error(str(exc))
            return
        if apply and hasattr(widget, "apply_live"):
            try:
                widget.apply_live()
            except Exception as exc:
                log.warning("apply_live failed for %s: %s", page_name, exc)
        if save:
            widget.mark_saved()
            self._page_has_changes[page_name] = False
            self._update_changes_banner()

    def _on_revert_clicked(self, _btn) -> None:
        """Revert UI (and live compositor) to the last saved state."""
        page = self._stack.get_visible_child_name()
        if page == "monitors":
            self._current_state = copy.deepcopy(self._applied_state)
            self._canvas.set_monitors(copy.deepcopy(self._current_state))
            self._sidebar.set_monitor(None)
            self._undo_stack.clear()
        elif page == "wallpaper":
            if hasattr(self._wallpaper_page, "revert_to_loaded"):
                self._wallpaper_page.revert_to_loaded()
            self._wallpaper_has_changes = False
        elif page in self._settings_pages:
            widget, _ = self._settings_pages[page]
            widget.revert_to_loaded()
            self._page_has_changes[page] = False
            # Write reverted state to disk and reload so the compositor reflects it
            self._do_live_write(page)
        self._update_changes_banner()

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def _on_undo(self) -> None:
        state = self._undo_stack.undo()
        if state is None:
            return
        self._current_state = state
        self._canvas.set_monitors(copy.deepcopy(self._current_state))
        # Clear sidebar selection — the user can re-select
        self._sidebar.set_monitor(None)
        self._update_changes_banner()

    # ------------------------------------------------------------------
    # Zoom helpers (keyboard shortcuts Ctrl+= / Ctrl+-)
    # ------------------------------------------------------------------

    def _zoom_canvas(self, direction: int) -> None:
        """Step canvas zoom in (direction > 0) or out (direction < 0)."""
        factor = 1.15 if direction > 0 else (1.0 / 1.15)
        new_zoom = max(0.10, min(2.0, self._canvas._zoom * factor))
        w = self._canvas.get_width() or 800
        h = self._canvas.get_height() or 600
        cx, cy = w / 2.0, h / 2.0
        lx, ly = self._canvas.canvas_to_logical(cx, cy)
        self._canvas._zoom = new_zoom
        new_cx, new_cy = self._canvas.logical_to_canvas(lx, ly)
        self._canvas._canvas_origin_x += cx - new_cx
        self._canvas._canvas_origin_y += cy - new_cy
        self._canvas.queue_draw()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_monitor(self, name: str) -> Monitor | None:
        for m in self._current_state:
            if m.name == name:
                return m
        return None

    def _on_wallpaper_changed(self, _widget) -> None:
        self._wallpaper_has_changes = True
        self._update_changes_banner()
        # Debounce: cancel any pending timer then schedule apply
        existing = self._live_timer_ids.pop("wallpaper", None)
        if existing is not None:
            GLib.source_remove(existing)
        timer_id = GLib.timeout_add(300, self._do_live_write_wallpaper)
        self._live_timer_ids["wallpaper"] = timer_id

    def _do_live_write_wallpaper(self) -> bool:
        self._live_timer_ids.pop("wallpaper", None)
        page = self._wallpaper_page
        if page is None or not hasattr(page, "apply_live"):
            return False
        try:
            page.apply_live()
        except Exception as exc:
            log.warning("Wallpaper live apply failed: %s", exc)
        return False

    def _do_apply_wallpaper(self, *, apply: bool, save: bool) -> None:
        page = self._wallpaper_page
        if page is None or not hasattr(page, "apply_live"):
            return
        existing = self._live_timer_ids.pop("wallpaper", None)
        if existing is not None:
            GLib.source_remove(existing)
        if apply and hasattr(page, "apply_live"):
            try:
                page.apply_live()
            except Exception as exc:
                log.warning("Wallpaper apply failed: %s", exc)
        if save and hasattr(page, "mark_saved"):
            page.mark_saved()
            self._wallpaper_has_changes = False
            self._update_changes_banner()

    def _on_settings_page_changed(self, page_name: str) -> None:
        self._page_has_changes[page_name] = True
        if self._stack.get_visible_child_name() == page_name:
            self._changes_banner.set_revealed(True)
        # Cancel any pending debounce timer for this page, then start a fresh one
        existing = self._live_timer_ids.pop(page_name, None)
        if existing is not None:
            GLib.source_remove(existing)
        timer_id = GLib.timeout_add(300, self._do_live_write, page_name)
        self._live_timer_ids[page_name] = timer_id

    def _do_live_write(self, page_name: str) -> bool:
        """Write page settings to its section file and apply live via hyprctl."""
        self._live_timer_ids.pop(page_name, None)
        widget, section_name = self._settings_pages.get(page_name, (None, None))
        if widget is None or not hasattr(widget, "collect_lines"):
            return False
        try:
            write_section_file(section_name, widget.collect_lines())
        except Exception as exc:
            log.warning("Live write failed for %s: %s", page_name, exc)
        if self._hyprctl_available and hasattr(widget, "apply_live"):
            try:
                widget.apply_live()
            except Exception as exc:
                log.warning("Live apply failed for %s: %s", page_name, exc)
        if self._hyprctl_available:
            try:
                reload_config()
            except Exception as exc:
                log.warning("hyprctl reload failed for %s: %s", page_name, exc)
        return False  # do not repeat

    def _update_changes_banner(self) -> None:
        page = self._stack.get_visible_child_name()
        if page == "monitors":
            changed = not _states_equal(self._current_state, self._applied_state)
        elif page == "wallpaper":
            changed = self._wallpaper_has_changes
        else:
            changed = self._page_has_changes.get(page, False)
        self._changes_banner.set_revealed(changed)

    def _log_warning(self, message: str) -> None:
        """Log a non-fatal warning (UI toast path is wired up when ToastOverlay is present)."""
        log.warning(message)

    def _show_apply_error(self, error_text: str) -> None:
        """Show an AlertDialog with the raw hyprctl error message."""
        dialog = Adw.AlertDialog()
        dialog.set_heading("Apply Failed")
        dialog.set_body(
            "hyprctl reported an error.\n"
            "The display state has been reverted to the last successful apply.\n\n"
            + error_text
        )
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.present(self)
