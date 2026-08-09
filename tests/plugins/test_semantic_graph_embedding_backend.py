from __future__ import annotations

import math

import pytest

from plugins.semantic_graph.embedding import (
    DeterministicFakeEmbeddingBackend,
    EmbeddingBackend,
    EmbeddingBackendError,
    EmbeddingModelIdentity,
)


def _identity(dimensions: int = 3) -> EmbeddingModelIdentity:
    return EmbeddingModelIdentity(
        provider="test",
        model="deterministic-fake",
        revision="v1",
        dimensions=dimensions,
        serializer_version=1,
    )


def test_identity_namespace_is_stable_and_complete() -> None:
    assert _identity().namespace == "test:deterministic-fake:v1:d3:s1"


def test_identity_uses_unversioned_namespace_for_empty_revision() -> None:
    identity = EmbeddingModelIdentity(
        provider=" test ",
        model=" fake ",
        revision="  ",
        dimensions=3,
        serializer_version=1,
    )
    assert identity.namespace == "test:fake:unversioned:d3:s1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", ""),
        ("model", ""),
        ("dimensions", 0),
        ("serializer_version", 0),
    ],
)
def test_identity_rejects_invalid_fields(field: str, value: object) -> None:
    kwargs: dict[str, object] = {
        "provider": "test",
        "model": "fake",
        "revision": "",
        "dimensions": 3,
        "serializer_version": 1,
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        EmbeddingModelIdentity(**kwargs)  # type: ignore[arg-type]


def test_fake_backend_satisfies_protocol() -> None:
    backend = DeterministicFakeEmbeddingBackend(
        identity=_identity(), vectors={"query": [1.0, 0.0, 0.0]}
    )
    assert isinstance(backend, EmbeddingBackend)


def test_fake_backend_preserves_batch_order() -> None:
    backend = DeterministicFakeEmbeddingBackend(
        identity=_identity(),
        vectors={"a": [1.0, 0.0, 0.0], "b": [0.0, 1.0, 0.0]},
    )
    assert backend.embed_documents(["b", "a"]) == [
        [0.0, 1.0, 0.0],
        [1.0, 0.0, 0.0],
    ]


def test_fake_backend_returns_defensive_copy() -> None:
    backend = DeterministicFakeEmbeddingBackend(
        identity=_identity(), vectors={"q": [1.0, 2.0, 3.0]}
    )
    first = backend.embed_query("q")
    first[0] = 99.0
    assert backend.embed_query("q") == [1.0, 2.0, 3.0]


def test_fake_backend_rejects_wrong_dimension() -> None:
    with pytest.raises(ValueError, match="expected 3"):
        DeterministicFakeEmbeddingBackend(
            identity=_identity(), vectors={"bad": [1.0, 2.0]}
        )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_fake_backend_rejects_non_finite_values(bad: float) -> None:
    with pytest.raises(ValueError, match="NaN or infinity"):
        DeterministicFakeEmbeddingBackend(
            identity=_identity(), vectors={"bad": [1.0, bad, 0.0]}
        )


def test_fake_backend_rejects_unknown_input() -> None:
    backend = DeterministicFakeEmbeddingBackend(identity=_identity(), vectors={})
    with pytest.raises(EmbeddingBackendError, match="no deterministic vector configured"):
        backend.embed_query("unknown")


def test_fake_backend_can_simulate_unavailable_backend() -> None:
    backend = DeterministicFakeEmbeddingBackend(
        identity=_identity(),
        vectors={"q": [1.0, 0.0, 0.0]},
        is_available=False,
    )
    assert backend.available() is False
    with pytest.raises(EmbeddingBackendError, match="unavailable"):
        backend.embed_query("q")


def test_fake_backend_can_inject_embed_failure() -> None:
    backend = DeterministicFakeEmbeddingBackend(
        identity=_identity(),
        vectors={"q": [1.0, 0.0, 0.0]},
        fail_on_embed=True,
    )
    with pytest.raises(EmbeddingBackendError, match="injected"):
        backend.embed_query("q")
