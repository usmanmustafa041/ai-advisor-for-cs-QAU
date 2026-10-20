import re
from datetime import date

from app.api.admin import _effective_date
from app.api.timetable import _room_value, _semester_pattern

GROUPS = [
    "BS Semester I Regular", "BS Semester II Self + Regular", "BS Semester III Self Support",
    "BS Semester IV", "BS Semester VII Regular", "BS Semester VIII Self Support",
    "MS-DS Semester II + III + IV", "Deficiency Courses", "MPhil & PhD",
]


def matching(semester: str) -> list[str]:
    # Postgres \m \M word boundaries behave like \b here.
    pattern = _semester_pattern(semester).replace(r"\m", r"\b").replace(r"\M", r"\b")
    return [g for g in GROUPS if re.search(pattern, g, re.IGNORECASE)]


def test_semester_number_and_roman_match_only_that_semester():
    assert matching("1") == ["BS Semester I Regular"]
    assert matching("Semester 1") == matching("I") == matching("sem i")
    assert matching("3") == ["BS Semester III Self Support", "MS-DS Semester II + III + IV"]
    assert matching("7") == ["BS Semester VII Regular"]
    assert matching("8") == ["BS Semester VIII Self Support"]


def test_semester_text_without_a_number_falls_back_to_plain_search():
    assert _semester_pattern("Regular") is None
    assert _semester_pattern("Deficiency") is None


def test_room_prefix_is_ignored():
    assert _room_value("Room 201") == "201"
    assert _room_value("room no. 217") == "217"
    assert _room_value("Lab 3") == "Lab 3"


def test_effective_date_parses_timetable_label():
    assert _effective_date("Monday 5th October, 2026") == date(2026, 10, 5)
    assert _effective_date("Monday 28th September, 2026") == date(2026, 9, 28)
    assert _effective_date("") is None
    assert _effective_date("soon") is None
