"""The layer-2 known-value tripwire (Phase 9 / Packet C, C5).

Defense in depth, NOT a boundary. The tests below are split deliberately into
what it catches and what it provably does not, so the packet cannot be
overclaimed on the strength of a green suite.
"""

import pytest

from agent import credential_tripwire as ct
from tests.security.test_credential_read_boundary import (
    OPQ_KEY,
    OPQ_VALUE,
    YELP_KEY,
    YELP_VALUE,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    ct.reset_cache()
    yield
    ct.reset_cache()


# --- the narrow trigger: C2 non-regression ----------------------------------
#
# Commit cd215c1ee4 destroyed browser_press {"key": "Enter"} by triggering on a
# key NAME. These rows prove such values cannot even enter the seed set.

C2_REGRESSION_VALUES = [
    "Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "F5", "PageDown",
    "Backspace", "Delete", "Home", "End", "Shift", "Control", "Meta",
]

NON_SECRET_VALUES = [
    "true", "false", "8080", "utf-8", "info", "debug", "GET", "POST",
    "application/json", "/usr/local/bin/python3", "./relative/path",
    "~/projects/thing", "localhost:5432", "db.internal:6379",
    "<your-token>", "{{TOKEN}}", "************", "xxxxxxxxxxxxxxxx",
    "changeme", "placeholder", "your-api-key", "aaaaaaaaaaaaaaaa",
    "----------------", "0000000000000000", "abcdefghijklmnop",
    "PRODUCTION", "development",
]


@pytest.mark.parametrize("value", C2_REGRESSION_VALUES)
def test_c2_regression_values_are_never_scrubbable(value):
    """Direct guard against the cd215c1ee4 failure class."""
    assert ct.is_scrubbable_secret_value(value) is False


@pytest.mark.parametrize("value", NON_SECRET_VALUES)
def test_ordinary_values_are_never_scrubbable(value):
    assert ct.is_scrubbable_secret_value(value) is False


@pytest.mark.parametrize("value", [YELP_VALUE, OPQ_VALUE])
def test_real_canaries_are_scrubbable(value):
    assert ct.is_scrubbable_secret_value(value) is True


# --- seeding -----------------------------------------------------------------

def test_seeds_from_hermes_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(f"{YELP_KEY}={YELP_VALUE}\n{OPQ_KEY}={OPQ_VALUE}\n")
    ct.reset_cache()
    assert ct.is_value_seeded(YELP_VALUE)
    assert ct.is_value_seeded(OPQ_VALUE)


def test_seeds_from_environment_with_no_file_present(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(OPQ_KEY, OPQ_VALUE)
    ct.reset_cache()
    assert ct.is_value_seeded(OPQ_VALUE)


def test_seeds_every_value_in_a_credential_file_whatever_the_key_is_called(
    tmp_path, monkeypatch
):
    """A .env is a credential store: the key name carries no information.

    Found by the PARTNER_HANDSHAKE_VALUE canary. The first implementation
    filtered on the key name everywhere, so a real credential under an
    unremarkable key was never seeded and never scrubbed.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(f"PARTNER_HANDSHAKE_VALUE={OPQ_VALUE}\n")
    ct.reset_cache()
    assert ct.is_value_seeded(OPQ_VALUE)


def test_key_name_filter_still_applies_to_general_purpose_files(tmp_path, monkeypatch):
    """A shell rc is NOT a credential store: most of it is PATH and EDITOR,
    so there the key name is the only available signal."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ct.reset_cache()
    assert ct._parse_env_values("EDITOR=/usr/bin/vim\nFOO_API_KEY=abc\n") == ["abc"]
    assert ct._parse_env_values(
        "EDITOR=/usr/bin/vim\n", all_values=True
    ) == ["/usr/bin/vim"]


def test_cache_invalidates_when_the_file_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text(f"{OPQ_KEY}={OPQ_VALUE}\n")
    ct.reset_cache()
    assert ct.is_value_seeded(OPQ_VALUE)

    rotated = "Nw3Xy8Qr5Vb2Mj7Kt4Pd1Zl6Hf0Cg9S"
    env.write_text(f"{OPQ_KEY}={rotated}\n")
    assert ct.is_value_seeded(rotated)


# --- scrubbing ---------------------------------------------------------------

@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(f"{YELP_KEY}={YELP_VALUE}\n{OPQ_KEY}={OPQ_VALUE}\n")
    ct.reset_cache()
    return tmp_path


def test_scrubs_assignment_form(seeded):
    out, hits = ct.scrub_known_secrets(f"{YELP_KEY}={YELP_VALUE}")
    assert YELP_VALUE not in out
    assert hits == 1
    assert ct.REDACTION_MARKER in out


def test_scrubs_bare_value_form(seeded):
    """The load-bearing case: no key name present, so no content pattern in
    agent/redact.py could ever catch it. Only the known-value set can."""
    out, hits = ct.scrub_known_secrets(f"the value is {OPQ_VALUE} ok")
    assert OPQ_VALUE not in out
    assert hits == 1


def test_leaves_ordinary_text_byte_identical(seeded):
    original = 'browser_press {"key": "Enter"} then {"key": "Tab"}'
    out, hits = ct.scrub_known_secrets(original)
    assert out == original
    assert hits == 0


def test_no_seed_values_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for k in list(__import__("os").environ):
        if ct._SECRET_NAME_RE.search(k):
            monkeypatch.delenv(k, raising=False)
    ct.reset_cache()
    text = "nothing secret here at all"
    assert ct.scrub_known_secrets(text) == (text, 0)


# --- residuals: pinned, not hidden -------------------------------------------

def test_base64_encoding_is_NOT_caught(seeded):
    import base64
    encoded = base64.b64encode(OPQ_VALUE.encode()).decode()
    out, hits = ct.scrub_known_secrets(f"payload: {encoded}")
    assert hits == 0, "documented residual: encodings defeat a verbatim matcher"


def test_reversed_value_is_NOT_caught(seeded):
    out, hits = ct.scrub_known_secrets(f"payload: {OPQ_VALUE[::-1]}")
    assert hits == 0, "documented residual"


def test_partial_read_is_NOT_caught(seeded):
    out, hits = ct.scrub_known_secrets(f"first half: {OPQ_VALUE[:16]}")
    assert hits == 0, "documented residual: chunked reads reconstruct the value"


def test_unseeded_secret_is_NOT_caught(seeded):
    out, hits = ct.scrub_known_secrets("hardcoded-in-source-Ab3Kd9Zx7Qw2")
    assert hits == 0, "documented residual: no seed value, no coverage"
