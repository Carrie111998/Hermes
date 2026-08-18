"""The test harness itself cannot read or render real credentials.

These are regressions on the test-isolation defect found while building this
packet: the tripwire seeder read the operator's real credential files during a
test run, and a failing assertion then printed those values into the
transcript. Two separate faults -- ingestion, and raw enumeration -- so two
separate sets of tests.

Measured before the fix: a plain test run had the seeder opening 18 real files
under the operator's home, including ~/.ssh private keys, ~/.docker/config.json
and ~/.config/gh/hosts.yml.

Nothing here may ever print a seeded value. Assertions report counts, booleans,
and PATHS only.
"""

import logging

import pytest

from agent import credential_tripwire as ct
from tests.security.conftest import (
    REAL_HOME,
    assert_seeder_is_hermetic,
    real_home_paths,
)

# Synthetic sentinels. Shaped like real credentials so they clear the
# tripwire's entropy guards -- if they did not, these tests would pass for the
# wrong reason.
SENTINEL_ENV = "SENTINELoutside_Kp9Xq2Wm7Zt4Rv8Bn3Ld6Hs1Gc5Jf0Yu"
SENTINEL_RC = "SENTINELrcfile_Qw3Er7Ty1Ui5Op9As2Df6Gh4Jk8Lz0Xc"
SENTINEL_SSH = "SENTINELsshkey_Mn6Bv2Cx8Zl4Kj7Hg1Fd5Sa9Pw3Qe0Ru"


@pytest.fixture
def outside_root(tmp_path):
    """A credential-bearing tree deliberately OUTSIDE the synthetic HOME.

    Uses a pytest tmp sibling, never the operator's real home -- a test that
    proves isolation by writing to the real home would be self-defeating.
    """
    outside = tmp_path / "outside_the_synthetic_root"
    (outside / ".ssh").mkdir(parents=True)
    (outside / ".env").write_text(f"OUTSIDE_API_KEY={SENTINEL_ENV}\n")
    (outside / ".zshrc").write_text(f'export OUTSIDE_TOKEN="{SENTINEL_RC}"\n')
    (outside / ".ssh" / "id_rsa").write_text(f"PRIVATE_KEY={SENTINEL_SSH}\n")
    ct.reset_cache()
    return outside


# =============================================================================
# 1. Ingestion — the seeder must not reach outside the synthetic root
# =============================================================================

def test_seeder_touches_no_real_home_file():
    """The core hermeticity assertion."""
    assert_seeder_is_hermetic()


def test_seed_paths_are_all_inside_the_synthetic_home(_hermetic_credential_environment):
    home = str(_hermetic_credential_environment)
    outside = [
        str(p) for p in ct._seed_paths()
        if not str(p).startswith(home)
    ]
    assert not outside, f"seed paths escaped the synthetic home: {outside}"


def test_nothing_is_seeded_in_a_clean_hermetic_environment():
    """A synthetic home with no credential files must yield an empty set."""
    assert ct.known_secret_count() == 0


@pytest.mark.parametrize(
    "sentinel", [SENTINEL_ENV, SENTINEL_RC, SENTINEL_SSH],
    ids=["dotenv", "shell-rc", "ssh-key"],
)
def test_sentinels_outside_the_root_are_not_ingested(outside_root, sentinel):
    """Item 3: plant credentials outside the synthetic root, prove no ingest."""
    assert ct.is_value_seeded(sentinel) is False
    assert ct.known_secret_count() == 0


def test_sentinels_outside_the_root_are_not_scrubbed(outside_root):
    """Corollary: not seeded means not matched, so text passes through."""
    probe = f"value was {SENTINEL_ENV} here"
    scrubbed, hits = ct.scrub_known_secrets(probe)
    assert hits == 0
    assert scrubbed == probe


def test_sentinels_are_never_rendered_in_logs(outside_root, caplog):
    """Not ingested is necessary but not sufficient -- also never emitted."""
    with caplog.at_level(logging.DEBUG):
        ct.reset_cache()
        ct.known_secret_count()
        ct.scrub_known_secrets(f"{SENTINEL_ENV} {SENTINEL_RC} {SENTINEL_SSH}")
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for sentinel in (SENTINEL_ENV, SENTINEL_RC, SENTINEL_SSH):
        assert sentinel not in blob


# =============================================================================
# 2. Enumeration — no API may return or render the seed set
# =============================================================================

def test_the_enumerating_accessor_stays_removed():
    """known_secret_values() returned the whole set. A failing assertion
    against it printed real credentials into the transcript. It must not
    come back."""
    assert not hasattr(ct, "known_secret_values")


def test_supported_accessors_are_count_and_membership_only():
    assert isinstance(ct.known_secret_count(), int)
    assert isinstance(ct.is_value_seeded("anything"), bool)


def test_no_public_callable_returns_a_collection_of_secrets():
    """Guards against a set-returning accessor being reintroduced under any
    name, not just the original one."""
    offenders = []
    for name in dir(ct):
        if name.startswith("_") or not callable(getattr(ct, name)):
            continue
        try:
            result = getattr(ct, name)()
        except Exception:
            continue  # needs arguments; not a zero-arg enumerator
        if isinstance(result, (set, frozenset, list, tuple, dict)):
            offenders.append(name)
    assert not offenders, (
        f"public zero-arg callable(s) return a collection and could dump the "
        f"seed set: {offenders}"
    )


def test_module_repr_does_not_expose_the_cache(outside_root):
    ct.known_secret_count()
    for sentinel in (SENTINEL_ENV, SENTINEL_RC, SENTINEL_SSH):
        assert sentinel not in repr(ct)
        assert sentinel not in str(ct)


# =============================================================================
# 3. The guard helper itself must have teeth
# =============================================================================

def test_real_home_paths_detects_an_escape():
    """If this helper cannot spot a real-home path, every test above is
    vacuous."""
    assert real_home_paths([f"{REAL_HOME}/.zshrc"]) == [f"{REAL_HOME}/.zshrc"]
    assert real_home_paths(["/nowhere/else/.zshrc"]) == []
