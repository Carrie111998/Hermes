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


def test_prepare_spoken_text_without_pronunciation():
    """prepare_spoken_text with no substitutions works as before."""
    raw = "## Hello **Tahlia**"
    spoken = prepare_spoken_text(raw)
    assert "Tahlia" in spoken
    assert "**" not in spoken
