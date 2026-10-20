"""Apply the corrections the Fall 2026 timetable PDF needs after automatic extraction.

The PDF (w.e.f. Monday 5th October 2026) has 119 sessions; the extractor reads 113. This script,
run against a live deployment, uses the admin timetable API (every change is audited and can be
undone from the admin panel) to:

  * add the 6 sessions the extractor missed,
  * fix 8 sessions whose room was read as "Lab 234 / Lab 3",
  * replace 7 sessions whose text picked up stray words from neighbouring cells
    (three of them also got the wrong section).

Safe to run again: corrections already in place are skipped.

    python3 scripts/fix_fall_2026_timetable.py                     # on the server (http://localhost)
    python3 scripts/fix_fall_2026_timetable.py --base http://127.0.0.1 --email admin@cs.qau.edu.pk
"""

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request

TT = "/advisor/api/v1/admin/timetable"

# Sessions missing from the extracted timetable.
MISSING = [
    dict(day="TUESDAY", start="10:10", end="11:45", room="Seminar room", program="MPhil/PhD", semester=None,
         section="", code="CS-828", title="Computational Modeling", teacher="Dr. Ghazanfar Farooq"),
    dict(day="TUESDAY", start="13:35", end="15:05", room="Seminar room", program="BS", semester=8,
         section="S", code="CS-483", title="Software Quality Assurance", teacher="Dr. Muddassar Sindhu"),
    dict(day="TUESDAY", start="11:55", end="13:55", room="Lab 3", program="BS", semester=5,
         section="S", code="CS-322", title="Software Construction", teacher="Dr. Umar Rashid"),
    dict(day="TUESDAY", start="15:15", end="16:45", room="BS Lab", program="BS", semester=2,
         section="", code="CS-121", title="Object Oriented Programming", teacher="Dr. Ghazanfar Farooq"),
    dict(day="WEDNESDAY", start="15:05", end="17:05", room="F8", program="BS", semester=7,
         section="R", code="MS-100", title="Entrepreneurship", teacher="Dr. M Usman Malik"),
    dict(day="THURSDAY", start="15:15", end="16:45", room="F6", program="BS", semester=3,
         section="S", code="EN-299", title="Technical and Business writing", teacher="Ms. Ayesha Jan"),
]

# (day, start, code) of PDF sessions read with room "Lab 234 / Lab 3" -> the room the PDF shows.
ROOMS = [
    ("TUESDAY", "15:15", "CS-311", "Lab 3"),
    ("WEDNESDAY", "08:30", "CS-211", "Lab 234"),
    ("WEDNESDAY", "10:10", "CS-211", "Lab 234"),
    ("WEDNESDAY", "11:55", "CS-322", "Lab 234"),
    ("WEDNESDAY", "13:35", "CS-322", "Lab 234"),
    ("THURSDAY", "11:55", "CS-322", "Lab 3"),
    ("THURSDAY", "13:35", "CS-224", "Lab 3"),
    ("THURSDAY", "15:15", "CS-224", "Lab 3"),
]

# PDF sessions with garbled text (and sometimes section or room): hidden and re-added cleanly.
# pdf_room is the room the extractor read, so only that session is hidden.
REPLACE = [
    dict(day="TUESDAY", start="08:30", end="10:00", pdf_room="217", room="217", program="BS", semester=1,
         section="R", code="EN-100", title="Functional English", teacher="Mr. Ali Naqi"),
    dict(day="TUESDAY", start="08:30", end="10:00", pdf_room="Lab 234 / Lab 3", room="Lab 3", program="BS", semester=7,
         section="R", code="CS-411", title="Compiler Construction", teacher="Dr. S. M. Naqi"),
    dict(day="WEDNESDAY", start="08:30", end="10:00", pdf_room="235", room="235", program="BS", semester=1,
         section="R", code="CSC-104", title="Problem Solving & Programming", teacher="Ms. Memoona Afsheen"),
    dict(day="WEDNESDAY", start="10:10", end="11:45", pdf_room="217", room="217", program="BS", semester=8,
         section="S", code="CS-449", title="ICT and Society", teacher="Dr. Shuaib Karim"),
    dict(day="THURSDAY", start="10:10", end="11:45", pdf_room="Lab 234 / Lab 3", room="Lab 3", program="BS", semester=5,
         section="S", code="CS-322", title="Software Construction", teacher="Dr. Umar Rashid"),
    dict(day="THURSDAY", start="10:10", end="11:45", pdf_room="Seminar room", room="Seminar room", program="MPhil/PhD", semester=None,
         section="", code="CS-652", title="Advanced Analysis of Algorithm", teacher="Dr. Onaiza Maqbool"),
    dict(day="THURSDAY", start="13:35", end="15:05", pdf_room="F6", room="F6", program="BS", semester=3,
         section="R", code="EN-299", title="Technical and Business writing", teacher="Ms. Ayesha Jan"),
]


def minutes(clock: str) -> int:
    h, m = clock.split(":")[:2]
    return int(h) * 60 + int(m)


def same_code(a: str, b: str) -> bool:
    norm = lambda c: (c or "").upper().replace("CSC-", "CS-")
    return norm(a) == norm(b)


class Api:
    def __init__(self, base: str):
        self.base, self.token = base.rstrip("/"), ""

    def call(self, method: str, path: str, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode())
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            sys.exit(f"{method} {path} failed: HTTP {exc.code} {exc.read().decode(errors='replace')[:300]}")


def find(sessions, day, start, code, room=None, src=None):
    return [s for s in sessions if s["day"] == day and minutes(s["start"]) == minutes(start)
            and same_code(s["code"], code) and (room is None or s["room"] == room)
            and (src is None or s["src"] in src)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://localhost")
    ap.add_argument("--email", default="admin@cs.qau.edu.pk")
    args = ap.parse_args()
    api = Api(args.base)
    password = getpass.getpass(f"Admin password for {args.email}: ")
    api.token = api.call("POST", "/api/v1/auth/login", {"email": args.email, "password": password})["access_token"]

    view = api.call("GET", TT)
    if view["term"] != "Fall 2026":
        sys.exit(f"The live timetable is {view['term']!r}, not Fall 2026; nothing changed.")
    print(f"Before: {view['counts']['total']} sessions")
    done = skipped = 0

    for day, start, code, room in ROOMS:
        rows = find(view["sessions"], day, start, code, src=("pdf", "edited"))
        if not rows:
            print(f"  ? not found: {day} {start} {code}")
            continue
        for s in rows:
            if s["room"] == room:
                skipped += 1
                continue
            api.call("PUT", f"{TT}/sessions/{s['id']}", {"room": room, "force": True})
            print(f"  room  {day[:3]} {start} {code}: {s['room']} -> {room}")
            done += 1

    for item in REPLACE:
        for s in find(view["sessions"], item["day"], item["start"], item["code"], room=item["pdf_room"], src=("pdf", "edited")):
            api.call("DELETE", f"{TT}/sessions/{s['id']}")
            print(f"  hide  {item['day'][:3]} {item['start']} {s['text']!r}")
            done += 1

    view = api.call("GET", TT)
    for item in REPLACE + MISSING:
        if find(view["sessions"], item["day"], item["start"], item["code"], room=item["room"], src=("manual",)):
            skipped += 1
            continue
        fields = {k: v for k, v in item.items() if k != "pdf_room"}
        result = api.call("POST", f"{TT}/sessions", {**fields, "force": True})
        warn = f"  (note: {'; '.join(result.get('warnings') or [])})" if result.get("warnings") else ""
        print(f"  add   {item['day'][:3]} {item['start']}-{item['end']} {item['room']}: {item['code']} {item['title']}{warn}")
        done += 1

    api.call("POST", f"{TT}/publish")  # make sure the students' Timetable page has the final version
    after = api.call("GET", TT)["counts"]
    print(f"After: {after['total']} sessions ({after['pdf']} from the PDF, {after['edited']} edited, "
          f"{after['manual']} added, {after['hidden']} hidden). {done} changes, {skipped} already correct.")
    if after["total"] != 119:
        print("WARNING: the PDF has 119 sessions; check the timetable in the admin panel.")


if __name__ == "__main__":
    main()
