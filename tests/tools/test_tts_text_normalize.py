import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from tools.tts_text_normalize import apply_pronunciation_substitutions, prepare_spoken_text


class _DummyAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, **kwargs):
        raise AssertionError("not used")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def test_prepare_spoken_text_expands_celsius_and_weather_units():
    raw = """## Christchurch today\n\n- **Now:** about **14°C**, feels like **14°C**\n- **Wind:** 9 km/h\n- **Rain:** 1.3 mm\n- **Range:** 11\u201317°C\n"""

    spoken = prepare_spoken_text(raw)

    assert "##" not in spoken
    assert "**" not in spoken
    assert "14 degrees Celsius" in spoken
    assert "11 to 17 degrees Celsius" in spoken
    assert "9 kilometres per hour" in spoken
    assert "1.3 millimetres" in spoken
    assert "°C" not in spoken
    assert "km/h" not in spoken


def test_prepare_spoken_text_polish_edge_cases():
    # Heading folds into the next sentence as a lead-in, not a bare label.
    assert prepare_spoken_text("## Weather\nIt will be sunny") == "Weather, It will be sunny."
    # Bare degree unit (no leading number) still expands.
    assert "degrees Celsius" in prepare_spoken_text("measured in °C")
    # Trailing comma is not swallowed into the amount.
    assert "300 US dollars" in prepare_spoken_text("US$300, next")
    # Real numeric rates expand, but and/or, N/A, IDs and dates are left intact.
    assert "5 dollars per month" in prepare_spoken_text("$5/month")
    assert "and/or" in prepare_spoken_text("choose and/or option")
    assert "N/A" in prepare_spoken_text("status N/A here")
    assert "2026/06/02" in prepare_spoken_text("due 2026/06/02 ok")


# ---------------------------------------------------------------------------
# Pronunciation substitution tests
# ---------------------------------------------------------------------------

def test_pronunciation_basic_substitution():
    """A word is replaced by its phonetic replacement."""
    result = apply_pronunciation_substitutions("Hello Tahlia", {"Tahlia": "Tarlia"})
    assert result == "Hello Tarlia"


def test_pronunciation_case_insensitive():
    """The match is case-insensitive — any casing of the key is replaced."""
    assert apply_pronunciation_substitutions("hello tahlia", {"Tahlia": "Tarlia"}) == "hello Tarlia"
    assert apply_pronunciation_substitutions("TAHLIA is here", {"Tahlia": "Tarlia"}) == "Tarlia is here"
    assert apply_pronunciation_substitutions("tAhLiA", {"Tahlia": "Tarlia"}) == "Tarlia"


def test_pronunciation_word_boundary_no_partial_match():
    """Whole-word boundary: 'Tahlias' is NOT substituted."""
    result = apply_pronunciation_substitutions("Tahlias and Tahlia", {"Tahlia": "Tarlia"})
    assert result == "Tahlias and Tarlia"


def test_pronunciation_empty_dict_noop():
    """Empty substitutions dict returns text unchanged."""
    assert apply_pronunciation_substitutions("Hello Tahlia", {}) == "Hello Tahlia"


def test_pronunciation_none_noop():
    """None substitutions returns text unchanged."""
    assert apply_pronunciation_substitutions("Hello Tahlia", None) == "Hello Tahlia"


def test_pronunciation_multiple_substitutions():
    """Multiple substitutions are all applied."""
    result = apply_pronunciation_substitutions(
        "Hello Tahlia and Siobhan",
        {"Tahlia": "Tarlia", "Siobhan": "Shi-vaun"},
    )
    assert result == "Hello Tarlia and Shi-vaun"


def test_pronunciation_special_regex_chars_escaped():
    """Special regex characters in the key are escaped, not interpreted."""
    # The dot in "foo.bar" is a regex metachar; it must be escaped so only
    # the literal "foo.bar" matches, not "fooxbar".
    result = apply_pronunciation_substitutions("foo.bar and fooxbar", {"foo.bar": "qux"})
    assert result == "qux and fooxbar"


def test_pronunciation_replacement_is_literal():
    """Backslashes and backreference-like text are inserted verbatim."""
    replacement = r"say \1 from C:\voices"
    assert apply_pronunciation_substitutions("Tahlia", {"Tahlia": replacement}) == replacement


def test_pronunciation_non_word_boundaries_support_technical_terms():
    """Terms ending or starting in punctuation still match as standalone literals."""
    substitutions = {"C++": "C plus plus", ".NET": "dot net"}
    result = apply_pronunciation_substitutions("C++ and .NET, not XC++ or .NETwork", substitutions)
    assert result == "C plus plus and dot net, not XC++ or .NETwork"


@pytest.mark.parametrize(
    "substitutions",
    [
        "Tahlia: Tarlia",
        ["Tahlia", "Tarlia"],
        {"Tahlia": 123},
        {123: "Tarlia"},
        {"": "INVALID", "Tahlia": "Tarlia"},
    ],
)
def test_pronunciation_invalid_config_is_a_noop(substitutions):
    """Malformed user config must not break or partially alter spoken text."""
    assert apply_pronunciation_substitutions("Hello Tahlia", substitutions) == "Hello Tahlia"
    assert prepare_spoken_text(
        "## Hello **Tahlia**",
        pronunciation_substitutions=substitutions,
    ) == "Hello Tahlia."


def test_pronunciation_is_single_pass_and_order_independent():
    """Replacement output is not fed back through another configured key."""
    forward = {"alpha": "beta", "beta": "gamma"}
    reverse = {"beta": "gamma", "alpha": "beta"}
    expected = "beta gamma"
    assert apply_pronunciation_substitutions("alpha beta", forward) == expected
    assert apply_pronunciation_substitutions("alpha beta", reverse) == expected


def test_pronunciation_preserves_replacement_casing():
    """The replacement's exact casing is used regardless of the match casing."""
    assert apply_pronunciation_substitutions("TAHLIA", {"Tahlia": "Tarlia"}) == "Tarlia"


def test_prepare_spoken_text_with_pronunciation():
    """prepare_spoken_text applies substitutions before markdown stripping."""
    raw = "## Hello **Tahlia**"
    spoken = prepare_spoken_text(raw, pronunciation_substitutions={"Tahlia": "Tarlia"})
    assert "Tarlia" in spoken
    assert "Tahlia" not in spoken
    assert "**" not in spoken
    assert "##" not in spoken


def test_pronunciation_cannot_mutate_protected_nonspoken_markers():
    """User substitutions run only after private/verifier blocks are removed."""
    raw = "<think>secret reasoning</think>Hello"
    spoken = prepare_spoken_text(
        raw,
        pronunciation_substitutions={"think": "thought"},
    )
    assert spoken == "Hello"
    assert "secret" not in spoken


def test_pronunciation_cannot_replace_an_entire_protected_block():
    """A source equal to protected content cannot rewrite it into spoken text."""
    protected = "```secret code```"
    spoken = prepare_spoken_text(
        f"{protected} Hello",
        pronunciation_substitutions={protected: "LEAK"},
    )
    assert spoken == "Hello"
    assert "LEAK" not in spoken


def test_unterminated_fenced_code_tail_is_never_spoken():
    raw = "Visible answer. ```python\nSECRET = 'do not speak'"
    spoken = prepare_spoken_text(
        raw,
        pronunciation_substitutions={"SECRET": "LEAK"},
    )
    assert spoken == "Visible answer."
    assert "SECRET" not in spoken
    assert "LEAK" not in spoken


@pytest.mark.parametrize(
    "literal",
    [
        "<think>literal documentation</think>",
        "<think>literal unclosed documentation",
    ],
)
def test_think_literal_inside_fenced_code_does_not_consume_visible_tail(literal):
    raw = f"```text\n{literal}\n```Visible answer survives."
    assert prepare_spoken_text(raw) == "Visible answer survives."


def test_truncation_inside_fenced_code_does_not_expose_its_contents():
    raw = "Visible answer. ```text\n" + ("SECRET " * 800) + "``` Later answer."
    truncated = raw[:4000]
    assert truncated.count("```") == 1

    spoken = prepare_spoken_text(
        truncated,
        pronunciation_substitutions={"SECRET": "LEAK"},
    )
    assert spoken == "Visible answer."
    assert "SECRET" not in spoken
    assert "LEAK" not in spoken


def test_prepare_spoken_text_without_pronunciation():
    """prepare_spoken_text with no substitutions works as before."""
    raw = "## Hello **Tahlia**"
    spoken = prepare_spoken_text(raw)
    assert "Tahlia" in spoken
    assert "**" not in spoken
