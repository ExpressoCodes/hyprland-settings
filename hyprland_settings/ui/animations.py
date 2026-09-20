"""Animations settings page for Hyprland.

Implements AnimationsPage — an Adw.PreferencesPage subclass that shows and
edits Hyprland animation settings.  It reads from live hyprctl state or a
config file fallback, and can apply changes live or write them to the config.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import logging
import re
from pathlib import Path

from gi.repository import Adw, GObject, Gtk

from hyprland_settings.backend.config_writer import (
    read_section_from_config,
    write_section_to_config,
)
from hyprland_settings.backend.hyprctl import HyprctlApplyError, apply_keyword, get_option

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, object] = {
    "animations_enabled": True,
    "global": 14.29,
    "border": 7.7,
    "windows": 6.84,
    "windowsIn": 5.86,
    "windowsOut": 2.13,
    "fadeIn": 2.47,
    "fadeOut": 2.09,
    "fade": 4.33,
    "layers": 5.44,
    "workspaces": 2.77,
    "zoomFactor": 10.0,
}

# Default curve/spring metadata per leaf — used for subtitles and round-trip output.
# Keys within each dict: "bezier" | "spring" and optionally "style".
_DEFAULT_META: dict[str, dict[str, str]] = {
    "global":     {"bezier": "default"},
    "border":     {"bezier": "easeOutQuint"},
    "windows":    {"spring": "easy"},
    "windowsIn":  {"spring": "easy", "style": "popin 87%"},
    "windowsOut": {"bezier": "linear", "style": "popin 87%"},
    "fadeIn":     {"bezier": "almostLinear"},
    "fadeOut":    {"bezier": "almostLinear"},
    "fade":       {"bezier": "quick"},
    "layers":     {"bezier": "easeOutQuint"},
    "workspaces": {"bezier": "almostLinear", "style": "fade"},
    "zoomFactor": {"bezier": "quick"},
}

_LEAF_TITLES: dict[str, str] = {
    "global":     "Global",
    "border":     "Border",
    "windows":    "Windows",
    "windowsIn":  "Windows In",
    "windowsOut": "Windows Out",
    "fadeIn":     "Fade In",
    "fadeOut":    "Fade Out",
    "fade":       "Fade",
    "layers":     "Layers",
    "workspaces": "Workspaces",
    "zoomFactor": "Zoom Factor",
}

# Groups: (title, [leaf, ...])
_GROUPS: list[tuple[str, list[str]]] = [
    ("Window Animations", ["windows", "windowsIn", "windowsOut"]),
    ("Fade Animations",   ["fadeIn", "fadeOut", "fade"]),
    ("Other",             ["border", "layers", "workspaces", "zoomFactor"]),
]

# Canonical leaf order (matches config output order)
_ALL_LEAVES: list[str] = ["global"] + [
    leaf for _, leaves in _GROUPS for leaf in leaves
]

# ---------------------------------------------------------------------------
# Regex helpers for config parsing
# ---------------------------------------------------------------------------

# Matches: hl.config({ animations = { enabled = true/false } })
_CONFIG_ENABLED_RE = re.compile(
    r'hl\.config\(\{.*?animations\s*=\s*\{.*?enabled\s*=\s*(true|false)',
    re.DOTALL,
)

# Per-field regexes used on individual hl.animation(…) lines
_RE_LEAF    = re.compile(r'leaf\s*=\s*"([^"]+)"')
_RE_ENABLED = re.compile(r'enabled\s*=\s*(true|false)')
_RE_SPEED   = re.compile(r'speed\s*=\s*([\d.]+)')
_RE_BEZIER  = re.compile(r'bezier\s*=\s*"([^"]*)"')
_RE_SPRING  = re.compile(r'spring\s*=\s*"([^"]*)"')
_RE_STYLE   = re.compile(r'style\s*=\s*"([^"]*)"')


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _make_subtitle(meta: dict[str, str]) -> str:
    """Build a subtitle string from animation metadata, e.g. 'bezier: easeOutQuint · style: fade'."""
    parts: list[str] = []
    if "bezier" in meta:
        parts.append(f"bezier: {meta['bezier']}")
    if "spring" in meta:
        parts.append(f"spring: {meta['spring']}")
    if "style" in meta:
        parts.append(f"style: {meta['style']}")
    return " · ".join(parts)


def _make_speed_adj(default: float = 1.0) -> Gtk.Adjustment:
    return Gtk.Adjustment(
        value=default,
        lower=0.1,
        upper=50.0,
        step_increment=0.1,
        page_increment=1.0,
        page_size=0.0,
    )


def _parse_animation_line(line: str) -> dict | None:
    """Parse a single hl.animation(…) line.

    Returns a dict with keys: leaf, enabled (bool), speed (float),
    and optionally bezier, spring, style.  Returns None if not an animation line.
    """
    if "hl.animation(" not in line:
        return None

    m_leaf = _RE_LEAF.search(line)
    if not m_leaf:
        return None

    result: dict = {"leaf": m_leaf.group(1)}

    m_enabled = _RE_ENABLED.search(line)
    result["enabled"] = (m_enabled.group(1) == "true") if m_enabled else True

    m_speed = _RE_SPEED.search(line)
    if m_speed:
        try:
            result["speed"] = float(m_speed.group(1))
        except ValueError:
            pass

    m_bezier = _RE_BEZIER.search(line)
    if m_bezier:
        result["bezier"] = m_bezier.group(1)

    m_spring = _RE_SPRING.search(line)
    if m_spring:
        result["spring"] = m_spring.group(1)

    m_style = _RE_STYLE.search(line)
    if m_style:
        result["style"] = m_style.group(1)

    return result


# ---------------------------------------------------------------------------
# AnimationsPage
# ---------------------------------------------------------------------------


class AnimationsPage(Adw.PreferencesPage):
    """Settings page for Hyprland animation configuration.

    Signals
    -------
    settings-changed()
        Emitted when any widget value changes from the last loaded state.
        The window uses this to show/hide the "Unsaved changes" banner.

    Public API
    ----------
    load(config_path, hyprctl_available)
        Load current settings from live hyprctl state or config file fallback.

    collect_lines() -> list[str]
        Return Lua lines representing current UI state (to write to config).

    apply_live()
        Apply current settings live via hyprctl keyword calls.
    """

    __gtype_name__ = "AnimationsPage"

    __gsignals__ = {
        "settings-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()

        # Per-leaf metadata (bezier/spring/style) — updated from config for round-trip
        self._meta: dict[str, dict[str, str]] = {
            leaf: dict(meta) for leaf, meta in _DEFAULT_META.items()
        }
        # Per-leaf enabled flag (independent from the global switch)
        self._leaf_enabled: dict[str, bool] = {leaf: True for leaf in _DEFAULT_META}

        # Last-loaded state — used to determine whether anything has changed
        self._loaded_enabled: bool = True
        self._loaded_speeds: dict[str, float] = {
            leaf: float(DEFAULTS[leaf])  # type: ignore[arg-type]
            for leaf in _ALL_LEAVES
        }

        # Guard: suppresses signal emissions during bulk population
        self._suppress_signals: bool = False

        # Widget references keyed by leaf name
        self._speed_rows: dict[str, Adw.SpinRow] = {}
        self._speed_adjs: dict[str, Gtk.Adjustment] = {}

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction (called once from __init__)
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # --- Group 1: Global ---
        global_group = Adw.PreferencesGroup()
        global_group.set_title("Global")
        self.add(global_group)

        self._enabled_row = Adw.SwitchRow()
        self._enabled_row.set_title("Enable Animations")
        self._enabled_row.set_subtitle("Master switch for all Hyprland animations")
        global_group.add(self._enabled_row)

        self._add_speed_row(global_group, "global")

        # --- Groups 2-4: Window, Fade, Other ---
        for group_title, leaves in _GROUPS:
            group = Adw.PreferencesGroup()
            group.set_title(group_title)
            self.add(group)
            for leaf in leaves:
                self._add_speed_row(group, leaf)

        self._connect_signals()

    def _add_speed_row(self, group: Adw.PreferencesGroup, leaf: str) -> None:
        default_speed = float(DEFAULTS.get(leaf, 1.0))  # type: ignore[arg-type]
        adj = _make_speed_adj(default_speed)
        self._speed_adjs[leaf] = adj

        row = Adw.SpinRow()
        row.set_title(_LEAF_TITLES.get(leaf, leaf))
        row.set_subtitle(_make_subtitle(self._meta.get(leaf, {})))
        row.set_adjustment(adj)
        row.set_digits(2)
        row.set_snap_to_ticks(False)
        self._speed_rows[leaf] = row
        group.add(row)

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._enabled_row.connect("notify::active", self._on_value_changed)
        for adj in self._speed_adjs.values():
            adj.connect("value-changed", self._on_value_changed)

    def _on_value_changed(self, *_args: object) -> None:
        if self._suppress_signals:
            return
        self.emit("settings-changed")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, config_path: Path, hyprctl_available: bool) -> None:
        """Load current settings from live hyprctl state or config file fallback."""
        self._suppress_signals = True
        try:
            self._load_inner(config_path, hyprctl_available)
        finally:
            self._suppress_signals = False

    def _load_inner(self, config_path: Path, hyprctl_available: bool) -> None:
        # Start from defaults
        enabled: bool = bool(DEFAULTS["animations_enabled"])
        speeds: dict[str, float] = {
            leaf: float(DEFAULTS[leaf])  # type: ignore[arg-type]
            for leaf in _ALL_LEAVES
        }

        # ------------------------------------------------------------------
        # Step 1: Parse config file — always, to recover metadata for round-trip
        # and as a fallback source when hyprctl is unavailable.
        # ------------------------------------------------------------------
        config_enabled: bool | None = None
        config_speeds: dict[str, float] = {}

        try:
            config_lines = read_section_from_config("animations", config_path)
        except Exception:
            log.debug("Could not read animations section from %s", config_path, exc_info=True)
            config_lines = []

        if config_lines:
            full_text = "\n".join(config_lines)

            # Global enabled
            m = _CONFIG_ENABLED_RE.search(full_text)
            if m:
                config_enabled = m.group(1) == "true"

            # Animation leaf lines
            for line in config_lines:
                parsed = _parse_animation_line(line)
                if parsed is None:
                    continue
                leaf = parsed["leaf"]
                if leaf not in _DEFAULT_META:
                    continue

                # Update round-trip metadata
                meta: dict[str, str] = {}
                if "bezier" in parsed:
                    meta["bezier"] = parsed["bezier"]
                if "spring" in parsed:
                    meta["spring"] = parsed["spring"]
                if "style" in parsed:
                    meta["style"] = parsed["style"]
                if meta:
                    self._meta[leaf] = meta

                # Update per-leaf enabled flag
                self._leaf_enabled[leaf] = parsed["enabled"]

                # Record speed as config fallback
                if "speed" in parsed:
                    config_speeds[leaf] = parsed["speed"]

        # ------------------------------------------------------------------
        # Step 2: Populate enabled / speeds from hyprctl if available,
        #         falling back to config values parsed above.
        # ------------------------------------------------------------------
        if hyprctl_available:
            # Global enabled
            opt = get_option("animations:enabled")
            if opt:
                enabled = bool(opt.get("int", 1))
            else:
                enabled = config_enabled if config_enabled is not None else bool(DEFAULTS["animations_enabled"])

            # Per-leaf speeds
            for leaf in _ALL_LEAVES:
                opt = get_option(f"animations:{leaf}:speed")
                if opt:
                    raw = opt.get("float", opt.get("int"))
                    if raw is not None:
                        try:
                            speeds[leaf] = float(raw)
                            continue
                        except (TypeError, ValueError):
                            pass
                # Fall back to config or default
                speeds[leaf] = config_speeds.get(leaf, float(DEFAULTS.get(leaf, 1.0)))  # type: ignore[arg-type]
        else:
            # No hyprctl — use config values entirely
            enabled = config_enabled if config_enabled is not None else bool(DEFAULTS["animations_enabled"])
            for leaf in _ALL_LEAVES:
                speeds[leaf] = config_speeds.get(leaf, float(DEFAULTS.get(leaf, 1.0)))  # type: ignore[arg-type]

        # ------------------------------------------------------------------
        # Step 3: Apply to widgets
        # ------------------------------------------------------------------
        self._enabled_row.set_active(enabled)

        for leaf in _ALL_LEAVES:
            adj = self._speed_adjs.get(leaf)
            if adj is not None:
                adj.set_value(speeds[leaf])

            row = self._speed_rows.get(leaf)
            if row is not None:
                row.set_subtitle(_make_subtitle(self._meta.get(leaf, {})))

        # Record loaded state for dirty detection
        self._loaded_enabled = enabled
        self._loaded_speeds = dict(speeds)

    def collect_lines(self) -> list[str]:
        """Return Lua lines representing current UI state (to write to config).

        The lines are in the same format as the original config section so that
        write_section_to_config() can persist them verbatim.
        """
        lines: list[str] = []

        # Global enabled
        enabled_str = "true" if self._enabled_row.get_active() else "false"
        lines.append(f"hl.config({{ animations = {{ enabled = {enabled_str} }} }})")

        # Each animation leaf in canonical order
        for leaf in _ALL_LEAVES:
            adj = self._speed_adjs.get(leaf)
            speed: float = adj.get_value() if adj is not None else float(DEFAULTS.get(leaf, 1.0))  # type: ignore[arg-type]

            meta = self._meta.get(leaf, _DEFAULT_META.get(leaf, {}))
            leaf_enabled_str = "true" if self._leaf_enabled.get(leaf, True) else "false"

            # Format speed: strip unnecessary trailing zeros but keep at least
            # the precision the SpinRow displays (2 decimal digits).
            speed_str = f"{speed:.2f}"

            # Build the Lua table entries
            parts: list[str] = [
                f'leaf = "{leaf}"',
                f"enabled = {leaf_enabled_str}",
                f"speed = {speed_str}",
            ]
            if "bezier" in meta:
                parts.append(f'bezier = "{meta["bezier"]}"')
            if "spring" in meta:
                parts.append(f'spring = "{meta["spring"]}"')
            if "style" in meta:
                parts.append(f'style = "{meta["style"]}"')

            inner = ", ".join(parts)
            lines.append(f"hl.animation({{ {inner} }})")

        return lines

    def apply_live(self) -> None:
        """Apply current settings live via hyprctl keyword calls."""
        enabled = self._enabled_row.get_active()
        try:
            apply_keyword("animations:enabled", "true" if enabled else "false")
        except HyprctlApplyError:
            log.warning("Failed to apply animations:enabled live", exc_info=True)

        for leaf in _ALL_LEAVES:
            adj = self._speed_adjs.get(leaf)
            if adj is None:
                continue
            speed = adj.get_value()
            try:
                apply_keyword(f"animations:{leaf}:speed", f"{speed:.2f}")
            except HyprctlApplyError:
                log.warning(
                    "Failed to apply animations:%s:speed live", leaf, exc_info=True
                )

    # ------------------------------------------------------------------
    # Convenience helpers the window may use
    # ------------------------------------------------------------------

    def has_unsaved_changes(self) -> bool:
        """Return True if any widget value differs from the last loaded state."""
        if self._enabled_row.get_active() != self._loaded_enabled:
            return True
        for leaf in _ALL_LEAVES:
            adj = self._speed_adjs.get(leaf)
            if adj is None:
                continue
            if abs(adj.get_value() - self._loaded_speeds.get(leaf, 0.0)) > 1e-9:
                return True
        return False
