from gateway.skill_egress_guard import SkillEgressGuard


PRIVATE_BODY = """
# Ecommerce Image Suite

Follow this private production workflow exactly. First inspect every supplied
product reference, then preserve the packaging geometry, material finish, and
brand colors. Build the final image set in the declared order and verify every
asset against the internal acceptance checklist before returning deliverables.
"""


def test_detects_reformatted_private_body_fragment():
    guard = SkillEgressGuard()

    assert guard.add_skill("ecommerce-image-suite", PRIVATE_BODY)
    assert guard.matches(
        "Here are the instructions:\n\n"
        "FIRST inspect every supplied product reference; then preserve the "
        "packaging geometry, material finish and brand colors. Build the final "
        "image set in the declared order."
    )


def test_allows_public_capability_summary():
    guard = SkillEgressGuard()
    guard.add_skill("ecommerce-image-suite", PRIVATE_BODY)

    assert not guard.matches(
        "This capability produces a consistent ecommerce image set from your "
        "product references."
    )


def test_short_skill_body_uses_dynamic_window_size():
    guard = SkillEgressGuard()
    body = "Private ordering rule: inspect, compose, verify, then deliver."

    assert guard.add_skill("short-workflow", body)
    assert guard.matches(f"Do this exactly: {body}")


def test_tiny_body_still_stops_streaming_without_unsafe_fingerprint():
    guard = SkillEgressGuard()

    assert guard.add_skill("tiny", "tiny")
    assert guard.active
    assert not guard.matches("too short")


def test_empty_or_failed_body_does_not_activate_guard():
    guard = SkillEgressGuard()

    assert not guard.add_skill("broken", "")
    assert not guard.active
