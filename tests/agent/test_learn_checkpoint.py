from pathlib import Path

import agent.learn_checkpoint as learn_checkpoint


def test_large_local_document_gets_a_persistent_skill_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / "Lei das Promocoes.pdf"
    source.write_bytes(b"pdf" * 50_000)
    calls = []

    def fake_skill_manage(**kwargs):
        calls.append(kwargs)
        return '{"success": true, "path": "knowledge-base/lei-das-promocoes"}'

    monkeypatch.setattr(learn_checkpoint, "skill_manage", fake_skill_manage)

    result = learn_checkpoint.prepare_learn_checkpoint(str(source))

    assert result.status == "created"
    assert result.name == "lei-das-promocoes"
    assert len(calls) == 1
    assert calls[0]["action"] == "create"
    assert calls[0]["category"] == "knowledge-base"
    assert "references/" in calls[0]["content"]
    assert Path(calls[0]["content"].split("source: ", 1)[1].splitlines()[0]).name == source.name


def test_same_stem_as_unrelated_skill_gets_a_distinct_checkpoint_name(tmp_path, monkeypatch):
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"pdf" * 50_000)
    unrelated = tmp_path / "unrelated-skill"
    unrelated.mkdir()
    (unrelated / "SKILL.md").write_text(
        "---\nname: manual\ndescription: An unrelated skill.\n---\n",
        encoding="utf-8",
    )
    calls = []

    monkeypatch.setattr(
        learn_checkpoint,
        "_find_skill",
        lambda name: {"path": unrelated} if name == "manual" else None,
    )
    monkeypatch.setattr(
        learn_checkpoint,
        "skill_manage",
        lambda **kwargs: calls.append(kwargs) or '{"success": true}',
    )

    result = learn_checkpoint.prepare_learn_checkpoint(str(source))

    assert result.status == "created"
    assert result.name.startswith("manual-")
    assert calls[0]["name"] == result.name


def test_legacy_checkpoint_without_source_id_is_not_reused_for_another_file(tmp_path, monkeypatch):
    source = tmp_path / "new" / "manual.pdf"
    source.parent.mkdir()
    source.write_bytes(b"pdf" * 50_000)
    legacy = tmp_path / "legacy-skill"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text(
        "---\nname: manual\ndescription: old\n---\n"
        "Checkpoint index for a knowledge-base skill.\n"
        "- source: manual.pdf\n",
        encoding="utf-8",
    )
    calls = []

    monkeypatch.setattr(
        learn_checkpoint,
        "_find_skill",
        lambda name: {"path": legacy} if name == "manual" else None,
    )
    monkeypatch.setattr(
        learn_checkpoint,
        "skill_manage",
        lambda **kwargs: calls.append(kwargs) or '{"success": true}',
    )

    result = learn_checkpoint.prepare_learn_checkpoint(str(source))

    assert result.status == "created"
    assert result.name.startswith("manual-")
    assert calls[0]["name"] == result.name
