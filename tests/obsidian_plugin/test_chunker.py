from plugins.memory.obsidian.chunker import chunk_markdown, Chunk


def test_strips_yaml_frontmatter():
    text = "---\ndate: 2026-07-22\ntags: [x]\n---\n# Titel\nbrödtext\n"
    chunks = chunk_markdown(text)
    joined = "\n".join(c.content for c in chunks)
    assert "date: 2026-07-22" not in joined
    assert "Titel" in joined


def test_no_headings_single_chunk():
    chunks = chunk_markdown("bara brödtext utan rubrik\nrad två")
    assert len(chunks) == 1
    assert chunks[0].heading_trail == ""
    assert "brödtext" in chunks[0].content


def test_splits_on_headings_with_trail():
    text = "# A\nalfa\n## B\nbeta\n## C\ngamma\n"
    chunks = chunk_markdown(text)
    trails = [c.heading_trail for c in chunks]
    assert trails == ["A", "B", "C"]
    assert "alfa" in chunks[0].content
    assert "beta" in chunks[1].content


def test_preamble_before_first_heading_is_its_own_chunk():
    text = "inledande text\n# Rubrik\nkropp\n"
    chunks = chunk_markdown(text)
    assert chunks[0].heading_trail == ""
    assert "inledande text" in chunks[0].content
    assert chunks[1].heading_trail == "Rubrik"


def test_empty_input_no_chunks():
    assert chunk_markdown("") == []
    assert chunk_markdown("---\nonly: frontmatter\n---\n") == []
