"""Regression tests for the stocks optional-skill's `compare` command.

These pin `52w_range_position_pct` -- the range-relative metric introduced by
bf9ccc3ba5 ("fix(stocks): implement the 52-week metric the comment always
described").

The bug that commit fixed was a silent comment/code divergence: the comment
documented (current - low) / (high - low), but the code computed
((price - low) / low) * 100 and discarded the parsed 52w high entirely. It
survived long enough to be caught by a ruff F841 sweep (the unused `high_f`
binding) rather than by any test, because there was no test. These are that
test.

`cmd_compare` does live Yahoo Finance HTTP, so the fetchers are stubbed on the
module object and `fetch_url` is booby-trapped -- no network, and a future code
path that starts fetching inside `compare` fails loudly instead of silently
reaching out to Yahoo from CI.

Two behaviours here are deliberate and must not be "tidied":

1. A non-numeric 52w high SUPPRESSES the metric. The parse sits inside the
   try/except (ValueError, TypeError, ZeroDivisionError), so unparseable input
   yields no figure at all rather than one computed without the high.
2. ZeroDivisionError is unreachable given the `high_f > low_f > 0` guard, but is
   kept in the except tuple as defence in depth.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "finance"
    / "stocks"
    / "scripts"
    / "stocks_client.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("stocks_skill", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stocks():
    """A freshly executed copy of the standalone script, per test."""
    return load_module()


# `compare` refuses fewer than 2 symbols, so every case is run alongside this
# fixed control row. It doubles as an assertion that the metric is computed
# per-row: a bug that leaked state between tickers would move CONTROL too.
CONTROL = {"symbol": "CTRL", "price": "150.00", "low": "100.00", "high": "200.00"}
CONTROL_EXPECTED = "50.00%"


def run_compare(module, monkeypatch, rows):
    """Drive the real `cmd_compare` over stubbed fetchers.

    `rows` is a list of {symbol, price, low, high} dicts; values are strings or
    None, matching what `fmt_price` really produces upstream. Returns the parsed
    JSON payload plus a symbol -> entry index.
    """
    by_symbol = {r["symbol"]: r for r in rows}

    def fake_fetch_url(*args, **kwargs):
        raise AssertionError("cmd_compare attempted real network I/O")

    def fake_yf_chart(symbol, interval="1d", range_="1d"):
        # Truthy sentinel: cmd_compare only checks it before handing it to
        # extract_quote_from_chart, which is stubbed below.
        return {"_stub_for": symbol}

    def fake_extract_quote_from_chart(symbol, chart_data):
        row = by_symbol[symbol]
        return {
            "symbol": symbol,
            "short_name": f"{symbol} Inc.",
            "price": row["price"],
            "change_pct": "1.00%",
            "52w_high": row["high"],
            "52w_low": row["low"],
        }

    def fake_yf_quote_summary(symbol):
        # None skips the enrichment block entirely, so 52w high/low come purely
        # from the stubbed chart and cannot be backfilled behind the test's back.
        return None

    monkeypatch.setattr(module, "fetch_url", fake_fetch_url)
    monkeypatch.setattr(module, "yf_chart", fake_yf_chart)
    monkeypatch.setattr(
        module, "extract_quote_from_chart", fake_extract_quote_from_chart
    )
    monkeypatch.setattr(module, "yf_quote_summary", fake_yf_quote_summary)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        module.cmd_compare([r["symbol"] for r in rows])

    payload = json.loads(buf.getvalue())
    return payload, {e["symbol"]: e for e in payload["comparison"]}


# (id, price, 52w_low, 52w_high, expected 52w_range_position_pct)
CASES = [
    # BOTH formulas give 50% here, so this case cannot detect the regression on
    # its own. Kept because it is the canonical mid-range reading -- but the
    # guard is the next case.
    ("mid_range_coincidence", "150.00", "100.00", "200.00", "50.00%"),
    # THE guard. The old (price - low) / low formula gave 50.00% here.
    ("diverging_old_formula_said_50", "150.00", "100.00", "300.00", "25.00%"),
    ("at_the_low", "100.00", "100.00", "200.00", "0.00%"),
    ("at_the_high", "200.00", "100.00", "200.00", "100.00%"),
    # A stale recorded high; deliberately NOT clamped to 100%.
    ("stale_high_not_clamped", "250.00", "100.00", "200.00", "150.00%"),
    # Load-bearing: float("N/A") raises ValueError inside the try, suppressing
    # the metric rather than emitting one computed without the high.
    ("non_numeric_high_suppresses", "150.00", "100.00", "N/A", None),
    # The intentional behaviour change in bf9ccc3ba5: an absent high used to
    # emit a low-only number, and now suppresses.
    ("absent_high_suppresses", "150.00", "100.00", None, None),
    # Undefined range; must not divide by zero.
    ("high_equals_low_no_zero_division", "100.00", "100.00", "100.00", None),
]


@pytest.mark.parametrize(
    ("case_id", "price", "low", "high", "expected"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_52w_range_position(stocks, monkeypatch, case_id, price, low, high, expected):
    case = {"symbol": "CASE", "price": price, "low": low, "high": high}
    _, entries = run_compare(stocks, monkeypatch, [case, CONTROL])

    assert entries["CASE"]["52w_range_position_pct"] == expected
    # Per-row independence: the control is computed from its own inputs.
    assert entries["CTRL"]["52w_range_position_pct"] == CONTROL_EXPECTED


def test_all_cases_side_by_side_in_one_comparison(stocks, monkeypatch):
    """`compare` is a side-by-side command; every row must stand on its own."""
    rows = [
        {"symbol": f"T{i}", "price": price, "low": low, "high": high}
        for i, (_, price, low, high, _expected) in enumerate(CASES)
    ]
    _, entries = run_compare(stocks, monkeypatch, rows)

    actual = [entries[f"T{i}"]["52w_range_position_pct"] for i in range(len(CASES))]
    assert actual == [c[4] for c in CASES]


def test_suppressed_metric_is_a_present_none_not_a_missing_key(stocks, monkeypatch):
    """Suppression leaves the default None in place; consumers can read the key."""
    case = {"symbol": "CASE", "price": "150.00", "low": "100.00", "high": None}
    _, entries = run_compare(stocks, monkeypatch, [case, CONTROL])

    assert "52w_range_position_pct" in entries["CASE"]
    assert entries["CASE"]["52w_range_position_pct"] is None


def test_old_performance_key_is_gone(stocks, monkeypatch):
    """bf9ccc3ba5 renamed 52w_performance_pct -> 52w_range_position_pct.

    The old name read as a return, which is exactly the confusion that produced
    the bug: a mid-range stock would be narrated as "up 50%".
    """
    case = {"symbol": "CASE", "price": "150.00", "low": "100.00", "high": "300.00"}
    payload, entries = run_compare(stocks, monkeypatch, [case, CONTROL])

    for entry in payload["comparison"]:
        assert "52w_performance_pct" not in entry
    assert "52w_performance_pct" not in json.dumps(payload)
    assert entries["CASE"]["52w_range_position_pct"] == "25.00%"


def test_compare_requires_at_least_two_symbols(stocks, monkeypatch):
    """The reason every case above is run alongside a control row."""
    monkeypatch.setattr(
        stocks,
        "fetch_url",
        lambda *a, **k: pytest.fail("cmd_compare attempted real network I/O"),
    )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        stocks.cmd_compare(["AAPL"])

    assert json.loads(buf.getvalue()) == {
        "error": "compare requires at least 2 symbols"
    }
