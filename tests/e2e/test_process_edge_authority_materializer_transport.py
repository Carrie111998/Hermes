"""Temporary e2e-lane entrypoint for the Phase-G materialized object transport.

The implementation remains in ``tests/security`` so the ordinary full suite
also verifies it. This wrapper exists only to use the immediately available,
untruncated e2e pytest lane. Neither transport file belongs in the final PR.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "security"
    / "test_process_edge_authority_materializer_transport.py"
)
_SPEC = importlib.util.spec_from_file_location("phase_g_transport", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _materializer_data(source: str) -> tuple[dict, tuple, tuple]:
    module = ast.parse(source, filename=str(_MODULE.MATERIALIZER))
    new_files = _MODULE._literal_assignment(module, "NEW_FILES")
    exact = _MODULE._literal_assignment(module, "EXACT_REPLACEMENTS")
    regex = _MODULE._literal_assignment(module, "REGEX_REPLACEMENTS")
    assert isinstance(new_files, dict)
    assert isinstance(exact, (list, tuple))
    assert isinstance(regex, (list, tuple))
    return new_files, tuple(exact), tuple(regex)


_MODULE._materializer_data = _materializer_data


def test_export_phase_g_product_through_e2e_lane():
    _MODULE.test_export_assertion_guarded_phase_g_product_object()
