"""Tests for Discord attachment routing/preflight/bounded-read contracts (feature M6)."""

import pytest

from plugins.platforms.discord.attachment_io import (
    THREAD_TARGETS,
    AttachmentError,
    AttachmentMeta,
    bounded_read_request,
    preflight_attachment,
    route_attachment,
)

THREAD = "112233445566778899"
PARENT = "998877665544332211"
CDN_URL = "https://cdn.discordapp.com/attachments/112233445566778899/1234/image.png"


def meta(
    url=CDN_URL,
    size=None,
    ctype=None,
    fname=None,
):
    return AttachmentMeta(url=url, size_bytes=size, content_type=ctype, filename=fname)


class TestRouteAttachment:
    @pytest.mark.parametrize("target", sorted(THREAD_TARGETS))
    def test_thread_target_preserves_thread_id(self, target):
        assert (
            route_attachment(
                meta(), thread_id=THREAD, parent_channel_id=PARENT, target=target
            )
            == THREAD
        )

    @pytest.mark.parametrize("target", ["channel", "dm", "guild", ""])
    def test_non_thread_target_routes_to_parent(self, target):
        assert (
            route_attachment(
                meta(), thread_id=THREAD, parent_channel_id=PARENT, target=target
            )
            == PARENT
        )

    def test_thread_target_without_thread_id_falls_back_to_parent(self):
        assert (
            route_attachment(
                meta(), thread_id=None, parent_channel_id=PARENT, target="image"
            )
            == PARENT
        )

    def test_thread_id_may_be_int_snowflake(self):
        assert (
            route_attachment(meta(), thread_id=12345, parent_channel_id=PARENT, target="text")
            == 12345
        )

    def test_both_none_raises(self):
        with pytest.raises(AttachmentError):
            route_attachment(meta(), thread_id=None, parent_channel_id=None)

    def test_both_none_raises_even_for_non_thread_target(self):
        with pytest.raises(AttachmentError):
            route_attachment(meta(), thread_id=None, parent_channel_id=None, target="channel")

    def test_invalid_thread_id_raises(self):
        with pytest.raises(AttachmentError):
            route_attachment(meta(), thread_id="not-a-snowflake", parent_channel_id=PARENT)

    def test_invalid_parent_id_raises(self):
        with pytest.raises(AttachmentError):
            route_attachment(meta(), thread_id=THREAD, parent_channel_id="-1")

    def test_error_is_value_error_subclass(self):
        with pytest.raises(ValueError):
            route_attachment(meta(), thread_id=None, parent_channel_id=None)


class TestPreflightAttachment:
    def test_under_limit_ok(self):
        assert preflight_attachment(meta(size=1024), max_bytes=2048) is None

    def test_at_limit_ok(self):
        assert preflight_attachment(meta(size=2048), max_bytes=2048) is None

    def test_over_limit_raises(self):
        with pytest.raises(AttachmentError):
            preflight_attachment(meta(size=2049), max_bytes=2048)

    def test_default_limit_is_8mb(self):
        assert preflight_attachment(meta(size=8 * 1024 * 1024)) is None
        with pytest.raises(AttachmentError):
            preflight_attachment(meta(size=8 * 1024 * 1024 + 1))

    def test_unknown_size_passes(self):
        assert preflight_attachment(meta(size=None)) is None


class TestBoundedReadRequest:
    def test_caps_to_limit_when_size_unknown(self):
        assert bounded_read_request(meta(size=None)) == {
            "url": CDN_URL,
            "limit_bytes": 4 * 1024 * 1024,
        }

    def test_caps_when_size_exceeds_limit(self):
        req = bounded_read_request(meta(size=10 * 1024 * 1024), limit_bytes=4 * 1024 * 1024)
        assert req["limit_bytes"] == 4 * 1024 * 1024

    def test_smaller_declared_size_wins(self):
        req = bounded_read_request(meta(size=1024), limit_bytes=4 * 1024 * 1024)
        assert req["limit_bytes"] == 1024

    def test_https_url_ok(self):
        req = bounded_read_request(meta(url="https://cdn.example.com/x.png"))
        assert req["url"] == "https://cdn.example.com/x.png"

    @pytest.mark.parametrize(
        "bad_url",
        [
            "ftp://cdn.example.com/x.png",
            "file:///etc/passwd",
            "not-a-url",
            "",
            "HTTP://cdn.example.com/x.png",  # scheme must be lowercase per contract
        ],
    )
    def test_non_http_url_raises(self, bad_url):
        with pytest.raises(AttachmentError):
            bounded_read_request(meta(url=bad_url))
