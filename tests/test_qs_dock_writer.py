"""Tests for hyprland_settings.backend.qs_dock_writer"""

import json
import os
from pathlib import Path

import pytest

from hyprland_settings.backend import qs_dock_writer
from hyprland_settings.backend.qs_dock_writer import (
    DEFAULT_DOCK_ENABLED,
    qs_dock_config_exists,
    qs_dock_settings_path,
    read_dock_enabled,
    write_dock_enabled,
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
