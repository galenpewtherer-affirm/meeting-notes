#!/usr/bin/env python3
"""Plain-assert tests for schedule_meeting_notes.merge_schedule().

No pytest dependency. Run: python3 test_schedule_meeting_notes.py
Exits non-zero on first failure.

Regression tests for the 2026-09-12 merge fix: the previous version rebuilt the
whole meetings list from scratch on every run, resetting every meeting's status
back to "pending" -- which meant running the scheduler more than once a day
(needed for an hourly cadence) would make run_due_meeting_notes.py re-fire any
meeting that had already completed within the last MAX_LATENESS_MINUTES.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import schedule_meeting_notes as s

failures = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        failures.append(name)


def zoom_event(eid, summary="Standup", end="2026-09-12T10:00:00-07:00", declined=False):
    ev = {
        "id": eid,
        "summary": summary,
        "start": {"dateTime": "2026-09-12T09:30:00-07:00"},
        "end": {"dateTime": end},
        "conferenceData": {"conferenceSolution": {"name": "Zoom Meeting"}},
    }
    if declined:
        ev["attendees"] = [{"self": True, "responseStatus": "declined"}]
    return ev


TODAY = "2026-09-12"

# --- fresh scan, no prior schedule ------------------------------------------
result = s.merge_schedule(None, [zoom_event("e1")], TODAY)
check("no prior schedule: new meeting added as pending", len(result["meetings"]) == 1
      and result["meetings"][0]["status"] == "pending")
check("no prior schedule: counted as new", result["_new_count"] == 1 and result["_updated_count"] == 0)

# --- prior schedule from a DIFFERENT day is discarded, not merged -----------
stale = {"date": "2026-09-11", "meetings": [{"id": "e1", "title": "Old", "end_time": "x",
                                             "trigger_time": "x", "status": "fired"}]}
result = s.merge_schedule(stale, [zoom_event("e1")], TODAY)
check("different-day prior schedule discarded (not treated as already-fired)",
      result["meetings"][0]["status"] == "pending")

# --- THE core regression: already-fired meeting is never reset to pending ---
same_day_fired = {"date": TODAY, "meetings": [{"id": "e1", "title": "Standup",
                   "end_time": "2026-09-12T10:00:00-07:00",
                   "trigger_time": "2026-09-12T10:05:00-07:00", "status": "fired"}]}
result = s.merge_schedule(same_day_fired, [zoom_event("e1")], TODAY)
check("already-fired meeting carried forward untouched, NOT reset to pending",
      result["meetings"][0]["status"] == "fired")
check("carried-forward meeting not counted as new or updated",
      result["_new_count"] == 0 and result["_updated_count"] == 0)

# --- same check for every other terminal status -----------------------------
for terminal_status in ["blocked", "skipped", "missed", "failed", "stuck"]:
    prior = {"date": TODAY, "meetings": [{"id": "e1", "title": "Standup", "end_time": "x",
              "trigger_time": "x", "status": terminal_status}]}
    result = s.merge_schedule(prior, [zoom_event("e1")], TODAY)
    check(f"'{terminal_status}' status also never reset to pending",
          result["meetings"][0]["status"] == terminal_status)

# --- still-pending meeting gets its fields refreshed (reschedule case) ------
prior_pending = {"date": TODAY, "meetings": [{"id": "e1", "title": "Standup",
                  "end_time": "2026-09-12T10:00:00-07:00",
                  "trigger_time": "2026-09-12T10:05:00-07:00", "status": "pending"}]}
result = s.merge_schedule(prior_pending, [zoom_event("e1", end="2026-09-12T10:30:00-07:00")], TODAY)
check("still-pending meeting refreshed to new end_time on reschedule",
      result["meetings"][0]["end_time"] == "2026-09-12T10:30:00-07:00")
check("refreshed meeting stays pending", result["meetings"][0]["status"] == "pending")
check("refreshed meeting counted as updated, not new",
      result["_updated_count"] == 1 and result["_new_count"] == 0)

# --- new meeting alongside an already-fired one: only the new one is added --
prior_mixed = {"date": TODAY, "meetings": [{"id": "e1", "title": "Standup", "end_time": "x",
                "trigger_time": "x", "status": "fired"}]}
result = s.merge_schedule(prior_mixed, [zoom_event("e1"), zoom_event("e2", summary="1:1")], TODAY)
by_id = {m["id"]: m for m in result["meetings"]}
check("mixed scan: prior fired meeting untouched", by_id["e1"]["status"] == "fired")
check("mixed scan: new meeting added as pending", by_id["e2"]["status"] == "pending")
check("mixed scan: counts reflect only the truly new one",
      result["_new_count"] == 1 and result["_updated_count"] == 0)

# --- meeting that vanishes from the fresh scan is carried forward, not lost -
prior_only = {"date": TODAY, "meetings": [{"id": "e1", "title": "Standup", "end_time": "x",
               "trigger_time": "x", "status": "pending"}]}
result = s.merge_schedule(prior_only, [], TODAY)
check("meeting missing from fresh scan is preserved, not dropped",
      any(m["id"] == "e1" for m in result["meetings"]))

# --- declined-after-queued: not re-added fresh, but not lost either ---------
result = s.merge_schedule(prior_only, [zoom_event("e1", declined=True)], TODAY)
check("meeting declined after being queued keeps its prior (pending) entry",
      any(m["id"] == "e1" and m["status"] == "pending" for m in result["meetings"]))

# --- non-Zoom / no-dateTime events are still filtered out on fresh scans ----
all_day_event = {"id": "e3", "summary": "Holiday", "start": {"date": "2026-09-12"},
                  "end": {"date": "2026-09-13"}}
non_zoom_event = {"id": "e4", "summary": "Lunch",
                   "start": {"dateTime": "2026-09-12T12:00:00-07:00"},
                   "end": {"dateTime": "2026-09-12T13:00:00-07:00"}}
result = s.merge_schedule(None, [all_day_event, non_zoom_event], TODAY)
check("all-day event filtered out", len(result["meetings"]) == 0)
result = s.merge_schedule(None, [non_zoom_event], TODAY)
check("non-Zoom event filtered out", len(result["meetings"]) == 0)


if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("\nAll schedule_meeting_notes tests passed.")
