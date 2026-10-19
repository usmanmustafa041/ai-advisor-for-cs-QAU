"""Import the reviewed Fall 2026 timetable dataset into the public timetable tables."""

import json
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "academic-data" / "timetable" / "cs_timetable_fall-2026.json"
DSN = "postgresql://qau_advisor:qau_advisor_local@127.0.0.1:55432/qau_advisor"


def main() -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    courses = {
        course["key"]: {**course, "group": group["group"], "program": group["program"]}
        for group in data["classes"]
        for course in group["courses"]
    }
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE academic_sessions SET is_current=false")
            cur.execute(
                """INSERT INTO academic_sessions(session_name,effective_from,is_current)
                   VALUES (%s,%s,true) ON CONFLICT(session_name) DO UPDATE SET
                   effective_from=excluded.effective_from,is_current=true,updated_at=now()
                   RETURNING id""",
                (data["term"], "2026-09-28"),
            )
            session_id = cur.fetchone()[0]
            cur.execute("DELETE FROM course_schedules WHERE academic_session_id=%s", (session_id,))
            for slot in data["sessions"]:
                info = courses.get(slot["key"], {})
                title = info.get("title") or slot["text"]
                teacher = info.get("teacher") or ""
                section = slot.get("section") or "Regular"
                section = {
                    "R": "Regular",
                    "S": "Self Support",
                    "RS": "Regular & Self Support",
                    "Rep": "Repeaters",
                }.get(section, section)
                program = info.get("program") or "ALL"
                group = info.get("group") or ""
                hour = int(slot["time"].split(":", 1)[0])
                shift = "Evening" if hour >= 17 else ("Afternoon" if hour >= 13 else "Morning")
                cur.execute(
                    """INSERT INTO course_schedules
                       (academic_session_id,course_code,course_abbreviation,course_name,program,
                        semester,section_type,day_of_week,time_slot,shift,room,is_active)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true) RETURNING id""",
                    (
                        session_id,
                        slot["code"],
                        slot["text"][:20],
                        title,
                        program,
                        group,
                        section,
                        slot["day"].title(),
                        slot["time"],
                        shift,
                        slot["room"],
                    ),
                )
                schedule_id = cur.fetchone()[0]
                if teacher:
                    title_prefix = teacher.split()[0] if teacher.split()[0].endswith(".") else None
                    cur.execute(
                        """INSERT INTO instructors(name,title) VALUES (%s,%s)
                           ON CONFLICT(name,department) DO UPDATE SET title=excluded.title
                           RETURNING id""",
                        (teacher, title_prefix),
                    )
                    instructor_id = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO course_schedule_instructors(course_schedule_id,instructor_id) VALUES (%s,%s)",
                        (schedule_id, instructor_id),
                    )
    print(f"Imported {len(data['sessions'])} Fall 2026 timetable entries")


if __name__ == "__main__":
    main()
