"""Tests for hyprland_settings.backend.config_writer"""

import os
import pytest
from pathlib import Path

from hyprland_settings.backend.config_writer import (
    ConfigNotFoundError,
    MonitorConfig,
    ConfigFormat,
    CONF_MARKER_START,
    CONF_MARKER_END,
    MARKER_START,
    MARKER_END,
    find_config_path,
    detect_format,
    read_monitors_from_config,
    write_monitors_to_config,
    write_section_file,
    get_section_file_path,
    _session_backed_up,
)
from hyprland_settings.backend.hyprctl import Monitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_conf(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def make_monitor(**overrides) -> Monitor:
    base = dict(
        id=0, name="DP-1", description="Test", make="", model="", serial="",
        width=1920, height=1080, refresh_rate=144.0,
        x=0, y=0, scale=1.0, transform=0,
        focused=True, dpms_status=True, vrr=False,
    )
    base.update(overrides)
    return Monitor(**base)


BASIC_MONITORS = [
    make_monitor(name="DP-1", width=1920, height=1080, refresh_rate=144.0,
                 x=0, y=0, scale=1.0),
    make_monitor(name="HDMI-A-1", width=2560, height=1440, refresh_rate=60.0,
                 x=1920, y=0, scale=1.5),
]


# ---------------------------------------------------------------------------
# MonitorConfig serialization
# ---------------------------------------------------------------------------

class TestToConfigLine:
    def test_basic(self):
        m = MonitorConfig("DP-1", "1920x1080", 144.0, "0x0", 1.0, None, None, "")
        assert m.to_config_line() == "monitor = DP-1, 1920x1080@144, 0x0, 1"

    def test_fractional_scale(self):
        m = MonitorConfig("HDMI-A-1", "2560x1440", 60.0, "1920x0", 1.5, None, None, "")
        assert m.to_config_line() == "monitor = HDMI-A-1, 2560x1440@60, 1920x0, 1.5"

    def test_disabled(self):
        m = MonitorConfig("HDMI-1", "disabled", None, "", 1, None, None, "")
        assert m.to_config_line() == "monitor = HDMI-1, disabled"

    def test_transform(self):
        m = MonitorConfig("eDP-1", "2880x1800", 90.0, "0x0", 1.0, 1, None, "")
        assert "transform, 1" in m.to_config_line()

    def test_mirror(self):
        m = MonitorConfig("HDMI-A-1", "preferred", None, "auto", 1.0, None, "eDP-1", "")
        assert "mirror, eDP-1" in m.to_config_line()

    def test_extra_preserved(self):
        m = MonitorConfig("DP-1", "1920x1080", 60.0, "0x0", 1.0, None, None, "bitdepth, 10")
        assert "bitdepth, 10" in m.to_config_line()

    def test_fractional_refresh(self):
        m = MonitorConfig("DP-1", "1920x1080", 59.94, "0x0", 1.0, None, None, "")
        assert "@59.94" in m.to_config_line()

    def test_auto_scale(self):
        m = MonitorConfig("DP-1", "preferred", None, "auto", "auto", None, None, "")
        assert "auto" in m.to_config_line()


class TestToLuaLine:
    def test_basic(self):
        m = MonitorConfig("DP-1", "1920x1080", 144.0, "0x0", 1.0, None, None, "")
        line = m.to_lua_line()
        assert 'output = "DP-1"' in line
        assert 'mode = "1920x1080@144"' in line
        assert 'position = "0x0"' in line
        assert 'scale = "1"' in line

    def test_disabled(self):
        m = MonitorConfig("DP-1", "disabled", None, "", 1, None, None, "")
        assert 'mode = "disabled"' in m.to_lua_line()

    def test_transform_omitted_when_zero(self):
        m = MonitorConfig("DP-1", "1920x1080", 60.0, "0x0", 1.0, 0, None, "")
        assert "transform" not in m.to_lua_line()

    def test_transform_included_when_nonzero(self):
        m = MonitorConfig("DP-1", "1920x1080", 60.0, "0x0", 1.0, 1, None, "")
        assert "transform = 1" in m.to_lua_line()


# ---------------------------------------------------------------------------
# detect_format
# ---------------------------------------------------------------------------

class TestDetectFormat:
    def test_lua(self, tmp_path):
        p = tmp_path / "hyprland.lua"
        p.touch()
        assert detect_format(p) == ConfigFormat.LUA

    def test_conf(self, tmp_path):
        p = tmp_path / "hyprland.conf"
        p.touch()
        assert detect_format(p) == ConfigFormat.HYPRLANG


# ---------------------------------------------------------------------------
# find_config_path
# ---------------------------------------------------------------------------

class TestFindConfigPath:
    def test_env_var_override(self, tmp_path, monkeypatch):
        cfg = tmp_path / "hyprland.conf"
        cfg.write_text("# test")
        monkeypatch.setenv("HYPRLAND_CONFIG", str(cfg))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert find_config_path() == cfg

    def test_prefers_lua_over_conf(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HYPRLAND_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        hypr = tmp_path / "hypr"
        hypr.mkdir()
        lua = hypr / "hyprland.lua"
        conf = hypr / "hyprland.conf"
        lua.write_text("-- lua")
        conf.write_text("# conf")
        assert find_config_path() == lua

    def test_raises_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HYPRLAND_CONFIG", raising=False)
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(empty))
        # Patch expanduser so ~/.config/... doesn't resolve to the real home
        monkeypatch.setattr(Path, "expanduser", lambda self: empty / self.name)
        with pytest.raises(ConfigNotFoundError):
            find_config_path()


# ---------------------------------------------------------------------------
# read_monitors_from_config — hyprlang
# ---------------------------------------------------------------------------

class TestReadHyprlang:
    def test_parses_basic_lines(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.conf",
            "monitor = DP-1, 1920x1080@144, 0x0, 1\n"
            "monitor = HDMI-A-1, 2560x1440@60, 1920x0, 1.5\n")
        monitors = read_monitors_from_config(cfg)
        assert len(monitors) == 2
        assert monitors[0].name == "DP-1"
        assert monitors[1].scale == 1.5

    def test_skips_comments_and_blank_lines(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.conf",
            "# comment\n\nmonitor = DP-1, 1920x1080@144, 0x0, 1\n")
        assert len(read_monitors_from_config(cfg)) == 1

    def test_empty_file(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.conf", "")
        assert read_monitors_from_config(cfg) == []

    def test_disabled_monitor(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.conf", "monitor = HDMI-1, disabled\n")
        assert read_monitors_from_config(cfg)[0].resolution == "disabled"


# ---------------------------------------------------------------------------
# read_monitors_from_config — Lua
# ---------------------------------------------------------------------------

class TestReadLua:
    def test_parses_basic_hl_monitor(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.lua",
            'hl.monitor({ output = "DP-1", mode = "1920x1080@144", position = "0x0", scale = "1" })\n')
        monitors = read_monitors_from_config(cfg)
        assert len(monitors) == 1
        assert monitors[0].name == "DP-1"
        assert monitors[0].resolution == "1920x1080"
        assert monitors[0].refresh == 144.0

    def test_skips_catch_all_empty_output(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.lua",
            'hl.monitor({ output = "DP-1", mode = "1920x1080@60", position = "0x0", scale = "1" })\n'
            'hl.monitor({ output = "", mode = "preferred", position = "auto", scale = "auto" })\n')
        monitors = read_monitors_from_config(cfg)
        assert len(monitors) == 1
        assert monitors[0].name == "DP-1"

    def test_parses_multiline_block(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.lua",
            'hl.monitor({\n'
            '    output   = "eDP-1",\n'
            '    mode     = "1920x1200@60",\n'
            '    position = "0x0",\n'
            '    scale    = "1",\n'
            '})\n')
        monitors = read_monitors_from_config(cfg)
        assert len(monitors) == 1
        assert monitors[0].name == "eDP-1"
        assert monitors[0].refresh == 60.0


# ---------------------------------------------------------------------------
# write_monitors_to_config — hyprlang
# ---------------------------------------------------------------------------

class TestWriteHyprlang:
    def setup_method(self):
        _session_backed_up.clear()

    def test_appends_markers_to_fresh_file(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.conf", "# user config\n")
        write_monitors_to_config(BASIC_MONITORS, cfg)
        content = cfg.read_text()
        assert CONF_MARKER_START in content
        assert CONF_MARKER_END in content
        assert "monitor = DP-1, 1920x1080@144, 0x0, 1" in content
        assert "# user config" in content

    def test_replaces_existing_marker_block(self, tmp_path):
        initial = (
            "# user config\n"
            f"{CONF_MARKER_START}\n"
            "monitor = OLD-MONITOR, 1280x720@60, 0x0, 1\n"
            f"{CONF_MARKER_END}\n"
            "# more user config\n"
        )
        cfg = make_conf(tmp_path, "hyprland.conf", initial)
        write_monitors_to_config(BASIC_MONITORS, cfg)
        content = cfg.read_text()
        assert "OLD-MONITOR" not in content
        assert "DP-1" in content
        assert "# more user config" in content

    def test_creates_backup(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.conf", "# original\n")
        write_monitors_to_config(BASIC_MONITORS, cfg)
        bak = cfg.with_suffix(".conf.hyprland-settings.bak")
        assert bak.exists()
        assert "# original" in bak.read_text()

    def test_atomic_write(self, tmp_path, monkeypatch):
        cfg = make_conf(tmp_path, "hyprland.conf", "")
        replaced = []
        orig = os.replace
        monkeypatch.setattr(os, "replace", lambda s, d: (replaced.append(s), orig(s, d)))
        write_monitors_to_config(BASIC_MONITORS, cfg)
        assert str(replaced[0]).endswith(".tmp")

    def test_crlf_preserved(self, tmp_path):
        cfg = tmp_path / "hyprland.conf"
        cfg.write_bytes(b"# user config\r\n")
        write_monitors_to_config(BASIC_MONITORS, cfg)
        assert b"\r\n" in cfg.read_bytes()


# ---------------------------------------------------------------------------
# write_monitors_to_config — Lua
# ---------------------------------------------------------------------------

class TestWriteLua:
    def setup_method(self):
        _session_backed_up.clear()

    def test_appends_lua_markers_to_fresh_file(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.lua", "-- user config\n")
        write_monitors_to_config(BASIC_MONITORS, cfg)
        content = cfg.read_text()
        assert MARKER_START in content
        assert MARKER_END in content
        assert 'output = "DP-1"' in content
        assert "-- user config" in content

    def test_replaces_existing_lua_marker_block(self, tmp_path):
        initial = (
            "-- user config\n"
            f"{MARKER_START}\n"
            'hl.monitor({ output = "OLD", mode = "1280x720@60", position = "0x0", scale = "1" })\n'
            f"{MARKER_END}\n"
            "-- after block\n"
        )
        cfg = make_conf(tmp_path, "hyprland.lua", initial)
        write_monitors_to_config(BASIC_MONITORS, cfg)
        content = cfg.read_text()
        assert '"OLD"' not in content
        assert '"DP-1"' in content
        assert "-- after block" in content

    def test_inserts_before_catch_all(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.lua",
            '-- preamble\n'
            'hl.monitor({ output = "", mode = "preferred", position = "auto", scale = "auto" })\n')
        write_monitors_to_config(BASIC_MONITORS, cfg)
        content = cfg.read_text()
        # Our block must appear before the catch-all
        our_pos = content.index(MARKER_START)
        catchall_pos = content.index('output = ""')
        assert our_pos < catchall_pos

    def test_creates_backup(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.lua", "-- original\n")
        write_monitors_to_config(BASIC_MONITORS, cfg)
        bak = cfg.with_suffix(".lua.hyprland-settings.bak")
        assert bak.exists()


# ---------------------------------------------------------------------------
# Round-trip — hyprlang
# ---------------------------------------------------------------------------

class TestRoundTripHyprlang:
    def setup_method(self):
        _session_backed_up.clear()

    def test_write_then_read(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.conf", "")
        write_monitors_to_config(BASIC_MONITORS, cfg)
        read_back = read_monitors_from_config(cfg)
        assert len(read_back) == len(BASIC_MONITORS)
        assert read_back[0].name == BASIC_MONITORS[0].name
        assert read_back[1].name == BASIC_MONITORS[1].name


# ---------------------------------------------------------------------------
# Round-trip — Lua
# ---------------------------------------------------------------------------

class TestRoundTripLua:
    def setup_method(self):
        _session_backed_up.clear()

    def test_write_then_read(self, tmp_path):
        cfg = make_conf(tmp_path, "hyprland.lua", "")
        write_monitors_to_config(BASIC_MONITORS, cfg)
        read_back = read_monitors_from_config(cfg)
        assert len(read_back) == len(BASIC_MONITORS)
        assert read_back[0].name == BASIC_MONITORS[0].name
        assert read_back[0].resolution == "1920x1080"
        assert read_back[0].refresh == 144.0


# ---------------------------------------------------------------------------
# write_section_file
# ---------------------------------------------------------------------------

class TestWriteSectionFile:
    def setup_method(self):
        _session_backed_up.clear()

    def test_writes_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "hypr").mkdir(parents=True, exist_ok=True)
        lines = ["hl.config({ general = { gaps_in = 5 } })"]
        write_section_file("appearance", lines)
        out = get_section_file_path("appearance")
        assert out.read_text() == "\n".join(lines) + "\n"

    def test_creates_backup_on_first_write(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        hypr = tmp_path / "hypr"
        hypr.mkdir(parents=True, exist_ok=True)
        section_path = hypr / "appearance.lua"
        section_path.write_text("-- original content\n")
        write_section_file("appearance", ["hl.config({})"])
        bak = section_path.with_suffix(".lua.hyprland-settings.bak")
        assert bak.exists()
        assert "original content" in bak.read_text()

    def test_backup_only_once_per_session(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        hypr = tmp_path / "hypr"
        hypr.mkdir(parents=True, exist_ok=True)
        section_path = hypr / "appearance.lua"
        section_path.write_text("-- original\n")
        write_section_file("appearance", ["-- v1"])
        section_path.write_text("-- modified by first write\n")
        write_section_file("appearance", ["-- v2"])
        bak = section_path.with_suffix(".lua.hyprland-settings.bak")
        assert "original" in bak.read_text()

    def test_no_backup_if_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "hypr").mkdir(parents=True, exist_ok=True)
        write_section_file("appearance", ["hl.config({})"])
        section_path = get_section_file_path("appearance")
        bak = section_path.with_suffix(".lua.hyprland-settings.bak")
        assert not bak.exists()

    def test_atomic_write(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "hypr").mkdir(parents=True, exist_ok=True)
        replaced = []
        orig_replace = os.replace
        monkeypatch.setattr(os, "replace", lambda s, d: (replaced.append(str(s)), orig_replace(s, d)))
        write_section_file("appearance", ["hl.config({})"])
        assert any(r.endswith(".tmp") for r in replaced)
