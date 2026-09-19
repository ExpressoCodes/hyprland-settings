"""Per-monitor settings sidebar panel.

Implements MonitorSidebar — a Gtk.Box subclass that shows and edits settings
for the currently selected monitor.  It only mutates the in-memory Monitor
object and emits "monitor-changed"; it never writes to hyprland.conf or calls
hyprctl.
"""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk

from hyprland_settings.backend.hyprctl import Monitor


class MonitorSidebar(Gtk.Box):
    """Side panel for editing per-monitor settings.

    Signals
    -------
    monitor-changed(monitor: Monitor)
        Emitted immediately after any field change.  The in-memory Monitor
        object passed as the argument already reflects the change.

    Public API
    ----------
    set_monitor(monitor | None)
        Populate the panel with the given monitor's data, or switch to the
        empty / no-selection state.

    get_monitor() -> Monitor | None
        Return the current monitor (already mutated by UI edits).

    set_modes(dict[tuple[int,int], list[float]])
        Provide available (width, height) -> [refresh_rate, …] mode map for
        the current monitor so the dropdowns can be fully populated.  Must be
        called before set_monitor() to take effect; calling it after re-populates
        the dropdowns in place.

    set_other_monitors(list[Monitor])
        Provide all other connected monitors so the mirror-source dropdown can
        list them.

    is_default_workspace() -> bool
        Whether the star-badge (default workspace) toggle is active.

    set_default_workspace(bool)
        Set the star-badge toggle without emitting signals.
    """

    __gtype_name__ = "MonitorSidebar"

    __gsignals__ = {
        "monitor-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self._monitor: Monitor | None = None
        self._other_monitors: list[Monitor] = []
        self._available_modes: list[str] = []
        self._res_row_data: dict = {}
        self._rate_row_data: dict = {}
        self._res_list_signal: bool = False
        self._rate_list_signal: bool = False
        # Guard flag: when True, signal handlers do nothing (used during bulk
        # population to prevent cascading partial-state emissions).
        self._suppress_signals: bool = False

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction (called once from __init__)
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Top-level stack: "empty" vs "content"
        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self.append(self._stack)

        # --- Empty state ---
        empty_page = Adw.StatusPage()
        empty_page.set_icon_name("video-display-symbolic")
        empty_page.set_title("No monitor selected")
        empty_page.set_description("Select a monitor on the canvas to edit its settings.")
        self._stack.add_named(empty_page, "empty")

        # --- Content (scrollable preferences page) ---
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._page = Adw.PreferencesPage()
        scroll.set_child(self._page)
        self._stack.add_named(scroll, "content")

        self._build_header_group()
        self._build_enable_group()
        self._build_display_group()
        self._build_rotation_group()
        self._build_mirror_group()
        self._build_workspace_group()

        self._connect_signals()

        # Default to empty
        self._stack.set_visible_child_name("empty")

    def _build_header_group(self) -> None:
        group = Adw.PreferencesGroup()
        self._page.add(group)

        self._header_row = Adw.ActionRow()
        self._header_row.set_title("Monitor")
        self._header_row.set_subtitle("")
        # Make the title look like a heading
        self._header_row.add_css_class("property")
        group.add(self._header_row)

    def _build_enable_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Status")
        self._page.add(group)

        self._active_row = Adw.SwitchRow()
        self._active_row.set_title("Active")
        self._active_row.set_subtitle("Enable or disable this monitor")
        group.add(self._active_row)

    @staticmethod
    def _make_suggestion_list() -> Gtk.ListBox:
        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        return lb

    @staticmethod
    def _make_suggestion_scroll(list_box: Gtk.ListBox) -> Gtk.ScrolledWindow:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(200, -1)
        scroll.set_max_content_height(260)
        scroll.set_propagate_natural_height(True)
        scroll.set_child(list_box)
        return scroll

    def _build_display_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Display")
        self._page.add(group)
        self._display_group = group

        # Resolution entry row with suggestions popover
        self._res_row = Adw.EntryRow()
        self._res_row.set_title("Resolution")
        self._res_row.set_input_purpose(Gtk.InputPurpose.FREE_FORM)

        self._res_popover = Gtk.Popover()
        self._res_popover.set_has_arrow(True)
        self._res_list = self._make_suggestion_list()
        res_scroll = self._make_suggestion_scroll(self._res_list)
        self._res_popover.set_child(res_scroll)

        self._res_btn = Gtk.MenuButton()
        self._res_btn.set_icon_name("pan-down-symbolic")
        self._res_btn.set_tooltip_text("Show available resolutions")
        self._res_btn.set_valign(Gtk.Align.CENTER)
        self._res_btn.add_css_class("flat")
        self._res_btn.set_popover(self._res_popover)
        self._res_btn.set_visible(False)
        self._res_row.add_suffix(self._res_btn)

        group.add(self._res_row)

        # Refresh rate entry row with suggestions popover
        self._rate_row = Adw.EntryRow()
        self._rate_row.set_title("Refresh Rate (Hz)")
        self._rate_row.set_input_purpose(Gtk.InputPurpose.NUMBER)

        self._rate_popover = Gtk.Popover()
        self._rate_popover.set_has_arrow(True)
        self._rate_list = self._make_suggestion_list()
        rate_scroll = self._make_suggestion_scroll(self._rate_list)
        self._rate_popover.set_child(rate_scroll)

        self._rate_btn = Gtk.MenuButton()
        self._rate_btn.set_icon_name("pan-down-symbolic")
        self._rate_btn.set_tooltip_text("Show available refresh rates")
        self._rate_btn.set_valign(Gtk.Align.CENTER)
        self._rate_btn.add_css_class("flat")
        self._rate_btn.set_popover(self._rate_popover)
        self._rate_btn.set_visible(False)
        self._rate_row.add_suffix(self._rate_btn)

        group.add(self._rate_row)

        # Scale factor spin row
        self._scale_adj = Gtk.Adjustment(
            value=1.0,
            lower=0.5,
            upper=4.0,
            step_increment=0.25,
            page_increment=0.5,
            page_size=0.0,
        )
        self._scale_row = Adw.SpinRow()
        self._scale_row.set_title("Scale Factor")
        self._scale_row.set_subtitle("Effective: — logical px")
        self._scale_row.set_adjustment(self._scale_adj)
        self._scale_row.set_digits(2)
        self._scale_row.set_snap_to_ticks(False)

        # Fractional-scaling advisory icon (hidden by default)
        self._scale_warning = Gtk.Image()
        self._scale_warning.set_from_icon_name("dialog-warning-symbolic")
        self._scale_warning.set_tooltip_text(
            "Non-0.5 multiple: fractional scaling may cause blurriness "
            "in apps that do not support xdg-fractional-scale-v1."
        )
        self._scale_warning.set_visible(False)
        self._scale_row.add_suffix(self._scale_warning)
        group.add(self._scale_row)

    def _build_rotation_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Rotation")
        self._page.add(group)
        self._rotation_group = group

        # --- Rotate row: D-pad arranged like a Nintendo C-pad ---
        self._rotation_row = Adw.ActionRow()
        self._rotation_row.set_title("Rotate")

        # 3×3 grid — buttons at N/E/S/W, centre left empty
        dpad = Gtk.Grid()
        dpad.set_column_spacing(3)
        dpad.set_row_spacing(3)
        dpad.set_valign(Gtk.Align.CENTER)
        dpad.set_halign(Gtk.Align.END)
        dpad.set_margin_top(8)
        dpad.set_margin_bottom(8)

        # (base_rotation_index, label, tooltip, grid_col, grid_row)
        _dirs = [
            (0, "↑", "0° — normal",        1, 0),
            (1, "→", "90° clockwise",       2, 1),
            (2, "↓", "180°",                1, 2),
            (3, "←", "270° clockwise",      0, 1),
        ]

        self._rotation_buttons: list[Gtk.ToggleButton] = []
        first_btn: Gtk.ToggleButton | None = None
        for rot_val, label, tip, col, row in _dirs:
            btn = Gtk.ToggleButton()
            btn.set_label(label)
            btn.set_tooltip_text(tip)
            btn.set_size_request(36, 36)
            btn._rotation_value = rot_val  # type: ignore[attr-defined]
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            dpad.attach(btn, col, row, 1, 1)
            self._rotation_buttons.append(btn)

        self._rotation_row.add_suffix(dpad)
        group.add(self._rotation_row)

        # --- Flip row: a simple switch for horizontal mirror ---
        self._flip_row = Adw.ActionRow()
        self._flip_row.set_title("Flip")
        self._flip_row.set_subtitle("Mirror the output horizontally")

        self._flip_switch = Gtk.Switch()
        self._flip_switch.set_valign(Gtk.Align.CENTER)
        self._flip_row.add_suffix(self._flip_switch)
        self._flip_row.set_activatable_widget(self._flip_switch)
        group.add(self._flip_row)

    def _build_mirror_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Mirror")
        self._page.add(group)
        self._mirror_group = group

        self._mirror_model = Gtk.StringList.new(["None"])
        self._mirror_row = Adw.ComboRow()
        self._mirror_row.set_title("Mirror Source")
        self._mirror_row.set_subtitle("Mirror another monitor's output")
        self._mirror_row.set_model(self._mirror_model)
        group.add(self._mirror_row)

    def _build_workspace_group(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Workspace")
        self._page.add(group)

        self._default_ws_row = Adw.ActionRow()
        self._default_ws_row.set_title("Default Workspace Monitor")
        self._default_ws_row.set_subtitle(
            "This monitor receives workspace 1 on startup"
        )
        self._default_ws_row.set_tooltip_text(
            "This monitor receives workspace 1 on startup."
        )

        self._default_ws_btn = Gtk.ToggleButton()
        self._default_ws_btn.set_icon_name("starred-symbolic")
        self._default_ws_btn.set_tooltip_text(
            "Mark as the default workspace monitor (receives workspace 1 on startup)"
        )
        self._default_ws_btn.set_valign(Gtk.Align.CENTER)
        self._default_ws_row.add_suffix(self._default_ws_btn)
        group.add(self._default_ws_row)

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._active_row.connect("notify::active", self._on_active_changed)
        self._res_row.connect("notify::text", self._on_resolution_changed)
        self._rate_row.connect("notify::text", self._on_rate_changed)
        self._scale_adj.connect("value-changed", self._on_scale_changed)
        self._mirror_row.connect("notify::selected", self._on_mirror_changed)
        self._default_ws_btn.connect("toggled", self._on_default_ws_toggled)
        for btn in self._rotation_buttons:
            btn.connect("toggled", self._on_rotation_toggled)
        self._flip_switch.connect("notify::active", self._on_flip_changed)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_active_changed(self, row: Adw.SwitchRow, _param: object) -> None:
        if self._suppress_signals or self._monitor is None:
            return
        self._monitor.disabled = not row.get_active()
        self._update_sensitivity()
        self.emit("monitor-changed", self._monitor)

    def _on_resolution_changed(self, row: Adw.EntryRow, _param: object) -> None:
        """Commit resolution immediately when valid; show error styling if not."""
        if self._suppress_signals or self._monitor is None:
            return
        if self._apply_resolution_to_monitor():
            self._update_scale_subtitle()
            self._rebuild_rate_suggestions()
            self.emit("monitor-changed", self._monitor)

    def _on_rate_changed(self, row: Adw.EntryRow, _param: object) -> None:
        """Commit refresh rate immediately when valid; show error styling if not."""
        if self._suppress_signals or self._monitor is None:
            return
        if self._apply_rate_to_monitor():
            self.emit("monitor-changed", self._monitor)

    def _on_scale_changed(self, adj: Gtk.Adjustment) -> None:
        if self._suppress_signals or self._monitor is None:
            return
        val = adj.get_value()
        self._monitor.scale = val
        # Advisory icon: warn when value is not a multiple of 0.5
        is_half_multiple = abs(val * 2.0 - round(val * 2.0)) < 1e-9
        self._scale_warning.set_visible(not is_half_multiple)
        self._update_scale_subtitle()
        self.emit("monitor-changed", self._monitor)

    def _on_mirror_changed(self, row: Adw.ComboRow, _param: object) -> None:
        if self._suppress_signals or self._monitor is None:
            return
        selected = row.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected == 0:
            self._monitor.mirror_of = ""
        else:
            item = self._mirror_model.get_item(selected)
            self._monitor.mirror_of = item.get_string() if item is not None else ""
        self._update_mirror_sensitivity()
        self.emit("monitor-changed", self._monitor)

    def _on_rotation_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._suppress_signals or self._monitor is None:
            return
        if btn.get_active():
            flipped = self._flip_switch.get_active()
            self._monitor.transform = btn._rotation_value + (4 if flipped else 0)  # type: ignore[attr-defined]
            self.emit("monitor-changed", self._monitor)

    def _on_flip_changed(self, switch: Gtk.Switch, _param: object) -> None:
        if self._suppress_signals or self._monitor is None:
            return
        rot = next(
            (b._rotation_value for b in self._rotation_buttons if b.get_active()), 0  # type: ignore[attr-defined]
        )
        self._monitor.transform = rot + (4 if switch.get_active() else 0)
        self.emit("monitor-changed", self._monitor)

    def _on_default_ws_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._suppress_signals or self._monitor is None:
            return
        # The star toggle is purely metadata that the parent window tracks; we
        # still emit so the canvas can update its badge.
        self.emit("monitor-changed", self._monitor)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_resolution_to_monitor(self) -> bool:
        """Parse the resolution entry and write into self._monitor. Returns True on success."""
        text = self._res_row.get_text().strip()
        w, h = _parse_resolution(text)
        if w and h:
            self._monitor.width = w   # type: ignore[union-attr]
            self._monitor.height = h  # type: ignore[union-attr]
            self._res_row.remove_css_class("error")
            return True
        self._res_row.add_css_class("error")
        return False

    def _apply_rate_to_monitor(self) -> bool:
        """Parse the refresh rate entry and write into self._monitor. Returns True on success."""
        text = self._rate_row.get_text().strip()
        try:
            hz = float(text)
            self._monitor.refresh_rate = hz  # type: ignore[union-attr]
            self._rate_row.remove_css_class("error")
            return True
        except ValueError:
            self._rate_row.add_css_class("error")
            return False

    def _update_scale_subtitle(self) -> None:
        if self._monitor is None:
            self._scale_row.set_subtitle("Effective: — logical px")
            return
        lw = self._monitor.logical_width
        lh = self._monitor.logical_height
        self._scale_row.set_subtitle(f"Effective: {lw}×{lh} logical px")

    def _update_sensitivity(self) -> None:
        """Gray out setting rows when the monitor is disabled."""
        is_active = self._monitor is not None and not self._monitor.disabled
        self._res_row.set_sensitive(is_active)
        self._rate_row.set_sensitive(is_active)
        self._scale_row.set_sensitive(is_active)
        self._rotation_row.set_sensitive(is_active)
        self._flip_row.set_sensitive(is_active)
        self._mirror_row.set_sensitive(is_active)
        self._default_ws_row.set_sensitive(is_active)
        if is_active:
            self._update_mirror_sensitivity()

    def _update_mirror_sensitivity(self) -> None:
        """When mirroring, gray out resolution / refresh / scale."""
        is_mirroring = (
            self._monitor is not None
            and bool(self._monitor.mirror_of)
            and not self._monitor.disabled
        )
        self._res_row.set_sensitive(not is_mirroring)
        self._rate_row.set_sensitive(not is_mirroring)
        self._scale_row.set_sensitive(not is_mirroring)

    def set_modes(self, modes: list[str]) -> None:
        """Populate suggestions from Hyprland mode strings like '1920x1080@144.00Hz'."""
        self._available_modes = modes or []
        self._rebuild_res_suggestions()
        self._rebuild_rate_suggestions()

        if not self._res_list_signal:
            self._res_list.connect("row-activated", self._on_res_row_activated)
            self._res_list_signal = True
        if not self._rate_list_signal:
            self._rate_list.connect("row-activated", self._on_rate_row_activated)
            self._rate_list_signal = True

    def _parsed_modes(self) -> list[tuple[str, float]]:
        """Return all modes as (resolution, hz) sorted big→small, fast→slow."""
        parsed = [_parse_mode_string(m) for m in self._available_modes]
        items = [p for p in parsed if p is not None]

        def _key(item: tuple[str, float]) -> tuple[int, float]:
            res, hz = item
            w, h = _parse_resolution(res)
            return (-(w or 0) * (h or 0), -hz)

        items.sort(key=_key)
        return items

    def _rebuild_res_suggestions(self) -> None:
        """Repopulate the resolution list (unique resolutions only)."""
        _clear_list(self._res_list)
        self._res_row_data: dict[Gtk.ListBoxRow, str] = {}

        seen: set[str] = set()
        for res, _hz in self._parsed_modes():
            if res in seen:
                continue
            seen.add(res)
            w, h = _parse_resolution(res)
            pixels = f"{(w or 0) * (h or 0) // 1_000_000:.1f} MP"
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(12)
            box.set_margin_end(12)
            lbl = Gtk.Label(label=res)
            lbl.set_xalign(0)
            lbl.set_hexpand(True)
            sub = Gtk.Label(label=pixels)
            sub.add_css_class("dim-label")
            sub.add_css_class("caption")
            box.append(lbl)
            box.append(sub)
            row.set_child(box)
            self._res_row_data[row] = res
            self._res_list.append(row)

        self._res_btn.set_visible(bool(seen))

    def _rebuild_rate_suggestions(self) -> None:
        """Repopulate refresh-rate list filtered to the current resolution text."""
        _clear_list(self._rate_list)
        self._rate_row_data: dict[Gtk.ListBoxRow, float] = {}

        current_res = self._res_row.get_text().strip().lower().replace("×", "x")
        rates: list[float] = []
        for res, hz in self._parsed_modes():
            if res.lower() == current_res and hz not in rates:
                rates.append(hz)

        # If the exact resolution isn't matched yet (e.g. field is empty during
        # initial population), show all unique rates as a fallback so the button
        # is never invisible when modes are available.
        if not rates and self._available_modes:
            seen_hz: set[float] = set()
            for _res, hz in self._parsed_modes():
                if hz not in seen_hz:
                    seen_hz.add(hz)
                    rates.append(hz)

        for hz in rates:
            hz_int = int(hz)
            hz_label = str(hz_int) if hz == hz_int else f"{hz:g}"
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(12)
            box.set_margin_end(12)
            lbl = Gtk.Label(label=f"{hz_label} Hz")
            lbl.set_xalign(0)
            box.append(lbl)
            row.set_child(box)
            self._rate_row_data[row] = hz
            self._rate_list.append(row)

        self._rate_btn.set_visible(bool(rates))

    def _on_res_row_activated(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        res = self._res_row_data.get(row)
        if res is not None:
            self._res_popover.popdown()
            self._res_row.set_text(res)
            # After resolution changes, refresh the rate suggestions
            self._rebuild_rate_suggestions()

    def _on_rate_row_activated(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        hz = self._rate_row_data.get(row)
        if hz is not None:
            self._rate_popover.popdown()
            hz_int = int(hz)
            self._rate_row.set_text(str(hz_int) if hz == hz_int else f"{hz:g}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_monitor(self, monitor: Monitor | None) -> None:
        """Populate the sidebar with *monitor*'s data, or show the empty state."""
        self._monitor = monitor

        if monitor is None:
            self._stack.set_visible_child_name("empty")
            return

        self._stack.set_visible_child_name("content")
        self._suppress_signals = True
        try:
            self._populate(monitor)
        finally:
            self._suppress_signals = False

        self._update_sensitivity()

    def _populate(self, monitor: Monitor) -> None:
        """Fill all widgets from *monitor*.  Called with _suppress_signals=True."""
        # Header
        display_name = monitor.description or monitor.name
        self._header_row.set_title(display_name)
        self._header_row.set_subtitle(monitor.name)

        # Active switch
        self._active_row.set_active(not monitor.disabled)

        # Resolution and refresh rate text entries
        self._res_row.set_text(f"{monitor.width}x{monitor.height}")
        self._res_row.remove_css_class("error")
        hz_int = int(monitor.refresh_rate)
        hz_str = str(hz_int) if monitor.refresh_rate == hz_int else f"{monitor.refresh_rate:g}"
        self._rate_row.set_text(hz_str)
        self._rate_row.remove_css_class("error")

        # Scale
        self._scale_adj.set_value(monitor.scale)
        is_half_multiple = abs(monitor.scale * 2.0 - round(monitor.scale * 2.0)) < 1e-9
        self._scale_warning.set_visible(not is_half_multiple)
        self._update_scale_subtitle()

        # Rotation D-pad + flip switch (transform = rotation_idx + 4 if flipped)
        rotation_idx = monitor.transform % 4
        flipped = monitor.transform >= 4
        for btn in self._rotation_buttons:
            btn.set_active(btn._rotation_value == rotation_idx)  # type: ignore[attr-defined]
        self._flip_switch.set_active(flipped)

        # Rate suggestions depend on the resolution text — rebuild now that it's set
        self._rebuild_rate_suggestions()

        # Mirror dropdown
        self._repopulate_mirror(monitor.mirror_of)

    def _repopulate_mirror(self, current_mirror: str) -> None:
        n = self._mirror_model.get_n_items()
        if n:
            self._mirror_model.splice(0, n, [])
        self._mirror_model.append("None")
        mirror_idx = 0
        for i, other in enumerate(self._other_monitors):
            if self._monitor and other.name == self._monitor.name:
                continue
            self._mirror_model.append(other.name)
            if other.name == current_mirror:
                mirror_idx = i + 1
        self._mirror_row.set_selected(mirror_idx)

    def set_other_monitors(self, monitors: list[Monitor]) -> None:
        """Provide other connected monitors for the mirror-source dropdown."""
        self._other_monitors = monitors
        if self._monitor is not None:
            suppress = self._suppress_signals
            self._suppress_signals = True
            try:
                self._repopulate_mirror(self._monitor.mirror_of)
            finally:
                self._suppress_signals = suppress

    def get_monitor(self) -> Monitor | None:
        """Return the current monitor with all UI-edited fields applied."""
        return self._monitor

    def is_default_workspace(self) -> bool:
        """Return whether the star / default-workspace toggle is active."""
        return self._default_ws_btn.get_active()

    def set_default_workspace(self, value: bool) -> None:
        """Set the star-badge toggle without emitting signals."""
        suppress = self._suppress_signals
        self._suppress_signals = True
        try:
            self._default_ws_btn.set_active(value)
        finally:
            self._suppress_signals = suppress


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _clear_list(list_box: Gtk.ListBox) -> None:
    """Remove all children from a ListBox."""
    while (child := list_box.get_first_child()) is not None:
        list_box.remove(child)


def _parse_resolution(res_str: str) -> tuple[int | None, int | None]:
    """Parse a resolution string like '1920×1080' or '1920x1080'.

    Returns (width, height) as ints, or (None, None) on parse failure.
    """
    for sep in ("×", "x"):
        if sep in res_str:
            parts = res_str.split(sep, 1)
            if len(parts) == 2:
                try:
                    return int(parts[0].strip()), int(parts[1].strip())
                except ValueError:
                    pass
    return None, None


def _parse_mode_string(mode: str) -> tuple[str, float] | None:
    """Parse a Hyprland mode string like '1920x1080@144.00Hz' → ('1920x1080', 144.0).

    Returns None on parse failure.
    """
    mode = mode.strip()
    if mode.lower().endswith("hz"):
        mode = mode[:-2]
    if "@" not in mode:
        return None
    res, _, hz_str = mode.partition("@")
    try:
        return res.strip(), float(hz_str)
    except ValueError:
        return None
