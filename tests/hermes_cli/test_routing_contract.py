"""Unit tests for the Wave 2 routing contract parser."""
import pytest
from hermes_cli.routing_contract import (
    parse_routing_envelope,
    RoutingContractError,
)


class TestParseRoutingEnvelope:
    """Tests for parse_routing_envelope."""

    def test_valid_full_envelope(self):
        body = """Some task description.

# ---routing:v1---
role: executor
model: DSv4Flash
provider: omniroute
reason: "Implement feature X"
ac_ids: [AC-1, AC-2]
enforcement_required: true
# ---/routing:v1---

More task body.
"""
        result = parse_routing_envelope(body)
        assert result["role"] == "executor"
        assert result["model"] == "DSv4Flash"
        assert result["provider"] == "omniroute"
        assert result["reason"] == "Implement feature X"
        assert result["ac_ids"] == ["AC-1", "AC-2"]
        assert result["enforcement_required"] is True

    def test_valid_minimal_envelope(self):
        body = """# ---routing:v1---
role: executor
reason: "Do the thing"
enforcement_required: true
# ---/routing:v1---
"""
        result = parse_routing_envelope(body)
        assert result["role"] == "executor"
        assert result["reason"] == "Do the thing"
        assert result["enforcement_required"] is True
        assert "model" not in result
        assert "ac_ids" not in result

    def test_no_envelope_returns_empty(self):
        body = "Just a regular task body with no routing envelope."
        result = parse_routing_envelope(body)
        assert result == {}

    def test_wave1_envelope_returns_empty(self):
        """Wave 1 envelopes must be classified as unenforced, never errors."""
        body = """Some task.

routing:
  origin: kanban
  assigned_role: executor
  action: implement
  edit_authorized: true
  root_cause: known
"""
        result = parse_routing_envelope(body)
        assert result == {}

    def test_wave1_assigned_role_only_returns_empty(self):
        body = """assigned_role: executor
action: implement
"""
        result = parse_routing_envelope(body)
        assert result == {}

    def test_missing_end_marker_raises(self):
        body = """# ---routing:v1---
role: executor
reason: "test"
enforcement_required: true
"""
        with pytest.raises(RoutingContractError, match="missing end marker"):
            parse_routing_envelope(body)

    def test_missing_required_field_raises(self):
        body = """# ---routing:v1---
role: executor
reason: "test"
# ---/routing:v1---
"""
        with pytest.raises(RoutingContractError, match="missing required fields"):
            parse_routing_envelope(body)

    def test_non_boolean_enforcement_raises(self):
        body = """# ---routing:v1---
role: executor
reason: "test"
enforcement_required: "yes"
# ---/routing:v1---
"""
        with pytest.raises(RoutingContractError, match="must be a boolean"):
            parse_routing_envelope(body)

    def test_empty_role_raises(self):
        body = """# ---routing:v1---
role: ""
reason: "test"
enforcement_required: true
# ---/routing:v1---
"""
        with pytest.raises(RoutingContractError, match="'role' must be a non-empty string"):
            parse_routing_envelope(body)

    def test_empty_reason_raises(self):
        body = """# ---routing:v1---
role: executor
reason: ""
enforcement_required: true
# ---/routing:v1---
"""
        with pytest.raises(RoutingContractError, match="'reason' must be a non-empty string"):
            parse_routing_envelope(body)

    def test_oversized_envelope_raises(self):
        big_reason = "x" * 2100
        body = f"""# ---routing:v1---
role: executor
reason: "{big_reason}"
enforcement_required: true
# ---/routing:v1---
"""
        with pytest.raises(RoutingContractError, match="exceeds"):
            parse_routing_envelope(body)

    def test_non_mapping_yaml_raises(self):
        body = """# ---routing:v1---
- just
- a
- list
# ---/routing:v1---
"""
        with pytest.raises(RoutingContractError, match="must be a YAML mapping"):
            parse_routing_envelope(body)

    def test_ac_ids_non_string_raises(self):
        body = """# ---routing:v1---
role: executor
reason: "test"
enforcement_required: true
ac_ids: [AC-1, 123]
# ---/routing:v1---
"""
        with pytest.raises(RoutingContractError, match="must contain only strings"):
            parse_routing_envelope(body)

    def test_ac_ids_non_list_raises(self):
        body = """# ---routing:v1---
role: executor
reason: "test"
enforcement_required: true
ac_ids: "AC-1"
# ---/routing:v1---
"""
        with pytest.raises(RoutingContractError, match="'ac_ids' must be a list"):
            parse_routing_envelope(body)

    def test_enforcement_false_returns_parsed(self):
        body = """# ---routing:v1---
role: executor
reason: "test"
enforcement_required: false
# ---/routing:v1---
"""
        result = parse_routing_envelope(body)
        assert result["enforcement_required"] is False