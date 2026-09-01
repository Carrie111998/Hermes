"""Contract checks for the privacy-safe NL evaluation corpus."""

import json
from pathlib import Path


CORPUS = Path(__file__).parent / "fixtures" / "nl_search_eval_v1.json"


def test_eval_corpus_is_versioned_private_safe_and_multilingual():
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert corpus["version"] == 1
    cases = corpus["cases"]
    assert len(cases) >= 45
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    languages = {case["lang"] for case in cases}
    assert len(languages) == 15
    for case in cases:
        assert set(case) == {"id", "lang", "query", "target"}
        assert case["lang"] in languages
        assert len(case["query"]) >= 8 and len(case["target"]) >= 8
        assert not any(marker in case["query"].lower() for marker in ("http://", "https://", "@"))
        assert not any(marker in case["target"].lower() for marker in ("http://", "https://", "@"))


def test_eval_runner_is_in_tree_and_uses_isolated_databases():
    runner = Path(__file__).parents[2] / "scripts" / "nl_search_eval.py"
    text = runner.read_text(encoding="utf-8")
    assert "TemporaryDirectory" in text
    assert "natural_language=natural_language" in text
    assert "precision_at_5" in text and "latency_p95_ms" in text
    assert "SessionDB" in text
    assert "state.db" in text
    assert "/home/" not in text
    assert "db_path=Path(\"/" not in text
    assert "http://" not in text and "https://" not in text
