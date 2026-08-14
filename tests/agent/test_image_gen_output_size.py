from pathlib import Path

import pytest
from PIL import Image

from agent.image_gen_provider import (
    normalize_image_file_size,
    resolve_output_size_enforcement,
)


def test_resolve_output_size_enforcement_prefers_provider_override():
    config = {
        "enforce_output_size": True,
        "openai-codex": {"enforce_output_size": False},
    }

    assert resolve_output_size_enforcement(config, "openai") is True
    assert resolve_output_size_enforcement(config, "openai-codex") is False


def test_resolve_output_size_enforcement_accepts_yaml_style_strings():
    assert resolve_output_size_enforcement(
        {"openai": {"enforce_output_size": "yes"}},
        "openai",
    ) is True


def test_normalize_image_file_size_crops_and_resizes_to_exact_target(tmp_path: Path):
    path = tmp_path / "generated.png"
    Image.new("RGB", (80, 80), color="navy").save(path)

    result = normalize_image_file_size(path, "160x90")

    with Image.open(path) as image:
        assert image.size == (160, 90)
    assert result == {
        "source_size": "80x80",
        "size": "160x90",
        "output_size_normalized": True,
    }


def test_normalize_image_file_size_is_byte_stable_when_already_exact(tmp_path: Path):
    path = tmp_path / "generated.png"
    Image.new("RGB", (64, 36), color="navy").save(path)
    before = path.read_bytes()

    result = normalize_image_file_size(path, "64x36")

    assert path.read_bytes() == before
    assert result == {
        "source_size": "64x36",
        "size": "64x36",
        "output_size_normalized": False,
    }


@pytest.mark.parametrize("value", ["", "16:9", "0x90", "160x0", "abcx90"])
def test_normalize_image_file_size_rejects_invalid_target(value: str, tmp_path: Path):
    path = tmp_path / "generated.png"
    Image.new("RGB", (64, 36), color="navy").save(path)

    with pytest.raises(ValueError, match="WIDTHxHEIGHT"):
        normalize_image_file_size(path, value)
