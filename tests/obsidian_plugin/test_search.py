from plugins.memory.obsidian.index import ObsidianIndex, SearchHit


def _seeded():
    idx = ObsidianIndex(":memory:")
    idx.upsert_note("forsakringar/bil.md", "# Bilförsäkring\nfullständig hos Folksam", 1.0)
    idx.upsert_note("memory/daniel.md", "# Daniel\ndricker kaffe varje morgon", 1.0)
    idx.upsert_note("projekt/saa.md", "# SAA\nbundle-split pågår", 1.0)
    return idx


def test_search_finds_relevant_note():
    hits = _seeded().search("bilförsäkring folksam")
    assert hits
    assert hits[0].path == "forsakringar/bil.md"
    assert isinstance(hits[0], SearchHit)


def test_search_returns_empty_on_no_match():
    assert _seeded().search("kvantfysik") == []


def test_search_respects_top_k():
    idx = ObsidianIndex(":memory:")
    for i in range(5):
        idx.upsert_note(f"n{i}.md", f"# N{i}\nkaffe kaffe kaffe", 1.0)
    assert len(idx.search("kaffe", top_k=3)) == 3


def test_empty_query_returns_empty():
    assert _seeded().search("") == []
