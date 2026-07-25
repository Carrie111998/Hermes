"""#71026: /insights must not crash with TypeError on non-numeric DB values."""
import pytest
from agent.insights import _safe_int


class TestSafeInt:
    def test_none(self):
        assert _safe_int(None) == 0
    def test_int(self):
        assert _safe_int(42) == 42
    def test_float(self):
        assert _safe_int(3.7) == 3
    def test_numeric_string(self):
        assert _safe_int("123") == 123
    def test_float_string(self):
        assert _safe_int("45.6") == 45
    def test_non_numeric_string(self):
        assert _safe_int("corrupt") == 0
    def test_empty_string(self):
        assert _safe_int("") == 0
    def test_bool(self):
        assert _safe_int(True) == 1
        assert _safe_int(False) == 0
    def test_reconciliation_no_crash(self):
        s = {"api_call_count": "corrupt", "input_tokens": "bad"}
        totals = {"api_call_count": 5, "input_tokens": 10}
        residual = max(0, _safe_int(s.get("api_call_count")) - totals["api_call_count"])
        assert residual == 0
