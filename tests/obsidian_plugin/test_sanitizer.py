from plugins.memory.obsidian.sanitizer import sanitize_fts_query


def test_or_joins_phrase_literal_tokens():
    out = sanitize_fts_query("bilförsäkring och hemförsäkring")
    assert '"bilförsäkring"' in out
    assert '"hemförsäkring"' in out
    assert " OR " in out


def test_drops_short_tokens():
    out = sanitize_fts_query("a bil")
    assert '"bil"' in out
    assert '"a"' not in out


def test_strips_fts_special_chars():
    out = sanitize_fts_query('spara: "något"* (viktigt)')
    # no raw FTS operator chars survive inside tokens
    for tok in out.split(" OR "):
        inner = tok.strip('"')
        assert not any(ch in inner for ch in '"()*^:')


def test_empty_returns_empty():
    assert sanitize_fts_query("") == ""


def test_all_stopwords_falls_back_to_raw():
    # if nothing survives, return raw (no crash, caller sees 0 results)
    out = sanitize_fts_query("och")
    assert out == "och"
