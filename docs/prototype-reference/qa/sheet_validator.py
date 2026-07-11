#!/usr/bin/env python3
"""
Sheet Validator for Silverline Kitchen Appliances Customer List
Validates country labels, emails, and sector relevance.
Only writes to cells that need updating - skips clean rows.
"""

import json
import subprocess
import sys
import re
import time
from datetime import datetime

SHEET_ID = "{{SHEET_ID}}"
ACCOUNT = "{{gog_account}}"
TODAY = datetime.now().strftime('%Y-%m-%d')

# Country TLDs - used to detect mismatches
# Maps country-specific TLD endings to expected country names
TLD_TO_COUNTRY = {
    ".com.br": "Brazil",
    ".com.ar": "Argentina",
    ".com.mx": "Mexico",
    ".com.co": "Colombia",
    ".com.pe": "Peru",
    ".com.ve": "Venezuela",
    ".com.cl": "Chile",
    ".com.ec": "Ecuador",
    ".com.tr": ["Turkey", "Türkiye"],
    ".co.uk": ["UK", "United Kingdom"],
    ".com.au": "Australia",
    ".co.in": "India",
    ".com.eg": "Egypt",
    ".com.sa": "Saudi Arabia",
    ".com.dz": "Algeria",
    ".co.za": "South Africa",
    ".com.ng": "Nigeria",
    ".com.pk": "Pakistan",
    ".com.sg": "Singapore",
    ".co.id": "Indonesia",
    ".com.my": "Malaysia",
    ".co.jp": "Japan",
    ".com.cn": "China",
    ".com.tw": "Taiwan",
    ".co.kr": ["South Korea", "Korea"],
    ".com.ph": "Philippines",
    ".com.kw": "Kuwait",
    ".com.bh": "Bahrain",
    ".com.jo": "Jordan",
}

# Single TLDs to country name
SINGLE_TLD_MAP = {
    ".de": "Germany",
    ".fr": "France",
    ".it": "Italy",
    ".es": "Spain",
    ".nl": "Netherlands",
    ".pl": "Poland",
    ".ru": "Russia",
    ".ua": "Ukraine",
    ".ro": "Romania",
    ".hu": "Hungary",
    ".cz": "Czech",
    ".sk": "Slovakia",
    ".bg": "Bulgaria",
    ".gr": "Greece",
    ".pt": "Portugal",
    ".at": "Austria",
    ".ch": "Switzerland",
    ".be": "Belgium",
    ".se": "Sweden",
    ".no": "Norway",
    ".dk": "Denmark",
    ".fi": "Finland",
    ".ie": "Ireland",
    ".il": "Israel",
    ".ir": "Iran",
    ".iq": "Iraq",
    ".ly": "Libya",
    ".tn": "Tunisia",
    ".ma": "Morocco",
    ".dz": "Algeria",
    ".eg": "Egypt",
    ".sa": ["Saudi Arabia", "KSA"],
    ".ae": ["UAE", "United Arab Emirates"],
    ".qa": "Qatar",
    ".kw": "Kuwait",
    ".bh": "Bahrain",
    ".om": "Oman",
    ".ye": "Yemen",
    ".jo": "Jordan",
    ".lb": "Lebanon",
    ".sy": "Syria",
    ".tr": ["Turkey", "Türkiye"],
    ".in": "India",
    ".pk": "Pakistan",
    ".bd": "Bangladesh",
    ".lk": "Sri Lanka",
    ".np": "Nepal",
    ".cn": "China",
    ".jp": "Japan",
    ".kr": ["South Korea", "Korea"],
    ".tw": "Taiwan",
    ".sg": "Singapore",
    ".my": "Malaysia",
    ".id": "Indonesia",
    ".ph": "Philippines",
    ".vn": "Vietnam",
    ".th": "Thailand",
    ".mm": "Myanmar",
    ".kh": "Cambodia",
    ".ng": "Nigeria",
    ".ke": "Kenya",
    ".gh": "Ghana",
    ".et": "Ethiopia",
    ".tz": "Tanzania",
    ".ug": "Uganda",
    ".rw": "Rwanda",
    ".zm": "Zambia",
    ".zw": "Zimbabwe",
    ".za": "South Africa",
    ".ao": "Angola",
    ".cm": "Cameroon",
    ".sn": "Senegal",
    ".az": "Azerbaijan",
    ".ge": "Georgia",
    ".am": "Armenia",
    ".kz": "Kazakhstan",
    ".uz": "Uzbekistan",
    ".tm": "Turkmenistan",
    ".tj": "Tajikistan",
    ".kg": "Kyrgyzstan",
    ".mn": "Mongolia",
    ".al": "Albania",
    ".mk": "Macedonia",
    ".rs": "Serbia",
    ".me": "Montenegro",
    ".ba": "Bosnia",
    ".hr": "Croatia",
    ".si": "Slovenia",
    ".lt": "Lithuania",
    ".lv": "Latvia",
    ".ee": "Estonia",
    ".md": "Moldova",
    ".by": "Belarus",
    ".lu": "Luxembourg",
    ".cy": "Cyprus",
    ".mt": "Malta",
    ".al": "Albania",
}

# Generic email providers - acceptable for any country
GENERIC_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "aol.com",
    "mail.com", "protonmail.com", "yandex.com", "yandex.ru",
    "mail.ru", "inbox.ru", "rambler.ru", "yahoo.co.uk",
    "yahoo.fr", "yahoo.de", "yahoo.es", "yahoo.it",
    "yahoo.com.ar", "yahoo.com.mx", "yahoo.com.br",
    "yahoo.com.tr", "yahoo.com.au",
    "googlemail.com", "pm.me", "fastmail.com",
}

# Keywords strongly suggesting wrong sector
WRONG_SECTOR_PATTERNS = [
    (r'\blaw firm\b', "law firm"),
    (r'\blegal services?\b', "legal"),
    (r'\battorney\b', "attorney"),
    (r'\btextile\b', "textile"),
    (r'\bgarment\b', "garment"),
    (r'\bclothing\b', "clothing"),
    (r'\bapparel\b', "apparel"),
    (r'\bconstruction company\b', "construction"),
    (r'\bpharmaceut', "pharmaceutical"),
    (r'\binsurance company\b', "insurance"),
    (r'\breal estate agency\b', "real estate"),
    (r'\bpetroleum company\b', "petroleum"),
]


def run_gog_get(range_str, retries=3):
    """Get data from Google Sheets."""
    for attempt in range(retries):
        cmd = [
            "gog", "sheets", "get",
            SHEET_ID, range_str,
            "--json", "--account", ACCOUNT
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                pass
        if attempt < retries - 1:
            print(f"  Retry {attempt+1} for {range_str}...", file=sys.stderr)
            time.sleep(2)
    print(f"  FAILED to get {range_str}", file=sys.stderr)
    return None


def run_gog_update(range_str, value, retries=3):
    """Update a single cell in Google Sheets."""
    for attempt in range(retries):
        cmd = [
            "gog", "sheets", "update",
            SHEET_ID, range_str,
            "--values-json", json.dumps([[value]]),
            "--input", "USER_ENTERED",
            "--account", ACCOUNT
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return True
        if attempt < retries - 1:
            time.sleep(2)
    print(f"  FAILED update {range_str}: {result.stderr[:100]}", file=sys.stderr)
    return False


def is_valid_email_format(email):
    """Basic email format validation."""
    if not email or not email.strip():
        return True  # Empty = not invalid, just missing
    email = email.strip()
    if ' ' in email and not email.startswith('"'):
        return False  # Space in email usually wrong
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def get_email_domain(email):
    """Extract domain from email."""
    if not email or '@' not in email:
        return None
    return email.strip().lower().split('@')[-1]


def country_matches(country_col, expected):
    """Check if country column matches an expected country name."""
    if not country_col:
        return False
    c = country_col.lower().strip()
    if isinstance(expected, list):
        return any(e.lower() in c or c in e.lower() for e in expected)
    return expected.lower() in c or c in expected.lower()


def check_email_country_mismatch(country, email):
    """
    Returns a mismatch description string if the email domain's country TLD
    clearly doesn't match the country in Column A, otherwise None.
    """
    if not email or not country:
        return None
    
    domain = get_email_domain(email)
    if not domain:
        return None
    
    # Skip generic providers
    if domain in GENERIC_PROVIDERS:
        return None
    
    # Check compound TLDs first (more specific)
    for tld, expected_country in TLD_TO_COUNTRY.items():
        if domain.endswith(tld):
            if not country_matches(country, expected_country):
                if isinstance(expected_country, list):
                    expected_str = "/".join(expected_country)
                else:
                    expected_str = expected_country
                return f"Email domain '{domain}' ({expected_str}) vs sütun A: '{country}'"
    
    # Check single TLDs
    dot_parts = domain.split('.')
    if len(dot_parts) >= 2:
        tld = '.' + dot_parts[-1]
        if tld in SINGLE_TLD_MAP:
            expected_country = SINGLE_TLD_MAP[tld]
            if not country_matches(country, expected_country):
                if isinstance(expected_country, list):
                    expected_str = "/".join(expected_country)
                else:
                    expected_str = expected_country
                # Only flag if it's a very specific country code (not .com, .net, .org etc.)
                common_tlds = {'.com', '.net', '.org', '.info', '.biz', '.co', '.io', '.ai'}
                if tld not in common_tlds:
                    return f"Email domain '{domain}' ({expected_str}) vs sütun A: '{country}'"
    
    return None


def check_phone_country_mismatch(country, phone):
    """
    Check phone country code against Column A country.
    Only flags clear mismatches.
    """
    if not phone or not country:
        return None
    
    c = country.lower().strip()
    
    # Check for specific country prefixes that we can verify
    checks = [
        ("+55", ["brazil", "brasil"], "Brezilya"),
        ("+54", ["argentina"], "Arjantin"),
        ("+52", ["mexico", "méxico"], "Meksika"),
        ("+57", ["colombia"], "Kolombiya"),
        ("+51", ["peru"], "Peru"),
        ("+58", ["venezuela"], "Venezuela"),
        ("+56", ["chile"], "Şili"),
        ("+593", ["ecuador"], "Ekvador"),
        ("+90", ["turkey", "türkiye"], "Türkiye"),
        ("+44", ["uk", "united kingdom", "england", "britain"], "İngiltere"),
        ("+61", ["australia"], "Avustralya"),
        ("+91", ["india"], "Hindistan"),
        ("+20", ["egypt", "mısır"], "Mısır"),
        ("+966", ["saudi", "arabia", "ksa"], "S. Arabistan"),
        ("+213", ["algeria", "cezayir"], "Cezayir"),
        ("+27", ["south africa"], "G. Afrika"),
        ("+234", ["nigeria"], "Nijerya"),
        ("+92", ["pakistan"], "Pakistan"),
        ("+65", ["singapore"], "Singapur"),
        ("+62", ["indonesia"], "Endonezya"),
        ("+60", ["malaysia"], "Malezya"),
        ("+81", ["japan", "japonya"], "Japonya"),
        ("+86", ["china", "çin"], "Çin"),
        ("+886", ["taiwan"], "Tayvan"),
        ("+82", ["korea", "kore"], "Güney Kore"),
        ("+63", ["philippines", "filipin"], "Filipinler"),
        ("+98", ["iran"], "İran"),
        ("+964", ["iraq", "irak"], "Irak"),
        ("+962", ["jordan", "ürdün"], "Ürdün"),
        ("+961", ["lebanon", "lübnan"], "Lübnan"),
        ("+971", ["uae", "united arab", "emirates", "dubai", "abu dhabi"], "BAE"),
        ("+974", ["qatar", "katar"], "Katar"),
        ("+965", ["kuwait", "kuveyt"], "Kuveyt"),
        ("+973", ["bahrain", "bahreyn"], "Bahreyn"),
        ("+968", ["oman"], "Umman"),
        ("+967", ["yemen"], "Yemen"),
        ("+216", ["tunisia", "tunus"], "Tunus"),
        ("+212", ["morocco", "maroko", "fas"], "Fas"),
        ("+218", ["libya", "libya"], "Libya"),
        ("+249", ["sudan"], "Sudan"),
        ("+251", ["ethiopia", "etiyopya"], "Etiyopya"),
        ("+254", ["kenya"], "Kenya"),
        ("+233", ["ghana"], "Gana"),
        ("+237", ["cameroon", "kamerun"], "Kamerun"),
        ("+994", ["azerbaijan", "azerbaycan"], "Azerbaycan"),
        ("+995", ["georgia", "gürcistan"], "Gürcistan"),
        ("+374", ["armenia", "ermenistan"], "Ermenistan"),
        ("+7", ["russia", "rusya", "kazakhstan", "kazakistan"], None),  # +7 is Russia or Kazakhstan
        ("+380", ["ukraine", "ukrayna"], "Ukrayna"),
        ("+375", ["belarus"], "Belarus"),
        ("+998", ["uzbekistan"], "Özbekistan"),
        ("+992", ["tajikistan", "tacikistan"], "Tacikistan"),
        ("+993", ["turkmenistan"], "Türkmenistan"),
        ("+996", ["kyrgyzstan", "kırgızistan"], "Kırgızistan"),
        ("+976", ["mongolia", "moğolistan"], "Moğolistan"),
    ]
    
    for code, country_keywords, label in checks:
        if code in phone:
            matches = any(kw in c for kw in country_keywords)
            if not matches:
                if code == "+7":
                    return f"Telefon öneki {code} (Rusya/Kazakistan) sütun A: '{country}'"
                else:
                    return f"Telefon öneki {code} ({label}) sütun A: '{country}'"
    
    return None


def check_wrong_sector(company):
    """Check for obvious wrong-sector companies."""
    if not company:
        return None
    cl = company.lower()
    for pattern, label in WRONG_SECTOR_PATTERNS:
        if re.search(pattern, cl, re.IGNORECASE):
            return f"Sektör şüphesi: '{label}' anahtar kelimesi"
    return None


def validate_row(row):
    """Validate a row; return list of issues."""
    while len(row) < 11:
        row.append("")
    
    country = row[0].strip()
    company = row[1].strip()
    email = row[4].strip() if len(row) > 4 else ""
    phone = row[6].strip() if len(row) > 6 else ""
    
    issues = []
    
    # 1. Email format check
    if email and not is_valid_email_format(email):
        issues.append(f"GEÇERSİZ EMAIL: '{email}'")
    
    # 2. Missing email
    if not email:
        issues.append("EMAİL EKSİK")
    
    # 3. Country-email mismatch
    if email and is_valid_email_format(email):
        mismatch = check_email_country_mismatch(country, email)
        if mismatch:
            issues.append(f"ÜLKE-EMAIL UYUMSUZ: {mismatch}")
    
    # 4. Phone-country mismatch
    if phone:
        ph_issue = check_phone_country_mismatch(country, phone)
        if ph_issue:
            issues.append(f"TELEFON UYUMSUZ: {ph_issue}")
    
    # 5. Sector check
    sector_issue = check_wrong_sector(company)
    if sector_issue:
        issues.append(sector_issue)
    
    return issues


def main():
    BATCH_SIZE = 300
    START_ROW = 2
    END_ROW = 5243
    
    stats = {
        "total_reviewed": 0,
        "rows_with_issues": 0,
        "total_issues": 0,
        "invalid_emails": 0,
        "missing_emails": 0,
        "country_email_mismatch": 0,
        "phone_mismatch": 0,
        "sector_issues": 0,
        "updates_written": 0,
        "update_failures": 0,
    }
    
    flagged_rows = []
    
    print(f"=== Silverline Sheet Validator ===")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Rows: {START_ROW} to {END_ROW} | Batch: {BATCH_SIZE}")
    print()
    
    for batch_start in range(START_ROW, END_ROW + 1, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE - 1, END_ROW)
        range_str = f"Sheet1!A{batch_start}:K{batch_end}"
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Rows {batch_start}-{batch_end}...", end=" ", flush=True)
        
        data = run_gog_get(range_str)
        if not data or "values" not in data:
            print("FAILED - skipping batch")
            continue
        
        rows = data["values"]
        pending_updates = []
        batch_issues = 0
        
        for i, row in enumerate(rows):
            actual_row_num = batch_start + i
            
            # Pad
            while len(row) < 11:
                row.append("")
            
            existing_j = row[9].strip() if len(row) > 9 else ""
            sent = row[10].strip() if len(row) > 10 else ""
            
            stats["total_reviewed"] += 1
            
            issues = validate_row(list(row))
            
            if not issues:
                continue  # Clean row - skip, no update needed
            
            # Deduplicate issues vs existing notes (don't re-flag same issue)
            new_issues = []
            for issue in issues:
                # Check if a similar flag already exists in column J
                # Simple heuristic: check if keywords are already in existing note
                if existing_j:
                    issue_type = issue.split(":")[0].strip()
                    if issue_type in existing_j:
                        continue  # Already noted
                new_issues.append(issue)
            
            if not new_issues:
                continue  # All issues already documented
            
            # Count issue types
            stats["rows_with_issues"] += 1
            for issue in new_issues:
                stats["total_issues"] += 1
                batch_issues += 1
                if "GEÇERSİZ EMAIL" in issue:
                    stats["invalid_emails"] += 1
                elif "EMAİL EKSİK" in issue:
                    stats["missing_emails"] += 1
                elif "ÜLKE-EMAIL UYUMSUZ" in issue:
                    stats["country_email_mismatch"] += 1
                elif "TELEFON UYUMSUZ" in issue:
                    stats["phone_mismatch"] += 1
                elif "Sektör" in issue:
                    stats["sector_issues"] += 1
            
            issues_str = " | ".join(new_issues)
            if existing_j:
                new_note = f"{existing_j} | KONTROL {TODAY}: {issues_str}"
            else:
                new_note = f"KONTROL {TODAY}: {issues_str}"
            
            if len(new_note) > 2500:
                new_note = new_note[:2497] + "..."
            
            pending_updates.append((f"Sheet1!J{actual_row_num}", new_note))
            
            flagged_rows.append({
                "row": actual_row_num,
                "country": row[0],
                "company": row[1],
                "email": row[4] if len(row) > 4 else "",
                "issues": new_issues,
            })
        
        print(f"{len(rows)} rows, {batch_issues} issues, {len(pending_updates)} updates", end=" ")
        
        # Write updates
        if pending_updates:
            written = 0
            for range_str_upd, value in pending_updates:
                if run_gog_update(range_str_upd, value):
                    written += 1
                    stats["updates_written"] += 1
                else:
                    stats["update_failures"] += 1
                time.sleep(0.4)  # Rate limit
            print(f"-> wrote {written}/{len(pending_updates)}")
        else:
            print()
        
        time.sleep(0.5)  # Pause between batches
    
    # Final summary
    print()
    print("=" * 50)
    print("TAMAMLANDI")
    print("=" * 50)
    print(f"İncelenen toplam satır: {stats['total_reviewed']}")
    print(f"Sorunlu satır sayısı:   {stats['rows_with_issues']}")
    print(f"Toplam sorun sayısı:    {stats['total_issues']}")
    print(f"  - Geçersiz email:     {stats['invalid_emails']}")
    print(f"  - Eksik email:        {stats['missing_emails']}")
    print(f"  - Ülke-email uyumsuz: {stats['country_email_mismatch']}")
    print(f"  - Telefon uyumsuz:    {stats['phone_mismatch']}")
    print(f"  - Sektör şüphesi:     {stats['sector_issues']}")
    print(f"Yazılan güncelleme:     {stats['updates_written']}")
    print(f"Başarısız güncelleme:   {stats['update_failures']}")
    print(f"Bitiş: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Save report
    report = {
        "run_date": TODAY,
        "stats": stats,
        "flagged_rows": flagged_rows[:300]
    }
    
    with open("{{WORKSPACE}}/validation_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print("\nRapor kaydedildi: validation_report.json")
    
    # Output stats for Telegram
    print("\n--- TELEGRAM SUMMARY ---")
    print(json.dumps(stats, ensure_ascii=False))
    
    return stats


if __name__ == "__main__":
    main()
