"""Unit tests for tools.image_formats shared sniffer."""

import pytest
from tools.image_formats import (
    sniff_isobmff_image_brand,
    sniff_image_mime,
    sniff_image_extension,
    HEIC_ISOBMFF_BRANDS,
    AVIF_ISOBMFF_BRANDS,
)


def test_raster_magic_sniffing():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    assert sniff_image_mime(png) == "image/png"
    assert sniff_image_extension(png) == ".png"

    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    assert sniff_image_mime(jpg) == "image/jpeg"
    assert sniff_image_extension(jpg) == ".jpg"

    gif = b"GIF89a" + b"\x00" * 32
    assert sniff_image_mime(gif) == "image/gif"
    assert sniff_image_extension(gif) == ".gif"

    bmp = b"BM" + b"\x00" * 32
    assert sniff_image_mime(bmp) == "image/bmp"
    assert sniff_image_extension(bmp) == ".bmp"

    webp = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
    assert sniff_image_mime(webp) == "image/webp"
    assert sniff_image_extension(webp) == ".webp"


def test_isobmff_heic_direct_major_brand():
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"\x00" * 32
    assert sniff_isobmff_image_brand(heic) == "heic"
    assert sniff_image_mime(heic) == "image/heic"
    assert sniff_image_extension(heic) == ".heic"


def test_isobmff_heic_compatible_brand_in_mif1():
    # Major brand mif1, compatible brand heic at offset 16
    data = b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00heic" + b"\x00" * 32
    assert sniff_isobmff_image_brand(data) == "heic"
    assert sniff_image_mime(data) == "image/heic"
    assert sniff_image_extension(data) == ".heic"


def test_isobmff_avif_direct_major_brand():
    avif = b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00" + b"\x00" * 32
    assert sniff_isobmff_image_brand(avif) == "avif"
    assert sniff_image_mime(avif) == "image/avif"
    assert sniff_image_extension(avif) == ".avif"


def test_isobmff_avif_compatible_brand_in_mif1():
    # Major brand mif1, compatible brand avif at offset 16
    data = b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00avif" + b"\x00" * 32
    assert sniff_isobmff_image_brand(data) == "avif"
    assert sniff_image_mime(data) == "image/avif"
    assert sniff_image_extension(data) == ".avif"


def test_isobmff_brand_beyond_32_bytes():
    # Brand located at offset 48 (past 32-byte header window, within scan_limit 128)
    padding = b"\x00\x00\x00\x00" * 9
    data = b"\x00\x00\x00\x40ftypmif1" + padding + b"hevc" + b"\x00" * 32
    assert sniff_isobmff_image_brand(data) == "heic"
    assert sniff_image_mime(data) == "image/heic"
    assert sniff_image_extension(data) == ".heic"


def test_non_image_and_empty():
    assert sniff_image_mime(b"") is None
    assert sniff_image_extension(b"") is None
    assert sniff_isobmff_image_brand(b"") is None
    assert sniff_image_mime(b"hello world plaintext") is None
