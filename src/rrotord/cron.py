"""Cron expressions, as a program's manifest names its schedules (docs/architecture.md, 5.6): five fields, minute
hour day-of-month month day-of-week, each `*`, a number, a range `a-b`, a step `*/n`, `a-b/n` or `a/n` (from `a` to
the field's end), or a list of those, comma-separated; months `JAN`-`DEC` and days `SUN`-`SAT` by name too, and day 7
is Sunday as 0 is. `@hourly`, `@daily` (`@midnight`), `@weekly`, `@monthly`, `@yearly` (`@annually`) stand for their
expressions.

Day-of-month and day-of-week as cron has always read them: when both are restricted (neither starts with `*`), a day
matching either fires; when one is `*`, the other alone decides.

Times are wall-clock times in the schedule's zone (UTC unless it names an IANA zone). Across a change of the clocks:
a time the clocks skip (02:30 when 02:00 jumps to 03:00) fires once, as far past the jump as it was past 02:00
(03:30); a time the clocks pass twice (01:30 when 02:00 falls back to 01:00) fires once, the first time."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MONTHS = {
    m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)
}
DAYS = {d: i for i, d in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}
MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
Spec = tuple[str, int, int, dict[str, int]]  # a field's name, its range, its names
NONE: dict[str, int] = {}
FIELDS: tuple[Spec, ...] = (
    ("minute", 0, 59, NONE),
    ("hour", 0, 23, NONE),
    ("day of month", 1, 31, NONE),
    ("month", 1, 12, MONTHS),
)
DOW: Spec = ("day of week", 0, 7, DAYS)
MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)  # the most each month can have
SEARCH_DAYS = 366 * 9  # a 29 February is never more than 8 years away
PART = re.compile(r"^(?:(\*)|([0-9A-Z]+)(?:-([0-9A-Z]+))?)(?:/([0-9]+))?$")


class CronError(ValueError):
    pass


def _value(text: str, name: str, low: int, high: int, names: dict[str, int]) -> int:
    found = names.get(text) if not text.isdigit() else int(text)
    if found is None or not low <= found <= high:
        raise CronError(f"{text!r} is not a {name} ({low}-{high})")
    return found


def _field(text: str, name: str, low: int, high: int, names: dict[str, int]) -> frozenset[int]:
    values: set[int] = set()
    for part in text.upper().split(","):
        m = PART.match(part)
        if m is None:
            raise CronError(f"{part!r} is not a {name}: a number, a-b, */n, a-b/n or a list of those")
        star, first, last, step = m.groups()
        every = int(step) if step is not None else 1
        if every < 1:
            raise CronError(f"a {name}'s step is 1 or more")
        if star:
            start, end = low, high
        else:
            start = _value(first, name, low, high, names)
            end = _value(last, name, low, high, names) if last else (high if step is not None else start)
            if end < start:
                raise CronError(f"{part!r}: a {name} range runs low to high")
        values.update(range(start, end + 1, every))
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class Cron:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 is Sunday
    any_day: bool  # day of month is `*`
    any_weekday: bool  # day of week is `*`

    @classmethod
    def parse(cls, text: str) -> Cron:
        given = text.strip()
        expanded = MACROS.get(given.lower(), given)
        parts = expanded.split()
        if len(parts) != 5:
            raise CronError(f"{text!r} is not a cron expression: five fields, minute hour day month weekday")
        minutes, hours, days, months = (_field(p, *f) for p, f in zip(parts[:4], FIELDS, strict=True))
        weekdays = frozenset(d % 7 for d in _field(parts[4], *DOW))
        made = cls(given, minutes, hours, days, months, weekdays, parts[2].startswith("*"), parts[4].startswith("*"))
        if made.any_weekday and not any(d <= MONTH_DAYS[m - 1] for m in months for d in days):
            raise CronError(f"{text!r} never fires: no month in it has that day")
        return made

    def day_matches(self, day: dt.date) -> bool:
        if day.month not in self.months:
            return False
        in_month, in_week = day.day in self.days, (day.weekday() + 1) % 7 in self.weekdays
        if self.any_day or self.any_weekday:
            return in_month and in_week
        return in_month or in_week

    def next_after(self, after: dt.datetime, zone: dt.tzinfo = dt.UTC) -> dt.datetime:
        """The first time it fires strictly after `after` (aware), in UTC."""
        local = after.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
        day, start = local.date(), local.time()
        hours, minutes = sorted(self.hours), sorted(self.minutes)
        for _ in range(SEARCH_DAYS):
            if self.day_matches(day):
                for hour in hours:
                    for minute in minutes:
                        if day == local.date() and (hour, minute) < (start.hour, start.minute):
                            continue
                        when = dt.datetime.combine(day, dt.time(hour, minute), zone).astimezone(dt.UTC)
                        if when > after:
                            return when
            day += dt.timedelta(days=1)
        raise CronError(f"{self.text!r} does not fire within {SEARCH_DAYS // 366} years")  # pragma: no cover


def zone(name: str) -> dt.tzinfo:
    """An IANA time zone by name (UTC for "UTC")."""
    if name == "UTC":
        return dt.UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise CronError(f"{name!r} is not an IANA time zone (Europe/Amsterdam, America/New_York, UTC)") from None
