"""The desktop can render more than the model is told — this makes that fail.

Mermaid diagrams rendered in the transcript for as long as
``embeds/registry.tsx`` has existed, and the desktop platform hint never
mentioned them. The capability was not missing. The sentence describing it
was — and without that sentence the model had no way to know a diagram would
survive the trip, so on anything short of an explicit "draw me X" it answered
in prose.

That gap is invisible to every other test in the repo: the renderer has its own
tests and passes, the prompt has its own tests and passes, and nothing compares
them. So these tests read the renderer's own source and assert the hint names
what it can do. A new fence language, or a new artifact kind, fails here until
someone writes the sentence.

Measured, 2026-08-28 (claude-sonnet-4, temperature 0, same question per arm,
the hint as the only variable):

  asked EXPLICITLY to draw ("show me the flow of ..."):
      without the hint 2/2 drew, with it 2/2 drew — no effect. The model
      already reaches for mermaid when the request names a diagram, so the
      original claim that it "never emitted one" was too strong.

  asked to EXPLAIN, where drawing is a judgement call
  ("explain how our OAuth login works", "what states does a job move
  through"), in English and Arabic:
      without the hint 0/4 drew.  With it, 4/4.

That second row is what this guard protects: the hint does not teach the model
mermaid syntax, it tells it the surface will render one — which is the fact it
needs to choose a diagram over a paragraph.
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
    # The FENCED form, not a bare substring. "ts" and "py" both occur inside
    # ordinary words in the hint ("artifacts", "copyable"), so a substring
    # check would pass a TypeScript or Python renderer that was never
    # advertised — a guard that reports success is worse than no guard.
    unadvertised = sorted(
        lang for lang in _rich_fence_languages() if f"```{lang}" not in _HINT
    )

    assert not unadvertised, (
        f"the desktop renders ```{{{','.join(unadvertised)}}} fences but the model is "
        "never told, so it will not emit them. Name the fence as ```<lang> in "
        "PLATFORM_HINTS['desktop'] in agent/prompt_builder.py."
    )


def test_the_fence_check_is_not_satisfied_by_an_accidental_substring():
    """Guard the guard, the other way: prove the strict form actually bites."""
    assert "ts" in _HINT and "```ts" not in _HINT  # "artifacts" contains "ts"
    assert "py" in _HINT and "```py" not in _HINT  # "copyable" contains "py"


def _artifact_kinds() -> set:
    """The kinds ``detectArtifact`` can promote a fence into.

    Parsed from the union, so adding one is what trips the test.
    """
    union = re.search(
        r"export type ArtifactKind =([^\n]+)", _source(_ARTIFACT_DETECT)
    )
    assert union, "ArtifactKind not found — artifact-detect.ts changed shape"

    return set(re.findall(r"'([a-z0-9-]+)'", union.group(1)))


# Kinds with no ```<kind> fence of their own, so the strict check below cannot
# apply. An entry here is a decision someone has to write down, which is the
# point — it must not become the easy way to silence this test.
_UNADVERTISED_KINDS = {
    # There is no ```code fence: 'code' is the kind ANY language promotes into
    # past the line threshold, and the hint says so in prose ("any code fence
    # past roughly 48 lines"), which the threshold test below pins.
    "code",
}


def test_the_artifact_kind_union_is_not_empty():
    """Guard the guard: a regex that silently matches nothing proves nothing."""
    assert _artifact_kinds(), "parsed zero artifact kinds — the regex rotted"


def test_every_artifact_kind_is_named_in_the_desktop_hint():
    """A fence that promotes to a card behaves differently from one that does not.

    A model that does not know long fences become artifact cards truncates them
    to protect the conversation — which is the opposite of what it should do.
    Asserted per kind, not as a single "artifact" mention: the word would stay
    in that hint forever while a newly added kind went unadvertised. And in the
    FENCED form, for the same reason the fence test uses it — a bare substring
    check would pass a kind named `chart` on the strength of "widget, chart, or
    visualization" already being in the hint for unrelated reasons.
    """
    assert "artifact" in _HINT, (
        "substantial html/svg/code fences promote to artifact cards and the hint "
        "never says so"
    )

    unnamed = sorted(
        kind
        for kind in _artifact_kinds() - _UNADVERTISED_KINDS
        if f"```{kind}" not in _HINT
    )

    assert not unnamed, (
        f"detectArtifact promotes ```{{{','.join(unnamed)}}} fences into artifact "
        "cards but the hint never names them, so the model cannot know what "
        "happens to such a fence. Name each as ```<kind> in "
        "PLATFORM_HINTS['desktop'] in agent/prompt_builder.py, or add it to "
        "_UNADVERTISED_KINDS with a reason."
    )


def test_the_promotion_threshold_the_hint_quotes_is_the_real_one():
    """The hint says "roughly 48 lines". 48 is a constant someone can tune.

    Nobody re-reads a prompt when adjusting a threshold, and neither the TS
    contract test (which samples at 60 lines) nor the fence tests notice: they
    would all stay green while the sentence the model acts on became false.
    """
    match = re.search(r"const CODE_MIN_LINES = (\d+)", _source(_ARTIFACT_DETECT))
    assert match, "CODE_MIN_LINES not found — artifact-detect.ts changed shape"

    assert match.group(1) in _HINT, (
        f"code fences promote at {match.group(1)} lines, but the desktop hint quotes a "
        "different number. Update the sentence in PLATFORM_HINTS['desktop']."
    )

    # SVG_MIN_CHARS has no counterpart to pin: the hint describes it as "small"
    # vs "large standalone graphic" on purpose, because a character count is not
    # something a model can usefully apply while writing. The behaviour is
    # pinned instead, in artifact-detect.prompt-contract.test.ts.


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
