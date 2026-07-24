"""Tests for inclusive calendar window generation."""

from datetime import date

import pytest

from presscorner_builder.windows import month_windows, year_windows


# Verify full calendar months are returned newest first.
def test_month_windows_full_months_descending() -> None:
    assert month_windows(date(2020, 1, 1), date(2020, 3, 31)) == [
        (date(2020, 3, 1), date(2020, 3, 31)),
        (date(2020, 2, 1), date(2020, 2, 29)),
        (date(2020, 1, 1), date(2020, 1, 31)),
    ]


# Verify the outer month windows are clipped to exact caller bounds.
def test_month_windows_clip_bounds() -> None:
    assert month_windows(date(2020, 1, 17), date(2020, 3, 4)) == [
        (date(2020, 3, 1), date(2020, 3, 4)),
        (date(2020, 2, 1), date(2020, 2, 29)),
        (date(2020, 1, 17), date(2020, 1, 31)),
    ]


# Verify a range within one month remains one partial window.
def test_month_windows_single_partial_month() -> None:
    assert month_windows(date(2021, 5, 11), date(2021, 5, 12)) == [
        (date(2021, 5, 11), date(2021, 5, 12))
    ]


# Verify inverted date bounds are rejected instead of silently returning nothing.
def test_month_windows_reject_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="since_date"):
        month_windows(date(2021, 5, 12), date(2021, 5, 11))


# Verify year windows apply the same clipping and newest-first behavior.
def test_year_windows_clip_bounds() -> None:
    assert year_windows(date(2019, 6, 1), date(2021, 2, 3)) == [
        (date(2021, 1, 1), date(2021, 2, 3)),
        (date(2020, 1, 1), date(2020, 12, 31)),
        (date(2019, 6, 1), date(2019, 12, 31)),
    ]
