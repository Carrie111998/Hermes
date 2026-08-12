import agent.learn_entrypoint as learn_entrypoint
from agent.learn_checkpoint import LearnCheckpoint


def test_slash_learn_query_runs_preflight_and_builds_checkpointed_prompt(monkeypatch):
    monkeypatch.setattr(
        learn_entrypoint,
        "prepare_learn_checkpoint",
        lambda _request: LearnCheckpoint(
            status="created",
            name="lei-promocoes",
            source="lei-promocoes.pdf",
            message="Checkpoint skill 'lei-promocoes' created.",
        ),
    )

    prompt = learn_entrypoint.normalize_learn_query(
        "/learn /tmp/lei-promocoes.pdf — criar referências por capítulos"
    )

    assert "Checkpoint skill 'lei-promocoes' created." in prompt
    assert "Continue from this checkpoint" in prompt
    assert "/tmp/lei-promocoes.pdf" in prompt


def test_non_learn_query_is_unchanged():
    query = "explique o artigo 5"
    assert learn_entrypoint.normalize_learn_query(query) == query
