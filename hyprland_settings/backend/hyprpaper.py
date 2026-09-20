"""hyprpaper IPC backend — the single module that talks to hyprpaper.

All subprocess calls for hyprpaper live here.  No other module should
call subprocess or interact with hyprpaper directly.

hyprpaper is controlled via ``hyprctl hyprpaper <subcommand>``.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HyprpaperUnavailableError(Exception):
    """Raised when hyprctl is not found or hyprpaper is not running."""


class HyprpaperApplyError(Exception):
    """Raised when a hyprpaper apply command exits with a non-zero status."""


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class HyprpaperConfig:
    preload: list[str] = field(default_factory=list)
    """Absolute paths to preload into hyprpaper."""

    wallpapers: dict[str, str] = field(default_factory=dict)
    """Mapping of monitor name → wallpaper path.  Empty key means all monitors."""

    splash: bool = False
    ipc: bool = True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """Run a ``hyprctl hyprpaper`` command with a 5-second timeout."""
    full_args = ["hyprctl", "hyprpaper"] + args
    log.debug("hyprpaper command: %s", " ".join(full_args))
    try:
        result = subprocess.run(
            full_args,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise HyprpaperUnavailableError(f"hyprctl not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HyprpaperUnavailableError(f"hyprctl timed out: {exc}") from exc
    return result


# ---------------------------------------------------------------------------
# Config file helpers
# ---------------------------------------------------------------------------


def find_hyprpaper_conf() -> Path:
    """Return the path to hyprpaper.conf, honouring XDG_CONFIG_HOME.

    Does not require the file to exist.
    """
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg_config:
        base = Path(xdg_config)
    else:
        base = Path.home() / ".config"
    return base / "hypr" / "hyprpaper.conf"


def read_hyprpaper_conf(path: Path) -> HyprpaperConfig:
    """Parse *path* as a hyprpaper.conf and return a :class:`HyprpaperConfig`.

    Returns a default config if the file does not exist.
    """
    config = HyprpaperConfig()

    if not path.exists():
        log.debug("hyprpaper.conf not found at %s, returning defaults", path)
        return config

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if key == "preload":
            config.preload.append(value)
        elif key == "wallpaper":
            # Format: monitor,/path  (monitor may be empty)
            monitor, _, image_path = value.partition(",")
            config.wallpapers[monitor] = image_path
        elif key == "splash":
            config.splash = value.lower() in ("true", "1", "yes", "on")
        elif key == "ipc":
            config.ipc = value.lower() in ("true", "1", "yes", "on")

    return config


def write_hyprpaper_conf(config: HyprpaperConfig, path: Path) -> None:
    """Write *config* to *path* atomically (tmp + os.replace).

    Creates parent directories if they do not exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    for image_path in config.preload:
        lines.append(f"preload = {image_path}")

    for monitor, image_path in config.wallpapers.items():
        lines.append(f"wallpaper = {monitor},{image_path}")

    lines.append(f"splash = {'true' if config.splash else 'false'}")
    lines.append(f"ipc = {'on' if config.ipc else 'off'}")

    content = "\n".join(lines) + "\n"

    tmp_path = path.with_suffix(".conf.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)
    log.debug("Wrote hyprpaper.conf to %s", path)


# ---------------------------------------------------------------------------
# Public API — IPC commands
# ---------------------------------------------------------------------------


def preload_wallpaper(image_path: str) -> None:
    """Preload *image_path* into hyprpaper.

    Raises :class:`HyprpaperApplyError` on failure.
    Raises :class:`HyprpaperUnavailableError` if hyprctl is not found.
    """
    result = _run(["preload", image_path])
    if result.returncode != 0:
        raise HyprpaperApplyError(
            f"hyprctl hyprpaper preload failed (rc={result.returncode}): {result.stderr}"
        )


def set_wallpaper(monitor: str, image_path: str) -> None:
    """Set the wallpaper on *monitor* to *image_path*.

    Pass an empty string for *monitor* to apply to all monitors.
    Raises :class:`HyprpaperApplyError` on failure.
    Raises :class:`HyprpaperUnavailableError` if hyprctl is not found.
    """
    arg = f"{monitor},{image_path}"
    result = _run(["wallpaper", arg])
    if result.returncode != 0:
        raise HyprpaperApplyError(
            f"hyprctl hyprpaper wallpaper failed (rc={result.returncode}): {result.stderr}"
        )


def list_active_wallpapers() -> dict[str, str]:
    """Return a mapping of monitor name → wallpaper path for active wallpapers.

    Parses ``hyprctl hyprpaper listactive`` output of the form::

        HDMI-A-1 -> /path/to/wallpaper.png

    Returns an empty dict on any error; never raises.
    """
    try:
        result = _run(["listactive"])
        if result.returncode != 0:
            log.debug(
                "hyprctl hyprpaper listactive failed (rc=%d): %s",
                result.returncode,
                result.stderr,
            )
            return {}

        active: dict[str, str] = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if " -> " not in line:
                continue
            monitor, _, path = line.partition(" -> ")
            active[monitor.strip()] = path.strip()
        return active
    except Exception:
        log.debug("list_active_wallpapers() failed", exc_info=True)
        return {}


def list_loaded_wallpapers() -> list[str]:
    """Return a list of paths currently loaded in hyprpaper.

    Parses ``hyprctl hyprpaper listloaded`` output (one path per line).
    Returns an empty list on any error; never raises.
    """
    try:
        result = _run(["listloaded"])
        if result.returncode != 0:
            log.debug(
                "hyprctl hyprpaper listloaded failed (rc=%d): %s",
                result.returncode,
                result.stderr,
            )
            return []

        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        log.debug("list_loaded_wallpapers() failed", exc_info=True)
        return []


def unload_wallpaper(image_path: str) -> None:
    """Unload *image_path* from hyprpaper.

    Raises :class:`HyprpaperApplyError` on failure.
    Raises :class:`HyprpaperUnavailableError` if hyprctl is not found.
    """
    result = _run(["unload", image_path])
    if result.returncode != 0:
        raise HyprpaperApplyError(
            f"hyprctl hyprpaper unload failed (rc={result.returncode}): {result.stderr}"
        )


def unload_all() -> None:
    """Unload all wallpapers from hyprpaper.

    Raises :class:`HyprpaperApplyError` on failure.
    Raises :class:`HyprpaperUnavailableError` if hyprctl is not found.
    """
    result = _run(["unload", "all"])
    if result.returncode != 0:
        raise HyprpaperApplyError(
            f"hyprctl hyprpaper unload all failed (rc={result.returncode}): {result.stderr}"
        )


def is_hyprpaper_running() -> bool:
    """Return True if a hyprpaper process is currently running.

    Uses ``pgrep -x hyprpaper``; never raises.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-x", "hyprpaper"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        log.debug("is_hyprpaper_running() failed", exc_info=True)
        return False
