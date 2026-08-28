"""The desktop can render more than the model is told — this makes that fail.

Mermaid diagrams rendered in the transcript for as long as
``embeds/registry.tsx`` has existed. The desktop platform hint never mentioned
them, so the model never emitted one: asked for a flow it wrote prose steps, or
built an HTML file and a ``::preview`` directive, because those were the only
rendering paths it had been told about. The capability was not missing. The
sentence describing it was.

That gap is invisible to every other test in the repo: the renderer has its own
tests and passes, the prompt has its own tests and passes, and nothing compares
them. So these tests read the renderer's own source and assert the hint names
what it can do. A new fence language, or a new artifact kind, fails here until
someone writes the sentence.
"""

import re
from pathlib import Path

import pytest

from agent.prompt_builder import PLATFORM_HINTS

_DESKTOP_SRC = Path(__file__).resolve().parents[2] / "apps" / "desktop" / "src"
_REGISTRY = _DESKTOP_SRC / "components" / "assistant-ui" / "embeds" / "registry.tsx"
_ARTIFACT_DETECT = _DESKTOP_SRC / "lib" / "artifact-detect.ts"

# Read once: the hint is a module constant, and the sources do not change
# mid-run.
_HINT = PLATFORM_HINTS["desktop"].lower()


def _source(path: Path) -> str:
    if not path.is_file():
        pytest.skip(f"desktop sources not present at {path}")
    return path.read_text(encoding="utf-8")


def _rich_fence_languages() -> set:
    """The languages ``RichCodeBlock`` routes to a dedicated renderer.

    Parsed from the LAZY_FENCE table rather than hard-coded, so adding a
    renderer is what trips this test — not editing a list in the test.
    """
    body = re.search(
        r"const LAZY_FENCE[^=]*=\s*\{(.*?)\n\}", _source(_REGISTRY), re.DOTALL
    )
    assert body, "LAZY_FENCE table not found — registry.tsx changed shape"

    return set(re.findall(r"^\s*([a-z0-9-]+):\s*lazy\(", body.group(1), re.MULTILINE))


def test_the_lazy_fence_table_is_not_empty():
    """Guard the guard: a regex that silently matches nothing proves nothing."""
    assert _rich_fence_languages(), "parsed zero fence languages — the regex rotted"


def test_every_inline_fence_renderer_is_named_in_the_desktop_hint():
    unadvertised = sorted(lang for lang in _rich_fence_languages() if lang not in _HINT)

    assert not unadvertised, (
        f"the desktop renders ```{{{','.join(unadvertised)}}} fences but the model is "
        "never told, so it will not emit them. Add them to "
        "PLATFORM_HINTS['desktop'] in agent/prompt_builder.py."
    )


def test_artifact_promotion_is_named_in_the_desktop_hint():
    """A fence that promotes to a card behaves differently from one that does not.

    A model that does not know long fences become artifact cards truncates them
    to protect the conversation — which is the opposite of what it should do.
    """
    kinds = set(
        re.findall(r"export type ArtifactKind =([^\n]+)", _source(_ARTIFACT_DETECT))
    )
    assert kinds, "ArtifactKind not found — artifact-detect.ts changed shape"

    assert "artifact" in _HINT, (
        "substantial html/svg/code fences promote to artifact cards and the hint "
        "never says so"
    )


def test_mermaid_is_exempt_from_artifact_promotion():
    """The hint promises mermaid ALWAYS draws in place. Keep that true.

    Artifact detection runs before the rich-fence router in markdown-text.tsx,
    so a language that starts promoting stops rendering inline — and the
    promise in the hint becomes a lie the model acts on.
    """
    non_artifact = re.search(
        r"const NON_ARTIFACT_LANGUAGES = new Set\(\[(.*?)\]\)",
        _source(_ARTIFACT_DETECT),
        re.DOTALL,
    )
    assert non_artifact, "NON_ARTIFACT_LANGUAGES not found"

    assert "'mermaid'" in non_artifact.group(1), (
        "mermaid left the artifact exemption list, so large diagrams now open in "
        "the right rail instead of drawing in place — the desktop hint promises "
        "otherwise"
    )


def test_the_directive_syntax_the_hint_teaches_is_the_one_the_parser_accepts():
    """``::name{key="value"}`` is model-facing syntax; a drift here is silent."""
    directive_src = _source(_DESKTOP_SRC / "lib" / "transcript-directives.ts")

    assert "::name{" in directive_src or "::<name>{" in directive_src
    assert "::preview{" in PLATFORM_HINTS["desktop"]
