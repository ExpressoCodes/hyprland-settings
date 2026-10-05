"""Tests for hyprland_settings.backend.qs_dock_writer"""

import json
import os
from pathlib import Path

import pytest

from hyprland_settings.backend import qs_dock_writer
from hyprland_settings.backend.qs_dock_writer import (
    DEFAULT_DOCK_ENABLED,
    DOCK_DEFAULTS,
    qs_dock_config_exists,
    qs_dock_settings_path,
    read_dock_enabled,
    read_dock_settings,
    write_dock_enabled,
    write_dock_settings,
)


@pytest.fixture
def qs_dock_home(tmp_path, monkeypatch):
    """Point qs-dock config at an isolated XDG_CONFIG_HOME and reset backup state."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    qs_dock_writer._session_backed_up.clear()
    return tmp_path / "qs-dock"


# ---------------------------------------------------------------------------
# Paths / existence
# ---------------------------------------------------------------------------

def test_config_exists_false_when_absent(qs_dock_home):
    assert qs_dock_config_exists() is False


def test_config_exists_true_when_dir_present(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    assert qs_dock_config_exists() is True


def test_config_exists_true_when_settings_present(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    (qs_dock_home / "settings.json").write_text("{}")
    assert qs_dock_config_exists() is True


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def test_read_default_when_missing_file(qs_dock_home):
    assert read_dock_enabled() == DEFAULT_DOCK_ENABLED is True


def test_read_default_when_key_absent(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    (qs_dock_home / "settings.json").write_text('{"behaviour": {"hideDelay": 500}}')
    assert read_dock_enabled() is True


def test_read_false(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    (qs_dock_home / "settings.json").write_text('{"behaviour": {"enabled": false}}')
    assert read_dock_enabled() is False


def test_read_malformed_returns_default(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    (qs_dock_home / "settings.json").write_text("{not valid json")
    assert read_dock_enabled() is True


# ---------------------------------------------------------------------------
# Writing: merge preserves other keys
# ---------------------------------------------------------------------------

def test_write_merge_preserves_other_keys(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    original = {
        "appearance": {"iconSize": 30, "spacing": 4},
        "behaviour": {"enabled": True, "hideDelay": 500, "showRunning": True},
        "pins": ["kitty", "discord"],
        "version": 1,
    }
    path = qs_dock_home / "settings.json"
    path.write_text(json.dumps(original, indent=4))

    write_dock_enabled(False)

    result = json.loads(path.read_text())
    # Only behaviour.enabled changed.
    assert result["behaviour"]["enabled"] is False
    # Every other key preserved.
    assert result["behaviour"]["hideDelay"] == 500
    assert result["behaviour"]["showRunning"] is True
    assert result["appearance"] == {"iconSize": 30, "spacing": 4}
    assert result["pins"] == ["kitty", "discord"]
    assert result["version"] == 1


def test_write_creates_missing_file_with_just_key(qs_dock_home):
    write_dock_enabled(False)
    path = qs_dock_settings_path()
    assert path.exists()
    result = json.loads(path.read_text())
    assert result == {"behaviour": {"enabled": False}}


def test_write_creates_missing_dir(qs_dock_home):
    assert not qs_dock_home.exists()
    write_dock_enabled(True)
    assert qs_dock_settings_path().exists()


def test_write_toggle_true_false_roundtrip(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    path = qs_dock_home / "settings.json"
    path.write_text('{"behaviour": {"enabled": true}, "version": 1}')

    write_dock_enabled(False)
    assert read_dock_enabled() is False
    assert json.loads(path.read_text())["version"] == 1

    write_dock_enabled(True)
    assert read_dock_enabled() is True
    assert json.loads(path.read_text())["version"] == 1


def test_write_handles_malformed_existing_gracefully(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    path = qs_dock_home / "settings.json"
    path.write_text("{ broken json")

    write_dock_enabled(True)

    # Malformed content is replaced with just the managed key (unparseable keys
    # cannot be preserved), but the write must not raise.
    assert json.loads(path.read_text()) == {"behaviour": {"enabled": True}}


# ---------------------------------------------------------------------------
# Atomicity: temp path target + replace
# ---------------------------------------------------------------------------

def test_write_is_atomic_via_os_replace(qs_dock_home, monkeypatch):
    qs_dock_home.mkdir(parents=True)
    path = qs_dock_home / "settings.json"
    path.write_text('{"behaviour": {"enabled": true}}')
    expected_tmp = path.with_suffix(path.suffix + ".tmp")

    seen = {}
    real_replace = os.replace

    def spy_replace(src, dst):
        seen["src"] = Path(src)
        seen["dst"] = Path(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(qs_dock_writer.os, "replace", spy_replace)
    write_dock_enabled(False)

    assert seen["src"] == expected_tmp
    assert seen["dst"] == path
    # Temp file must not linger after the atomic replace.
    assert not expected_tmp.exists()


def test_write_leaves_backup_once(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    path = qs_dock_home / "settings.json"
    path.write_text('{"behaviour": {"enabled": true}, "version": 1}')

    write_dock_enabled(False)

    bak = path.with_suffix(path.suffix + ".pbak")
    assert bak.exists()
    # Backup holds the pre-edit content.
    assert json.loads(bak.read_text())["behaviour"]["enabled"] is True


# ---------------------------------------------------------------------------
# General reader: read_dock_settings
# ---------------------------------------------------------------------------

def test_read_settings_defaults_when_missing(qs_dock_home):
    assert read_dock_settings() == DOCK_DEFAULTS


def test_read_settings_from_file(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    (qs_dock_home / "settings.json").write_text(json.dumps({
        "appearance": {"iconSize": 64, "spacing": 10, "magnification": False,
                       "maxMagnifiedSize": 120},
        "behaviour": {"enabled": False, "visibilityMode": "autohide",
                      "hideOnFullscreen": False, "showDelay": 300, "hideDelay": 900},
    }))
    got = read_dock_settings()
    assert got["icon_size"] == 64
    assert got["spacing"] == 10
    assert got["magnification"] is False
    assert got["max_magnified_size"] == 120
    assert got["dock_enabled"] is False
    assert got["visibility_mode"] == "autohide"
    assert got["hide_on_fullscreen"] is False
    assert got["show_delay"] == 300
    assert got["hide_delay"] == 900


def test_read_settings_partial_falls_back_per_key(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    (qs_dock_home / "settings.json").write_text(json.dumps({
        "appearance": {"iconSize": 50},
    }))
    got = read_dock_settings()
    assert got["icon_size"] == 50
    # Missing keys fall back to the qs-dock defaults.
    assert got["spacing"] == DOCK_DEFAULTS["spacing"]
    assert got["visibility_mode"] == DOCK_DEFAULTS["visibility_mode"]
    assert got["dock_enabled"] == DOCK_DEFAULTS["dock_enabled"]


def test_read_settings_malformed_returns_defaults(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    (qs_dock_home / "settings.json").write_text("{ not json")
    assert read_dock_settings() == DOCK_DEFAULTS


# ---------------------------------------------------------------------------
# General writer: write_dock_settings merge + preservation
# ---------------------------------------------------------------------------

FULL_FLAT = {
    "dock_enabled": False,
    "icon_size": 40,
    "spacing": 2,
    "magnification": False,
    "max_magnified_size": 100,
    "visibility_mode": "intelligent",
    "hide_on_fullscreen": False,
    "show_delay": 250,
    "hide_delay": 750,
}


def test_write_settings_merges_all_keys_preserving_others(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    original = {
        "appearance": {"iconSize": 48, "spacing": 6, "backgroundOpacity": 85,
                       "cornerRadius": 18},
        "behaviour": {"enabled": True, "visibilityMode": "reserve",
                      "animationSpeed": 1, "clickFocused": "cycle"},
        "position": {"edge": "bottom", "margin": 6},
        "filtering": {"hideNoDisplay": True, "exclusions": ["quickshell"]},
        "pins": ["kitty", "discord"],
        "version": 1,
    }
    path = qs_dock_home / "settings.json"
    path.write_text(json.dumps(original, indent=4))

    write_dock_settings(FULL_FLAT)

    result = json.loads(path.read_text())
    # Managed appearance keys updated...
    assert result["appearance"]["iconSize"] == 40
    assert result["appearance"]["spacing"] == 2
    assert result["appearance"]["magnification"] is False
    assert result["appearance"]["maxMagnifiedSize"] == 100
    # ...managed behaviour keys updated...
    assert result["behaviour"]["enabled"] is False
    assert result["behaviour"]["visibilityMode"] == "intelligent"
    assert result["behaviour"]["hideOnFullscreen"] is False
    assert result["behaviour"]["showDelay"] == 250
    assert result["behaviour"]["hideDelay"] == 750
    # ...unmanaged keys in the SAME sections preserved...
    assert result["appearance"]["backgroundOpacity"] == 85
    assert result["appearance"]["cornerRadius"] == 18
    assert result["behaviour"]["animationSpeed"] == 1
    assert result["behaviour"]["clickFocused"] == "cycle"
    # ...and whole unrelated sections preserved verbatim.
    assert result["position"] == {"edge": "bottom", "margin": 6}
    assert result["filtering"] == {"hideNoDisplay": True, "exclusions": ["quickshell"]}
    assert result["pins"] == ["kitty", "discord"]
    assert result["version"] == 1


def test_write_settings_roundtrip(qs_dock_home):
    write_dock_settings(FULL_FLAT)
    assert read_dock_settings() == FULL_FLAT


def test_write_settings_creates_missing_file(qs_dock_home):
    write_dock_settings(FULL_FLAT)
    path = qs_dock_settings_path()
    assert path.exists()
    result = json.loads(path.read_text())
    assert result["appearance"]["iconSize"] == 40
    assert result["behaviour"]["enabled"] is False


def test_write_settings_malformed_existing_graceful(qs_dock_home):
    qs_dock_home.mkdir(parents=True)
    path = qs_dock_home / "settings.json"
    path.write_text("{ broken")
    write_dock_settings({"icon_size": 72})
    # Does not raise; unparseable content replaced with just the managed key.
    assert json.loads(path.read_text()) == {"appearance": {"iconSize": 72}}


def test_write_settings_ignores_unknown_keys(qs_dock_home):
    write_dock_settings({"icon_size": 55, "not_a_real_key": 123})
    result = json.loads(qs_dock_settings_path().read_text())
    assert result["appearance"]["iconSize"] == 55
    assert "not_a_real_key" not in result
    assert "not_a_real_key" not in result.get("appearance", {})


def test_write_settings_dock_enabled_maps_to_behaviour_enabled(qs_dock_home):
    write_dock_settings({"dock_enabled": False})
    result = json.loads(qs_dock_settings_path().read_text())
    assert result["behaviour"]["enabled"] is False
    assert read_dock_enabled() is False


def test_write_settings_atomic_via_os_replace(qs_dock_home, monkeypatch):
    qs_dock_home.mkdir(parents=True)
    path = qs_dock_home / "settings.json"
    path.write_text('{"behaviour": {"enabled": true}}')
    expected_tmp = path.with_suffix(path.suffix + ".tmp")

    seen = {}
    real_replace = os.replace

    def spy_replace(src, dst):
        seen["src"] = Path(src)
        seen["dst"] = Path(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(qs_dock_writer.os, "replace", spy_replace)
    write_dock_settings(FULL_FLAT)

    assert seen["src"] == expected_tmp
    assert seen["dst"] == path
    assert not expected_tmp.exists()
