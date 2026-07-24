"""Calendar-window helpers for bounded Press Corner API requests."""

from __future__ import annotations

from datetime import date, timedelta


# Return the first day of the month containing a date.
def _month_start(value: date) -> date:
    return value.replace(day=1)


# Return the final day of the month containing a date.
def _month_end(value: date) -> date:
    next_month = (value.replace(day=28) + timedelta(days=4)).replace(day=1)
    return next_month - timedelta(days=1)


# Generate clipped calendar-month windows in newest-first order.
def month_windows(since_date: date, until_date: date) -> list[tuple[date, date]]:
    """Return inclusive month windows clipped to the supplied inclusive bounds."""
    if since_date > until_date:
        raise ValueError("since_date must be on or before until_date")

    windows: list[tuple[date, date]] = []
    cursor = _month_start(until_date)
    first_month = _month_start(since_date)

    while cursor >= first_month:
        window_start = max(cursor, since_date)
        window_end = min(_month_end(cursor), until_date)
        windows.append((window_start, window_end))
        cursor = (cursor - timedelta(days=1)).replace(day=1)

    return windows


# Generate clipped calendar-year windows in newest-first order.
def year_windows(since_date: date, until_date: date) -> list[tuple[date, date]]:
    """Return inclusive year windows clipped to the supplied inclusive bounds."""
    if since_date > until_date:
        raise ValueError("since_date must be on or before until_date")

    windows: list[tuple[date, date]] = []
    for year in range(until_date.year, since_date.year - 1, -1):
        window_start = max(date(year, 1, 1), since_date)
        window_end = min(date(year, 12, 31), until_date)
        windows.append((window_start, window_end))
    return windows
