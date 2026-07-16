"""Regression coverage for collection-time gateway imports."""

import os
from pathlib import Path

import gateway.run as gateway_run


def test_gateway_home_snapshot_tracks_hermetic_fixture():
    assert gateway_run._hermes_home == Path(os.environ["HERMES_HOME"])
