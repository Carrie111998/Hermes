"""Shared magic-byte image format and container sniffing.

Provides authoritative byte-level image sniffing for WebUI uploads, vision tools,
and agent image routing. Prevents table drift across the codebase.
"""

from __future__ import annotations

from typing import Optional

# ISO-BMFF brands that indicate HEIC/HEIF or AVIF still images.
# ``mif1`` and ``msf1`` are generic HEIF brands and may appear as the major brand
# even when a compatible brand identifies HEIC.
HEIC_ISOBMFF_BRANDS = frozenset({
    b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis",
    b"mif1", b"msf1",
})

AVIF_ISOBMFF_BRANDS = frozenset({
    b"avif", b"avis",
})

ALL_ISOBMFF_IMAGE_BRANDS = HEIC_ISOBMFF_BRANDS | AVIF_ISOBMFF_BRANDS

# Common raster magic signatures: (signature, mime_type, extension)
_COMMON_IMAGE_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"BM", "image/bmp", ".bmp"),
)


def sniff_isobmff_image_brand(data: bytes, scan_limit: int = 128) -> Optional[str]:
    """Sniff ISO-BMFF brand from image bytes.

    Returns "avif", "heic", or None if no recognized still-image brand is found.
    Scans the major brand (offset 8) and aligned compatible-brand slots up to `scan_limit`.
    """
    if len(data) < 12 or data[4:8] != b"ftyp":
        return None

    # Brands are 4-byte aligned tokens starting after the 8-byte box header.
    limit = min(len(data), scan_limit)
    brands = {
        data[offset:offset + 4]
        for offset in range(8, limit - 3, 4)
    }

    # AVIF check first (or check specific brand sets)
    for brand in brands:
        if brand in AVIF_ISOBMFF_BRANDS:
            return "avif"
    for brand in brands:
        if brand in HEIC_ISOBMFF_BRANDS:
            return "heic"

    return None


def sniff_image_mime(data: bytes) -> Optional[str]:
    """Sniff image MIME type from raw magic bytes.

    Returns MIME string (e.g. "image/png", "image/heic", "image/avif") or None.
    """
    if not data:
        return None

    for sig, mime, _ in _COMMON_IMAGE_SIGNATURES:
        if data.startswith(sig):
            return mime

    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    isobmff_brand = sniff_isobmff_image_brand(data)
    if isobmff_brand == "avif":
        return "image/avif"
    if isobmff_brand == "heic":
        return "image/heic"

    return None


def sniff_image_extension(data: bytes) -> Optional[str]:
    """Sniff image file extension from raw magic bytes.

    Returns extension string (e.g. ".png", ".heic", ".avif") or None.
    """
    if not data:
        return None

    for sig, _, ext in _COMMON_IMAGE_SIGNATURES:
        if data.startswith(sig):
            return ext

    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"

    isobmff_brand = sniff_isobmff_image_brand(data)
    if isobmff_brand == "avif":
        return ".avif"
    if isobmff_brand == "heic":
        return ".heic"

    return None
