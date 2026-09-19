import pytest
from unittest.mock import patch

@pytest.fixture
def mock_hyprctl_monitors():
    """Returns sample hyprctl monitors -j output as a list of dicts."""
    return [
        {
            "id": 0, "name": "DP-1", "description": "Test Monitor (DP-1)",
            "make": "Test", "model": "TM-1", "serial": "",
            "width": 1920, "height": 1080, "refreshRate": 144.0,
            "x": 0, "y": 0, "scale": 1.0, "transform": 0,
            "focused": True, "dpmsStatus": True, "vrr": False,
            "activeWorkspace": {"id": 1, "name": "1"},
            "specialWorkspace": {"id": 0, "name": ""},
            "reserved": [0, 0, 0, 0],
        }
    ]
