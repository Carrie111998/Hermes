"""Build a .docx from plain text without a document library.

The final document goes to a client's inbox, and a lawyer's client expects
an attachment they can edit — not a code block in an e-mail body. A .docx
is a zip of four small XML parts, so writing one directly is cheaper than
carrying python-docx into the image.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# 맑은 고딕 for Hangul; the eastAsia hint is what makes Word pick it.
STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Malgun Gothic" w:eastAsia="Malgun Gothic" w:hAnsi="Malgun Gothic"/>
<w:sz w:val="22"/><w:szCs w:val="22"/>
</w:rPr></w:rPrDefault></w:docDefaults>
</w:styles>"""

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")


def _paragraph(text: str, *, bold: bool = False, align: str = "", size: int = 0) -> str:
    props = []
    if align:
        props.append(f'<w:jc w:val="{align}"/>')
    para_props = f"<w:pPr>{''.join(props)}</w:pPr>" if props else ""

    run_props = []
    if bold:
        run_props.append("<w:b/>")
    if size:
        run_props.append(f'<w:sz w:val="{size}"/>')
    rpr = f"<w:rPr>{''.join(run_props)}</w:rPr>" if run_props else ""

    if not text:
        return f"<w:p>{para_props}</w:p>"

    # Preserve single newlines inside a paragraph as line breaks.
    runs = []
    for index, line in enumerate(text.split("\n")):
        if index:
            runs.append("<w:br/>")
        runs.append(f'<w:t xml:space="preserve">{escape(line)}</w:t>')
    return f"<w:p>{para_props}<w:r>{rpr}{''.join(runs)}</w:r></w:p>"


def build_docx(title: str, body: str) -> bytes:
    """Render ``title`` + ``body`` (plain text, ``#`` headings ok) as .docx."""
    parts: list[str] = []
    if title:
        parts.append(_paragraph(title, bold=True, align="center", size=32))
        parts.append(_paragraph(""))

    for block in (body or "").replace("\r\n", "\n").split("\n\n"):
        block = block.rstrip()
        if not block.strip():
            parts.append(_paragraph(""))
            continue
        heading = _HEADING_RE.match(block.strip())
        if heading:
            level = len(heading.group(1))
            parts.append(_paragraph(heading.group(2), bold=True, size=32 - (level - 1) * 4))
            continue
        parts.append(_paragraph(block))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(parts)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1418" w:right="1134" w:bottom="1418" w:left="1134"/></w:sectPr>'
        "</w:body></w:document>"
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
        archive.writestr("word/styles.xml", STYLES)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def safe_filename(name: str, suffix: str = ".docx") -> str:
    cleaned = re.sub(r"[^\w가-힣 .-]+", "", name or "document").strip() or "document"
    return f"{cleaned[:60]}{suffix}"
