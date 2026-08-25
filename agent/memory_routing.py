"""Scope policy for user-requested persistent memory.

This is intentionally not a storage backend. Project facts are maintained in
the current project's AGENTS.md through normal file-edit tools, while user and
cross-project facts continue to use the existing memory tool.
"""

from enum import Enum


class MemoryScope(str, Enum):
    """The destination decision for a requested memory."""

    PROJECT = "project"
    USER = "user"
    GLOBAL = "global"
    TEMPORARY = "temporary"
    AMBIGUOUS = "ambiguous"


EXECUTION_INTENT_MARKERS = (
    "설정해", "설치해", "수정해", "테스트해", "만들어", "실행해", "확인해", "적용해", "환경을 구성해",
)


def is_memory_intent(content: str) -> bool:
    """Return whether content explicitly asks for durable memory storage.

    Execution intent markers are guidance-only: an explicit request to remember
    always wins, while wording such as "앞으로" alone is not a memory request.
    """
    text = content.casefold()
    return any(marker in text for marker in (
        "기억해",
        "기억해둬",
        "다음에도 기억해",
        "앞으로도 이 설정을 기억해",
        "장기적으로 저장해둬",
        "내 선호로 기억해",
    ))


def classify_memory_scope(content: str) -> MemoryScope:
    """Conservatively classify a requested memory's stated content.

    This policy aid supports the prompt and deterministic callers; unclear
    durable settings deliberately return AMBIGUOUS for user clarification.
    """
    text = content.casefold()

    # Scope uncertainty must win over every other signal: global storage is
    # intentionally conservative, and callers should clarify before writing.
    if any(marker in text for marker in (
        "\ud604\uc7ac \ud504\ub85c\uc81d\ud2b8\uc5d0\ub9cc \uc801\uc6a9\ud560\uc9c0\ub294 \uc544\uc9c1 \uc815\ud558\uc9c0 \uc54a\uc558\uc5b4",
        "\uc804\uccb4 \ud504\ub85c\uc81d\ud2b8\uc5d0 \uc801\uc6a9\ud560\uc9c0\ub294 \ubaa8\ub974\uaca0\uc5b4",
        "\uc5b4\ub514\uc5d0 \uc801\uc6a9\ud560\uc9c0\ub294 \uc544\uc9c1 \ubaa8\ub974\uaca0\uc5b4",
        "\ubc94\uc704\uac00 \uc560\ub9e4\ud574",
        "\uc774 \ud504\ub85c\uc81d\ud2b8\ub9cc\uc778\uc9c0 \uc804\uccb4\uc778\uc9c0 \ubaa8\ub974\uaca0\uc5b4",
        "\uc544\uc9c1 \uc801\uc6a9 \ubc94\uc704\ub97c \uc815\ud558\uc9c0 \uc54a\uc558\uc5b4",
        "\uc801\uc6a9 \ubc94\uc704\uac00 \uc560\ub9e4\ud574",
    )):
        return MemoryScope.AMBIGUOUS

    if any(marker in text for marker in (
        "\uc9c0\uae08 ", "\ud604\uc7ac ", "\ud14c\uc2a4\ud2b8 \uc11c\ubc84", "\ud3ec\ud2b8\uac00", "todo", "debug", "\ub85c\uadf8",
    )):
        return MemoryScope.TEMPORARY
    if any(marker in text for marker in (
        "\uc124\uba85\uc740", "\uc124\uba85\ud560 \ub54c", "\ub2f5\ubcc0\uc740", "\ucf54\ub4dc\ub97c \uba3c\uc800 \ubcf4\uc5ec\uc918", "\ud55c\uad6d\uc5b4\ub85c \ub2f5\ud574\uc918",
        "\uc608\uc81c\ub97c \uac19\uc774 \ubcf4\uc5ec\uc8fc\ub294 \uac78 \uc120\ud638\ud574",
    )):
        return MemoryScope.USER
    if any(marker in text for marker in (
        "\uc774 \uba38\uc2e0", "\uacf5\ud1b5 ", "\ubaa8\ub4e0 \ud504\ub85c\uc81d\ud2b8", "shared tool", "machine",
    )):
        return MemoryScope.GLOBAL
    if any(marker in text for marker in (
        "\uc774 \ud504\ub85c\uc81d\ud2b8", "\uc774 \uc800\uc7a5\uc18c", "\ud504\ub85c\uc81d\ud2b8\uc5d0\uc11c", "api \uc751\ub2f5", "db \uc2a4\ud0a4\ub9c8",
        "\uc544\ud0a4\ud14d\ucc98", "\ucf54\ub529 \ucee8\ubca4\uc158",
    )):
        return MemoryScope.PROJECT
    return MemoryScope.AMBIGUOUS


MEMORY_ROUTING_GUIDANCE = (
    "Only route explicit memory requests (for example, 기억해) to memory storage; "
    "execution requests for package installs, venv creation, running tests, editing "
    "files, environment or PATH changes, dev-environment setup, debugging procedures, "
    "or running commands must not be stored in USER.md, MEMORY.md, or AGENTS.md unless "
    "the user explicitly asks to remember them. 앞으로 alone does not make something a "
    "memory request. When asked to remember something, classify its scope before writing. "
    "Project-specific facts, decisions, architecture, conventions, commands, or "
    "configuration belong in the current project's AGENTS.md: identify the current "
    "project from the active CWD, prefer its git root when present, then use loaded "
    ".hermes.md/AGENTS.md provenance or the CWD as a fallback. Read any existing "
    "AGENTS.md first, then merge or replace the related concise entry. If it is missing, "
    "create only a minimal Project Context structure. Do not append a work log or "
    "overwrite unrelated instructions; use the normal patch/write_file path.\n"
    "USER.md via memory(target='user') is only for durable personal preferences and "
    "interaction style, such as response language, brevity, or showing code first. "
    "Technical or operational defaults (models, package managers, formats, databases, "
    "frameworks, architecture, storage, analysis, APIs, deployment, or implementation "
    "rules) belong in Project or Global scope, not USER.md. Only durable cross-project "
    "facts useful without the current repository belong in MEMORY.md via "
    "memory(target='memory'); keep MEMORY.md strictly small. Temporary facts, logs, "
    "one-off errors, current ports, and short-lived TODOs are not persisted. If the "
    "scope could be Project, User, or Global, or the user expresses uncertainty about "
    "scope, do not guess: ask the user with clarify before writing."
)
