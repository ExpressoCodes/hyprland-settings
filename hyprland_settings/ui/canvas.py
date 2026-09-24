"""Monitor arrangement canvas widget.

A Gtk.DrawingArea subclass that renders connected monitors as draggable,
labeled rectangles and emits signals when the user repositions them.

Signals
-------
monitor-moved(monitor_name: str, new_x: int, new_y: int)
    Emitted after a successful drag-and-drop that changed a monitor's position.

monitor-selected(monitor_name: str | None)
    Emitted when the user clicks a monitor rectangle (or clicks on empty canvas
    to deselect).
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import GLib, GObject, Gdk, Gtk  # noqa: E402

from hyprland_settings.backend.hyprctl import Monitor  # noqa: E402

# ---------------------------------------------------------------------------
# Accent colours – one per monitor index, cycling.
# ---------------------------------------------------------------------------

_ACCENT_COLORS: list[tuple[float, float, float]] = [
    (0.31, 0.45, 0.82),  # blue
    (0.24, 0.65, 0.54),  # teal
    (0.78, 0.40, 0.23),  # orange
    (0.65, 0.32, 0.75),  # purple
    (0.82, 0.72, 0.22),  # yellow
    (0.29, 0.68, 0.31),  # green
    (0.80, 0.28, 0.38),  # red
    (0.25, 0.58, 0.78),  # sky
]

_SNAP_THRESHOLD_LOGICAL = 24  # logical pixels


def _accent(index: int, alpha: float = 1.0) -> tuple[float, float, float, float]:
    r, g, b = _ACCENT_COLORS[index % len(_ACCENT_COLORS)]
    return r, g, b, alpha


# ---------------------------------------------------------------------------
# Internal drag state
# ---------------------------------------------------------------------------

class _DragState:
    """Mutable drag operation state."""

    def __init__(
        self,
        monitor_name: str,
        start_canvas_x: float,
        start_canvas_y: float,
        monitor_origin_lx: int,
        monitor_origin_ly: int,
    ) -> None:
        self.monitor_name = monitor_name
        self.start_canvas_x = start_canvas_x
        self.start_canvas_y = start_canvas_y
        self.monitor_origin_lx = monitor_origin_lx
        self.monitor_origin_ly = monitor_origin_ly
        # Current ghost position in logical coords.
        self.ghost_lx: int = monitor_origin_lx
        self.ghost_ly: int = monitor_origin_ly
        # Snapped-to logical coordinate (None = no snap active on that axis)
        self.snap_guide_x: Optional[int] = None
        self.snap_guide_y: Optional[int] = None


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------

class MonitorCanvas(Gtk.DrawingArea):
    """Visual drag-and-drop monitor arrangement canvas."""

    # ------------------------------------------------------------------
    # GObject signal registration
    # ------------------------------------------------------------------

    __gsignals__ = {
        "monitor-moved": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (str, int, int),  # monitor_name, new_x, new_y
        ),
        "monitor-selected": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (object,),  # monitor_name: str | None  (object allows None)
        ),
    }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, editable: bool = True) -> None:
        super().__init__()

        self._editable = editable
        self._monitors: list[Monitor] = []
        self._selected_name: Optional[str] = None
        self._snap_enabled: bool = True

        # Viewport state
        self._zoom: float = 1.0
        self._pan_x: float = 0.0   # logical px visible at canvas left
        self._pan_y: float = 0.0   # logical px visible at canvas top
        self._canvas_origin_x: float = 0.0  # canvas px that maps to pan_x
        self._canvas_origin_y: float = 0.0  # canvas px that maps to pan_y

        # Drag state
        self._drag: Optional[_DragState] = None

        # Pan-via-middle-click state
        self._pan_drag_active: bool = False
        self._pan_drag_start_canvas: tuple[float, float] = (0.0, 0.0)
        self._pan_drag_start_pan: tuple[float, float] = (0.0, 0.0)

        # Space-bar pressed (alternative pan modifier)
        self._space_pressed: bool = False

        # Set up drawing callback
        self.set_draw_func(self._on_draw)

        # Allow the widget to receive keyboard events
        self.set_focusable(True)
        self.set_can_focus(True)

        # --- Event controllers ---

        # Keyboard (space bar pan + ctrl detection)
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        key_ctrl.connect("key-released", self._on_key_released)
        self.add_controller(key_ctrl)

        # Scroll (zoom + middle-click fallback)
        scroll_ctrl = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES
            | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll_ctrl.connect("scroll", self._on_scroll)
        self.add_controller(scroll_ctrl)

        # Click (select + middle-click pan start)
        click_ctrl = Gtk.GestureClick.new()
        click_ctrl.set_button(0)  # listen to all buttons
        click_ctrl.connect("pressed", self._on_button_pressed)
        click_ctrl.connect("released", self._on_button_released)
        self.add_controller(click_ctrl)

        # Motion (drag ghost update + pan update)
        motion_ctrl = Gtk.EventControllerMotion.new()
        motion_ctrl.connect("motion", self._on_motion)
        self.add_controller(motion_ctrl)

        # Zoom gesture (pinch)
        zoom_ctrl = Gtk.GestureZoom.new()
        zoom_ctrl.connect("scale-changed", self._on_zoom_gesture)
        self._zoom_gesture_start: Optional[float] = None
        self._zoom_gesture_start_zoom: float = 1.0
        zoom_ctrl.connect("begin", self._on_zoom_gesture_begin)
        self.add_controller(zoom_ctrl)

        # Size allocation — re-run auto-fit when first sized.
        self._initial_fit_done = False
        self.connect("realize", self._on_realize)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_monitors(self, monitors: list[Monitor]) -> None:
        """Replace displayed monitors. Triggers redraw."""
        if self._drag is not None:
            # Don't reset pan/zoom during a live drag; just update list.
            by_name = {m.name: m for m in self._monitors}
            new_by_name = {m.name: m for m in monitors}
            by_name.update(new_by_name)
            self._monitors = list(by_name.values())
        else:
            self._monitors = list(monitors)
            if not self._initial_fit_done:
                self.reset_view()
        self.queue_draw()

    def get_monitors(self) -> list[Monitor]:
        """Return monitors with updated positions from drag."""
        return list(self._monitors)

    def set_selected(self, name: Optional[str]) -> None:
        """Highlight the named monitor (called from sidebar selection)."""
        self._selected_name = name
        self.queue_draw()

    def set_snap_enabled(self, enabled: bool) -> None:
        """Toggle edge snapping."""
        self._snap_enabled = enabled

    def reset_view(self) -> None:
        """Auto-fit all monitors in the viewport with 40px padding."""
        if not self._monitors:
            self._zoom = 1.0
            self._pan_x = 0.0
            self._pan_y = 0.0
            self._canvas_origin_x = 40.0
            self._canvas_origin_y = 40.0
            self.queue_draw()
            return

        w = self.get_width() or 800
        h = self.get_height() or 600

        min_lx = min(m.x for m in self._monitors)
        min_ly = min(m.y for m in self._monitors)
        max_lx = max(m.x + m.logical_width for m in self._monitors)
        max_ly = max(m.y + m.logical_height for m in self._monitors)

        span_lw = max(max_lx - min_lx, 1)
        span_lh = max(max_ly - min_ly, 1)

        pad = 40.0
        zoom_x = (w - 2 * pad) / span_lw
        zoom_y = (h - 2 * pad) / span_lh
        self._zoom = max(0.10, min(2.0, min(zoom_x, zoom_y)))

        # Centre the logical bounding box.
        scaled_w = span_lw * self._zoom
        scaled_h = span_lh * self._zoom
        self._canvas_origin_x = (w - scaled_w) / 2
        self._canvas_origin_y = (h - scaled_h) / 2
        self._pan_x = float(min_lx)
        self._pan_y = float(min_ly)

        self._initial_fit_done = True
        self.queue_draw()

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def logical_to_canvas(self, lx: float, ly: float) -> tuple[float, float]:
        """Convert Hyprland logical coords to canvas pixel coords."""
        cx = (lx - self._pan_x) * self._zoom + self._canvas_origin_x
        cy = (ly - self._pan_y) * self._zoom + self._canvas_origin_y
        return cx, cy

    def canvas_to_logical(self, cx: float, cy: float) -> tuple[int, int]:
        """Inverse of logical_to_canvas."""
        lx = (cx - self._canvas_origin_x) / self._zoom + self._pan_x
        ly = (cy - self._canvas_origin_y) / self._zoom + self._pan_y
        return round(lx), round(ly)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _monitor_at_canvas(self, cx: float, cy: float) -> Optional[Monitor]:
        """Return the topmost monitor whose canvas rect contains (cx, cy)."""
        lx, ly = self.canvas_to_logical(cx, cy)
        # Iterate reversed so the selected (top z-order) is checked first.
        candidates = sorted(
            self._monitors,
            key=lambda m: (0 if m.name == self._selected_name else 1),
        )
        for mon in candidates:
            if mon.x <= lx < mon.x + mon.logical_width and mon.y <= ly < mon.y + mon.logical_height:
                return mon
        return None

    def _snap_position(
        self, name: str, lx: int, ly: int, lw: int, lh: int
    ) -> tuple[int, int, Optional[int], Optional[int]]:
        """Snap (lx, ly) to nearby monitor edges if within threshold.

        Returns (snapped_lx, snapped_ly, guide_x, guide_y) where guide_x/y are
        the snapped logical coordinate on each axis (None = no snap on that axis).
        """
        if not self._snap_enabled:
            return lx, ly, None, None

        best_dx = _SNAP_THRESHOLD_LOGICAL + 1
        best_dy = _SNAP_THRESHOLD_LOGICAL + 1
        snapped_x = lx
        snapped_y = ly
        guide_x: Optional[int] = None
        guide_y: Optional[int] = None

        for other in self._monitors:
            if other.name == name:
                continue
            ox, oy = other.x, other.y
            ow, oh = other.logical_width, other.logical_height

            # Candidate snap X: align own left/right to other's left/right
            for candidate_x, gx in (
                (ox, ox),           # our left → other's left
                (ox + ow, ox + ow), # our left → other's right
                (ox - lw, ox),      # our right → other's left
                (ox + ow - lw, ox + ow),  # our right → other's right
            ):
                dx = abs(lx - candidate_x)
                if dx < best_dx:
                    best_dx = dx
                    snapped_x = candidate_x
                    guide_x = gx

            # Candidate snap Y: align own top/bottom to other's top/bottom
            for candidate_y, gy in (
                (oy, oy),
                (oy + oh, oy + oh),
                (oy - lh, oy),
                (oy + oh - lh, oy + oh),
            ):
                dy = abs(ly - candidate_y)
                if dy < best_dy:
                    best_dy = dy
                    snapped_y = candidate_y
                    guide_y = gy

        if best_dx <= _SNAP_THRESHOLD_LOGICAL:
            lx = snapped_x
        else:
            guide_x = None

        if best_dy <= _SNAP_THRESHOLD_LOGICAL:
            ly = snapped_y
        else:
            guide_y = None

        return lx, ly, guide_x, guide_y

    def _update_monitor_position(self, name: str, lx: int, ly: int) -> None:
        """Update a monitor's position in the internal list."""
        self._monitors = [
            replace(m, x=lx, y=ly) if m.name == name else m
            for m in self._monitors
        ]

    def _index_of(self, name: str) -> int:
        for i, m in enumerate(self._monitors):
            if m.name == name:
                return i
        return 0

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _on_draw(self, area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        """Cairo draw callback."""
        self._draw_background(cr, width, height)
        self._draw_grid(cr, width, height)
        self._draw_monitors(cr)
        if self._drag is not None:
            self._draw_ghost(cr)
            self._draw_snap_guides(cr, width, height)

    def _draw_background(self, cr, width: int, height: int) -> None:
        cr.set_source_rgb(0.118, 0.118, 0.18)  # #1e1e2e
        cr.paint()

    def _draw_grid(self, cr, width: int, height: int) -> None:
        """Draw a subtle pixel-ruler grid every 100 logical pixels."""
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.05)
        cr.set_line_width(0.5)

        # Logical coordinate range visible.
        lx0, ly0 = self.canvas_to_logical(0, 0)
        lx1, ly1 = self.canvas_to_logical(width, height)

        step = 100
        # Vertical lines.
        x = (lx0 // step) * step
        while x <= lx1:
            cx, _ = self.logical_to_canvas(x, 0)
            cr.move_to(cx, 0)
            cr.line_to(cx, height)
            cr.stroke()
            x += step

        # Horizontal lines.
        y = (ly0 // step) * step
        while y <= ly1:
            _, cy = self.logical_to_canvas(0, y)
            cr.move_to(0, cy)
            cr.line_to(width, cy)
            cr.stroke()
            y += step

        # Major labels every 500 px.
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.18)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(10)

        major = 500
        x = (lx0 // major) * major
        while x <= lx1:
            cx, _ = self.logical_to_canvas(x, 0)
            cr.move_to(cx + 2, 12)
            cr.show_text(str(x))
            x += major

        y = (ly0 // major) * major
        while y <= ly1:
            _, cy = self.logical_to_canvas(0, y)
            cr.move_to(2, cy + 12)
            cr.show_text(str(y))
            y += major

    def _draw_monitors(self, cr) -> None:
        """Draw all monitor rectangles in z-order: mirrored first, then normal, then selected."""
        def _z_key(m: Monitor) -> int:
            if m.name == self._selected_name:
                return 2  # topmost
            if bool(m.mirror_of):
                return 0  # bottom layer (peeking card)
            return 1

        order = sorted(self._monitors, key=_z_key)
        for i, mon in enumerate(order):
            # During drag we draw a ghost instead of the real rect.
            if self._drag and self._drag.monitor_name == mon.name:
                continue
            idx = self._index_of(mon.name)
            self._draw_monitor_rect(cr, mon, idx, ghost=False)

    def _draw_monitor_rect(
        self,
        cr,
        mon: Monitor,
        color_index: int,
        ghost: bool = False,
        lx: Optional[int] = None,
        ly: Optional[int] = None,
    ) -> None:
        """Draw a single monitor rectangle."""
        pos_lx = lx if lx is not None else mon.x
        pos_ly = ly if ly is not None else mon.y

        cx, cy = self.logical_to_canvas(pos_lx, pos_ly)
        cw = mon.logical_width * self._zoom
        ch = mon.logical_height * self._zoom

        is_selected = (mon.name == self._selected_name)
        is_disabled = mon.disabled
        is_mirrored = bool(mon.mirror_of)

        # Stacked-card offset: mirrored monitors peek out +10 canvas px right/down.
        if is_mirrored and not ghost:
            cx += 10.0
            cy += 10.0

        alpha = 0.35 if ghost else (0.50 if is_disabled else (0.65 if is_mirrored else 0.85))
        r, g, b, a = _accent(color_index, alpha)

        # --- Fill ---
        cr.save()
        self._rounded_rect(cr, cx, cy, cw, ch, 4.0)
        cr.set_source_rgba(r, g, b, a)
        cr.fill_preserve()

        # --- Border ---
        border_alpha = 1.0 if not ghost else 0.6
        if is_selected:
            cr.set_source_rgba(1.0, 1.0, 1.0, border_alpha)
            cr.set_line_width(3.0)
        elif is_disabled or is_mirrored:
            cr.set_source_rgba(0.8, 0.8, 0.8, 0.5 * border_alpha)
            cr.set_line_width(1.5)
            cr.set_dash([6.0, 4.0], 0)
        else:
            cr.set_source_rgba(r + 0.2, g + 0.2, b + 0.2, border_alpha)
            cr.set_line_width(2.0)
        cr.stroke()
        cr.set_dash([], 0)

        if ghost:
            cr.restore()
            return

        # --- Labels ---
        # Line 1: monitor name
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.9)
        cr.select_font_face("Sans", 0, 1)  # bold
        font_size = max(10.0, min(16.0, cw / 8))
        cr.set_font_size(font_size)

        name_label = mon.name
        te = cr.text_extents(name_label)
        tx = cx + (cw - te.width) / 2 - te.x_bearing
        ty = cy + ch / 2 - font_size * 0.6

        # Slight text shadow
        cr.set_source_rgba(0, 0, 0, 0.5)
        cr.move_to(tx + 1, ty + 1)
        cr.show_text(name_label)

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
        cr.move_to(tx, ty)
        cr.show_text(name_label)

        # Line 2: mirror label or resolution@hz ×scale
        cr.select_font_face("Sans", 0, 0)
        small_size = max(8.0, font_size * 0.72)
        cr.set_font_size(small_size)
        if is_mirrored:
            info = f"↪ mirror of {mon.mirror_of}"
        else:
            hz = f"{mon.refresh_rate:g}"
            info = f"{mon.width}×{mon.height}@{hz}Hz ×{mon.scale:g}"
        te2 = cr.text_extents(info)
        tx2 = cx + (cw - te2.width) / 2 - te2.x_bearing
        ty2 = ty + font_size * 1.3

        cr.set_source_rgba(0, 0, 0, 0.4)
        cr.move_to(tx2 + 1, ty2 + 1)
        cr.show_text(info)

        cr.set_source_rgba(1.0, 1.0, 1.0, 0.80)
        cr.move_to(tx2, ty2)
        cr.show_text(info)

        # Mirror indicator: chain icon (simple text)
        if is_mirrored:
            self._draw_chain_overlay(cr, cx, cy, cw, ch)

        cr.restore()

    def _draw_snap_guides(self, cr, width: int, height: int) -> None:
        """Draw alignment guide lines when snap is active during drag."""
        if self._drag is None:
            return
        cr.save()
        cr.set_source_rgba(1.0, 0.85, 0.2, 0.85)  # amber
        cr.set_line_width(1.5)
        cr.set_dash([6.0, 4.0], 0)

        if self._drag.snap_guide_x is not None:
            cx, _ = self.logical_to_canvas(self._drag.snap_guide_x, 0)
            cr.move_to(cx, 0)
            cr.line_to(cx, height)
            cr.stroke()

        if self._drag.snap_guide_y is not None:
            _, cy = self.logical_to_canvas(0, self._drag.snap_guide_y)
            cr.move_to(0, cy)
            cr.line_to(width, cy)
            cr.stroke()

        cr.restore()

    def _draw_chain_overlay(self, cr, cx: float, cy: float, cw: float, ch: float) -> None:
        """Draw a small chain symbol to indicate mirroring."""
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.7)
        cr.select_font_face("Monospace", 0, 0)
        cr.set_font_size(14)
        label = "⛓"
        te = cr.text_extents(label)
        cr.move_to(cx + cw - te.width - 6, cy + 18)
        cr.show_text(label)

    def _draw_ghost(self, cr) -> None:
        """Draw the ghost rectangle during drag."""
        if self._drag is None:
            return
        mon = next((m for m in self._monitors if m.name == self._drag.monitor_name), None)
        if mon is None:
            return
        idx = self._index_of(mon.name)
        self._draw_monitor_rect(
            cr, mon, idx, ghost=True,
            lx=self._drag.ghost_lx,
            ly=self._drag.ghost_ly,
        )

    @staticmethod
    def _rounded_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
        """Append a rounded rectangle path to the current Cairo context."""
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_realize(self, widget) -> None:
        if not self._initial_fit_done and self._monitors:
            self.reset_view()

    def _on_key_pressed(self, ctrl, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_space:
            self._space_pressed = True
            return True
        return False

    def _on_key_released(self, ctrl, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_space:
            self._space_pressed = False
            self._pan_drag_active = False
            return True
        return False

    def _on_scroll(self, ctrl, dx: float, dy: float) -> bool:
        """Handle scroll events: Ctrl+scroll zooms, plain scroll pans."""
        state = ctrl.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            # Zoom around cursor position.
            seq = ctrl.get_current_event()
            # Fallback: zoom around centre.
            w = self.get_width() or 800
            h = self.get_height() or 600
            cx, cy = w / 2, h / 2

            old_zoom = self._zoom
            factor = 0.9 if dy > 0 else (1.0 / 0.9)
            new_zoom = max(0.10, min(2.0, old_zoom * factor))
            if new_zoom == old_zoom:
                return True

            # Keep the logical point under the cursor fixed.
            lx, ly = self.canvas_to_logical(cx, cy)
            self._zoom = new_zoom
            new_cx, new_cy = self.logical_to_canvas(lx, ly)
            self._canvas_origin_x += cx - new_cx
            self._canvas_origin_y += cy - new_cy
            self.queue_draw()
            return True
        return False

    def _on_button_pressed(self, ctrl: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        button = ctrl.get_current_button()

        if button == 2 or self._space_pressed:
            # Start pan.
            self._pan_drag_active = True
            self._pan_drag_start_canvas = (x, y)
            self._pan_drag_start_pan = (self._canvas_origin_x, self._canvas_origin_y)
            self.grab_focus()
            return

        if button != 1:
            return

        if not self._editable:
            # Read-only: just select.
            mon = self._monitor_at_canvas(x, y)
            name = mon.name if mon else None
            self._selected_name = name
            self.emit("monitor-selected", name)
            self.queue_draw()
            return

        mon = self._monitor_at_canvas(x, y)
        if mon is None:
            self._selected_name = None
            self.emit("monitor-selected", None)
            self.queue_draw()
            return

        # Mirrored monitors cannot be repositioned independently.
        self._selected_name = mon.name
        self.emit("monitor-selected", mon.name)
        if bool(mon.mirror_of):
            self.queue_draw()
            return

        # Start drag.
        self._drag = _DragState(
            monitor_name=mon.name,
            start_canvas_x=x,
            start_canvas_y=y,
            monitor_origin_lx=mon.x,
            monitor_origin_ly=mon.y,
        )
        self._drag.ghost_lx = mon.x
        self._drag.ghost_ly = mon.y
        self.grab_focus()
        self.queue_draw()

    def _on_button_released(self, ctrl: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        button = ctrl.get_current_button()

        if button == 2 or (self._space_pressed and self._pan_drag_active):
            self._pan_drag_active = False
            return

        if button != 1:
            return

        if self._drag is None:
            return

        # Finalise drag.
        name = self._drag.monitor_name
        new_lx = self._drag.ghost_lx
        new_ly = self._drag.ghost_ly

        self._update_monitor_position(name, new_lx, new_ly)
        self.emit("monitor-moved", name, new_lx, new_ly)
        self._drag = None
        self.queue_draw()

    def _on_motion(self, ctrl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        # Pan update.
        if self._pan_drag_active:
            start_x, start_y = self._pan_drag_start_canvas
            origin_x, origin_y = self._pan_drag_start_pan
            self._canvas_origin_x = origin_x + (x - start_x)
            self._canvas_origin_y = origin_y + (y - start_y)
            self.queue_draw()
            return

        # Drag update.
        if self._drag is None:
            return

        dx_canvas = x - self._drag.start_canvas_x
        dy_canvas = y - self._drag.start_canvas_y
        dx_logical = dx_canvas / self._zoom
        dy_logical = dy_canvas / self._zoom

        new_lx = self._drag.monitor_origin_lx + round(dx_logical)
        new_ly = self._drag.monitor_origin_ly + round(dy_logical)

        # Find the dragged monitor's dimensions for snap.
        mon = next((m for m in self._monitors if m.name == self._drag.monitor_name), None)
        if mon is not None:
            new_lx, new_ly, gx, gy = self._snap_position(
                self._drag.monitor_name, new_lx, new_ly,
                mon.logical_width, mon.logical_height,
            )
            self._drag.snap_guide_x = gx
            self._drag.snap_guide_y = gy
        else:
            self._drag.snap_guide_x = None
            self._drag.snap_guide_y = None

        self._drag.ghost_lx = new_lx
        self._drag.ghost_ly = new_ly
        self.queue_draw()

    def _on_zoom_gesture_begin(self, ctrl: Gtk.GestureZoom, seq) -> None:
        self._zoom_gesture_start = None
        self._zoom_gesture_start_zoom = self._zoom

    def _on_zoom_gesture(self, ctrl: Gtk.GestureZoom, scale: float) -> None:
        if scale <= 0:
            return
        new_zoom = max(0.10, min(2.0, self._zoom_gesture_start_zoom * scale))
        w = self.get_width() or 800
        h = self.get_height() or 600
        cx, cy = w / 2, h / 2
        lx, ly = self.canvas_to_logical(cx, cy)
        self._zoom = new_zoom
        new_cx, new_cy = self.logical_to_canvas(lx, ly)
        self._canvas_origin_x += cx - new_cx
        self._canvas_origin_y += cy - new_cy
        self.queue_draw()
