#!/usr/bin/env python3
"""scan.py — deterministic scam/phishing signal scanner for the scam-shield skill.

Reads a message (and any URLs in it), matches it against an extensible pattern
set (``references/patterns.json``) and returns a probabilistic risk assessment.
It speaks in probabilities, not verdicts, and describes the *scheme* present in
the text — it never labels the sender as a person.

Cross-platform: standard library only, ``pathlib`` for paths, no shell calls.

Usage:
    python scripts/scan.py --text "message text here"
    python scripts/scan.py --file message.txt --json
    python scripts/scan.py --text "..." --patterns /path/to/patterns.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_PATTERNS = Path(__file__).resolve().parent.parent / "references" / "patterns.json"

_URL_RE = re.compile(r"""(?xi)\b(?:https?://|www\.)[^\s<>"'）)]+""")
_IP_HOST_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def load_patterns(path: Path) -> dict:
    """Load the pattern set. Raises FileNotFoundError with a clear message."""
    if not path.exists():
        raise FileNotFoundError(f"pattern file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _noisy_or(weights):
    """Combine independent per-hit probabilities: P(at least one) = 1 - prod(1-w).

    Naturally caps below 1.0 and gives diminishing returns as evidence stacks.
    """
    product = 1.0
    for w in weights:
        w = max(0.0, min(1.0, float(w)))
        product *= (1.0 - w)
    return 1.0 - product


def extract_urls(text: str):
    return [m.group(0).rstrip(".,);") for m in _URL_RE.finditer(text)]


def _host_of(url: str) -> str:
    u = re.sub(r"^https?://", "", url, flags=re.I)
    u = re.sub(r"^www\.", "", u, flags=re.I)
    return u.split("/")[0].split("?")[0].split("#")[0].lower()


def _registrable(host: str) -> str:
    """Best-effort registrable domain: the last two labels (sld.tld)."""
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def score_urls(urls, url_cfg):
    """Return (weights, notes) for URL-based signals."""
    weights, notes = [], []
    w = url_cfg.get("weights", {})
    brands = url_cfg.get("protected_brands", {})
    for url in urls:
        host = _host_of(url)
        if not host:
            continue
        if "@" in url.split("//")[-1].split("/")[0]:
            weights.append(w.get("at_symbol_in_url", 0.6))
            notes.append(f"ссылка содержит '@' перед доменом ({url[:40]}…) — реальный хост скрыт")
        if _IP_HOST_RE.match(host):
            weights.append(w.get("ip_host", 0.55))
            notes.append(f"ссылка ведёт на голый IP ({host}), а не на домен")
        if "xn--" in host:
            weights.append(w.get("punycode", 0.7))
            notes.append(f"домен в punycode ({host}) — вероятна подмена символов")
        base = host[4:] if host.startswith("www.") else host
        if base in url_cfg.get("shorteners", []):
            weights.append(w.get("shortener", 0.35))
            notes.append(f"сокращатель ссылок ({base}) прячет настоящий адрес")
        tld = host.rsplit(".", 1)[-1] if "." in host else ""
        if tld in url_cfg.get("suspicious_tlds", []):
            weights.append(w.get("suspicious_tld", 0.4))
            notes.append(f"подозрительная зона .{tld}")
        if len(host.split(".")) >= 5:
            weights.append(w.get("many_subdomains", 0.3))
            notes.append(f"много поддоменов в хосте ({host})")
        # Lookalike: a protected brand appears as a distinct token in the host,
        # but the registrable domain is not one of that brand's official ones.
        # Token-boundary matching (split on non-alphanumeric) avoids false
        # positives on unrelated words that merely contain a short brand string
        # (e.g. "colgate.com" / "gateway.com" are NOT flagged for brand "gate").
        # For brands >= 6 chars a concatenated prefix ("binancelogin.top") also
        # matches. Bare-brand deceptive domains ("metamask.top") are caught,
        # while official domains ("metamask.io", "support.metamask.io") are not.
        reg = _registrable(host)
        host_tokens = [t for t in re.split(r"[^a-z0-9]+", host) if t]
        sld = host.split(".")[-2] if len(host.split(".")) >= 2 else host
        for brand, official in brands.items():
            if reg in official:
                continue
            if brand in host_tokens or (len(brand) >= 6 and sld.startswith(brand)):
                weights.append(w.get("lookalike_domain", 0.75))
                official_str = ", ".join(official)
                notes.append(
                    f"домен маскируется под '{brand}' ({host}) — официальный: {official_str}"
                )
                break
    return weights, notes


def match_signals(text: str, signals):
    """Return list of fired signals (dedup by id)."""
    lowered = text.lower()
    fired = {}
    for sig in signals:
        hit = False
        for kw in sig.get("keywords", []):
            if kw.lower() in lowered:
                hit = True
                break
        if not hit:
            for rx in sig.get("regexes", []):
                try:
                    if re.search(rx, lowered):
                        hit = True
                        break
                except re.error:
                    continue
        if hit and sig["id"] not in fired:
            fired[sig["id"]] = sig
    return list(fired.values())


def band(score: int) -> str:
    if score >= 80:
        return "очень высокий"
    if score >= 50:
        return "высокий"
    if score >= 20:
        return "повышенный"
    return "низкий"


def build_report(text: str, patterns: dict) -> dict:
    signals = patterns.get("signals", [])
    url_cfg = patterns.get("url_config", {})

    fired = match_signals(text, signals)
    urls = extract_urls(text)
    url_weights, url_notes = score_urls(urls, url_cfg)

    weights = [s.get("weight", 0.5) for s in fired] + url_weights
    prob = _noisy_or(weights)
    score = int(round(prob * 100))

    # Confidence rises with the number of *independent* scheme categories seen.
    categories = {s.get("scheme_tag", s["id"]) for s in fired}
    if url_weights:
        categories.add("suspicious_link")
    n = len(categories)
    confidence = "низкая" if n <= 1 else ("средняя" if n == 2 else "высокая")

    reasons = [
        {"id": s["id"], "scheme": s.get("scheme_tag"), "why": s.get("description")}
        for s in fired
    ]
    safe_actions = []
    seen = set()
    for s in fired:
        a = s.get("safe_action")
        if a and a not in seen:
            safe_actions.append(a)
            seen.add(a)
    if url_notes:
        safe_actions.append(
            "Не переходи по ссылке из сообщения — открой сайт вручную из закладки и сверь домен."
        )

    return {
        "risk_score": score,
        "risk_band": band(score),
        "confidence": confidence,
        "scheme_tags": sorted(categories),
        "reasons": reasons,
        "url_findings": url_notes,
        "safe_actions": safe_actions,
        "disclaimer": (
            "Оценка вероятностная, а не вердикт. Анализируется схема сообщения, "
            "не личность отправителя. Низкий балл не гарантирует безопасность."
        ),
    }


def _format_human(rep: dict) -> str:
    lines = [
        f"Риск: {rep['risk_score']}/100 ({rep['risk_band']}), уверенность {rep['confidence']}",
    ]
    if rep["reasons"]:
        lines.append("Сработавшие схемы:")
        for r in rep["reasons"]:
            lines.append(f"  • {r['scheme']}: {r['why']}")
    for note in rep["url_findings"]:
        lines.append(f"  • ссылка: {note}")
    if rep["safe_actions"]:
        lines.append("Что делать:")
        for a in rep["safe_actions"]:
            lines.append(f"  → {a}")
    lines.append(rep["disclaimer"])
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Scam/phishing signal scanner.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="message text to scan")
    src.add_argument("--file", help="path to a UTF-8 file with the message")
    p.add_argument("--patterns", default=str(DEFAULT_PATTERNS), help="pattern JSON path")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = p.parse_args(argv)

    text = args.text if args.text is not None else Path(args.file).read_text(encoding="utf-8")
    patterns = load_patterns(Path(args.patterns))
    report = build_report(text, patterns)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
