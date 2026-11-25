"""Deadline arithmetic for service standards: business or calendar days in the authority's own timezone.

Conventions (the same ones the dashboard explains):

    * Day 0 is the local calendar day the report was submitted, whatever time it was submitted.
    * "N business days" ends at 23:59:59 local time on the N-th business day AFTER day 0. A report filed on a
      Saturday therefore starts counting on Monday, and one filed on Friday evening counts Monday as day 1.
    * "N calendar days" ends at 23:59:59 local time N days after day 0.
    * Business days skip the weekend (Saturday and Sunday by default) and the configured holidays. An observed
      holiday (Christmas on a Saturday observed on Monday) must be configured on the observed date.
    * Every input timestamp must be timezone-aware, and every result is UTC, so daylight-saving changes and
      a server in another timezone never shift a deadline.

Pure functions with no database access, so they are easy to test.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

BUSINESS, CALENDAR = "business_days", "calendar_days"
UNITS = (BUSINESS, CALENDAR)
END_OF_DAY = time(23, 59, 59)


def parse_ts(value: str | datetime) -> datetime:
    """An ISO timestamp as an aware datetime. Naive values are refused: guessing a timezone moves deadlines."""
    ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no timezone")
    return ts


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


class Calendar:
    def __init__(self, tz: str = "America/Halifax", holidays: Iterable[date | str] = (),
                 weekend: Iterable[int] = (5, 6)):
        self.tz_name = tz
        self.tz = ZoneInfo(tz)
        self.holidays = frozenset(d if isinstance(d, date) else date.fromisoformat(d) for d in holidays)
        self.weekend = frozenset(weekend)

    def is_business_day(self, d: date) -> bool:
        return d.weekday() not in self.weekend and d not in self.holidays

    def local_date(self, ts: str | datetime) -> date:
        return parse_ts(ts).astimezone(self.tz).date()

    def end_of_day(self, d: date) -> datetime:
        return datetime.combine(d, END_OF_DAY, self.tz).astimezone(timezone.utc)

    def shift(self, d: date, n: int, unit: str) -> date:
        """The local date n business/calendar days after (n > 0) or before (n < 0) d."""
        if unit == CALENDAR:
            return d + timedelta(days=n)
        if unit != BUSINESS:
            raise ValueError(f"time unit must be one of {UNITS}")
        step = 1 if n >= 0 else -1
        left = abs(n)
        while left:
            d += timedelta(days=step)
            if self.is_business_day(d):
                left -= 1
        return d

    def deadline(self, start: str | datetime, n: int, unit: str) -> datetime:
        """End of the n-th business/calendar day after the local day of `start`, in UTC."""
        if n < 0:
            raise ValueError("a deadline cannot be negative")
        return self.end_of_day(self.shift(self.local_date(start), n, unit))

    def warning(self, due: str | datetime, n: int, unit: str) -> datetime:
        """When the last n business/calendar days before `due` begin (end of the day n days before due)."""
        return self.end_of_day(self.shift(self.local_date(due), -n, unit))

    def elapsed(self, start: str | datetime, end: str | datetime, unit: str) -> int:
        """Whole business/calendar days used between two timestamps, counted the way deadlines are: days after the
        local day of `start` up to and including the local day of `end`."""
        a, b = self.local_date(start), self.local_date(end)
        if b <= a:
            return 0
        if unit == CALENDAR:
            return (b - a).days
        return sum(self.is_business_day(a + timedelta(days=i)) for i in range(1, (b - a).days + 1))

    def holidays_between(self, start: date, end: date) -> list[str]:
        return sorted(d.isoformat() for d in self.holidays if start <= d <= end)
