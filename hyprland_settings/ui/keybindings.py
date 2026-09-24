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
    get_section_file_path,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported actions
# ---------------------------------------------------------------------------

_ACTIONS = [
    ("exec",           "Run command"),
    ("close",          "Close window"),
    ("float_toggle",   "Toggle floating"),
    ("fullscreen",     "Toggle fullscreen"),
    ("pseudo",         "Toggle pseudo-tile"),
    ("exit",           "Exit Hyprland"),
    ("focus_dir",      "Focus direction"),
    ("focus_ws",       "Focus workspace"),
    ("move_to_ws",     "Move to workspace"),
    ("toggle_special", "Toggle special workspace"),
    ("layout",         "Layout action"),
    ("drag",           "Drag window"),
    ("resize",         "Resize window"),
]
_ACTION_KEYS   = [a[0] for a in _ACTIONS]
_ACTION_LABELS = [a[1] for a in _ACTIONS]
_ACTION_LABEL  = dict(_ACTIONS)

_MOD_ORDER = ["SUPER", "CTRL", "SHIFT", "ALT"]

# ---------------------------------------------------------------------------
# Lua preprocessing helpers
# ---------------------------------------------------------------------------


def _preprocess_lua(text: str) -> dict[str, str]:
    """Extract simple local variable assignments from Lua source.

    Handles:
        local VAR = "string value"
        local VAR = 'string value'
        local VAR = number
    Returns a dict mapping variable name to its string value.
    """
    vars_: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'^local\s+(\w+)\s*=\s*"([^"]*)"', line)
        if m:
            vars_[m.group(1)] = m.group(2)
            continue
        m = re.match(r"^local\s+(\w+)\s*=\s*'([^']*)'", line)
        if m:
            vars_[m.group(1)] = m.group(2)
            continue
        m = re.match(r'^local\s+(\w+)\s*=\s*(-?\d+(?:\.\d+)?)\s*(?:--.*)?$', line)
        if m:
            vars_[m.group(1)] = m.group(2)
    return vars_


def _split_lua_args(s: str) -> list[str]:
    """Split comma-separated Lua arguments respecting strings, parens and braces."""
    args: list[str] = []
    current = ""
    depth = 0
    in_string: str | None = None
    i = 0
    while i < len(s):
        c = s[i]
        if in_string:
            if c == '\\':
                current += c
                i += 1
                if i < len(s):
                    current += s[i]
                i += 1
                continue
            if c == in_string:
                in_string = None
            current += c
            i += 1
            continue
        if c in ('"', "'"):
            in_string = c
            current += c
            i += 1
            continue
        if c in ('(', '{', '['):
            depth += 1
            current += c
            i += 1
            continue
        if c in (')', '}', ']'):
            depth -= 1
            current += c
            i += 1
            continue
        if c == ',' and depth == 0:
            args.append(current.strip())
            current = ""
            i += 1
            continue
        current += c
        i += 1
    if current.strip():
        args.append(current.strip())
    return args


def _find_bind_inner(line: str) -> str | None:
    """Return the content inside the outermost hl.bind(...).

    Strips optional leading assignment (``local x =`` or ``x =``).
    Returns None if the line is not a hl.bind() call.
    """
    line = line.strip()
    # Strip optional assignment: [local] IDENTIFIER = <lookahead hl.bind>
    line = re.sub(r'^(?:local\s+)?\w+\s*=\s*(?=hl\.bind)', '', line)

    if not line.startswith('hl.bind('):
        return None

    paren_start = line.index('(')
    depth = 1
    in_string: str | None = None
    i = paren_start + 1
    while i < len(line) and depth > 0:
        c = line[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == in_string:
                in_string = None
        elif c in ('"', "'"):
            in_string = c
        elif c in ('(', '{', '['):
            depth += 1
        elif c in (')', '}', ']'):
            depth -= 1
        i += 1

    if depth != 0:
        return None

    return line[paren_start + 1: i - 1]


def _split_concat(expr: str) -> list[str] | None:
    """Split a Lua expression on ``..`` concatenation operators."""
    parts: list[str] = []
    current = ""
    in_string: str | None = None
    i = 0
    while i < len(expr):
        c = expr[i]
        if in_string:
            if c == '\\':
                current += c
                i += 1
                if i < len(expr):
                    current += expr[i]
                i += 1
                continue
            if c == in_string:
                in_string = None
            current += c
            i += 1
            continue
        if c in ('"', "'"):
            in_string = c
            current += c
            i += 1
            continue
        if c == '.' and i + 1 < len(expr) and expr[i + 1] == '.':
            if i + 2 < len(expr) and expr[i + 2] == '.':
                return None  # varargs '...'
            parts.append(current.strip())
            current = ""
            i += 2
            continue
        current += c
        i += 1
    if current.strip():
        parts.append(current.strip())
    return parts if parts else None


def _resolve_combo(expr: str, vars_: dict[str, str]) -> str | None:
    """Resolve the first argument of hl.bind() to a plain combo string.

    Returns None if the expression cannot be resolved (contains function
    calls, os.getenv, or unknown variables).
    """
    expr = expr.strip()
    if any(kw in expr for kw in ("function", "os.getenv")):
        return None

    # Already a plain string literal
    m = re.match(r'^"([^"]*)"$', expr)
    if m:
        return m.group(1)
    m = re.match(r"^'([^']*)'$", expr)
    if m:
        return m.group(1)

    parts = _split_concat(expr)
    if parts is None:
        return None

    result = ""
    for part in parts:
        part = part.strip()
        m = re.match(r'^"([^"]*)"$', part)
        if m:
            result += m.group(1)
            continue
        m = re.match(r"^'([^']*)'$", part)
        if m:
            result += m.group(1)
            continue
        m = re.match(r'^(\d+)$', part)
        if m:
            result += m.group(1)
            continue
        if part in vars_:
            result += vars_[part]
            continue
        return None

    return result


def _contains_lambda(s: str) -> bool:
    """Return True if the expression contains a Lua function literal."""
    return bool(re.search(r'\bfunction\b', s))


def _expand_body(
    body: list[str],
    vars_: dict[str, str],
    output: list[str],
) -> None:
    """Expand a loop body into *output*, evaluating simple conditionals.

    Numeric loop variables in *vars_* are substituted directly into each line
    so that subsequent ``..`` resolution can treat them as integer literals.
    String variables are left for ``_resolve_combo`` to handle later.
    """
    j = 0
    while j < len(body):
        bl = body[j].strip()

        # local KEY = VAR % N
        m = re.match(r'^local\s+(\w+)\s*=\s*(\w+)\s*%\s*(\d+)\s*(?:--.*)?$', bl)
        if m:
            var_name = m.group(1)
            src_var = m.group(2)
            mod_val = int(m.group(3))
            if src_var in vars_:
                try:
                    computed = int(vars_[src_var]) % mod_val
                    vars_[var_name] = str(computed)
                except ValueError:
                    pass
            j += 1
            continue

        # if VAR OP VALUE then ... end
        m = re.match(
            r'^if\s+(\w+)\s*(~=|==|<=|>=|<|>)\s*(\d+)\s*then\s*(?:--.*)?$', bl
        )
        if m:
            if_var = m.group(1)
            if_op = m.group(2)
            if_val = int(m.group(3))
            if_body: list[str] = []
            j += 1
            depth = 1
            while j < len(body) and depth > 0:
                ibl = body[j].strip()
                if re.match(r'^if\s', ibl) and ibl.endswith('then'):
                    depth += 1
                elif ibl in ('end',) or ibl.startswith('end ') or ibl.startswith('end--'):
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                if depth > 0:
                    if_body.append(body[j])
                j += 1

            if if_var in vars_:
                try:
                    actual = int(vars_[if_var])
                    _OPS = {
                        '~=': lambda a, b: a != b,
                        '==': lambda a, b: a == b,
                        '<':  lambda a, b: a < b,
                        '>':  lambda a, b: a > b,
                        '<=': lambda a, b: a <= b,
                        '>=': lambda a, b: a >= b,
                    }
                    op_fn = _OPS.get(if_op)
                    if op_fn and op_fn(actual, if_val):
                        _expand_body(if_body, vars_, output)
                except ValueError:
                    pass
            continue

        # Regular line — substitute numeric loop variables
        substituted = bl
        for name, val in vars_.items():
            if re.match(r'^\d+$', val):
                substituted = re.sub(
                    r'\b' + re.escape(name) + r'\b', val, substituted
                )
        output.append(substituted)
        j += 1


def _expand_for_loops(text: str, vars_: dict[str, str]) -> str:
    """Expand simple ``for VAR = N, M do … end`` loops in Lua source text."""
    lines = text.splitlines()
    result: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        m = re.match(
            r'^for\s+(\w+)\s*=\s*(\d+)\s*,\s*(\d+)\s*do\s*(?:--.*)?$', stripped
        )
        if m:
            loop_var = m.group(1)
            loop_start = int(m.group(2))
            loop_end = int(m.group(3))
            body: list[str] = []
            depth = 1
            i += 1
            while i < len(lines) and depth > 0:
                bl = lines[i].strip()
                if re.match(r'^for\s+\w+\s*=', bl) or re.match(r'^while\s', bl):
                    depth += 1
                elif re.match(r'^do\s*(?:--.*)?$', bl):
                    depth += 1
                elif bl in ('end',) or bl.startswith('end ') or bl.startswith('end--'):
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                if depth > 0:
                    body.append(lines[i])
                i += 1

            for loop_val in range(loop_start, loop_end + 1):
                iter_vars = dict(vars_)
                iter_vars[loop_var] = str(loop_val)
                _expand_body(body, iter_vars, result)
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)


def _try_parse_bind_line(
    line: str, vars_: dict[str, str]
) -> "ManagedBind | None":
    """Try to parse a bind line, resolving Lua variables first.

    Returns a ManagedBind (possibly ``raw=True``) or None if the line
    contains no hl.bind() call at all.
    """
    stripped = line.strip()

    if 'hl.bind' not in stripped:
        return None

    # Function literal as dispatch — must be raw
    if _contains_lambda(stripped):
        return ManagedBind(
            modifiers=[], key="", action="exec", raw=True, raw_line=stripped
        )

    inner = _find_bind_inner(stripped)
    if inner is None:
        return ManagedBind(
            modifiers=[], key="", action="exec", raw=True, raw_line=stripped
        )

    args = _split_lua_args(inner)
    if len(args) < 2:
        return ManagedBind(
            modifiers=[], key="", action="exec", raw=True, raw_line=stripped
        )

    combo_expr = args[0].strip()
    dispatch_expr = args[1].strip()
    options_expr = args[2].strip() if len(args) >= 3 else ""

    combo_str = _resolve_combo(combo_expr, vars_)
    if combo_str is None:
        return ManagedBind(
            modifiers=[], key="", action="exec", raw=True, raw_line=stripped
        )

    # Substitute known string variables in the dispatch expression so that
    # exec_cmd(terminal) → exec_cmd("kitty") and becomes parseable.
    resolved_dispatch = dispatch_expr
    for vname, vval in vars_.items():
        resolved_dispatch = re.sub(
            r'\b' + re.escape(vname) + r'\b',
            f'"{vval}"',
            resolved_dispatch,
        )

    if options_expr:
        resolved = f'hl.bind("{combo_str}", {resolved_dispatch}, {options_expr})'
    else:
        resolved = f'hl.bind("{combo_str}", {resolved_dispatch})'

    result = ManagedBind.from_lua_line(resolved)
    return result  # may be None if dispatch still unresolvable


def _preprocess_and_parse_file(text: str) -> list["ManagedBind"]:
    """Parse all hl.bind() calls from Lua source text.

    1. Extracts simple variable assignments.
    2. Expands numeric for-loops.
    3. Resolves ``..`` expressions in combo strings.
    4. Returns ManagedBind objects; unparseable bind lines are marked
       ``raw=True`` so they appear read-only in the UI.
    """
    vars_ = _preprocess_lua(text)
    expanded = _expand_for_loops(text, vars_)

    result: list[ManagedBind] = []
    seen: set[str] = set()

    for raw_line in expanded.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith('--'):
            continue
        if 'hl.bind' not in stripped:
            continue

        b = _try_parse_bind_line(stripped, vars_)
        if b is None:
            # hl.bind present but dispatch is unresolvable — emit as raw
            key = stripped
            if key not in seen:
                seen.add(key)
                result.append(
                    ManagedBind(
                        modifiers=[], key="", action="exec",
                        raw=True, raw_line=stripped,
                    )
                )
            continue

        key = b.raw_line.strip() if b.raw else b.to_lua_line()
        if key not in seen:
            seen.add(key)
            result.append(b)

    return result


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ManagedBind:
    modifiers: list[str]
    key: str
    action: str        # one of _ACTION_KEYS
    arg: str = ""
    locked: bool = False
    repeating: bool = False
    mouse: bool = False
    raw: bool = False       # True = shown read-only, not editable via UI
    raw_line: str = ""      # original line for read-only display

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_lua_line(self) -> str:
        if self.raw:
            return self.raw_line
        combo = " + ".join(self.modifiers + [self.key])
        dispatch = self._action_lua()
        opts: list[str] = []
        if self.locked:
            opts.append("locked = true")
        if self.repeating:
            opts.append("repeating = true")
        if self.mouse:
            opts.append("mouse = true")
        if opts:
            opts_str = "{ " + ", ".join(opts) + " }"
            return f'hl.bind("{combo}", {dispatch}, {opts_str})'
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
        if a == "focus_dir":
            return f'hl.dsp.focus({{ direction = "{self.arg}" }})'
        if a == "focus_ws":
            if re.match(r'^\d+$', self.arg):
                return f'hl.dsp.focus({{ workspace = {self.arg}}})'
            return f'hl.dsp.focus({{ workspace = "{self.arg}"}})'
        if a == "move_to_ws":
            if re.match(r'^\d+$', self.arg):
                return f'hl.dsp.window.move({{ workspace = {self.arg} }})'
            return f'hl.dsp.window.move({{ workspace = "{self.arg}" }})'
        if a == "toggle_special":
            return f'hl.dsp.workspace.toggle_special("{self.arg}")'
        if a == "layout":
            return f'hl.dsp.layout("{self.arg}")'
        if a == "drag":
            return "hl.dsp.window.drag()"
        if a == "resize":
            return "hl.dsp.window.resize()"
        # fallback
        escaped = self.arg.replace('"', '\\"')
        return f'hl.dsp.exec_cmd("{escaped}")'

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @classmethod
    def from_lua_line(cls, line: str) -> "ManagedBind | None":
        """Parse a flat hl.bind("COMBO", hl.dsp.XXX(...)[, {opts}]) line."""
        line = line.strip()

        inner = _find_bind_inner(line)
        if inner is None:
            return None

        args = _split_lua_args(inner)
        if len(args) < 2:
            return None

        combo_expr = args[0].strip()
        dispatch_expr = args[1].strip()
        options_expr = args[2].strip() if len(args) >= 3 else ""

        # Combo must be a literal string at this point
        m = re.match(r'^"([^"]*)"$', combo_expr)
        if not m:
            return None
        combo_str = m.group(1)

        parts = [p.strip() for p in combo_str.split(" + ")]
        if not parts:
            return None
        key = parts[-1]
        modifiers = [p.upper() for p in parts[:-1] if p]

        action, arg = cls._parse_dispatch(dispatch_expr)
        if action is None:
            return None

        locked = repeating = mouse = False
        if options_expr:
            locked    = bool(re.search(r'\blocked\s*=\s*true\b',    options_expr))
            repeating = bool(re.search(r'\brepeating\s*=\s*true\b', options_expr))
            mouse     = bool(re.search(r'\bmouse\s*=\s*true\b',     options_expr))

        return cls(
            modifiers=modifiers, key=key, action=action, arg=arg,
            locked=locked, repeating=repeating, mouse=mouse,
        )

    @staticmethod
    def _parse_dispatch(s: str) -> tuple[str | None, str]:
        # exec_cmd — literal string arg only
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
        if re.search(r'hl\.dsp\.exit\s*\(', s):
            return "exit", ""

        # focus({ direction = "X" })
        m = re.match(
            r'hl\.dsp\.focus\s*\(\s*\{\s*direction\s*=\s*"([^"]+)"\s*\}\s*\)', s
        )
        if m:
            return "focus_dir", m.group(1)

        # focus({ workspace = "e+1" }) — string workspace
        m = re.match(
            r'hl\.dsp\.focus\s*\(\s*\{\s*workspace\s*=\s*"([^"]+)"\s*\}\s*\)', s
        )
        if m:
            return "focus_ws", m.group(1)

        # focus({ workspace = N }) — integer workspace
        m = re.match(
            r'hl\.dsp\.focus\s*\(\s*\{\s*workspace\s*=\s*(\d+)\s*\}\s*\)', s
        )
        if m:
            return "focus_ws", m.group(1)

        # window.move({ workspace = "..." })
        m = re.match(
            r'hl\.dsp\.window\.move\s*\(\s*\{\s*workspace\s*=\s*"([^"]+)"\s*\}\s*\)', s
        )
        if m:
            return "move_to_ws", m.group(1)

        # window.move({ workspace = N })
        m = re.match(
            r'hl\.dsp\.window\.move\s*\(\s*\{\s*workspace\s*=\s*(\d+)\s*\}\s*\)', s
        )
        if m:
            return "move_to_ws", m.group(1)

        # workspace.toggle_special("name")
        m = re.match(
            r'hl\.dsp\.workspace\.toggle_special\s*\(\s*"([^"]*)"\s*\)', s
        )
        if m:
            return "toggle_special", m.group(1)

        # layout("arg")
        m = re.match(r'hl\.dsp\.layout\s*\(\s*"([^"]*)"\s*\)', s)
        if m:
            return "layout", m.group(1)

        # window.drag()
        if re.match(r'hl\.dsp\.window\.drag\s*\(\s*\)', s):
            return "drag", ""

        # window.resize()
        if re.match(r'hl\.dsp\.window\.resize\s*\(\s*\)', s):
            return "resize", ""

        if "function" in s:
            return None, ""

        return None, ""

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def combo_label(self) -> str:
        if self.raw:
            return self.raw_line.strip()
        parts = self.modifiers + [self.key]
        return " + ".join(p.capitalize() if len(p) > 1 else p for p in parts)

    def action_label(self) -> str:
        if self.raw:
            return "Custom — edit in keybinds.lua"
        label = _ACTION_LABEL.get(self.action, self.action)
        if self.action in ("exec", "focus_dir", "focus_ws", "move_to_ws",
                           "toggle_special", "layout") and self.arg:
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

        self._managed_binds = self._read_managed_binds()
        self._saved_binds = copy.deepcopy(self._managed_binds)

        self._rebuild_managed_rows()
        self._rebuild_live_groups()

    def collect_lines(self) -> list[str]:
        return [b.to_lua_line() for b in self._managed_binds if not b.raw]

    def mark_saved(self) -> None:
        self._saved_binds = copy.deepcopy(self._managed_binds)

    def revert_to_loaded(self) -> None:
        self._managed_binds = copy.deepcopy(self._saved_binds)
        self._rebuild_managed_rows()

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _read_managed_binds(self) -> list[ManagedBind]:
        """Parse ALL hl.bind() calls from keybinds.lua using full preprocessing."""
        keybinds_path = get_section_file_path("keybinds")
        if not keybinds_path.exists():
            return []
        text = keybinds_path.read_text(encoding="utf-8")
        return _preprocess_and_parse_file(text)

    def write_keybinds(self) -> None:
        """Write managed binds to keybinds.lua with markers, stripping any
        previously-handwritten hl.bind() lines that are now managed."""
        import os
        from hyprland_settings.backend.config_writer import (
            _section_markers, ConfigFormat, write_section_to_config,
        )
        keybinds_path = get_section_file_path("keybinds")
        marker_start, marker_end = _section_markers("keybinds", ConfigFormat.LUA)
        # Only non-raw binds are written to the managed section
        non_raw_binds = [b for b in self._managed_binds if not b.raw]
        managed_lua: set[str] = {b.to_lua_line() for b in non_raw_binds}

        if keybinds_path.exists():
            lines = keybinds_path.read_text(encoding="utf-8").splitlines(keepends=True)
            inside = False
            cleaned: list[str] = []
            for line in lines:
                stripped = line.strip()
                if stripped == marker_start:
                    inside = True
                    cleaned.append(line)
                    continue
                if stripped == marker_end:
                    inside = False
                    cleaned.append(line)
                    continue
                if not inside:
                    b = ManagedBind.from_lua_line(stripped)
                    if b is not None and not b.raw and b.to_lua_line() in managed_lua:
                        continue  # will be written inside the managed section
                cleaned.append(line)
            tmp = keybinds_path.with_suffix(".tmp")
            tmp.write_text("".join(cleaned), encoding="utf-8")
            os.replace(tmp, keybinds_path)

        write_section_to_config(
            "keybinds",
            [b.to_lua_line() for b in non_raw_binds],
            keybinds_path,
        )

    # ------------------------------------------------------------------
    # Managed binds UI
    # ------------------------------------------------------------------

    def _rebuild_managed_rows(self) -> None:
        self._repopulate_managed_group()

    def _repopulate_managed_group(self) -> None:
        # Remove all existing ActionRows from the group
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

            if bind.raw:
                # Read-only row for unparseable / custom binds
                row.set_title(bind.raw_line.strip())
                row.set_subtitle("Custom — edit in keybinds.lua")
                row.set_activatable(False)
            else:
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
        # Remove any previously added live-bind preference groups
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
            bind = self._managed_binds[idx]
            if bind.raw:
                return
            dialog = _BindDialog(bind=bind)
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
