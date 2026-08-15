"""Tests for profile-wide real-child delegation admission."""

import pytest

from tools import delegation_admission as admission


@pytest.fixture(autouse=True)
def _clean_admission():
    admission._reset_for_tests()
    yield
    admission._reset_for_tests()


def test_batch_consumes_one_per_real_child():
    lease = admission.try_acquire(real_children=3, ceiling=15)

    assert lease.accepted is True
    assert admission.active_real_children() == 3


def test_ceiling_rejects_overflow_and_release_restores_capacity():
    first = admission.try_acquire(real_children=12, ceiling=15)
    second = admission.try_acquire(real_children=3, ceiling=15)

    assert first.accepted is True
    assert second.accepted is True
    assert admission.active_real_children() == 15

    rejected = admission.try_acquire(real_children=1, ceiling=15)
    assert rejected.accepted is False
    assert "15/15" in rejected.error

    assert admission.release(first.lease_id) is True
    retry = admission.try_acquire(real_children=1, ceiling=15)
    assert retry.accepted is True


def test_invalid_request_is_rejected():
    result = admission.try_acquire(real_children=0, ceiling=15)

    assert result.accepted is False
    assert "positive" in result.error


def test_invalid_ceiling_falls_back_to_default():
    lease = admission.try_acquire(real_children=15, ceiling="invalid")

    assert lease.accepted is True
    assert lease.ceiling == admission.DEFAULT_PROFILE_REAL_CHILD_CEILING


def test_release_is_idempotent():
    lease = admission.try_acquire(real_children=1, ceiling=15)

    assert admission.release(lease.lease_id) is True
    assert admission.release(lease.lease_id) is False
    assert admission.active_real_children() == 0


def test_profiles_have_independent_capacity():
    token = admission._profile_context.set("profile-a")
    try:
        first = admission.try_acquire(real_children=15, ceiling=15)
        assert first.accepted is True
        assert admission.active_real_children() == 15
    finally:
        admission._profile_context.reset(token)

    token = admission._profile_context.set("profile-b")
    try:
        second = admission.try_acquire(real_children=15, ceiling=15)
        assert second.accepted is True
        assert admission.active_real_children() == 15
    finally:
        admission._profile_context.reset(token)

    assert admission.release(first.lease_id) is True
    assert admission.release(second.lease_id) is True
