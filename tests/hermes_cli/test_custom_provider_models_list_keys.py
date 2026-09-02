"""Regression tests for ``models:`` list-row key handling in
``_normalize_custom_provider_entry`` (#95416).

The legacy list-of-dicts ``models:`` format accepted rows keyed by ``id:`` or
``name:``. Hand-edited configs naturally spell the row key as ``model:`` —
mirroring the entry-level singular ``model:`` field — but those rows fell
through the normalizer's ``continue``, silently dropping the whole models
block (per-model ``context_length`` / ``max_tokens`` never applied; the user
saw the default context-length fallback warning instead).
"""

from hermes_cli.config import _normalize_custom_provider_entry


def _entry(models):
    return {
        "name": "my-prov",
        "base_url": "https://example.com/v1",
        "models": models,
    }


def test_list_rows_keyed_by_model_are_converted():
    """The reporter's exact shape: ``[{model: X, ...}]`` rows (#95416)."""
    out = _normalize_custom_provider_entry(
        _entry([
            {
                "max_tokens": 32768,
                "model": "my-model",
                "context_length": 1000000,
            }
        ]),
        provider_key="my-prov",
    )
    assert out is not None
    assert out["models"] == {
        "my-model": {"max_tokens": 32768, "context_length": 1000000}
    }


def test_list_rows_keyed_by_model_do_not_leak_key_into_metadata():
    """``model`` is the id key, not per-model metadata — same as id/name."""
    out = _normalize_custom_provider_entry(
        _entry([{"model": "m1", "context_length": 8192}]),
        provider_key="my-prov",
    )
    assert out is not None
    assert out["models"] == {"m1": {"context_length": 8192}}


def test_list_rows_accept_id_name_and_model_mixed():
    """All three id spellings convert; existing id/name behavior unchanged."""
    out = _normalize_custom_provider_entry(
        _entry([
            {"id": "by-id", "context_length": 4096},
            {"name": "by-name", "max_tokens": 1024},
            {"model": "by-model"},
            "bare-string",
            {"model": "  "},  # blank id row is still skipped
            {"context_length": 1},  # no id key at all: skipped
        ]),
        provider_key="my-prov",
    )
    assert out is not None
    assert out["models"] == {
        "by-id": {"context_length": 4096},
        "by-name": {"max_tokens": 1024},
        "by-model": {},
        "bare-string": {},
    }


def test_id_and_name_still_win_over_model_in_same_row():
    """``model:`` is a fallback only — explicit id/name keep priority."""
    out = _normalize_custom_provider_entry(
        _entry([{"id": "canonical", "model": "ignored", "context_length": 8}]),
        provider_key="my-prov",
    )
    assert out is not None
    # All three id spellings are structural keys, so the losing ``model``
    # value is stripped from metadata — same as id/name are today.
    assert out["models"] == {"canonical": {"context_length": 8}}
