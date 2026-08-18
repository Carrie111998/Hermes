from ai_usage.quota_signal import BALANCE_WARN_USD, WARN_PCT, evaluate


def _snapshot(providers):
    return {"generated_at": "2026-08-18T23:25:12Z", "providers": providers}


def test_below_threshold_produces_no_finding():
    snap = _snapshot(
        [
            {
                "key": "openai-codex",
                "mode": "budget",
                "windows": [
                    {
                        "id": "wk",
                        "label": "Weekly",
                        "used_pct": 7.0,
                        "resets_at": "2026-08-20T03:35:25Z",
                    }
                ],
            }
        ]
    )
    assert evaluate(snap) == []


def test_exactly_ninety_pct_produces_one_diverted_finding():
    snap = _snapshot(
        [
            {
                "key": "anthropic",
                "mode": "budget",
                "windows": [
                    {"id": "5h", "label": "5h", "used_pct": WARN_PCT},
                ],
            }
        ]
    )
    findings = evaluate(snap)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["provider"] == "anthropic"
    assert finding["window_id"] == "5h"
    assert finding["window_label"] == "5h"
    assert finding["kind"] == "window"
    assert finding["used_pct"] == 90.0
    assert finding["outcome"] == "diverted"


def test_one_hundred_pct_produces_chain_exhausted():
    snap = _snapshot(
        [
            {
                "key": "anthropic",
                "mode": "budget",
                "windows": [
                    {
                        "id": "wk",
                        "label": "Weekly",
                        "used_pct": 100.0,
                        "resets_at": "2026-08-17T08:00:00Z",
                    },
                ],
            }
        ]
    )
    findings = evaluate(snap)
    assert len(findings) == 1
    assert findings[0]["outcome"] == "chain_exhausted"


def test_window_absent_entirely_produces_no_finding():
    """Live quirk: Codex nulls its 5h window when the weekly is capped.

    SCOPE NOTE — read before "strengthening" this test. At THIS layer, absence
    and 0% are genuinely indistinguishable: both correctly yield no finding, so
    no fixture here can tell a correct `is None: continue` apart from a buggy
    `used_pct or 0.0`. The property that actually matters — absence must never
    read as RECOVERED and clear an open episode — is a property of the emit and
    recovery path, and is pinned in Task 2, not here.

    What this test does pin: a provider whose windows list is EMPTY, and one
    that omits a window id another provider has, must not cause `evaluate` to
    synthesise a window from an expected-id schema. It iterates what is present.
    """
    snap = _snapshot(
        [
            # Empty windows list entirely — a shape the live file really produces
            # for balance/unconfigured providers.
            {"key": "openai-codex", "mode": "budget", "windows": []},
            # Present-but-partial: has "wk", omits "5h", while the provider below
            # HAS a 5h window. A schema-driven implementation that iterated
            # expected ids and defaulted the missing one would diverge here.
            {
                "key": "kimi",
                "mode": "budget",
                "windows": [
                    {"id": "wk", "label": "Weekly", "used_pct": 7.0},
                ],
            },
            {
                "key": "anthropic",
                "mode": "budget",
                "windows": [
                    {"id": "5h", "label": "5h", "used_pct": 3.0},
                    {"id": "wk", "label": "Weekly", "used_pct": 4.0},
                ],
            },
        ]
    )
    assert evaluate(snap) == []


def test_used_pct_none_produces_no_finding():
    # Live quirk: a present window with used_pct == None means UNKNOWN, not 0%.
    snap = _snapshot(
        [
            {
                "key": "openai-codex",
                "mode": "budget",
                "windows": [
                    {"id": "5h", "label": "5h", "used_pct": None},
                    {
                        "id": "wk",
                        "label": "Weekly",
                        "used_pct": 100.0,
                        "resets_at": "2026-08-20T03:35:25Z",
                    },
                ],
            }
        ]
    )
    findings = evaluate(snap)
    # Only the "wk" window (100%) should fire; "5h" with used_pct None must not.
    assert len(findings) == 1
    assert findings[0]["window_id"] == "wk"
    assert findings[0]["outcome"] == "chain_exhausted"


def test_balance_provider_under_threshold_produces_a_finding():
    assert BALANCE_WARN_USD > 0  # sanity: a real threshold, not disabled
    snap = _snapshot(
        [
            {
                "key": "deepseek",
                "mode": "balance",
                "balance_usd": BALANCE_WARN_USD - 0.02,
                "windows": [],
            }
        ]
    )
    findings = evaluate(snap)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["provider"] == "deepseek"
    assert finding["kind"] == "balance"
    assert finding["balance"] == BALANCE_WARN_USD - 0.02
    assert finding["outcome"] in ("diverted", "chain_exhausted")
    assert finding["window_id"] is None
    assert finding["window_label"] is None


def test_balance_provider_at_or_above_threshold_produces_no_finding():
    snap = _snapshot(
        [
            {
                "key": "deepseek",
                "mode": "balance",
                "balance_usd": BALANCE_WARN_USD + 8.0,
                "windows": [],
            }
        ]
    )
    assert evaluate(snap) == []


def test_resets_at_in_past_with_used_pct_100_still_produces_a_finding():
    # The live anthropic case: resets_at is a day in the PAST while used_pct
    # is still 100.0. resets_at is DISPLAY-ONLY -- must never be used to infer
    # the window recovered.
    snap = _snapshot(
        [
            {
                "key": "anthropic",
                "mode": "budget",
                "windows": [
                    {"id": "5h", "label": "5h", "used_pct": 0.0},
                    {
                        "id": "wk",
                        "label": "Weekly",
                        "used_pct": 100.0,
                        "resets_at": "2026-08-17T08:00:00Z",
                    },
                ],
            }
        ]
    )
    findings = evaluate(snap)
    assert len(findings) == 1
    assert findings[0]["window_id"] == "wk"
    assert findings[0]["outcome"] == "chain_exhausted"
    # resets_at is carried through for display but must not have been used
    # to suppress the finding.
    assert findings[0]["resets_at"] == "2026-08-17T08:00:00Z"


def test_no_providers_at_all_produces_no_findings_trivially():
    # Distinguishes "correctly found nothing to warn about" from the two
    # dedicated absent-window / None-used_pct tests above, which each carry
    # a provider that could have (wrongly) fired.
    assert evaluate(_snapshot([])) == []


def test_multiple_providers_only_the_offending_one_fires():
    snap = _snapshot(
        [
            {
                "key": "kimi",
                "mode": "budget",
                "windows": [
                    {"id": "5h", "label": "Session", "used_pct": 0.0},
                    {"id": "wk", "label": "Weekly", "used_pct": 0.0},
                ],
            },
            {
                "key": "anthropic",
                "mode": "budget",
                "windows": [
                    {"id": "5h", "label": "5h", "used_pct": 95.0},
                ],
            },
        ]
    )
    findings = evaluate(snap)
    assert len(findings) == 1
    assert findings[0]["provider"] == "anthropic"
    assert findings[0]["outcome"] == "diverted"
