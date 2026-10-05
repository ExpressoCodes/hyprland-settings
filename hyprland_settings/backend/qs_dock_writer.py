"""Writer for the qs-dock persisted settings.

qs-dock keeps its own settings at ``~/.config/qs-dock/settings.json`` and
rewrites that file itself at runtime (it watches the file for changes).  This
module exposes a flat-key view of a curated subset of those settings and merges
edits back into the nested ``appearance.*`` / ``behaviour.*`` structure,
preserving every other key in the document.

Unlike the Hyprland config writers, these values have NO Hyprland keyword and
emit no Lua managed-block — they are written straight into the JSON document.

Defaults/ranges mirror the ground truth in qs-dock's ``Settings.qml``.

Writes are atomic (temp file in the same directory + ``os.replace``) and the
original file is backed up once per session to ``settings.json.pbak``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema — flat GTK keys mapped to nested qs-dock JSON keys
#
# Defaults taken verbatim from ~/.config/qs-dock/Settings.qml:
#   appearance.iconSize=48, spacing=6, magnification=true, maxMagnifiedSize=80
#   behaviour.enabled=true, visibilityMode="reserve", hideOnFullscreen=true,
#   showDelay=150, hideDelay=500
# ---------------------------------------------------------------------------

# flat_key -> (json_section, json_key, python_type)
_FIELD_MAP: dict[str, tuple[str, str, type]] = {
    "dock_enabled":       ("behaviour",  "enabled",          bool),
    "icon_size":          ("appearance", "iconSize",         int),
    "spacing":            ("appearance", "spacing",          int),
    "magnification":      ("appearance", "magnification",    bool),
    "max_magnified_size": ("appearance", "maxMagnifiedSize", int),
    "visibility_mode":    ("behaviour",  "visibilityMode",   str),
    "hide_on_fullscreen": ("behaviour",  "hideOnFullscreen", bool),
    "show_delay":         ("behaviour",  "showDelay",        int),
    "hide_delay":         ("behaviour",  "hideDelay",        int),
}

DOCK_DEFAULTS: dict[str, int | bool | str] = {
    "dock_enabled":       True,
    "icon_size":          48,
    "spacing":            6,
    "magnification":      True,
    "max_magnified_size": 80,
    "visibility_mode":    "reserve",
    "hide_on_fullscreen": True,
    "show_delay":         150,
    "hide_delay":         500,
}

# Backwards-compatible alias for the enabled-only default.
DEFAULT_DOCK_ENABLED = DOCK_DEFAULTS["dock_enabled"]

_session_backed_up: set[Path] = set()


# ---------------------------------------------------------------------------
# Paths / existence
# ---------------------------------------------------------------------------

def qs_dock_config_dir() -> Path:
    """Return ``$XDG_CONFIG_HOME/qs-dock`` (falls back to ``~/.config/qs-dock``)."""
    xdg_cfg = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return xdg_cfg / "qs-dock"


def qs_dock_settings_path() -> Path:
    """Return the path to ``qs-dock/settings.json``."""
    return qs_dock_config_dir() / "settings.json"


def qs_dock_config_exists() -> bool:
    """Return True when the qs-dock config dir or its settings.json is present.

    Used for conditional UI registration: the Dock page must appear only when
    qs-dock is installed/configured for this user.
    """
    return qs_dock_config_dir().exists() or qs_dock_settings_path().exists()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    """Load settings.json as a dict, returning {} on missing/malformed/empty."""
    path = qs_dock_settings_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("qs-dock settings.json is malformed, ignoring: %s", exc)
        return {}
    if not isinstance(data, dict):
        log.warning("qs-dock settings.json is not a JSON object, ignoring")
        return {}
    return data


def _coerce(value: object, py_type: type, default: object) -> object:
    """Coerce a loaded JSON value to the expected python type, else the default."""
    try:
        if py_type is bool:
            return bool(value)
        if py_type is int:
            return int(value)
        if py_type is str:
            return str(value)
    except (TypeError, ValueError):
        return default
    return value


def read_dock_settings() -> dict[str, int | bool | str]:
    """Return a flat dict of all managed dock settings.

    Reads nested ``appearance.*`` / ``behaviour.*`` values from settings.json,
    falling back to :data:`DOCK_DEFAULTS` for any missing/malformed key.
    """
    data = _load_settings()
    result: dict[str, int | bool | str] = dict(DOCK_DEFAULTS)
    for flat_key, (section, json_key, py_type) in _FIELD_MAP.items():
        sect = data.get(section)
        if isinstance(sect, dict) and json_key in sect:
            result[flat_key] = _coerce(
                sect[json_key], py_type, DOCK_DEFAULTS[flat_key]
            )
    return result


def read_dock_enabled() -> bool:
    """Return ``behaviour.enabled`` from settings.json, or the default if absent."""
    return bool(read_dock_settings()["dock_enabled"])


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _backup_once(path: Path) -> None:
    if path in _session_backed_up:
        return
    if path.exists():
        bak = path.with_suffix(path.suffix + ".pbak")
        try:
            bak.write_bytes(path.read_bytes())
            log.info("Backed up qs-dock settings to %s", bak)
        except OSError as exc:
            log.warning("Could not back up qs-dock settings: %s", exc)
    _session_backed_up.add(path)


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_dock_settings(values: dict[str, int | bool | str]) -> None:
    """Merge the given flat dock settings into settings.json atomically.

    Only the keys present in *values* that are part of :data:`_FIELD_MAP` are
    written, into their nested ``appearance.*`` / ``behaviour.*`` locations.
    Every other key in the file (pins, filtering, position, version, unmanaged
    appearance/behaviour keys, ...) is preserved.  A missing file is created
    with just the managed keys.  Malformed/empty files are treated as empty
    (unparseable keys cannot be preserved).  The write is atomic (temp file +
    ``os.replace``).
    """
    path = qs_dock_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    _backup_once(path)

    data = _load_settings()
    for flat_key, value in values.items():
        mapping = _FIELD_MAP.get(flat_key)
        if mapping is None:
            continue
        section, json_key, py_type = mapping
        sect = data.get(section)
        if not isinstance(sect, dict):
            sect = {}
        sect[json_key] = _coerce(value, py_type, value)
        data[section] = sect

    _atomic_write(path, data)
    log.info("Wrote qs-dock settings (%d key(s)) to %s", len(values), path)


def write_dock_enabled(enabled: bool) -> None:
    """Merge ``behaviour.enabled = enabled`` into settings.json atomically.

    Thin wrapper over :func:`write_dock_settings` kept for callers that only
    need to toggle the master switch.
    """
    write_dock_settings({"dock_enabled": bool(enabled)})


# ---------------------------------------------------------------------------
# Reload / external window
# ---------------------------------------------------------------------------

def reload_dock() -> None:
    """Reload qs-dock so new settings take effect.  Fail-soft.

    qs-dock watches settings.json and reloads on its own, but ``qs-restart`` is
    invoked when present as a belt-and-suspenders full restart.  There is NO
    reload verb in qs-dock's DockIpc.qml, so the previous ``settings-style
    reload`` IPC fallback (which never existed) has been removed.  Never raises.
    """
    restart = Path("~/.local/bin/qs-restart").expanduser()
    if not restart.exists():
        log.info("qs-restart not found; relying on qs-dock's own file watch")
        return
    try:
        subprocess.Popen(
            [str(restart)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001 — reload is strictly best-effort
        log.warning("qs-dock reload failed (non-fatal): %s", exc)


def open_advanced_settings() -> None:
    """Open qs-dock's own settings window via IPC.  Fail-soft.

    The only settings verb exposed by qs-dock's DockIpc.qml is ``settings``,
    which calls ``UiState.toggleSettings()`` — there is no dedicated ``open``
    verb — so this toggles the window.  Runs detached; never raises.
    """
    cfg_dir = qs_dock_config_dir()
    try:
        subprocess.Popen(
            ["qs", "ipc", "-p", str(cfg_dir), "call", "dock", "settings"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort launch
        log.warning("Could not open qs-dock settings window (non-fatal): %s", exc)
