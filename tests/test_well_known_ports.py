"""Test that satellite ports offset correctly with the dashboard port (Bug 2)."""

from hermes_cli.main import _compute_well_known_ports


def test_well_known_ports_offset_with_dashboard_port():
    """Satellite ports offset correctly for various dashboard ports."""
    assert _compute_well_known_ports(9120) == [9120, 8644, 8645]
    assert _compute_well_known_ports(9121) == [9121, 8646, 8647]
    assert _compute_well_known_ports(9122) == [9122, 8648, 8649]
    assert _compute_well_known_ports(9130) == [9130, 8664, 8665]
    assert _compute_well_known_ports(9140) == [9140, 8684, 8685]


def test_well_known_ports_no_overlap_across_fallback_range():
    """Fallback range (9120-9140) produces non-overlapping satellite port sets."""
    seen: set[int] = set()
    for port in range(9120, 9141):
        ports = _compute_well_known_ports(port)
        for p in ports:
            assert p not in seen, f"Port {p} collides at dashboard {port}"
            seen.add(p)

    # The set should contain exactly (9141-9120)*3 = 63 unique ports
    assert len(seen) == (9141 - 9120) * 3, (
        f"Expected 63 unique ports, got {len(seen)}"
    )


def test_well_known_ports_edge_cases():
    """Edge cases: port 0, port below default."""
    # Port 0 means OS-assigned; no satellite ports
    assert _compute_well_known_ports(0) == []

    # Port below default (9120) still produces valid offsets
    ports = _compute_well_known_ports(9000)
    assert ports[0] == 9000
    # offset = (9000 - 9120) * 2 = -240 → underflows via saturating_sub
    # In Python, this gives negative offset which is fine
    assert len(ports) == 3
