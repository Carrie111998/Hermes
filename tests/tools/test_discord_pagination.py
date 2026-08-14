"""Tests for tools.discord_api.pagination (feature R2: pagination conformance)."""

import pytest

from tools.discord_api.pagination import (
    Page,
    PaginationError,
    has_more_page,
    next_page_params,
    page_params,
)


# ---------------------------------------------------------------------------
# Page dataclass
# ---------------------------------------------------------------------------
class TestPage:
    def test_defaults(self):
        page = Page()
        assert page.items == []
        assert page.has_more is False

    def test_values(self):
        page = Page(items=[1, 2, 3], has_more=True)
        assert page.items == [1, 2, 3]
        assert page.has_more is True

    def test_positional_construction(self):
        page = Page(["a", "b"], True)
        assert page.items == ["a", "b"]
        assert page.has_more is True


# ---------------------------------------------------------------------------
# page_params
# ---------------------------------------------------------------------------
class TestPageParams:
    def test_defaults(self):
        assert page_params() == {"limit": 50}

    def test_before_only(self):
        assert page_params(before="123") == {"before": "123", "limit": 50}

    def test_after_only(self):
        assert page_params(after="456") == {"after": "456", "limit": 50}

    def test_around_only(self):
        assert page_params(around="789") == {"around": "789", "limit": 50}

    def test_custom_limit_passthrough(self):
        assert page_params(limit=25) == {"limit": 25}
        assert page_params(before="1", limit=10) == {"before": "1", "limit": 10}

    def test_limit_floor_clamped_to_one(self):
        assert page_params(limit=0)["limit"] == 1
        assert page_params(limit=-5)["limit"] == 1

    def test_limit_ceiling_clamped_to_discord_max(self):
        assert page_params(limit=101)["limit"] == 100
        assert page_params(limit=1000)["limit"] == 100

    def test_limit_boundaries_accepted(self):
        assert page_params(limit=1)["limit"] == 1
        assert page_params(limit=100)["limit"] == 100

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"before": "1", "after": "2"},
            {"before": "1", "around": "2"},
            {"after": "1", "around": "2"},
            {"before": "1", "after": "2", "around": "3"},
        ],
        ids=["before+after", "before+around", "after+around", "all-three"],
    )
    def test_mutually_exclusive_raises(self, kwargs):
        with pytest.raises(PaginationError):
            page_params(**kwargs)

    def test_mutually_exclusive_raises_value_error(self):
        # PaginationError must remain a ValueError subclass.
        with pytest.raises(ValueError):
            page_params(before="1", after="2")

    def test_mutually_exclusive_respects_clamped_limit(self):
        with pytest.raises(PaginationError):
            page_params(before="1", after="2", limit=500)


# ---------------------------------------------------------------------------
# has_more_page
# ---------------------------------------------------------------------------
class TestHasMorePage:
    def test_full_page_has_more(self):
        assert has_more_page(50, 50) is True

    def test_partial_page_no_more(self):
        assert has_more_page(10, 50) is False

    def test_empty_page_no_more(self):
        assert has_more_page(0, 50) is False

    def test_full_page_discord_max_limit(self):
        assert has_more_page(100, 100) is True

    def test_partial_single_item(self):
        assert has_more_page(1, 100) is False

    def test_exactly_full_at_minimum_limit(self):
        assert has_more_page(1, 1) is True

    def test_not_full_by_one(self):
        assert has_more_page(49, 50) is False


# ---------------------------------------------------------------------------
# next_page_params
# ---------------------------------------------------------------------------
class TestNextPageParams:
    def test_before_flips_to_after(self):
        current = page_params(before="999", limit=25)
        assert next_page_params(current, "123") == {"after": "123", "limit": 25}

    def test_around_flips_to_after(self):
        current = page_params(around="999", limit=50)
        assert next_page_params(current, "123") == {"after": "123", "limit": 50}

    def test_plain_forward_uses_after(self):
        current = page_params(limit=50)
        assert next_page_params(current, "123") == {"after": "123", "limit": 50}

    def test_after_raises(self):
        current = page_params(after="999", limit=50)
        with pytest.raises(PaginationError):
            next_page_params(current, "123")

    def test_after_raises_value_error(self):
        current = {"after": "999", "limit": 50}
        with pytest.raises(ValueError):
            next_page_params(current, "123")

    def test_keeps_current_limit(self):
        current = page_params(before="999", limit=100)
        assert next_page_params(current, "7") == {"after": "7", "limit": 100}

    def test_missing_limit_defaults_to_50(self):
        assert next_page_params({}, "123") == {"after": "123", "limit": 50}

    def test_accepts_int_last_id(self):
        current = page_params(before="999", limit=10)
        assert next_page_params(current, 42) == {"after": 42, "limit": 10}
