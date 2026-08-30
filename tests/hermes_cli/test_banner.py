"""Tests for hermes_cli/banner.py — CLI banner rendering."""


def test_build_banner_non_empty():
    from hermes_cli.banner import format_banner
    result = format_banner("Hermes", "v1.0")
    assert len(result) > 0
