"""Writer for the qs-dock persisted settings.

qs-dock keeps its own settings at ``~/.config/qs-dock/settings.json`` and
rewrites that file itself at runtime.  This module touches only the single
key ``behaviour.enabled`` (a bool, default ``True``) and preserves every other
key in the file.

Unlike the Hyprland config writers, this value has NO Hyprland keyword and
emits no Lua managed-block — it is written straight into the JSON document.

Writes are atomic (temp file in the same directory + ``os.replace``) and the
original file is backed up once per session to ``settings.json.pbak`` so the
pre-edit state can be recovered, mirroring the backup/atomic approach used by
``config_writer``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Default for behaviour.enabled when the key (or file) is absent.
DEFAULT_DOCK_ENABLED = True

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

    Used for conditional UI registration: the Dock option must appear only when
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


def read_dock_enabled() -> bool:
    """Return ``behaviour.enabled`` from settings.json, or the default if absent."""
    data = _load_settings()
    behaviour = data.get("behaviour")
    if isinstance(behaviour, dict) and "enabled" in behaviour:
        return bool(behaviour["enabled"])
    return DEFAULT_DOCK_ENABLED


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


def write_dock_enabled(enabled: bool) -> None:
    """Merge ``behaviour.enabled = enabled`` into settings.json atomically.

    Every other key in the file is preserved.  A missing file is created with
    just ``{"behaviour": {"enabled": <bool>}}``.  Malformed/empty files are
    treated as empty (other keys are necessarily lost in that case because they
    cannot be parsed).  The write is atomic (temp file + ``os.replace``).
    """
    path = qs_dock_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    _backup_once(path)

    data = _load_settings()
    behaviour = data.get("behaviour")
    if not isinstance(behaviour, dict):
        behaviour = {}
    behaviour["enabled"] = bool(enabled)
    data["behaviour"] = behaviour

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    log.info("Wrote qs-dock behaviour.enabled=%s to %s", bool(enabled), path)


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------

def reload_dock() -> None:
    """Reload qs-dock so the new setting takes effect.  Fail-soft.

    Prefers ``~/.local/bin/qs-restart`` when present, otherwise falls back to
    the qs-dock IPC.  Never raises — a missing reload mechanism must not break
    the save.
    """
    restart = Path("~/.local/bin/qs-restart").expanduser()
    try:
        if restart.exists():
            subprocess.Popen(
                [str(restart)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        cfg_dir = qs_dock_config_dir()
        subprocess.run(
            ["qs", "ipc", "-p", str(cfg_dir), "call", "dock", "settings-style", "reload"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — reload is strictly best-effort
        log.warning("qs-dock reload failed (non-fatal): %s", exc)
