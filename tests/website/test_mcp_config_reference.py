from pathlib import Path


DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "website"
    / "docs"
    / "reference"
    / "mcp-config-reference.md"
)


def _document() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def _table_row(document: str, key: str) -> str:
    prefix = f"| `{key}` |"
    return next(line for line in document.splitlines() if line.startswith(prefix))


def test_protocol_reference_matches_runtime_authority():
    document = _document()
    assert "### Protocol authority" in document
    assert "| omitted or `auto` | `server/discover` |" in document
    assert "One same-generation `initialize` proof" in document
    assert "canonical MCP 1.28.1 `-32602`" in document
    assert "`stateless` or `2026-07-28` | `server/discover` |" in document
    assert "strict modern mode and never sends `initialize`" in document
    assert "| `legacy` | `initialize` |" in document
    assert "rejected before transport creation or network I/O" in document

    protocol_row = _table_row(document, "protocol")
    assert "omitted/`auto` is modern-first" in protocol_row
    assert "`stateless` and `2026-07-28` are strict-modern aliases" in protocol_row
    assert "`legacy` is initialise-only" in protocol_row
    assert "Unknown values fail before transport creation" in protocol_row
    assert "legacy `initialize` handshake first" not in document
    assert "falling back to the 2026-07-28 `server/discover` stateless probe" not in document


def test_keepalive_reference_distinguishes_protocol_eras():
    document = _document()
    keepalive_row = _table_row(document, "keepalive_interval")
    assert "Modern stateless peers" in keepalive_row
    assert "stateless discovery" in keepalive_row
    assert "Mcp-Session-Id" in keepalive_row
    assert "Legacy peers use `ping`" in keepalive_row
    assert "`tools/list`" in keepalive_row
