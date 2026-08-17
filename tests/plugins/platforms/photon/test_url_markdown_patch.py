"""Behavior tests for the Photon styled-send data-detection patch.

spectrum-ts' iMessage provider renders markdown to styled text (string +
UTF-16 formatting ranges) and calls ``sendText(..., { formatting })`` without
``enableDataDetection``. The server then defaults data detection ON for the
styled path and 500s when the text contains a raw URL. The sidecar's
``patch-spectrum-url-markdown.mjs`` rewrites the provider's two styled
outbound call sites (sendContent markdown case + group sendMultipart call) to
pass ``enableDataDetection: false`` explicitly, so URL-bearing markdown sends
as one styled message with the URL embedded.

These tests execute the real patch module under node against hermetic
fixtures that reproduce the published tarball's tab-indented anchors; they do
not read the installed SDK.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_PATCHER = Path("plugins/platforms/photon/sidecar/patch-spectrum-url-markdown.mjs")

# The published @spectrum-ts/imessage dist/index.js is tab-indented; these
# anchors mirror the real call sites exactly (verified against spectrum-ts
# 8.0.0's installed tarball).
_FIXTURE = """\
const sendContent = async (remote, spaceId, chat, content, replyTo, effect) => {
\tswitch (content.type) {
\t\tcase "effect": return sendContent(remote, spaceId, chat, content.content, replyTo, content.effect);
\t\tcase "text": return outboundMessage(spaceId, await remote.messages.sendText(chat, content.text, withReply(effectOption(effect), replyTo)), content);
\t\tcase "markdown": {
\t\t\tconst rendered = renderMarkdown(content.markdown);
\t\t\treturn outboundMessage(spaceId, await remote.messages.sendText(chat, rendered.text, withReply({
\t\t\t\t...effectOption(effect),
\t\t\t\t...formattingOption(rendered.formatting)
\t\t\t}, replyTo)), content);
\t\t}
\t}
};
const send$1 = async (remote, spaceId, content) => {
\tconst chat = toChatGuid(spaceId);
\tif (content.type === "group") {
\t\tconst resolved = await Promise.all(content.items.map((sub) => resolvePart(remote, sub.content)));
\t\tconst message = await remote.messages.sendMultipart(chat, resolved.map((part, idx) => ({
\t\t\t...part,
\t\t\tbubbleIndex: idx
\t\t})));
\t}
};
"""


def _write_fixture(tmp_path: Path, content: str) -> Path:
    dist = tmp_path / "node_modules" / "@spectrum-ts" / "imessage" / "dist"
    dist.mkdir(parents=True)
    file = dist / "index.js"
    file.write_text(content, encoding="utf-8")
    return file


def _run_patcher(tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", str(_PATCHER.resolve()), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_patch_applies_to_real_anchors(tmp_path: Path) -> None:
    file = _write_fixture(tmp_path, _FIXTURE)
    run = _run_patcher(tmp_path)
    assert run.returncode == 0, run.stderr
    assert "patch patched" in run.stderr
    patched = file.read_text(encoding="utf-8")
    # Both styled call sites must carry the explicit opt-out, in place.
    assert (
        "\t\t\t\t...effectOption(effect),\n"
        "\t\t\t\tenableDataDetection: false,\n"
        "\t\t\t\t...formattingOption(rendered.formatting)"
    ) in patched
    assert "\t\t\tbubbleIndex: idx\n\t\t})), { enableDataDetection: false });" in patched
    # The marker guards idempotency on later runs.
    assert "Hermes patch: disable iMessage data detection on styled sends" in patched


def test_patch_is_idempotent(tmp_path: Path) -> None:
    file = _write_fixture(tmp_path, _FIXTURE)
    first = _run_patcher(tmp_path)
    assert first.returncode == 0, first.stderr
    after_first = file.read_text(encoding="utf-8")
    second = _run_patcher(tmp_path)
    assert second.returncode == 0, second.stderr
    assert "patch ok" in second.stderr
    assert file.read_text(encoding="utf-8") == after_first


def test_patch_fails_loudly_when_sdk_is_reshaped(tmp_path: Path) -> None:
    # A future spectrum-ts major that reshapes the provider must fail loudly,
    # not silently no-op: the sidecar exits so the platform is visibly down
    # rather than 500ing sends at runtime.
    reshaped = _FIXTURE.replace("...formattingOption(rendered.formatting)", "...formatting(rendered)")
    _write_fixture(tmp_path, reshaped)
    run = _run_patcher(tmp_path)
    assert run.returncode == 1
    assert "patch failed" in run.stderr


def test_patch_fails_loudly_when_sdk_is_missing(tmp_path: Path) -> None:
    run = _run_patcher(tmp_path)
    assert run.returncode == 1
    assert "dist not found" in run.stderr


def test_patch_preserves_crlf_line_endings(tmp_path: Path) -> None:
    file = _write_fixture(tmp_path, _FIXTURE.replace("\n", "\r\n"))
    run = _run_patcher(tmp_path)
    assert run.returncode == 0, run.stderr
    patched = file.read_bytes()
    assert b"\r\n" in patched
    assert b"\n" not in patched.replace(b"\r\n", b"")
    assert b"enableDataDetection: false" in patched
