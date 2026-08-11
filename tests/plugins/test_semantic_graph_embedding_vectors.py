"""Contract tests for float32-le vector operations."""

from __future__ import annotations

import math

import pytest

from plugins.semantic_graph.embedding.vectors import (
    EmbeddingVectorError,
    FLOAT32_LE_DTYPE,
    cosine_similarity,
    dot_similarity,
    l2_normalize,
    pack_float32_le,
    unpack_float32_le,
    validate_vector,
)


def test_pack_is_little_endian_float32() -> None:
    assert pack_float32_le([1.0, 0.0], normalize=False)[:4] == b"\x00\x00\x80\x3f"


def test_pack_normalizes_to_unit_length() -> None:
    values = unpack_float32_le(pack_float32_le([3.0, 4.0]), dimensions=2)
    assert math.isclose(math.fsum(v * v for v in values), 1.0, rel_tol=1e-6)


def test_unpack_round_trip() -> None:
    values = unpack_float32_le(pack_float32_le([0.5, -0.5, 0.70710678]), dimensions=3, normalize=False)
    assert values == pytest.approx((0.5, -0.5, 0.70710678), rel=1e-6, abs=1e-7)


def test_unpack_accepts_bytes_like_and_rejects_wrong_length() -> None:
    blob = pack_float32_le([1.0, 2.0, 3.0], normalize=False)
    assert unpack_float32_le(bytearray(blob), dimensions=3) == pytest.approx((1.0, 2.0, 3.0))
    with pytest.raises(EmbeddingVectorError, match="blob length"):
        unpack_float32_le(blob, dimensions=2)


@pytest.mark.parametrize(
    "values, message",
    [([], "must not be empty"), ([0.0, 0.0], "must not be zero"), ([1.0, float("nan")], "NaN"), ([1.0, float("inf")], "infinity"), ([1.0, float("-inf")], "infinity")],
)
def test_validate_rejects_invalid_vectors(values: list[float], message: str) -> None:
    with pytest.raises(EmbeddingVectorError, match=message):
        validate_vector(values)


def test_validate_rejects_dimension_mismatch() -> None:
    with pytest.raises(EmbeddingVectorError, match="dimension"):
        validate_vector([1.0, 2.0], expected_dimensions=3)


def test_pack_rejects_float32_overflow() -> None:
    with pytest.raises(EmbeddingVectorError, match="float32"):
        pack_float32_le([1e39, 1e39], normalize=False)


def test_l2_normalize_preserves_direction() -> None:
    assert l2_normalize([3.0, 4.0]) == pytest.approx((0.6, 0.8))


def test_cosine_known_values() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_rejects_dimension_mismatch() -> None:
    with pytest.raises(EmbeddingVectorError, match="dimension"):
        cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])


def test_dot_similarity_is_clamped() -> None:
    score = dot_similarity(l2_normalize([1.0, 1e-8]), l2_normalize([1.0, -1e-8]))
    assert -1.0 <= score <= 1.0


def test_dtype_constant() -> None:
    assert FLOAT32_LE_DTYPE == "float32-le"


def test_unpack_normalize_true_re_normalizes() -> None:
    values = unpack_float32_le(pack_float32_le([3.0, 4.0], normalize=False), dimensions=2, normalize=True)
    assert math.isclose(math.fsum(v * v for v in values), 1.0, rel_tol=1e-6)

def test_validate_accepts_finite_nonzero_values() -> None:
    assert validate_vector([1.0, -2.0, 3.0]) == (1.0, -2.0, 3.0)


def test_validate_expected_dimensions_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        validate_vector([1.0], expected_dimensions=0)


def test_unpack_dimensions_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        unpack_float32_le(b"", dimensions=0)


def test_pack_without_normalization_preserves_magnitude() -> None:
    assert unpack_float32_le(pack_float32_le([3.0, 4.0], normalize=False), dimensions=2, normalize=False) == pytest.approx((3.0, 4.0))


def test_memoryview_unpack() -> None:
    blob = pack_float32_le([1.0, 2.0], normalize=False)
    assert unpack_float32_le(memoryview(blob), dimensions=2, normalize=False) == pytest.approx((1.0, 2.0))


def test_dot_rejects_nonfinite_result_inputs() -> None:
    with pytest.raises(EmbeddingVectorError):
        dot_similarity([float("nan")], [1.0])


def test_pack_normalizes_extreme_finite_values() -> None:
    values = unpack_float32_le(pack_float32_le([1e-300, 2e-300]), dimensions=2)
    assert values[1] > values[0]
    assert math.isclose(math.fsum(v * v for v in values), 1.0, rel_tol=1e-6)


def test_l2_normalize_rejects_zero_after_conversion() -> None:
    with pytest.raises(EmbeddingVectorError):
        l2_normalize([0.0, 0.0])


def test_pack_non_numeric_is_rejected() -> None:
    with pytest.raises(EmbeddingVectorError, match="non-numeric"):
        pack_float32_le(["bad"], normalize=False)  # type: ignore[list-item]


def test_unpack_non_bytes_is_rejected() -> None:
    with pytest.raises(EmbeddingVectorError, match="bytes-like"):
        unpack_float32_le("bad", dimensions=1)  # type: ignore[arg-type]


def test_dot_dimension_mismatch_is_rejected() -> None:
    with pytest.raises(EmbeddingVectorError, match="dimension"):
        dot_similarity([1.0], [1.0, 2.0])


def test_cosine_uses_normalized_inputs() -> None:
    assert cosine_similarity([3.0, 4.0], [6.0, 8.0]) == pytest.approx(1.0)


def test_pack_output_size_matches_dimensions() -> None:
    assert len(pack_float32_le([1.0, 2.0, 3.0], normalize=False)) == 12


def test_vector_error_is_exception() -> None:
    assert issubclass(EmbeddingVectorError, Exception)
