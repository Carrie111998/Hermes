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
