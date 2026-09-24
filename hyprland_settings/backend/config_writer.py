"""Config file writer for Hyprland Settings.

Supports both hyprland.lua (Lua API, preferred) and hyprland.conf (hyprlang).
Writes only the monitor block, leaving all other content untouched.
Atomic writes via .tmp + os.replace().
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprland_settings.backend.hyprctl import Monitor

log = logging.getLogger(__name__)

MARKER_START = "-- >>> hyprland-settings: monitors (do not edit this line)"
MARKER_END   = "-- <<< hyprland-settings: monitors (do not edit this line)"
CONF_MARKER_START = "# >>> hyprland-settings: monitors (do not edit this line)"
CONF_MARKER_END   = "# <<< hyprland-settings: monitors (do not edit this line)"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ConfigNotFoundError(FileNotFoundError):
    """Raised when no Hyprland config file can be located."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class ConfigFormat(Enum):
    LUA      = auto()
    HYPRLANG = auto()


@dataclass
class MonitorConfig:
    """Intermediate representation of a monitor config entry."""
    name: str
    resolution: str        # "1920x1080" or "preferred" or "disabled"
    refresh: float | None  # Hz; None when resolution is "preferred"/"disabled"
    position: str          # "0x0" or "auto" etc.
    scale: float | str     # float or "auto"
    transform: int | None
    mirror: str | None
    extra: str             # verbatim extra params (hyprlang only, round-trip safe)

    def to_config_line(self) -> str:
        """Serialize to a hyprlang monitor= line."""
        if self.resolution == "disabled":
            return f"monitor = {self.name}, disabled"
        hz_str = ""
        if self.refresh is not None:
            hz_int = int(self.refresh)
            hz_str = f"@{hz_int}" if self.refresh == hz_int else f"@{self.refresh:g}"
        scale_str = self.scale if isinstance(self.scale, str) else f"{self.scale:g}"
        parts = [
            f"monitor = {self.name}",
            f"{self.resolution}{hz_str}",
            self.position,
            scale_str,
        ]
        base = ", ".join(parts)
        if self.transform is not None:
            base += f", transform, {self.transform}"
        if self.mirror:
            base += f", mirror, {self.mirror}"
        if self.extra:
            base += f", {self.extra}"
        return base

    def to_lua_line(self) -> str:
        """Serialize to an hl.monitor({...}) single-line Lua call."""
        if self.resolution == "disabled":
            return f'hl.monitor({{ output = "{self.name}", mode = "disabled" }})'
        mode_str = self.resolution
        if self.refresh is not None:
            hz_int = int(self.refresh)
            hz_str = str(hz_int) if self.refresh == hz_int else f"{self.refresh:g}"
            mode_str = f"{self.resolution}@{hz_str}"
        scale_str = f'"{self.scale}"' if isinstance(self.scale, str) else f'"{self.scale:g}"'
        parts = [
            f'output = "{self.name}"',
            f'mode = "{mode_str}"',
            f'position = "{self.position}"',
            f'scale = {scale_str}',
        ]
        if self.transform is not None and self.transform != 0:
            parts.append(f'transform = {self.transform}')
        if self.mirror:
            parts.append(f'mirror = "{self.mirror}"')
        return "hl.monitor({ " + ", ".join(parts) + " })"


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------

def find_config_path() -> Path:
    """Locate the Hyprland config file.

    Checks (in order):
      1. $HYPRLAND_CONFIG env var
      2. $XDG_CONFIG_HOME/hypr/hyprland.lua
      3. ~/.config/hypr/hyprland.lua
      4. $XDG_CONFIG_HOME/hypr/hyprland.conf
      5. ~/.config/hypr/hyprland.conf

    Returns the first path that exists.
    Raises ConfigNotFoundError if none are found.
    """
    env_path = os.environ.get("HYPRLAND_CONFIG")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p

    xdg_cfg = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    candidates = [
        xdg_cfg / "hypr" / "hyprland.lua",
        Path("~/.config/hypr/hyprland.lua").expanduser(),
        xdg_cfg / "hypr" / "hyprland.conf",
        Path("~/.config/hypr/hyprland.conf").expanduser(),
    ]
    for p in candidates:
        if p.exists():
            return p
    raise ConfigNotFoundError(
        "No Hyprland config found. Searched: " + ", ".join(str(c) for c in candidates)
    )


def detect_format(config_path: Path) -> ConfigFormat:
    return ConfigFormat.LUA if config_path.suffix == ".lua" else ConfigFormat.HYPRLANG


# ---------------------------------------------------------------------------
# Reading monitors from config (fallback when hyprctl unavailable)
# ---------------------------------------------------------------------------

def read_monitors_from_config(config_path: Path) -> list[MonitorConfig]:
    """Parse monitor entries from the config file."""
    if detect_format(config_path) == ConfigFormat.LUA:
        return _read_lua(config_path)
    return _read_hyprlang(config_path)


def _read_lua(config_path: Path) -> list[MonitorConfig]:
    """Parse hl.monitor({...}) blocks from a Lua config."""
    text = config_path.read_text(encoding="utf-8")
    monitors: list[MonitorConfig] = []

    # Match single-line and multi-line hl.monitor({...}) blocks
    pattern = re.compile(r'hl\.monitor\s*\(\s*\{([^}]*)\}\s*\)', re.DOTALL)
    for m in pattern.finditer(text):
        body = m.group(1)
        fields = _parse_lua_table(body)
        name = fields.get("output", "")
        if not name:  # skip catch-all rules
            continue
        mode = fields.get("mode", "preferred")
        position = fields.get("position", "auto")
        scale_raw = fields.get("scale", "1")
        transform_raw = fields.get("transform")
        mirror_raw = fields.get("mirror")

        resolution, refresh = _split_mode(mode)
        try:
            scale: float | str = float(scale_raw)
        except (ValueError, TypeError):
            scale = scale_raw or "auto"
        transform = int(transform_raw) if transform_raw is not None else None
        monitors.append(MonitorConfig(
            name=name,
            resolution=resolution,
            refresh=refresh,
            position=position,
            scale=scale,
            transform=transform,
            mirror=mirror_raw or None,
            extra="",
        ))
    return monitors


def _read_hyprlang(config_path: Path) -> list[MonitorConfig]:
    """Parse monitor= lines from a hyprlang config."""
    monitors: list[MonitorConfig] = []
    for line in config_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("monitor"):
            continue
        if "=" not in stripped:
            continue
        _, _, rest = stripped.partition("=")
        rest = rest.strip()
        parts = [p.strip() for p in rest.split(",")]
        if len(parts) < 2:
            continue
        name = parts[0]
        if parts[1].lower() == "disabled":
            monitors.append(MonitorConfig(
                name=name, resolution="disabled", refresh=None,
                position="", scale=1, transform=None, mirror=None, extra="",
            ))
            continue
        resolution, refresh = _split_mode(parts[1]) if len(parts) > 1 else ("preferred", None)
        position = parts[2] if len(parts) > 2 else "auto"
        scale_raw = parts[3] if len(parts) > 3 else "1"
        try:
            scale: float | str = float(scale_raw)
        except ValueError:
            scale = scale_raw
        transform = None
        mirror = None
        extra_parts: list[str] = []
        i = 4
        while i < len(parts):
            if parts[i] == "transform" and i + 1 < len(parts):
                try:
                    transform = int(parts[i + 1])
                except ValueError:
                    pass
                i += 2
            elif parts[i] == "mirror" and i + 1 < len(parts):
                mirror = parts[i + 1]
                i += 2
            else:
                extra_parts.append(parts[i])
                i += 1
        monitors.append(MonitorConfig(
            name=name, resolution=resolution, refresh=refresh,
            position=position, scale=scale, transform=transform,
            mirror=mirror, extra=", ".join(extra_parts),
        ))
    return monitors


# ---------------------------------------------------------------------------
# Writing monitors to config
# ---------------------------------------------------------------------------

_session_backed_up: set[Path] = set()


def write_monitors_to_config(monitors: list["Monitor"], config_path: Path) -> None:
    """Write monitor entries into the config, preserving all other content.

    On first call per session, backs up the file to <path>.hyprland-settings.bak.
    Uses marker comments to locate and replace only the managed block.
    Write is atomic (temp file + os.replace).
    """
    fmt = detect_format(config_path)
    if fmt == ConfigFormat.LUA:
        _write_lua(monitors, config_path)
    else:
        _write_hyprlang(monitors, config_path)


def _backup_once(config_path: Path) -> None:
    if config_path not in _session_backed_up:
        bak = config_path.with_suffix(config_path.suffix + ".hyprland-settings.bak")
        try:
            bak.write_bytes(config_path.read_bytes())
            log.info("Backed up config to %s", bak)
        except OSError as exc:
            log.warning("Could not write backup: %s", exc)
        _session_backed_up.add(config_path)


def _write_lua(monitors: list["Monitor"], config_path: Path) -> None:
    _backup_once(config_path)

    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # Build the new managed block
    new_block = [MARKER_START + "\n"]
    for m in monitors:
        cfg = _monitor_to_config(m)
        new_block.append(cfg.to_lua_line() + "\n")
    new_block.append(MARKER_END + "\n")

    # Find existing marker block
    start_idx = end_idx = None
    for i, line in enumerate(lines):
        if line.strip() == MARKER_START:
            start_idx = i
        elif line.strip() == MARKER_END:
            end_idx = i

    if start_idx is not None and end_idx is not None:
        lines[start_idx:end_idx + 1] = new_block
    else:
        # No existing block — append before the first hl.monitor with output=""
        # (the catch-all), or at end of file
        insert_at = len(lines)
        for i, line in enumerate(lines):
            if re.search(r'hl\.monitor\s*\(\s*\{[^}]*output\s*=\s*""', line):
                insert_at = i
                break
        lines[insert_at:insert_at] = ["\n"] + new_block

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, config_path)
    log.info("Wrote %d monitor(s) to %s", len(monitors), config_path)


def _write_hyprlang(monitors: list["Monitor"], config_path: Path) -> None:
    _backup_once(config_path)

    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    crlf = b"\r\n" in config_path.read_bytes()

    new_block = [CONF_MARKER_START + "\n"]
    for m in monitors:
        cfg = _monitor_to_config(m)
        new_block.append(cfg.to_config_line() + "\n")
    new_block.append(CONF_MARKER_END + "\n")

    start_idx = end_idx = None
    for i, line in enumerate(lines):
        if line.strip() == CONF_MARKER_START:
            start_idx = i
        elif line.strip() == CONF_MARKER_END:
            end_idx = i

    if start_idx is not None and end_idx is not None:
        lines[start_idx:end_idx + 1] = new_block
    else:
        lines.append("\n")
        lines.extend(new_block)

    content = "".join(lines)
    if crlf:
        content = content.replace("\n", "\r\n")

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, config_path)
    log.info("Wrote %d monitor(s) to %s", len(monitors), config_path)


# ---------------------------------------------------------------------------
# Generic section read/write
# ---------------------------------------------------------------------------


def _section_markers(section_name: str, fmt: ConfigFormat) -> tuple[str, str]:
    """Return (start_marker, end_marker) for the given section and format."""
    if fmt == ConfigFormat.LUA:
        return (
            f"-- >>> hyprland-settings: {section_name} (do not edit this line)",
            f"-- <<< hyprland-settings: {section_name} (do not edit this line)",
        )
    else:
        return (
            f"# >>> hyprland-settings: {section_name} (do not edit this line)",
            f"# <<< hyprland-settings: {section_name} (do not edit this line)",
        )


def write_section_to_config(section_name: str, lines: list[str], config_path: Path) -> None:
    """Replace content between named section markers in the config.

    Markers look like:
      -- >>> hyprland-settings: animations (do not edit this line)
      ... lines ...
      -- <<< hyprland-settings: animations (do not edit this line)

    If markers don't exist, append the section at end of file.
    Atomic write (tmp + os.replace). Backs up first time in session (reuse _backup_once).
    `lines` are the raw Lua lines to write (strings without trailing newlines).
    Works for both .lua and .conf formats (use -- vs # for markers).
    """
    fmt = detect_format(config_path)
    marker_start, marker_end = _section_markers(section_name, fmt)

    _backup_once(config_path)

    text = config_path.read_text(encoding="utf-8")
    file_lines = text.splitlines(keepends=True)

    new_block = [marker_start + "\n"]
    for ln in lines:
        new_block.append(ln + "\n")
    new_block.append(marker_end + "\n")

    start_idx = end_idx = None
    for i, file_line in enumerate(file_lines):
        if file_line.strip() == marker_start:
            start_idx = i
        elif file_line.strip() == marker_end:
            end_idx = i

    if start_idx is not None and end_idx is not None:
        file_lines[start_idx:end_idx + 1] = new_block
    else:
        if file_lines and not file_lines[-1].endswith("\n"):
            file_lines.append("\n")
        file_lines.append("\n")
        file_lines.extend(new_block)

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text("".join(file_lines), encoding="utf-8")
    os.replace(tmp, config_path)
    log.info("Wrote section '%s' (%d line(s)) to %s", section_name, len(lines), config_path)


def read_section_from_config(section_name: str, config_path: Path) -> list[str]:
    """Return lines from a named section (between markers), or [] if not present.

    Returns the lines as-is without the marker lines themselves.
    Returns [] if the section markers are not found.
    """
    fmt = detect_format(config_path)
    marker_start, marker_end = _section_markers(section_name, fmt)

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read config for section '%s': %s", section_name, exc)
        return []

    file_lines = text.splitlines(keepends=False)
    inside = False
    result: list[str] = []

    for line in file_lines:
        if line.strip() == marker_start:
            inside = True
            continue
        if line.strip() == marker_end:
            inside = False
            continue
        if inside:
            result.append(line)

    return result


# ---------------------------------------------------------------------------
# Section file management (live preview)
# ---------------------------------------------------------------------------

def _section_dir() -> Path:
    xdg_cfg = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return xdg_cfg / "hypr"


def get_section_file_path(section_name: str) -> Path:
    """Return path to ~/.config/hypr/{section_name}.lua (sibling of hyprland.lua)."""
    return _section_dir() / f"{section_name}.lua"


def ensure_section_file_sourced(section_name: str, config_path: Path) -> None:
    """Inject a dofile() line for section_name into config_path if not already there.

    Only acts on .lua configs. Inserts after the last existing dofile() line,
    or at end of file. No-op if the line is already present.
    """
    if detect_format(config_path) != ConfigFormat.LUA:
        return

    dofile_line = f'dofile(hypr_dir .. "{section_name}.lua")'

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read config to inject dofile: %s", exc)
        return

    if dofile_line in text:
        return

    _backup_once(config_path)
    lines = text.splitlines(keepends=True)
    last_dofile_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("dofile("):
            last_dofile_idx = i
    insert_at = (last_dofile_idx + 1) if last_dofile_idx is not None else len(lines)
    lines.insert(insert_at, dofile_line + "\n")

    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, config_path)
    log.info("Injected dofile for %s into %s", section_name, config_path)


def write_section_file(section_name: str, lines: list[str]) -> None:
    """Atomically write lines to the section's dedicated .lua file.

    On the first call per session, backs up the file to <name>.lua.bak so
    that the original state can be restored if the user reverts or closes
    without saving.
    """
    path = get_section_file_path(section_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _backup_once(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    log.info("Wrote section file %s (%d lines)", path, len(lines))




# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _monitor_to_config(m: "Monitor") -> MonitorConfig:
    """Convert a live Monitor to a MonitorConfig for serialization."""
    if m.disabled:
        return MonitorConfig(
            name=m.name, resolution="disabled", refresh=None,
            position="", scale=1, transform=None, mirror=None, extra="",
        )
    return MonitorConfig(
        name=m.name,
        resolution=f"{m.width}x{m.height}",
        refresh=float(m.refresh_rate),
        position=f"{m.x}x{m.y}",
        scale=m.scale,
        transform=m.transform if m.transform != 0 else None,
        mirror=m.mirror_of if m.mirror_of else None,
        extra="",
    )


def _split_mode(mode: str) -> tuple[str, float | None]:
    """Split "1920x1080@60" into ("1920x1080", 60.0).

    Returns (mode, None) for modes without a refresh rate.
    """
    if "@" in mode:
        res, _, hz = mode.partition("@")
        try:
            return res.strip(), float(hz.strip())
        except ValueError:
            return mode, None
    return mode, None


def _parse_lua_table(body: str) -> dict[str, str]:
    """Naively parse key = "value" or key = number pairs from a Lua table body."""
    fields: dict[str, str] = {}
    # Match: key = "value"  or  key = value  (no nested tables)
    pattern = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([\w./-]+))')
    for m in pattern.finditer(body):
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else m.group(3)
        fields[key] = val
    return fields
