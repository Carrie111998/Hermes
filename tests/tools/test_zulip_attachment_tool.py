"""Focused URL-shape tests for the Zulip attachment fetch tool."""

import pytest

from tools.zulip_attachment_tool import _resolve_url


@pytest.mark.parametrize(
    "path",
    [
        "/user_uploads/2/d/AxIXpwzkpyqzMFIbU2W6tyzr/report.xlsx",
        "/user_uploads/2/a9/r_UiE6RDYr72t0Csrhp_ibtR/report.xlsx",
    ],
)
def test_resolve_url_accepts_live_self_hosted_path_shapes(monkeypatch, path):
    monkeypatch.setenv("ZULIP_URL", "https://zulip.example")

    absolute, filename = _resolve_url(path)

    assert absolute == "https://zulip.example" + path
    assert filename == "report.xlsx"


@pytest.mark.parametrize(
    "path",
    [
        "/user_uploads/2/xyz/AxIXpwzkpyqzMFIbU2W6tyzr/report.xlsx",
        "/user_uploads/2/d/too-short/report.xlsx",
        "/other_uploads/2/d/AxIXpwzkpyqzMFIbU2W6tyzr/report.xlsx",
    ],
)
def test_resolve_url_still_rejects_non_attachment_shapes(monkeypatch, path):
    monkeypatch.setenv("ZULIP_URL", "https://zulip.example")

    with pytest.raises(ValueError, match="user_uploads path"):
        _resolve_url(path)
