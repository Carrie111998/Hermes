"""Explicit opt-in live tests for the llama.cpp embedding probe."""

from __future__ import annotations

import os

import pytest


_RUN_LIVE = os.environ.get("HERMES_RUN_LLAMA_EMBEDDING_LIVE") == "1"
_REQUIRED = (
    "HERMES_TEST_LLAMA_EMBEDDING_URL",
    "HERMES_TEST_LLAMA_EMBEDDING_MODEL",
    "HERMES_TEST_LLAMA_EMBEDDING_REPO",
    "HERMES_TEST_LLAMA_EMBEDDING_DIMENSIONS",
)


@pytest.mark.skipif(
    not _RUN_LIVE or any(not os.environ.get(name) for name in _REQUIRED),
    reason="explicit llama.cpp live probe configuration is not enabled",
)
def test_live_candidate_probe() -> None:
    """Run the external probe only when the operator explicitly opts in."""
    from scripts.semantic_graph_llama_embedding_probe import ProbeConfig, run_probe_sync

    result = run_probe_sync(
        ProbeConfig(
            base_url=os.environ["HERMES_TEST_LLAMA_EMBEDDING_URL"],
            model=os.environ["HERMES_TEST_LLAMA_EMBEDDING_MODEL"],
            repo=os.environ["HERMES_TEST_LLAMA_EMBEDDING_REPO"],
            expected_dimensions=int(os.environ["HERMES_TEST_LLAMA_EMBEDDING_DIMENSIONS"]),
            profile=os.environ.get("HERMES_TEST_LLAMA_EMBEDDING_PROFILE", "unknown"),
            soak_requests=int(os.environ.get("HERMES_LLAMA_EMBEDDING_SOAK_REQUESTS", "0")),
        )
    )
    assert result.verdict == "pass_text_only", result.to_json()


@pytest.mark.skipif(
    not _RUN_LIVE or os.environ.get("HERMES_LLAMA_EMBEDDING_SOAK") != "1",
    reason="explicit llama.cpp soak configuration is not enabled",
)
def test_live_soak_requires_explicit_opt_in() -> None:
    """Keep long-running soak outside the normal CI contract."""
    assert int(os.environ.get("HERMES_LLAMA_EMBEDDING_SOAK_REQUESTS", "0")) >= 500


__all__ = ["test_live_candidate_probe", "test_live_soak_requires_explicit_opt_in"]
