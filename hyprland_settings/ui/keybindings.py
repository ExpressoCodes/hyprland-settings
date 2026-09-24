"""Keybindings page — editable managed-bind manager + live binds viewer."""

from __future__ import annotations

import copy
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402

from hyprland_settings.backend.config_writer import (
    ensure_section_file_sourced,
    get_section_file_path,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported actions
# ---------------------------------------------------------------------------

_ACTIONS = [
    ("exec",         "Run command"),
    ("close",        "Close window"),
    ("float_toggle", "Toggle floating"),
    ("fullscreen",   "Toggle fullscreen"),
    ("pseudo",       "Toggle pseudo-tile"),
    ("exit",         "Exit Hyprland"),
]
_ACTION_KEYS    = [a[0] for a in _ACTIONS]
_ACTION_LABELS  = [a[1] for a in _ACTIONS]
_ACTION_LABEL   = dict(_ACTIONS)

_MOD_ORDER = ["SUPER", "CTRL", "SHIFT", "ALT"]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ManagedBind:
    modifiers: list[str]
    key: str
    action: str   # one of _ACTION_KEYS
    arg: str = ""

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_lua_line(self) -> str:
        combo = " + ".join(self.modifiers + [self.key])
        dispatch = self._action_lua()
        return f'hl.bind("{combo}", {dispatch})'

    def _action_lua(self) -> str:
        a = self.action
        if a == "exec":
            escaped = self.arg.replace('"', '\\"')
            return f'hl.dsp.exec_cmd("{escaped}")'
        if a == "close":
            return "hl.dsp.window.close()"
        if a == "float_toggle":
            return 'hl.dsp.window.float({ action = "toggle" })'
        if a == "fullscreen":
            return "hl.dsp.window.fullscreen()"
        if a == "pseudo":
            return "hl.dsp.window.pseudo()"
        if a == "exit":
            return "hl.dsp.exit()"
        return f'hl.dsp.exec_cmd("{self.arg}")'

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @classmethod
    def from_lua_line(cls, line: str) -> "ManagedBind | None":
        """Parse a flat hl.bind("COMBO", hl.dsp.XXX(...)) line."""
        line = line.strip()
        # Match hl.bind("...", hl.dsp.DISPATCH...)
        m = re.match(
            r'hl\.bind\s*\(\s*"([^"]+)"\s*,\s*(hl\.dsp\..+?)\s*\)\s*$',
            line,
        )
        if not m:
            return None
        combo_str, dispatch_str = m.group(1), m.group(2)

        parts = [p.strip() for p in combo_str.split("+")]
        parts = [p.strip() for p in combo_str.split(" + ")]
        if not parts:
            return None
        key = parts[-1]
        modifiers = [p.upper() for p in parts[:-1] if p]

        action, arg = cls._parse_dispatch(dispatch_str)
        if action is None:
            return None
        return cls(modifiers=modifiers, key=key, action=action, arg=arg)

    @staticmethod
    def _parse_dispatch(s: str) -> tuple[str | None, str]:
        if re.match(r'hl\.dsp\.exec_cmd\s*\(', s):
            m = re.match(r'hl\.dsp\.exec_cmd\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)', s)
            if m:
                return "exec", m.group(1).replace('\\"', '"')
            return None, ""
        if "hl.dsp.window.close" in s:
            return "close", ""
        if "hl.dsp.window.float" in s:
            return "float_toggle", ""
        if "hl.dsp.window.fullscreen" in s:
            return "fullscreen", ""
        if "hl.dsp.window.pseudo" in s:
            return "pseudo", ""
        if "hl.dsp.exit" in s:
            return "exit", ""
        return None, ""

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def combo_label(self) -> str:
        parts = self.modifiers + [self.key]
        return " + ".join(p.capitalize() if len(p) > 1 else p for p in parts)

    def action_label(self) -> str:
        label = _ACTION_LABEL.get(self.action, self.action)
        if self.action == "exec" and self.arg:
            return f"{label}: {self.arg}"
        return label


# ---------------------------------------------------------------------------
# Live binds helpers (read-only viewer)
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
    return {k: v for k, v in buckets.items() if v}


# ---------------------------------------------------------------------------
# Edit / Add dialog
# ---------------------------------------------------------------------------


class _BindDialog(Adw.Dialog):
    """Dialog for adding or editing a ManagedBind."""

    def __init__(self, bind: ManagedBind | None = None) -> None:
        super().__init__()
        self.set_title("Edit Keybind" if bind else "Add Keybind")
        self.set_content_width(440)
        self.set_content_height(520)

        self._result: ManagedBind | None = None
        self._editing = copy.deepcopy(bind) if bind else ManagedBind(
            modifiers=["SUPER"], key="", action="exec", arg=""
        )

        self._build_ui()

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("flat")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        self._save_btn = Gtk.Button(label="Save")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", self._on_save)
        header.pack_end(self._save_btn)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(scroll)

        page = Adw.PreferencesPage()
        scroll.set_child(page)

        # Modifiers group
        mod_group = Adw.PreferencesGroup()
        mod_group.set_title("Modifiers")
        page.add(mod_group)

        mod_row = Adw.ActionRow()
        mod_row.set_title("Modifier Keys")
        mod_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        mod_box.set_valign(Gtk.Align.CENTER)
        self._mod_buttons: dict[str, Gtk.ToggleButton] = {}
        for mod in _MOD_ORDER:
            label = mod.capitalize() if mod != "CTRL" else "Ctrl"
            btn = Gtk.ToggleButton(label=label)
            btn.set_active(mod in self._editing.modifiers)
            btn.connect("toggled", self._on_mod_toggled, mod)
            mod_box.append(btn)
            self._mod_buttons[mod] = btn
        mod_row.add_suffix(mod_box)
        mod_group.add(mod_row)

        # Key group
        key_group = Adw.PreferencesGroup()
        key_group.set_title("Key")
        page.add(key_group)

        self._key_row = Adw.EntryRow()
        self._key_row.set_title("Key Name")
        self._key_row.set_text(self._editing.key)
        self._key_row.connect("notify::text", self._on_key_changed)
        key_group.add(self._key_row)

        # Action group
        action_group = Adw.PreferencesGroup()
        action_group.set_title("Action")
        page.add(action_group)

        self._action_model = Gtk.StringList.new(_ACTION_LABELS)
        self._action_row = Adw.ComboRow()
        self._action_row.set_title("Action")
        self._action_row.set_model(self._action_model)
        try:
            idx = _ACTION_KEYS.index(self._editing.action)
        except ValueError:
            idx = 0
        self._action_row.set_selected(idx)
        self._action_row.connect("notify::selected", self._on_action_changed)
        action_group.add(self._action_row)

        self._cmd_row = Adw.EntryRow()
        self._cmd_row.set_title("Command")
        self._cmd_row.set_text(self._editing.arg)
        self._cmd_row.connect("notify::text", self._on_cmd_changed)
        action_group.add(self._cmd_row)

        self._update_cmd_row_sensitivity()
        self._update_save_sensitivity()

    def _on_mod_toggled(self, btn: Gtk.ToggleButton, mod: str) -> None:
        if btn.get_active():
            if mod not in self._editing.modifiers:
                self._editing.modifiers.append(mod)
        else:
            self._editing.modifiers = [m for m in self._editing.modifiers if m != mod]

    def _on_key_changed(self, row: Adw.EntryRow, _param) -> None:
        self._editing.key = row.get_text().strip()
        self._update_save_sensitivity()

    def _on_action_changed(self, row: Adw.ComboRow, _param) -> None:
        idx = row.get_selected()
        if 0 <= idx < len(_ACTION_KEYS):
            self._editing.action = _ACTION_KEYS[idx]
        self._update_cmd_row_sensitivity()

    def _on_cmd_changed(self, row: Adw.EntryRow, _param) -> None:
        self._editing.arg = row.get_text()

    def _update_cmd_row_sensitivity(self) -> None:
        self._cmd_row.set_sensitive(self._editing.action == "exec")

    def _update_save_sensitivity(self) -> None:
        self._save_btn.set_sensitive(bool(self._editing.key.strip()))

    def _on_save(self, _btn) -> None:
        self._editing.modifiers = [
            mod for mod in _MOD_ORDER if mod in self._editing.modifiers
        ]
        self._result = copy.deepcopy(self._editing)
        self.close()

    def get_result(self) -> ManagedBind | None:
        return self._result


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


class KeybindingsPage(Adw.PreferencesPage):
    """Editable managed-bind manager with live binds viewer."""

    __gtype_name__ = "KeybindingsPage"

    __gsignals__ = {
        "settings-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Keybindings")
        self.set_icon_name("input-keyboard-symbolic")

        self._config_path = None
        self._hyprctl_available = True
        self._managed_binds: list[ManagedBind] = []
        self._saved_binds: list[ManagedBind] = []

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Managed binds group
        self._managed_group = Adw.PreferencesGroup()
        self._managed_group.set_title("Managed Binds")
        self._managed_group.set_description(
            "Binds created here are written to ~/.config/hypr/keybinds.lua"
        )

        add_btn = Gtk.Button()
        add_btn.set_icon_name("list-add-symbolic")
        add_btn.set_tooltip_text("Add keybind")
        add_btn.add_css_class("flat")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.connect("clicked", self._on_add_clicked)
        self._managed_group.set_header_suffix(add_btn)

        self.add(self._managed_group)

        # Live binds group (header)
        self._live_header_group = Adw.PreferencesGroup()
        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.set_valign(Gtk.Align.CENTER)
        refresh_btn.add_css_class("flat")
        refresh_btn.connect("clicked", self._on_refresh_clicked)
        self._live_header_group.set_header_suffix(refresh_btn)
        self.add(self._live_header_group)

    # ------------------------------------------------------------------
    # Page API
    # ------------------------------------------------------------------

    def load(self, config_path, hyprctl_available: bool) -> None:
        self._config_path = config_path
        self._hyprctl_available = hyprctl_available

        # Ensure hyprland.lua sources managed-keybinds.lua (safe no-op if already present)
        if config_path is not None:
            try:
                ensure_section_file_sourced("managed-keybinds", config_path)
            except Exception as exc:
                log.warning("Could not inject managed-keybinds dofile: %s", exc)

        self._managed_binds = self._read_managed_binds()
        self._saved_binds = copy.deepcopy(self._managed_binds)

        self._rebuild_managed_rows()
        self._rebuild_live_groups()

    def collect_lines(self) -> list[str]:
        return [b.to_lua_line() for b in self._managed_binds]

    def mark_saved(self) -> None:
        self._saved_binds = copy.deepcopy(self._managed_binds)

    def revert_to_loaded(self) -> None:
        self._managed_binds = copy.deepcopy(self._saved_binds)
        self._rebuild_managed_rows()

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _read_managed_binds(self) -> list[ManagedBind]:
        keybinds_path = get_section_file_path("managed-keybinds")
        if not keybinds_path.exists():
            return []
        lines = keybinds_path.read_text(encoding="utf-8").splitlines()
        result: list[ManagedBind] = []
        for line in lines:
            b = ManagedBind.from_lua_line(line)
            if b is not None:
                result.append(b)
        return result

    # ------------------------------------------------------------------
    # Managed binds UI
    # ------------------------------------------------------------------

    def _rebuild_managed_rows(self) -> None:
        # Remove all existing rows from the managed group
        while True:
            child = self._managed_group.get_first_child()
            if child is None:
                break
            # PreferencesGroup first child may be the title box; we need to
            # remove ActionRows that were added via .add()
            # Walk all children and remove Adw.ActionRow instances
            break

        # Adw.PreferencesGroup doesn't expose a clean remove-all for added rows,
        # so we track row widgets ourselves and reparent via a listbox trick.
        # Simpler: clear by removing children from the internal listbox via the
        # public API — rebuild from scratch each time via a helper container.
        self._repopulate_managed_group()

    def _repopulate_managed_group(self) -> None:
        # Collect all ActionRows currently in the group and remove them
        rows_to_remove = []
        child = self._managed_group.get_first_child()
        while child is not None:
            if isinstance(child, Adw.ActionRow):
                rows_to_remove.append(child)
            child = child.get_next_sibling()
        for row in rows_to_remove:
            self._managed_group.remove(row)

        if not self._managed_binds:
            empty_row = Adw.ActionRow()
            empty_row.set_title("No managed binds yet")
            empty_row.set_subtitle('Click "+" to add your first keybind')
            empty_row.set_sensitive(False)
            self._managed_group.add(empty_row)
            return

        for idx, bind in enumerate(self._managed_binds):
            row = Adw.ActionRow()
            row.set_title(bind.combo_label())
            row.set_subtitle(bind.action_label())
            row.set_activatable(True)
            row.connect("activated", self._on_row_activated, idx)

            del_btn = Gtk.Button()
            del_btn.set_icon_name("user-trash-symbolic")
            del_btn.set_tooltip_text("Remove this keybind")
            del_btn.add_css_class("flat")
            del_btn.add_css_class("destructive-action")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.connect("clicked", self._on_delete_clicked, idx)
            row.add_suffix(del_btn)

            self._managed_group.add(row)

    # ------------------------------------------------------------------
    # Live binds UI
    # ------------------------------------------------------------------

    def _rebuild_live_groups(self) -> None:
        # Remove any previously added live-bind preference groups (after the
        # first two permanent groups: managed_group and live_header_group)
        to_remove = []
        child = self.get_first_child()
        while child is not None:
            if child not in (self._managed_group, self._live_header_group):
                to_remove.append(child)
            child = child.get_next_sibling()
        for c in to_remove:
            self.remove(c)

        if not self._hyprctl_available:
            self._show_live_unavailable()
            return

        binds = _get_binds()
        if not binds:
            self._show_live_unavailable()
            return

        categories = _categorise(binds)
        for group_name, group_binds in categories.items():
            group = Adw.PreferencesGroup()
            group.set_title(f"Active Binds (Live) — {group_name}")
            for bind in group_binds:
                combo, action = _format_bind(bind)
                row = Adw.ActionRow()
                row.set_title(combo if combo else "(no key)")
                row.set_subtitle(action)
                row.set_activatable(False)
                if bind.get("locked"):
                    lock_label = Gtk.Label(label="\U0001F512")
                    lock_label.set_valign(Gtk.Align.CENTER)
                    row.add_suffix(lock_label)
                group.add(row)
            self.add(group)

    def _show_live_unavailable(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("Active Binds (Live)")
        status = Adw.StatusPage()
        status.set_icon_name("input-keyboard-symbolic")
        status.set_title("Live Binds Unavailable")
        status.set_description(
            "Start the app inside a Hyprland session to view active keybindings."
        )
        group.add(status)
        self.add(group)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_add_clicked(self, _btn) -> None:
        dialog = _BindDialog(bind=None)
        dialog.connect("closed", self._on_dialog_closed, None)
        dialog.present(self)

    def _on_row_activated(self, _row, idx: int) -> None:
        if 0 <= idx < len(self._managed_binds):
            dialog = _BindDialog(bind=self._managed_binds[idx])
            dialog.connect("closed", self._on_dialog_closed, idx)
            dialog.present(self)

    def _on_delete_clicked(self, _btn, idx: int) -> None:
        if 0 <= idx < len(self._managed_binds):
            self._managed_binds.pop(idx)
            self._repopulate_managed_group()
            self.emit("settings-changed")

    def _on_dialog_closed(self, dialog: _BindDialog, idx) -> None:
        result = dialog.get_result()
        if result is None:
            return  # cancelled
        if idx is None:
            self._managed_binds.append(result)
        elif 0 <= idx < len(self._managed_binds):
            self._managed_binds[idx] = result
        self._repopulate_managed_group()
        self.emit("settings-changed")

    def _on_refresh_clicked(self, _btn) -> None:
        self._rebuild_live_groups()
