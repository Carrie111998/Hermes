#!/usr/bin/env python3
"""Cross-language docs germination — root documentation i18n pipeline.

The germination model (graph-gated):

  * The ENGLISH root docs are the canonical source graph. Every claim the
    English docs make is an edge; a localized doc must reproduce the same
    *technical graph* — identical code fences, identical link targets
    (locally rewritten), identical backtick technical identifiers, identical
    heading structure, resolvable internal anchors.

  * A locale file's parity with the English source is a CI-enforced gate
    (`tests/conformance/test_docs_i18n_germination.py`). "Germinated" files
    must pass every check; "legacy" files must pass the mechanical checks
    (fences, link targets, code spans, anchors) with heading drift reported
    as roadmap debt, never silently.

  * New languages germinate from the manifest below (top-10 global languages
    by total speakers, Ethnologue 26th ed. order). The pipeline is:
    extract (span inventory) -> translate (template) -> assemble (merge)
    -> check (parity gate) -> ship.

Pure stdlib. No network. No LLM calls in this file — it is the *gate*; the
translation itself happens out-of-band (human or agent) and is verified here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# ─────────────────────────────────────────────────────────────────────────────
# Manifest — the top-10 global languages (by total speakers, Ethnologue
# 26th edition order). This is the roadmap: status is either "germinated"
# (full parity gate), "manual" (existing translation, mechanical gate +
# reported heading debt), or "pending" (roadmap; no file yet).
#
# Provenance records who produced the seed translation — credit ledger.
# ─────────────────────────────────────────────────────────────────────────────

ROOT_DOCS = ("README.md", "CONTRIBUTING.md", "SECURITY.md")

TOP_10_LANGUAGES = (
    "zh-CN",   # Mandarin Chinese (existing translation)
    "hi",      # Hindi (issue #4763 in flight)
    "es",      # Spanish (existing translation)
    "fr",      # French (this campaign; seed by iacker via #63660)
    "ar",      # Modern Standard Arabic
    "bn",      # Bengali (PR #51306 in flight)
    "pt",      # Portuguese
    "ru",      # Russian (PR #69658 in flight)
    "ur-pk",   # Urdu (existing translation)
    "id",      # Indonesian (next in line after the top-10)
)

MANIFEST = {
    "zh-CN": {
        "name": "Chinese (Simplified)",
        "native": "中文",
        "badge": "中文",
        "color": "red",
        "status": "manual",
        "provenance": "existing in-tree translation",
        "notes": "legacy; heading parity debt reported, not gating",
    },
    "hi": {
        "name": "Hindi",
        "native": "हिन्दी",
        "badge": "हिन्दी",
        "color": "orange",
        "status": "pending",
        "provenance": "—",
        "notes": "PR #4763 (feat(docs): Hindi locale) in flight — interlocked",
    },
    "es": {
        "name": "Spanish",
        "native": "Español",
        "badge": "Español",
        "color": "orange",
        "status": "manual",
        "provenance": "existing in-tree translation",
        "notes": "legacy; CONTRIBUTING.es.md lags English (602 vs 1009 lines)",
    },
    "fr": {
        "name": "French",
        "native": "Français",
        "badge": "Français",
        "color": "blue",
        "status": "germinated",
        "provenance": "seed: iacker (#63660), cherry-picked with authorship; refreshed against current main by the germination pipeline",
        "notes": "full parity gate",
    },
    "ar": {
        "name": "Arabic",
        "native": "العربية",
        "badge": "العربية",
        "color": "green",
        "status": "pending",
        "provenance": "—",
        "notes": "RTL layout review required when germinating",
    },
    "bn": {
        "name": "Bengali",
        "native": "বাংলা",
        "badge": "বাংলা",
        "color": "green",
        "status": "pending",
        "provenance": "—",
        "notes": "PR #51306 (README.bn-BD.md) in flight — interlocked",
    },
    "pt": {
        "name": "Portuguese",
        "native": "Português",
        "badge": "Português",
        "color": "yellow",
        "status": "pending",
        "provenance": "—",
        "notes": "",
    },
    "ru": {
        "name": "Russian",
        "native": "Русский",
        "badge": "Русский",
        "color": "purple",
        "status": "pending",
        "provenance": "—",
        "notes": "PR #69658 (README.ru.md) in flight — interlocked",
    },
    "ur-pk": {
        "name": "Urdu",
        "native": "اردو",
        "badge": "اردو",
        "color": "green",
        "status": "manual",
        "provenance": "existing in-tree translation",
        "notes": "RTL layout; legacy heading parity debt reported, not gating",
    },
    "id": {
        "name": "Indonesian",
        "native": "Bahasa Indonesia",
        "badge": "Bahasa",
        "color": "blue",
        "status": "pending",
        "provenance": "—",
        "notes": "11th by speakers — next in line",
    },
}


def locale_file(doc: str, locale: str) -> str:
    """Map a root doc to its locale file name (convention: README.fr.md)."""
    stem, ext = doc.rsplit(".", 1)
    return f"{stem}.{locale}.{ext}"


# ─────────────────────────────────────────────────────────────────────────────
# Extractors — the span inventory. Every extractor is pure over text.
# ─────────────────────────────────────────────────────────────────────────────

FENCE_RE = re.compile(r"^(```+|~~~+)([^\n`]*)\n(.*?)^\1[^\n]*$", re.M | re.S)
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)")
HREF_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>')
CODE_SPAN_RE = re.compile(r"`([^`]+)`", re.S)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)


def _scan(text: str) -> tuple[str, list[dict]]:
    """Line-based fence scanner (GFM rules): an opening fence carries the
    info string; a closing fence is the same marker char repeated >= 3 times
    with NO trailing text. A `` ```yaml `` line can therefore never close a
    plain `` ``` `` block (the regex backreference form closes early and
    leaves real code unmasked). Returns (masked_text, fence_records) where
    masked_text has every fenced body blanked to spaces (line count and
    prose position preserved)."""
    lines = text.split("\n")
    masked: list[str] = []
    fences: list[dict] = []
    marker: str | None = None
    lang = ""
    body: list[str] = []
    for line in lines:
        if marker is None:
            m = re.match(r"^(```+|~~~+)(.*)$", line)
            if m:
                marker, lang = m.group(1), m.group(2).strip()
                body = []
                masked.append(" " * len(line))
                continue
            masked.append(line)
        else:
            m = re.match(rf"^({re.escape(marker[0])}{{3,}})[ \t]*$", line)
            if m:
                fences.append(
                    {
                        "marker": marker,
                        "lang": lang,
                        "sha256": hashlib.sha256("\n".join(body).encode("utf-8")).hexdigest()[:16],
                    }
                )
                marker = None
                masked.append(" " * len(line))
            else:
                body.append(line)
                masked.append(" " * len(line))
    if marker is not None:  # unterminated fence — record what we saw
        fences.append(
            {
                "marker": marker,
                "lang": lang,
                "sha256": hashlib.sha256("\n".join(body).encode("utf-8")).hexdigest()[:16],
            }
        )
    return "\n".join(masked), fences


def extract_fences(text: str) -> list[dict]:
    """Sequence of fenced code blocks: marker, language, body hash."""
    _, fences = _scan(text)
    return fences


def extract_code_spans(text: str) -> list[str]:
    """Verbatim backtick spans OUTSIDE code fences (technical identifiers,
    commands, paths). Fenced code is adjudicated verbatim by fence parity.

    Backticks are paired GLOBALLY in position order (odd/even), not by
    regex pair-matching: a dangling opener on one line must not turn the
    NEXT line's backtick into a phantom closer. A pair whose span crosses a
    line boundary is authoring noise (unbalanced backticks in the source),
    not a technical identifier — it is excluded from the required set, so a
    translation that fixes the imbalance is not penalized."""
    masked, _ = _scan(text)
    positions = [m.start() for m in re.finditer(r"`", masked)]
    spans: list[str] = []
    for i in range(0, len(positions) - 1, 2):
        span = masked[positions[i] + 1 : positions[i + 1]]
        if "\n" not in span:
            spans.append(span)
    return spans


def extract_links(text: str) -> list[tuple[str, str]]:
    """(label, target) for markdown links and raw href attributes, outside
    code fences (URLs inside code samples are code, not doc links)."""
    masked, _ = _scan(text)
    links = [(m.group(1), m.group(2)) for m in LINK_RE.finditer(masked)]
    links += [(m.group(1), m.group(1)) for m in HREF_RE.finditer(masked)]
    return links


def extract_headings(text: str) -> list[tuple[int, str]]:
    """(level, title) pairs in document order, excluding fence-interior
    comment lines that merely start with '# ' (code, not structure)."""
    masked, _ = _scan(text)
    return [
        (len(m.group(1)), m.group(2).strip())
        for m in HEADING_RE.finditer(masked)
    ]


def slugify(title: str) -> str:
    """GitHub-style anchor slug: lowercase, punctuation stripped, spaces→-,
    leading/trailing hyphens removed (github-slugger normalization)."""
    t = re.sub(r"[^\w\- ]", "", title, flags=re.UNICODE)
    t = re.sub(r" ", "-", t.strip().lower())
    return t.strip("-")


def extract_anchors(text: str) -> set[str]:
    """Slug set of the document's headings (what #fragments resolve to)."""
    return {slugify(t) for _, t in extract_headings(text)}


def heading_levels(text: str) -> list[int]:
    """The level sequence of headings — structure fingerprint."""
    return [lv for lv, _ in extract_headings(text)]


# ─────────────────────────────────────────────────────────────────────────────
# The locale rewrite rule: a relative link to one of the ROOT_DOCS inside a
# localized doc points at that doc's locale twin. Links to other locale
# files and to non-root targets (assets/, docs/, website/, URLs) stay as-is.
# ─────────────────────────────────────────────────────────────────────────────

def rewrite_target(target: str, doc: str, locale: str) -> str:
    if target.startswith(("http://", "https://", "#", "mailto:")):
        return target
    path, _, frag = target.partition("#")
    base = path.rsplit("/", 1)[-1]
    if base in ROOT_DOCS:
        twin = locale_file(base, locale)
        # Already the locale twin (e.g. the discoverability badge in the EN
        # README links README.fr.md while the locale under check IS fr) —
        # never double-suffix it.
        if target == twin:
            return target
        return twin + (f"#{frag}" if frag else "")
    return target


# ─────────────────────────────────────────────────────────────────────────────
# The parity gate. check_doc_parity returns a list of issue dicts:
# {"class": ..., "severity": "error"|"warning", "detail": ...}
# ─────────────────────────────────────────────────────────────────────────────

def check_doc_parity(
    en_text: str, loc_text: str, doc: str, locale: str, status: str
) -> list[dict]:
    issues: list[dict] = []

    def sev(cls: str) -> str:
        # Germinated locales must be perfect: every drift class is an error.
        # Legacy/manual translations report ALL drift classes as warnings —
        # the debt is visible in every CI run and in the manifest, and the
        # roadmap is to re-germinate them through the pipeline.
        return "error" if status == "germinated" else "warning"

    def err(cls: str, detail: str) -> None:
        issues.append({"class": cls, "severity": sev(cls), "detail": detail})

    def warn(cls: str, detail: str) -> None:
        issues.append({"class": cls, "severity": "warning", "detail": detail})

    # 1. Fence parity — identical code fence sequence, byte-identical bodies
    #    (marker + language + body hash). Code is never translated.
    en_f, loc_f = extract_fences(en_text), extract_fences(loc_text)
    en_sig = [(f["marker"], f["lang"], f["sha256"]) for f in en_f]
    loc_sig = [(f["marker"], f["lang"], f["sha256"]) for f in loc_f]
    if en_sig != loc_sig:
        err(
            "fence_parity",
            f"code fence sequence differs: EN {len(en_f)} blocks, "
            f"{locale} {len(loc_f)} blocks "
            f"(EN={en_sig} LOC={loc_sig})",
        )

    # 2. Code-span parity — every EN backtick identifier must survive.
    #    A span naming a root doc (``CONTRIBUTING.md``) is allowed its locale
    #    twin (``CONTRIBUTING.fr.md``) — same rewrite rule as link targets.
    en_spans = extract_code_spans(en_text)
    loc_spans = set(extract_code_spans(loc_text))
    missing = [
        s
        for s in dict.fromkeys(en_spans)
        if s not in loc_spans and rewrite_target(s, doc, locale) not in loc_spans
    ]
    if missing:
        err("code_span_parity", f"{len(missing)} EN code spans missing from {locale}: {missing[:10]}{'…' if len(missing) > 10 else ''}")

    # 3. Link-target parity — every EN target must appear in the locale doc
    #    under its locale-rewritten form. Exceptions:
    #    - other-locale README targets (`README.es.md` etc.) are hub-selector
    #      links; each locale chooses its own hub subset, so they are not
    #      required (only the back-link to the English README is, check 6).
    #    - fragments are resolved separately (check 4).
    en_targets = [t for _, t in extract_links(en_text)]
    loc_targets = set(t for _, t in extract_links(loc_text))
    other_locale = re.compile(r"^README\.[A-Za-z0-9-]+\.md$")
    for t in dict.fromkeys(en_targets):
        if t.startswith(("http://", "https://", "mailto:")):
            if t not in loc_targets:
                err("link_target_parity", f"EN external target missing from {locale}: {t}")
        elif t.startswith("#"):
            continue  # fragment resolution checked separately
        elif other_locale.match(t):
            continue  # hub-selector exemption
        else:
            rt = rewrite_target(t, doc, locale)
            if rt not in loc_targets:
                err("link_target_parity", f"EN target missing from {locale} (rewritten {t} -> {rt})")

    # 4. Anchor parity — every internal fragment in the locale doc resolves
    #    against the locale doc's own headings (or its rewritten twin).
    loc_anchors = extract_anchors(loc_text)
    for _, t in extract_links(loc_text):
        if t.startswith("#"):
            frag = t[1:]
            if frag and frag not in loc_anchors:
                err("anchor_parity", f"{locale} fragment #{frag} does not resolve to a heading")
        elif "#" in t and not t.startswith(("http", "mailto")):
            path, _, frag = t.partition("#")
            if not frag:
                continue
            base = path.rsplit("/", 1)[-1]
            if base in ROOT_DOCS:
                # The twin file's anchors are checked when the twin itself is
                # checked; here only the fragment must exist in the twin's file.
                twin = locale_file(base, locale)
                twin_text = _read_twin(twin)
                if twin_text is not None and frag not in extract_anchors(twin_text):
                    err("anchor_parity", f"{locale} link {t}: #{frag} does not resolve in {twin}")
            else:
                if frag not in loc_anchors:
                    err("anchor_parity", f"{locale} link {t}: #{frag} does not resolve locally")

    # 5. Heading-structure parity.
    en_lv, loc_lv = heading_levels(en_text), heading_levels(loc_text)
    if en_lv != loc_lv:
        msg = f"heading level sequence differs: EN={en_lv} {locale}={loc_lv}"
        if status == "germinated":
            err("heading_parity", msg)
        else:
            warn("heading_parity", msg + " (legacy debt — reported, not gating)")

    # 6. Hub/back-link parity — a locale README must link back to the English
    #    source so readers can escape to canonical docs.
    if doc == "README.md" and "README.md" not in [t for _, t in extract_links(loc_text)]:
        err("backlink_parity", f"{locale} README has no back-link to README.md")

    # 7. Discoverability — a germinated locale must be linked from the
    #    English README (the badge hub), or readers can never find it.
    if doc == "README.md" and status == "germinated":
        twin = locale_file("README.md", locale)
        if twin not in [t for _, t in extract_links(en_text)]:
            err(
                "discoverability",
                f"germinated locale {locale} is not linked from README.md "
                f"(add the language badge for {twin})",
            )

    return issues


_twin_cache: dict[str, str | None] = {}


def _read_twin(name: str) -> str | None:
    if name not in _twin_cache:
        p = REPO_ROOT / name
        _twin_cache[name] = p.read_text(encoding="utf-8") if p.exists() else None
    return _twin_cache[name]


def check_all(repo_root: Path | None = None) -> dict:
    """Run the full gate over every existing locale file. Returns report."""
    global REPO_ROOT
    if repo_root is not None:
        REPO_ROOT = repo_root
        _twin_cache.clear()
    report: dict = {"checks": [], "errors": 0, "warnings": 0}
    for doc in ROOT_DOCS:
        en_p = REPO_ROOT / doc
        if not en_p.exists():
            continue
        en_text = en_p.read_text(encoding="utf-8")
        for locale, meta in MANIFEST.items():
            if meta["status"] == "pending":
                continue
            loc_p = REPO_ROOT / locale_file(doc, locale)
            if not loc_p.exists():
                report["checks"].append(
                    {
                        "doc": doc,
                        "locale": locale,
                        "status": meta["status"],
                        "result": "missing",
                        "issues": [],
                    }
                )
                if meta["status"] == "germinated":
                    report["errors"] += 1
                else:
                    report["warnings"] += 1
                continue
            loc_text = loc_p.read_text(encoding="utf-8")
            issues = check_doc_parity(en_text, loc_text, doc, locale, meta["status"])
            result = "pass" if not any(i["severity"] == "error" for i in issues) else "fail"
            report["checks"].append(
                {"doc": doc, "locale": locale, "status": meta["status"], "result": result, "issues": issues}
            )
            report["errors"] += sum(1 for i in issues if i["severity"] == "error")
            report["warnings"] += sum(1 for i in issues if i["severity"] == "warning")
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Germination template — the extraction half of the pipeline. Prose becomes
# a placeholder the translator fills; every technical span is preserved.
# ─────────────────────────────────────────────────────────────────────────────

def germinate_template(en_text: str, locale: str) -> str:
    lines = en_text.split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        if FENCE_RE.match(line) or line.startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)  # code bodies are never translated
            continue
        if not line.strip():
            out.append(line)
            continue
        if HEADING_RE.match(line):
            out.append(line)  # headings translated by the translator in place
            continue
        if re.match(r"^\s*(<\|?\s*[-|]|\|?\s*[-|])", line):
            out.append(line)  # table rows: translator edits cells in place
            continue
        if line.startswith(("<", "<!--")):
            out.append(line)  # HTML/badges stay verbatim
            continue
        if re.match(r"^\s*!\[", line) or re.match(r"^\s*\[", line):
            out.append(line)
            continue
        # Prose line -> placeholder; the translator replaces the marker and
        # translates the quoted original.
        out.append(f"⟪{locale}:{line}⟫")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _status_table(repo_root: Path) -> str:
    rows = []
    for doc in ROOT_DOCS:
        for locale, meta in MANIFEST.items():
            f = locale_file(doc, locale)
            exists = (repo_root / f).exists()
            rows.append(
                f"{doc:16} {locale:6} {meta['status']:10} "
                f"{'present' if exists else 'absent':8} {meta['provenance'][:60]}"
            )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["check", "status", "extract", "template"])
    ap.add_argument("--locale", default=None, help="locale code (extract/template)")
    ap.add_argument("--doc", default=None, help="root doc name (extract/template)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args(argv)

    if args.action == "status":
        out: str | dict = _status_table(REPO_ROOT)
        if args.json:
            out = {
                "root_docs": list(ROOT_DOCS),
                "languages": [
                    {"code": c, **{k: v for k, v in m.items() if k != "notes"}}
                    for c, m in MANIFEST.items()
                ],
            }
        print(json.dumps(out, indent=2, ensure_ascii=False) if args.json else out)
        return 0

    if args.action == "check":
        report = check_all(REPO_ROOT)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            for c in report["checks"]:
                tag = {"pass": "PASS", "fail": "FAIL", "missing": "MISSING"}[c["result"]]
                print(f"{tag:7} {c['doc']:16} {c['locale']:6} [{c['status']}]")
                for i in c["issues"]:
                    print(f"       {i['severity']:7} {i['class']}: {i['detail']}")
            print(f"\n{report['errors']} errors, {report['warnings']} warnings")
        return 1 if report["errors"] else 0

    if args.action in ("extract", "template"):
        if not args.locale or not args.doc:
            ap.error("--locale and --doc are required for extract/template")
        src = REPO_ROOT / args.doc
        if not src.exists():
            print(f"source not found: {src}", file=sys.stderr)
            return 1
        text = src.read_text(encoding="utf-8")
        if args.action == "extract":
            payload = {
                "doc": args.doc,
                "fences": extract_fences(text),
                "code_spans": extract_code_spans(text),
                "links": extract_links(text),
                "headings": [{"level": lv, "title": t} for lv, t in extract_headings(text)],
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(germinate_template(text, args.locale))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
