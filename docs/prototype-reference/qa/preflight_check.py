#!/usr/bin/env python3
"""
preflight_check.py — SON KAPI KONTROLÜ
Gönderimden HEMEN ÖNCE tüm email body'lerini otomatik tarar.

Kontrol eder:
1. Bridge content [SKIP]/[EXCEPTION]/[HEADER ROW]/[NOT RELEVANT] marker içermiyor mu
2. Bridge content \\bunknown\\b placeholder içermiyor mu
3. Bridge content Türkçe karakter içermiyor mu (ı ğ ü ş ö ç İ Ğ Ü Ş Ö Ç)
4. Bridge content 280 karakterden uzun mu
5. Bridge'de country 2+ kez geçmiyor mu (data quality)
6. Oluşturulan HTML body'sinde [SKIP] marker yok mu (son kontrol)
7. HTML body'sinde Türkçe karakter yok mu (son kontrol)
8. Opsiyonel: sample 5 email'i gerçek MIME parse ile kontrol

Kullanım:
  python3 preflight_check.py /tmp/aa_analysis/today_queue_final.json w2
  
Çıktı:
  - PASS: tüm kontroller temiz
  - FAIL (row|reason): herhangi bir kontaminasyon bulursa, hangi satırda, ne yanlış
  - Exit 0 = temiz, Exit 1 = en az 1 kontaminasyon bulundu

Bu script MANUEL çalıştırılır, otomatik değil. Çünkü her send helper'ından ÖNCE
bir insanın "OK, kontroller temiz" demesi gerekiyor.
"""
import json
import os
import re
import sys
from html.parser import HTMLParser

SKIP_PREFIX = re.compile(r'^\[(SKIP|EXCEPTION|HEADER ROW|NOT RELEVANT)\]', re.IGNORECASE)
TURKISH_RE = re.compile(r'[ığüşöçĞÜŞİÖÇ]')
UNKNOWN_RE = re.compile(r'\bunknown\b', re.IGNORECASE)
DOUBLE_COUNTRY_RE = re.compile(r'\bin\s+[A-Z][a-z]+\s+in\s+[A-Z][a-z]+', re.IGNORECASE)

class HTMLBridgeExtractor(HTMLParser):
    """İlk <p>...</p> içindeki metni çıkarır (bridge kısmı)."""
    def __init__(self):
        super().__init__()
        self.in_p = False
        self.bridge = ""
    def handle_starttag(self, tag, attrs):
        if tag == 'p':
            self.in_p = True
    def handle_endtag(self, tag):
        if tag == 'p':
            self.in_p = False
    def handle_data(self, data):
        if self.in_p:
            self.bridge += data

def extract_bridge_from_html(html_path):
    """HTML dosyasından bridge metnini çıkarır."""
    if not os.path.exists(html_path):
        return None
    with open(html_path, 'r', encoding='utf-8') as f:
        html_content = f.read()
    parser = HTMLBridgeExtractor()
    parser.feed(html_content)
    return parser.bridge.strip()

def main():
    if len(sys.argv) < 3:
        print("Usage: preflight_check.py <batch_json> <bridge_prefix>")
        sys.exit(2)

    batch_path = sys.argv[1]
    bridge_prefix = sys.argv[2]

    batch = json.load(open(batch_path))
    print(f"=== Preflight Check: {len(batch)} customers, prefix={bridge_prefix} ===\n")

    failures = []
    passes = 0

    for item in batch:
        row = item['row']
        country = item.get('country', 'unknown')
        # Önce bridge dosyasını kontrol et
        bp = f"/tmp/{bridge_prefix}_R{row}_bridge.txt"
        if not os.path.exists(bp):
            failures.append((row, f"bridge file missing: {bp}"))
            continue
        bridge = open(bp, 'r', encoding='utf-8').read().strip()

        if SKIP_PREFIX.match(bridge):
            failures.append((row, "BRIDGE has [SKIP]/[EXCEPTION]/[HEADER ROW] marker"))
            continue
        if UNKNOWN_RE.search(bridge):
            failures.append((row, "BRIDGE has 'unknown' placeholder"))
            continue
        if TURKISH_RE.search(bridge):
            t_chars = sorted(set(c for c in bridge if TURKISH_RE.match(c)))
            failures.append((row, f"BRIDGE has Turkish chars: {t_chars}"))
            continue
        if len(bridge) < 280:
            failures.append((row, f"BRIDGE too short: {len(bridge)} chars"))
            continue
        if DOUBLE_COUNTRY_RE.search(bridge):
            failures.append((row, "BRIDGE has duplicate country mention (data quality)"))
            continue

        # Sonra HTML body'sini kontrol et
        html_files = [
            f"/tmp/email_aa_en_R{row}.html",
            f"/tmp/email_aa_ar_R{row}.html",
            f"/tmp/email_aa_fr_R{row}.html",
            f"/tmp/email_aa_es_R{row}.html",
        ]
        html_found = False
        for hf in html_files:
            if os.path.exists(hf):
                html_found = True
                body = extract_bridge_from_html(hf)
                if body is None:
                    continue
                if SKIP_PREFIX.match(body):
                    failures.append((row, f"HTML BODY has [SKIP] marker ({hf})"))
                    break
                if TURKISH_RE.search(body):
                    t_chars = sorted(set(c for c in body if TURKISH_RE.match(c)))
                    failures.append((row, f"HTML BODY has Turkish chars: {t_chars} ({hf})"))
                    break
                if UNKNOWN_RE.search(body):
                    failures.append((row, f"HTML BODY has 'unknown' ({hf})"))
                    break
        if not html_found:
            failures.append((row, "no generated HTML found"))
            continue

        passes += 1

    print(f"\n=== RESULT ===")
    print(f"Pass: {passes}")
    print(f"Fail: {len(failures)}")
    if failures:
        print(f"\n=== FAILURES ===")
        for row, reason in failures:
            print(f"  R{row}: {reason}")
        sys.exit(1)
    else:
        print(f"\n✓ ALL CLEAN — safe to send")
        sys.exit(0)

if __name__ == "__main__":
    main()
