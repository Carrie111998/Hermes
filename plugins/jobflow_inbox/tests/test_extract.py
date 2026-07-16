from plugins.jobflow_inbox import extract


def test_find_first_url_plain():
    assert extract.find_first_url("https://boards.greenhouse.io/acme/jobs/123") \
        == "https://boards.greenhouse.io/acme/jobs/123"


def test_find_first_url_with_note():
    text = "check this one out https://jobs.lever.co/acme/abc?utm_source=x nice role"
    assert extract.find_first_url(text) == "https://jobs.lever.co/acme/abc?utm_source=x"


def test_find_first_url_none():
    assert extract.find_first_url("no link here") is None


def test_normalize_strips_tracking_and_trailing_slash():
    raw = "https://boards.greenhouse.io/Acme/Jobs/123/?utm_source=li&gh_src=abc&ref=y"
    assert extract.normalize_url(raw) == "https://boards.greenhouse.io/Acme/Jobs/123"


def test_normalize_keeps_meaningful_query():
    raw = "https://jobs.lever.co/acme/abc?lever-source=foo"
    assert extract.normalize_url(raw) == "https://jobs.lever.co/acme/abc?lever-source=foo"


import pathlib

FIX = pathlib.Path(__file__).parent / "fixtures"


def _read(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_jsonld_full():
    jf = extract.parse_job_html(_read("jsonld_full.html"))
    assert jf.enrichment_status == "enriched"
    assert jf.title == "Senior Data Engineer"
    assert jf.company == "Acme Corp"
    assert "Austin" in jf.location and "TX" in jf.location
    assert "150000" in jf.salary and "190000" in jf.salary
    assert "pipelines" in jf.description and "<b>" not in jf.description


def test_parse_og_only_is_partial():
    jf = extract.parse_job_html(_read("og_only.html"))
    assert jf.enrichment_status == "partial"
    assert jf.title == "Staff ML Engineer at Globex"
    assert jf.company == "Globex Careers"


def test_parse_bare_is_failed():
    jf = extract.parse_job_html(_read("bare.html"))
    assert jf.enrichment_status == "failed"
    assert jf.title is None


def test_extract_job_fetch_error_returns_failed():
    def boom(url):
        raise OSError("blocked")

    jf = extract.extract_job("https://x.test/job/1", fetch=boom)
    assert jf.enrichment_status == "failed"
    assert jf.title is None


def test_extract_job_uses_injected_fetch():
    jf = extract.extract_job("https://x.test/job/1", fetch=lambda u: _read("jsonld_full.html"))
    assert jf.title == "Senior Data Engineer"
