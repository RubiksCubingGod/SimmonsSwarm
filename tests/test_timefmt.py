from __future__ import annotations

from simmons.timefmt import pacific


def test_pacific_reads_like_a_date_and_follows_daylight_saving():
    assert pacific("2026-10-04T18:02:42.069Z") == "Sunday, October 4, 2026 at 11:02 AM PDT"
    assert pacific("2026-12-25T20:00:00Z") == "Friday, December 25, 2026 at 12:00 PM PST"
    assert pacific("2026-10-05T06:30:00+00:00") == "Sunday, October 4, 2026 at 11:30 PM PDT"


def test_pacific_ignores_values_that_are_not_timestamps():
    assert pacific(None) is None
    assert pacific("") is None
    assert pacific("20261004T075653Z-atin-7dcc") is None
