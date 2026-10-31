"""Cron expressions: fields, ranges, steps, lists and names; day-of-month and day-of-week as cron reads them; the
next fire in UTC and in a zone across both changes of the clocks; refusals that say why."""

from __future__ import annotations

import datetime as dt

import pytest

from rrotord.cron import Cron, CronError, zone

UTC = dt.UTC


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def fires(text: str, start: dt.datetime, n: int = 3, tz: str = "UTC") -> list[dt.datetime]:
    cron, found, when = Cron.parse(text), [], start
    for _ in range(n):
        when = cron.next_after(when, zone(tz))
        found.append(when)
    return found


def test_every_minute_and_strictly_after():
    assert fires("* * * * *", at(2026, 10, 31, 9, 0, 30)) == [
        at(2026, 10, 31, 9, 1),
        at(2026, 10, 31, 9, 2),
        at(2026, 10, 31, 9, 3),
    ]
    assert Cron.parse("0 9 * * *").next_after(at(2026, 10, 31, 9, 0)) == at(2026, 11, 1, 9, 0)


def test_ranges_steps_and_lists():
    c = Cron.parse("*/15 9-17/4 1,15 */3 *")
    assert c.minutes == {0, 15, 30, 45} and c.hours == {9, 13, 17}
    assert c.days == {1, 15} and c.months == {1, 4, 7, 10}
    assert Cron.parse("5/20 * * * *").minutes == {5, 25, 45}
    assert Cron.parse("1-3,10,50-59/5 * * * *").minutes == {1, 2, 3, 10, 50, 55}
    assert fires("0 9-10 * * *", at(2026, 10, 31, 9, 30)) == [
        at(2026, 10, 31, 10, 0),
        at(2026, 11, 1, 9, 0),
        at(2026, 11, 1, 10, 0),
    ]


def test_names_and_sunday_as_seven():
    c = Cron.parse("0 0 * jan-mar,Dec MON-fri")
    assert c.months == {1, 2, 3, 12} and c.weekdays == {1, 2, 3, 4, 5}
    assert Cron.parse("0 0 * * 7").weekdays == {0} == Cron.parse("0 0 * * SUN").weekdays
    assert Cron.parse("0 0 * * 5-7").weekdays == {5, 6, 0}
    assert Cron.parse("0 0 * * */2").weekdays == {0, 2, 4, 6}


@pytest.mark.parametrize(
    ("macro", "same"),
    [
        ("@yearly", "0 0 1 1 *"),
        ("@annually", "0 0 1 1 *"),
        ("@monthly", "0 0 1 * *"),
        ("@weekly", "0 0 * * 0"),
        ("@daily", "0 0 * * *"),
        ("@MIDNIGHT", "0 0 * * *"),
        ("@hourly", "0 * * * *"),
    ],
)
def test_macros(macro, same):
    a, b = Cron.parse(macro), Cron.parse(same)
    assert (a.minutes, a.hours, a.days, a.months, a.weekdays) == (b.minutes, b.hours, b.days, b.months, b.weekdays)
    assert a.text == macro


def test_day_of_month_or_day_of_week_when_both_are_restricted():
    # the 13th, or any Friday: 2026-11-06 is a Friday, 2026-11-13 both
    assert fires("0 0 13 * FRI", at(2026, 10, 31)) == [at(2026, 11, 6), at(2026, 11, 13), at(2026, 11, 20)]


def test_day_of_week_alone_when_day_of_month_is_a_star():
    assert fires("0 8 * * 1", at(2026, 10, 31)) == [at(2026, 11, 2, 8), at(2026, 11, 9, 8), at(2026, 11, 16, 8)]
    # a star with a step still counts as a star: Mondays that are the 1st (every day of the month is in */1)
    assert fires("0 0 */1 * 1", at(2026, 10, 31), 1) == [at(2026, 11, 2)]


def test_day_of_month_alone_skips_months_without_it():
    assert fires("0 0 31 * *", at(2026, 10, 31, 1)) == [at(2026, 12, 31), at(2027, 1, 31), at(2027, 3, 31)]
    assert fires("0 12 29 2 *", at(2026, 10, 31), 2) == [at(2028, 2, 29, 12), at(2032, 2, 29, 12)]


def test_in_a_zone():
    # 09:00 in Amsterdam: 07:00 UTC in summer (CEST), 08:00 once the clocks go back on 25 October
    assert fires("0 9 * * *", at(2026, 10, 23, 12), 2, "Europe/Amsterdam") == [at(2026, 10, 24, 7), at(2026, 10, 25, 8)]


def test_a_time_the_clocks_skip_fires_once_past_the_jump():
    # New York, 2026-03-08: 02:00 EST becomes 03:00 EDT; 02:30 does not exist and fires at 03:30 EDT (07:30 UTC)
    assert fires("30 2 * * *", at(2026, 3, 7, 12), 3, "America/New_York") == [
        at(2026, 3, 8, 7, 30),
        at(2026, 3, 9, 6, 30),
        at(2026, 3, 10, 6, 30),
    ]
    # hourly across the jump: 01:00 EST, then 03:00 EDT once (02:00 and 03:00 are the same instant)
    assert fires("0 * * * *", at(2026, 3, 8, 5, 30), 3, "America/New_York") == [
        at(2026, 3, 8, 6),
        at(2026, 3, 8, 7),
        at(2026, 3, 8, 8),
    ]


def test_a_time_the_clocks_pass_twice_fires_once_the_first_time():
    # New York, 2026-11-01: 02:00 EDT falls back to 01:00 EST; 01:30 happens at 05:30 and 06:30 UTC
    assert fires("30 1 * * *", at(2026, 10, 31, 12), 2, "America/New_York") == [
        at(2026, 11, 1, 5, 30),
        at(2026, 11, 2, 6, 30),
    ]
    # asked during the second 01:xx, the repeated wall times are not fired again
    assert Cron.parse("*/30 * * * *").next_after(at(2026, 11, 1, 6, 10), zone("America/New_York")) == at(
        2026, 11, 1, 7, 0
    )


def test_zones():
    assert zone("UTC") is UTC and str(zone("Europe/Amsterdam")) == "Europe/Amsterdam"
    for bad in ("Mars/Olympus", "", "../etc/passwd"):
        with pytest.raises(CronError, match="IANA"):
            zone(bad)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("* * * *", "five fields"),
        ("* * * * * *", "five fields"),
        ("60 * * * *", "minute"),
        ("* 24 * * *", "hour"),
        ("* * 0 * *", "day of month"),
        ("* * * 13 *", "month"),
        ("* * * * 8", "day of week"),
        ("* * * FOO *", "month"),
        ("*/0 * * * *", "step"),
        ("5-1 * * * *", "low to high"),
        ("1-2-3 * * * *", "a number"),
        ("? * * * *", "a number"),
        ("0 0 30 2 *", "never fires"),
        ("0 0 31 4,6,9,11 *", "never fires"),
    ],
)
def test_refusals_say_why(text, why):
    with pytest.raises(CronError, match=why):
        Cron.parse(text)


def test_an_impossible_day_is_fine_when_a_weekday_can_fire_it():
    assert Cron.parse("0 0 30 2 MON").next_after(at(2026, 10, 31)) == at(2027, 2, 1)  # a Monday in February
