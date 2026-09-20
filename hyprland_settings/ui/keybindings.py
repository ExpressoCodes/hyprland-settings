"""Keybindings page — read-only viewer for active Hyprland keybindings."""

from __future__ import annotations

import json
import logging
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DISPATCHER_GROUPS = {
    "Application Shortcuts": {"exec"},
    "Window Management": {
        "killactive",
        "togglefloating",
        "pseudo",
        "fullscreen",
        "movetoworkspace",
        "movetoworkspacesilent",
    },
    "Navigation": {"workspace", "movefocus", "focusmonitor"},
}

_READABLE_DISPATCHERS: dict[str, str] = {
    "exec": "Run",
    "killactive": "Close window",
    "workspace": "Go to workspace",
    "movetoworkspace": "Move to workspace",
    "movetoworkspacesilent": "Move to workspace (silent)",
    "togglefloating": "Toggle float",
    "pseudo": "Toggle pseudo-tile",
    "movefocus": "Move focus",
    "exit": "Exit Hyprland",
    "togglespecialworkspace": "Toggle scratchpad",
    "fullscreen": "Toggle fullscreen",
    "focusmonitor": "Focus monitor",
}


def _get_binds() -> list[dict]:
    """Run hyprctl binds -j and return parsed JSON, or [] on error."""
    try:
        result = subprocess.run(
            ["hyprctl", "binds", "-j"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        log.debug("_get_binds() failed", exc_info=True)
    return []


def _modmask_to_str(modmask: int) -> str:
    parts: list[str] = []
    if modmask & 64:
        parts.append("Super")
    if modmask & 4:
        parts.append("Ctrl")
    if modmask & 1:
        parts.append("Shift")
    if modmask & 8:
        parts.append("Alt")
    return " + ".join(parts) if parts else ""


def _format_bind(bind: dict) -> tuple[str, str]:
    """Return (key_combo, action_description)."""
    mods = _modmask_to_str(bind.get("modmask", 0))
    key = bind.get("key", "")
    combo = f"{mods} + {key}".strip(" +") if mods else key

    dispatcher = bind.get("dispatcher", "")
    arg = bind.get("arg", "")

    action = _READABLE_DISPATCHERS.get(dispatcher, dispatcher)
    if arg:
        action = f"{action}: {arg}"
    return combo, action


def _categorise(binds: list[dict]) -> dict[str, list[dict]]:
    """Return an ordered dict mapping group name → list of bind dicts."""
    buckets: dict[str, list[dict]] = {name: [] for name in _DISPATCHER_GROUPS}
    buckets["Other"] = []

    for bind in binds:
        if bind.get("mouse"):
            continue
        dispatcher = bind.get("dispatcher", "")
        placed = False
        for group_name, dispatchers in _DISPATCHER_GROUPS.items():
            if dispatcher in dispatchers:
                buckets[group_name].append(bind)
                placed = True
                break
        if not placed:
            buckets["Other"].append(bind)

    # Drop empty groups
    return {k: v for k, v in buckets.items() if v}


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


class KeybindingsPage(Adw.PreferencesPage):
    """Read-only viewer for active Hyprland keybindings."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Keybindings")
        self.set_icon_name("input-keyboard-symbolic")
        self._config_path = None
        self._hyprctl_available = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, config_path, hyprctl_available: bool) -> None:
        """Fetch binds via hyprctl and populate the list."""
        self._config_path = config_path
        self._hyprctl_available = hyprctl_available

        # Clear existing children
        while True:
            child = self.get_first_child()
            if child is None:
                break
            self.remove(child)

        # Always add a refresh group at the top
        refresh_group = Adw.PreferencesGroup()
        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.set_valign(Gtk.Align.CENTER)
        refresh_btn.add_css_class("flat")
        refresh_btn.connect(
            "clicked",
            lambda _: self.load(self._config_path, self._hyprctl_available),
        )
        refresh_group.set_header_suffix(refresh_btn)
        self.add(refresh_group)

        if not hyprctl_available:
            self._show_unavailable()
            return

        binds = _get_binds()
        if not binds:
            self._show_unavailable()
            return

        self._populate(binds)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _show_unavailable(self) -> None:
        status = Adw.StatusPage()
        status.set_icon_name("input-keyboard-symbolic")
        status.set_title("Keybindings Unavailable")
        status.set_description(
            "Start the app inside a Hyprland session to view active keybindings."
        )
        # Wrap in a group so it sits below the refresh button group
        group = Adw.PreferencesGroup()
        group.add(status)
        self.add(group)

    def _populate(self, binds: list[dict]) -> None:
        categories = _categorise(binds)

        for group_name, group_binds in categories.items():
            group = Adw.PreferencesGroup()
            group.set_title(group_name)

            for bind in group_binds:
                combo, action = _format_bind(bind)
                row = Adw.ActionRow()
                row.set_title(combo if combo else "(no key)")
                row.set_subtitle(action)
                row.set_activatable(False)

                if bind.get("locked"):
                    lock_label = Gtk.Label(label="\U0001F512")  # 🔒
                    lock_label.set_valign(Gtk.Align.CENTER)
                    row.add_suffix(lock_label)

                group.add(row)

            self.add(group)
