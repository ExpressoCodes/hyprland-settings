"""hyprpaper IPC backend — the single module that talks to hyprpaper.

All subprocess calls for hyprpaper live here.  No other module should
call subprocess or interact with hyprpaper directly.

hyprpaper is controlled via ``hyprctl hyprpaper <subcommand>``.

Config format (hyprpaper >= 0.8):
    wallpaper {
        monitor = DP-3
        path = /path/to/image.png
        fit_mode = cover
    }
    splash = false
    ipc = on
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
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class WallpaperEntry:
    monitor: str
    """Monitor name, or empty string for the fallback (all monitors)."""

    path: str
    """Absolute path to the wallpaper image."""

    fit_mode: str = "cover"
    """How the image fills the screen: cover, contain, tile, stretch, center."""


@dataclass
class HyprpaperConfig:
    wallpapers: list[WallpaperEntry] = field(default_factory=list)
    """One entry per monitor assignment (plus optional fallback with empty monitor)."""

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
    """Return the path to hyprpaper.conf, honouring XDG_CONFIG_HOME."""
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg_config:
        base = Path(xdg_config)
    else:
        base = Path.home() / ".config"
    return base / "hypr" / "hyprpaper.conf"


def read_hyprpaper_conf(path: Path) -> HyprpaperConfig:
    """Parse *path* as a hyprpaper.conf and return a :class:`HyprpaperConfig`.

    Understands the block format used by hyprpaper >= 0.8::

        wallpaper {
            monitor = DP-3
            path = ~/image.png
            fit_mode = cover
        }

    Old-format ``preload =`` and ``wallpaper = monitor,path`` lines are
    silently ignored for backward compatibility.
    Returns a default config if the file does not exist.
    """
    config = HyprpaperConfig()

    if not path.exists():
        log.debug("hyprpaper.conf not found at %s, returning defaults", path)
        return config

    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        i += 1

        if not raw or raw.startswith("#"):
            continue

        # Block: wallpaper { ... }
        if raw.startswith("wallpaper") and "{" in raw:
            entry = WallpaperEntry(monitor="", path="", fit_mode="cover")
            while i < len(lines):
                inner = lines[i].strip()
                i += 1
                if inner == "}":
                    break
                if not inner or inner.startswith("#") or "=" not in inner:
                    continue
                key, _, val = inner.partition("=")
                key, val = key.strip(), val.strip()
                if key == "monitor":
                    entry.monitor = val
                elif key == "path":
                    entry.path = str(Path(val).expanduser()) if val else ""
                elif key == "fit_mode":
                    entry.fit_mode = val
            if entry.path:
                config.wallpapers.append(entry)
            continue

        # Top-level key = value
        if "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        key, value = key.strip(), value.strip()

        if key == "splash":
            config.splash = value.lower() in ("true", "1", "yes", "on")
        elif key == "ipc":
            config.ipc = value.lower() in ("true", "1", "yes", "on")
        # Old-format preload/wallpaper lines are intentionally ignored

    return config


def write_hyprpaper_conf(config: HyprpaperConfig, path: Path) -> None:
    """Write *config* to *path* atomically using the new block format."""
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for entry in config.wallpapers:
        lines.append("wallpaper {")
        lines.append(f"    monitor = {entry.monitor}")
        lines.append(f"    path = {entry.path}")
        lines.append(f"    fit_mode = {entry.fit_mode}")
        lines.append("}")
        lines.append("")

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
    """Preload *image_path* into hyprpaper's cache via IPC.

    Must be called before :func:`set_wallpaper` for the same path.
    Silently succeeds if the image is already loaded.
    """
    result = _run(["preload", image_path])
    if result.returncode != 0:
        raise HyprpaperApplyError(
            f"hyprctl hyprpaper preload failed (rc={result.returncode}): {result.stderr}"
        )


def set_wallpaper(monitor: str, image_path: str) -> None:
    """Preload then set the wallpaper on *monitor* to *image_path* via hyprpaper IPC.

    Pass an empty string for *monitor* to apply to all monitors.
    """
    preload_wallpaper(image_path)
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
            monitor, _, wp_path = line.partition(" -> ")
            active[monitor.strip()] = wp_path.strip()
        return active
    except Exception:
        log.debug("list_active_wallpapers() failed", exc_info=True)
        return {}


def is_hyprpaper_running() -> bool:
    """Return True if a hyprpaper process is currently running."""
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
