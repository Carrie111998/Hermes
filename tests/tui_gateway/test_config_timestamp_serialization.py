"""Test that config.get RPC handles YAML datetime values correctly.

Regression test for #89203: PyYAML parses unquoted timestamps as Python
datetime objects, which json.dumps() cannot serialize. The config.get RPC
must convert these to ISO strings before returning them.
"""

import datetime
import json
import tempfile
from pathlib import Path

import pytest

from tui_gateway.json_safe import make_json_safe


def test_make_json_safe_datetime():
    """datetime.datetime converts to ISO 8601 string."""
    dt = datetime.datetime(2024, 1, 15, 10, 30, 45)
    result = make_json_safe(dt)
    assert result == "2024-01-15T10:30:45"
    # Verify it's actually JSON-serializable
    assert json.dumps(result) == '"2024-01-15T10:30:45"'


def test_make_json_safe_date():
    """datetime.date converts to ISO 8601 date string."""
    d = datetime.date(2024, 1, 15)
    result = make_json_safe(d)
    assert result == "2024-01-15"
    assert json.dumps(result) == '"2024-01-15"'


def test_make_json_safe_time():
    """datetime.time converts to ISO 8601 time string."""
    t = datetime.time(10, 30, 45)
    result = make_json_safe(t)
    assert result == "10:30:45"
    assert json.dumps(result) == '"10:30:45"'


def test_make_json_safe_nested_dict():
    """Nested dicts with datetime values are recursively sanitized."""
    data = {
        "last_check": datetime.datetime(2024, 1, 15, 10, 30, 0),
        "user": {
            "created": datetime.date(2023, 12, 1),
            "preferences": {
                "reminder_time": datetime.time(9, 0, 0),
            },
        },
        "count": 42,
    }
    result = make_json_safe(data)
    assert result["last_check"] == "2024-01-15T10:30:00"
    assert result["user"]["created"] == "2023-12-01"
    assert result["user"]["preferences"]["reminder_time"] == "09:00:00"
    assert result["count"] == 42
    # Verify the entire structure is JSON-serializable
    json_str = json.dumps(result)
    assert isinstance(json_str, str)


def test_make_json_safe_list():
    """Lists with datetime values are recursively sanitized."""
    data = [
        datetime.datetime(2024, 1, 1, 0, 0, 0),
        {"timestamp": datetime.datetime(2024, 1, 2, 12, 0, 0)},
        "plain string",
        123,
    ]
    result = make_json_safe(data)
    assert result[0] == "2024-01-01T00:00:00"
    assert result[1]["timestamp"] == "2024-01-02T12:00:00"
    assert result[2] == "plain string"
    assert result[3] == 123
    json_str = json.dumps(result)
    assert isinstance(json_str, str)


def test_make_json_safe_preserves_json_safe_types():
    """Primitives (str, int, float, bool, None) pass through unchanged."""
    data = {
        "string": "hello",
        "number": 42,
        "float": 3.14,
        "bool": True,
        "none": None,
    }
    result = make_json_safe(data)
    assert result == data
    json_str = json.dumps(result)
    assert isinstance(json_str, str)


def test_make_json_safe_does_not_mutate():
    """make_json_safe returns a new structure without mutating the input."""
    original = {
        "timestamp": datetime.datetime(2024, 1, 1, 0, 0, 0),
        "nested": {
            "date": datetime.date(2024, 1, 1),
        },
    }
    original_timestamp = original["timestamp"]
    original_date = original["nested"]["date"]
    
    result = make_json_safe(original)
    
    # Original still has datetime objects
    assert isinstance(original["timestamp"], datetime.datetime)
    assert isinstance(original["nested"]["date"], datetime.date)
    assert original["timestamp"] is original_timestamp
    assert original["nested"]["date"] is original_date
    
    # Result has ISO strings
    assert isinstance(result["timestamp"], str)
    assert isinstance(result["nested"]["date"], str)


def test_config_get_full_with_yaml_timestamps(tmp_path, monkeypatch):
    """config.get with key='full' serializes datetime values from config.yaml.
    
    This is the end-to-end scenario from #89203: a config.yaml file with an
    unquoted timestamp, parsed by PyYAML into a datetime object, must be
    serialized successfully when the TUI requests config.get.
    """
    # Create a temporary HERMES_HOME with a config.yaml containing timestamps
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        """
model:
  provider: openai
  model: gpt-4

agent:
  last_check: 2024-01-15 10:30:00  # Unquoted timestamp

display:
  skin: default
""",
        encoding="utf-8",
    )
    
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    
    # Import after setting HERMES_HOME so the module sees the test config
    from hermes_cli.config import load_config
    
    config = load_config()
    
    # Verify PyYAML parsed the timestamp as a datetime object
    assert isinstance(config["agent"]["last_check"], datetime.datetime)
    
    # Verify make_json_safe converts it
    safe_config = make_json_safe(config)
    assert isinstance(safe_config["agent"]["last_check"], str)
    assert safe_config["agent"]["last_check"] == "2024-01-15T10:30:00"
    
    # Verify the entire config is now JSON-serializable
    json_str = json.dumps(safe_config)
    assert isinstance(json_str, str)
    
    # Verify we can round-trip through JSON
    parsed_back = json.loads(json_str)
    assert parsed_back["agent"]["last_check"] == "2024-01-15T10:30:00"
