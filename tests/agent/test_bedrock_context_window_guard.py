"""Guards on the Bedrock static context-window fallback table.

``get_bedrock_context_length()`` prefers a live probe and falls back to
``BEDROCK_CONTEXT_LENGTHS``. Its own docstring names the failure mode the
fallback is prone to:

    a stale entry silently caps the window (e.g. a 1M-token Opus pinned to
    200K via an ``opus-4`` substring match)

That is a *silent* failure: nothing raises, the CLI just advertises the wrong
window and context compression fires several times earlier than it needs to.
The offline/display paths (``probe=False``, or no region) have only the table,
so drift there is not masked by the probe.

Three properties are asserted offline, all of which the table currently
satisfies and none of which was covered:

* **Every row is reachable.** Matching is ``key in model_id.lower()``, so a key
  carrying an uppercase character can never match anything and the model
  silently drops to a shorter row.
* **Removing a row can only lower the result.** That is the situation for every
  model AWS ships before this table is updated. Falling low costs premature
  compression; falling *high* means the agent builds a prompt the model rejects.
  So no generic row may be more generous than the specific rows it covers.
* **The Claude rows agree with ``model_metadata.DEFAULT_CONTEXT_LENGTHS``.** The
  table's own comment requires this ("These 1M entries must match
  agent/model_metadata.py DEFAULT_CONTEXT_LENGTHS or the agent compresses
  context prematurely") but nothing enforced it.

A third check compares the table against what Bedrock actually enforces. It
needs live AWS, so it is opt-in twice over — ``@pytest.mark.integration``
(excluded by the default ``-m 'not integration'``) plus ``HERMES_E2E_BEDROCK=1``
— and it reuses the production probe rather than reimplementing it, so it
exercises ``probe_bedrock_context_length()`` at the same time.

The expected windows below were measured against live Bedrock in ``us-east-2``
on 2026-08-30 by provoking the length-validation error, which reports the real
maximum before any inference runs. All five current-generation Claude models
returned ``1000000``, with and without the ``context-1m-2025-08-07`` beta —
that beta was retired on 2026-04-30 when 1M went GA, so it is a no-op now.
Ref: issue #31277.
"""

import os

import pytest

from agent import bedrock_adapter
from agent.bedrock_adapter import (
    BEDROCK_CONTEXT_LENGTHS,
    BEDROCK_DEFAULT_CONTEXT_LENGTH,
    _static_bedrock_context_length,
)

# Snapshot of the AWS environment as it exists at *collection* time, which is
# before the autouse isolation fixture in tests/conftest.py runs. That fixture
# deletes AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY/SESSION_TOKEN and sets
# AWS_EC2_METADATA_DISABLED=true so no unit test can ever reach a real account —
# right by default, but it also hides ambient credentials from an opt-in live
# test, which then fails to sign and skips itself with a misleading "no
# credentials" reason. Collection-time capture is the only place the real values
# are still visible.
_AWS_ENV_AT_COLLECTION = {
    k: v for k, v in os.environ.items() if k.startswith("AWS_")
}

# Escape hatch for the coverage check below: Claude slugs that model_metadata
# knows but Bedrock genuinely does not offer, so BEDROCK_CONTEXT_LENGTHS is right
# to stay silent about them. Empty today — every claude-* slug in
# DEFAULT_CONTEXT_LENGTHS is served by Bedrock. Anthropic sometimes ships to
# their own API before Bedrock, so add a slug here (with a note) rather than
# inventing a Bedrock row for a model that isn't there.
_METADATA_SLUGS_NOT_ON_BEDROCK: frozenset = frozenset()

# Measured against live Bedrock, us-east-2, 2026-08-30. See module docstring.
LIVE_VERIFIED_WINDOWS = {
    "us.anthropic.claude-sonnet-4-6": 1_000_000,
    "us.anthropic.claude-opus-4-6-v1": 1_000_000,
    "us.anthropic.claude-opus-4-7": 1_000_000,
    "us.anthropic.claude-opus-4-8": 1_000_000,
    "us.anthropic.claude-sonnet-5": 1_000_000,
}


class TestLongestSubstringResolution:
    """Properties of the longest-substring lookup itself."""

    @pytest.mark.parametrize("key", sorted(BEDROCK_CONTEXT_LENGTHS))
    def test_every_row_is_reachable(self, key):
        """A row must at minimum match itself.

        Matching is ``key in model_id.lower()``, so a key containing an
        uppercase character is dead on arrival: it can never match any input and
        the model silently drops to a shorter row or the default. Nothing else
        would surface that typo.
        """
        expected = BEDROCK_CONTEXT_LENGTHS[key]
        actual = _static_bedrock_context_length(key)
        assert actual == expected, (
            f"{key!r} does not match itself — it resolves to {actual:,} instead "
            f"of {expected:,}, so no model ID can ever reach this row (an "
            f"uppercase character in the key will do this)"
        )

    @pytest.mark.parametrize("key", sorted(BEDROCK_CONTEXT_LENGTHS))
    def test_removing_a_row_can_only_lower_the_window(self, key, monkeypatch):
        """Staleness must fail safe.

        Longest-substring means a *shorter* generic row can never outrank a
        longer specific one, so the interesting direction is what a model falls
        back to when its own row is missing — which is what happens for every
        model AWS ships before this table is updated.

        Falling *low* costs some premature context compression. Falling *high*
        means the agent packs a prompt the model will reject outright. So every
        generic row must be no more generous than the specific rows it covers;
        e.g. dropping ``claude-opus-4-8`` lands on ``claude-opus-4`` (200K),
        never above 1M.
        """
        own = BEDROCK_CONTEXT_LENGTHS[key]
        without = {k: v for k, v in BEDROCK_CONTEXT_LENGTHS.items() if k != key}
        monkeypatch.setattr(bedrock_adapter, "BEDROCK_CONTEXT_LENGTHS", without)
        fallback = _static_bedrock_context_length(key)

        assert fallback <= own, (
            f"without its own row, {key!r} would resolve to {fallback:,} — "
            f"higher than its real {own:,}. A generic row is over-generous, so "
            f"the next model in this family will be handed a window it does not "
            f"have and its prompts will be rejected"
        )

    def test_versioned_claude_ids_beat_the_generic_opus_4_row(self):
        """The exact hazard named in get_bedrock_context_length's docstring.

        'anthropic.claude-opus-4' is a substring of every opus-4-x ID and is
        200K. If a versioned row is ever dropped, that ID silently becomes 200K.
        """
        assert _static_bedrock_context_length("anthropic.claude-opus-4") == 200_000
        for model_id, expected in LIVE_VERIFIED_WINDOWS.items():
            assert _static_bedrock_context_length(model_id) == expected, model_id

    def test_opus_4_1_is_not_over_matched_to_1m(self):
        """Opus 4.1 is a 200K model and must fall to the generic row, not to a
        1M row — the mirror image of the shadowing hazard."""
        assert _static_bedrock_context_length("us.anthropic.claude-opus-4-1-v1:0") == 200_000

    @pytest.mark.parametrize("prefix", ["", "us.", "global.", "eu.", "apac."])
    def test_inference_profile_prefixes_resolve_identically(self, prefix):
        """A cross-region profile ID is the same model; the prefix must not
        change the window."""
        assert _static_bedrock_context_length(
            f"{prefix}anthropic.claude-opus-4-8"
        ) == 1_000_000

    @pytest.mark.parametrize("suffix", ["", "-v1", "-v1:0", "-20250514-v1:0"])
    def test_version_suffixes_resolve_identically(self, suffix):
        assert _static_bedrock_context_length(
            f"us.anthropic.claude-sonnet-4-6{suffix}"
        ) == 1_000_000

    def test_unknown_model_falls_back_to_the_conservative_default(self):
        assert (
            _static_bedrock_context_length("acme.does-not-exist-v9")
            == BEDROCK_DEFAULT_CONTEXT_LENGTH
        )


class TestTableAgreesWithModelMetadata:
    """The table comment requires parity with DEFAULT_CONTEXT_LENGTHS."""

    def test_claude_rows_match_default_context_lengths(self):
        from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS

        compared = 0
        divergences = []
        for key, bedrock_window in BEDROCK_CONTEXT_LENGTHS.items():
            if not key.startswith("anthropic."):
                continue
            slug = key.split("anthropic.", 1)[1]
            if slug not in DEFAULT_CONTEXT_LENGTHS:
                continue  # Bedrock-only row; nothing to compare against.
            compared += 1
            if DEFAULT_CONTEXT_LENGTHS[slug] != bedrock_window:
                divergences.append(
                    f"{key}: bedrock table {bedrock_window:,} != "
                    f"model_metadata {DEFAULT_CONTEXT_LENGTHS[slug]:,}"
                )

        assert not divergences, (
            "Bedrock table disagrees with model_metadata; the agent will "
            "compress at the wrong threshold on one of the two paths:\n  "
            + "\n  ".join(divergences)
        )
        assert compared, "no rows compared — the key-slug mapping has drifted"

    def test_every_claude_model_in_metadata_is_covered(self):
        """The other direction: a *missing* row, which is the one that bites.

        The test above only compares rows the Bedrock table already has, so it
        is blind to the failure that actually happens — a model the rest of the
        codebase knows about having no Bedrock row at all, and silently
        resolving to the 128K default. That halves the compression threshold to
        64K, and the symptom users report is constant "🗜️ Compacting context"
        rather than anything naming a context window.

        This is exactly how ``claude-opus-5`` was missed (#92273): present in
        DEFAULT_CONTEXT_LENGTHS at 1M, absent from BEDROCK_CONTEXT_LENGTHS.
        """
        from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS

        checked = 0
        gaps = []
        for slug, want in sorted(DEFAULT_CONTEXT_LENGTHS.items()):
            if not slug.startswith("claude-"):
                continue
            if "." in slug:
                continue  # dotted aliases (claude-opus-4.6) are not Bedrock IDs
            if slug in _METADATA_SLUGS_NOT_ON_BEDROCK:
                continue
            checked += 1
            got = _static_bedrock_context_length(f"anthropic.{slug}")
            if got != want:
                how = (
                    "no row at all — falls to the default"
                    if got == BEDROCK_DEFAULT_CONTEXT_LENGTH
                    else "resolves via a different row"
                )
                gaps.append(f"{slug}: metadata {want:,} vs Bedrock {got:,} ({how})")

        assert not gaps, (
            "Claude models known to model_metadata do not resolve to the same "
            "window on Bedrock:\n  "
            + "\n  ".join(gaps)
            + "\n\nAdd the missing row to BEDROCK_CONTEXT_LENGTHS. If the model "
            "genuinely is not offered on Bedrock, add its slug to "
            "_METADATA_SLUGS_NOT_ON_BEDROCK in this file with a note saying so."
        )
        assert checked, "no slugs checked — the claude- prefix filter has drifted"


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("HERMES_E2E_BEDROCK", "").strip() != "1",
    reason="live Bedrock: set HERMES_E2E_BEDROCK=1 to opt in",
)
class TestTableMatchesLiveBedrock:
    """Does the fallback table still match what Bedrock enforces?

    Run manually after a model launch::

        HERMES_E2E_BEDROCK=1 AWS_REGION=us-east-2 \\
            pytest tests/agent/test_bedrock_context_window_guard.py -m integration -v

    Each case provokes Bedrock's length-validation error, which reports the real
    maximum before inference runs, so this costs nothing in tokens. It does
    upload a multi-megabyte prompt per model, hence the opt-in.
    """

    @pytest.fixture
    def ambient_aws_credentials(self, monkeypatch):
        """Undo the conftest AWS scrub for this test only.

        Also drops the cached bedrock-runtime client on both sides: entering,
        because a client built earlier under the scrubbed env has no usable
        credentials; leaving, so a live-credentialled client never leaks into a
        unit test.
        """
        bedrock_adapter.reset_client_cache()
        for key, value in _AWS_ENV_AT_COLLECTION.items():
            monkeypatch.setenv(key, value)
        if "AWS_EC2_METADATA_DISABLED" not in _AWS_ENV_AT_COLLECTION:
            # Instance-profile credentials come from IMDS; conftest turns it off.
            for key in (
                "AWS_EC2_METADATA_DISABLED",
                "AWS_METADATA_SERVICE_TIMEOUT",
                "AWS_METADATA_SERVICE_NUM_ATTEMPTS",
            ):
                monkeypatch.delenv(key, raising=False)
        try:
            yield
        finally:
            bedrock_adapter.reset_client_cache()

    @pytest.mark.parametrize(
        "model_id,expected", sorted(LIVE_VERIFIED_WINDOWS.items())
    )
    def test_probe_agrees_with_static_table(
        self, model_id, expected, ambient_aws_credentials
    ):
        from agent.bedrock_adapter import probe_bedrock_context_length

        region = os.environ.get("AWS_REGION") or "us-east-2"
        probed = probe_bedrock_context_length(model_id, region)
        if probed is None:
            pytest.skip(
                f"probe could not run for {model_id} in {region} "
                "(no credentials, model not enabled, or unparseable error)"
            )

        static = _static_bedrock_context_length(model_id)
        assert probed == expected, (
            f"{model_id}: Bedrock now reports {probed:,}, but this test expects "
            f"{expected:,} — AWS changed the window; update LIVE_VERIFIED_WINDOWS "
            f"and BEDROCK_CONTEXT_LENGTHS together"
        )
        assert static == probed, (
            f"{model_id}: static fallback table says {static:,} but Bedrock "
            f"enforces {probed:,} — offline/display paths will advertise the "
            f"wrong window and compress at the wrong threshold"
        )
