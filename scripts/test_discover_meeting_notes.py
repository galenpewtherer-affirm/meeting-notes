#!/usr/bin/env python3
"""Plain-assert tests for discover_meeting_notes.merge_schedule() and
extract_candidates().

No pytest dependency. Run: python3 test_discover_meeting_notes.py
Exits non-zero on first failure.

Mirrors test_schedule_meeting_notes.py's structure (same duplicate-fire
regression coverage, keyed by Notion page id instead of Calendar event id),
plus the new retry-on-skipped-until-expiry behavior this version adds.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import discover_meeting_notes as d

failures = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        failures.append(name)


TODAY = "2026-09-12"
NOW_ISO = "2026-09-12T14:00:00-07:00"  # used only for eyeballing; merge_schedule uses real now()


def cand(cid, title="Standup", created="2026-09-12T10:00:00-07:00", ready=True):
    return {"id": cid, "title": title, "created_time": created, "ready": ready}


# --- fresh scan, no prior schedule ------------------------------------------
result = d.merge_schedule(None, [cand("p1")], TODAY)
check("no prior schedule: ready meeting added as pending",
      len(result["meetings"]) == 1 and result["meetings"][0]["status"] == "pending")
check("no prior schedule: counted as new", result["_new_count"] == 1 and result["_updated_count"] == 0)

# --- not-ready candidate: not added at all on first sighting ---------------
result = d.merge_schedule(None, [cand("p1", ready=False)], TODAY)
check("not-ready new candidate is not added to the schedule at all", result["meetings"] == [])

# --- different-day prior schedule discarded, not merged ---------------------
stale = {"date": "2026-09-11", "meetings": [{"id": "p1", "title": "Old", "end_time": "x",
         "trigger_time": "x", "status": "fired"}]}
result = d.merge_schedule(stale, [cand("p1")], TODAY)
check("different-day prior schedule discarded", result["meetings"][0]["status"] == "pending")

# --- core regression: already-fired meeting never reset to pending ---------
same_day_fired = {"date": TODAY, "meetings": [{"id": "p1", "title": "Standup",
                   "end_time": "2026-09-12T10:00:00-07:00", "trigger_time": "x",
                   "status": "fired"}]}
result = d.merge_schedule(same_day_fired, [cand("p1")], TODAY)
check("already-fired meeting carried forward untouched", result["meetings"][0]["status"] == "fired")
check("carried-forward meeting not counted", result["_new_count"] == 0 and result["_updated_count"] == 0)

for terminal in ["blocked", "failed", "stuck", "missed"]:
    prior = {"date": TODAY, "meetings": [{"id": "p1", "title": "Standup", "end_time": "x",
              "trigger_time": "x", "status": terminal}]}
    result = d.merge_schedule(prior, [cand("p1")], TODAY)
    check(f"'{terminal}' status never reset to pending", result["meetings"][0]["status"] == terminal)

# --- still-pending candidate refreshed in place -----------------------------
prior_pending = {"date": TODAY, "meetings": [{"id": "p1", "title": "Standup",
                  "end_time": "2026-09-12T10:00:00-07:00", "trigger_time": "x", "status": "pending"}]}
result = d.merge_schedule(prior_pending, [cand("p1", title="Standup (renamed)")], TODAY)
check("still-pending entry refreshed (e.g. title correction)",
      result["meetings"][0]["title"] == "Standup (renamed)")
check("refreshed entry stays pending", result["meetings"][0]["status"] == "pending")
check("refresh counted as updated, not new", result["_updated_count"] == 1 and result["_new_count"] == 0)

# --- THE new behavior: skipped entry retried once ready ---------------------
prior_skipped = {"date": TODAY, "meetings": [{"id": "p1", "title": "Standup", "end_time": "x",
                  "trigger_time": "x", "status": "skipped"}]}
result = d.merge_schedule(prior_skipped, [cand("p1", ready=True)], TODAY)
check("skipped entry promoted back to pending once ready",
      result["meetings"][0]["status"] == "pending")
check("promoted-from-skipped counted as updated", result["_updated_count"] == 1)

# --- skipped entry NOT retried while still not ready ------------------------
result = d.merge_schedule(prior_skipped, [cand("p1", ready=False)], TODAY)
check("skipped entry stays skipped while still not ready", result["meetings"][0]["status"] == "skipped")

# --- skipped entry stops being retried once past the retry window ----------
old_created = "2026-09-08T10:00:00-07:00"  # >2 days before TODAY (2026-09-12)
result = d.merge_schedule(prior_skipped, [cand("p1", ready=True, created=old_created)], TODAY)
check("skipped entry past retry window stays skipped even if now ready",
      result["meetings"][0]["status"] == "skipped")

# --- fired_no_zoom is retryable the same way as skipped ---------------------
prior_no_zoom = {"date": TODAY, "meetings": [{"id": "p1", "title": "Standup", "end_time": "x",
                  "trigger_time": "x", "status": "fired_no_zoom"}]}
result = d.merge_schedule(prior_no_zoom, [cand("p1", ready=True)], TODAY)
check("fired_no_zoom promoted back to pending once ready", result["meetings"][0]["status"] == "pending")

# --- mixed scan: new + already-fired coexist --------------------------------
prior_mixed = {"date": TODAY, "meetings": [{"id": "p1", "title": "Standup", "end_time": "x",
                "trigger_time": "x", "status": "fired"}]}
result = d.merge_schedule(prior_mixed, [cand("p1"), cand("p2", title="1:1")], TODAY)
by_id = {m["id"]: m for m in result["meetings"]}
check("mixed scan: prior fired untouched", by_id["p1"]["status"] == "fired")
check("mixed scan: new candidate added pending", by_id["p2"]["status"] == "pending")

# --- vanished-from-scan entry preserved -------------------------------------
prior_only = {"date": TODAY, "meetings": [{"id": "p1", "title": "Standup", "end_time": "x",
               "trigger_time": "x", "status": "pending"}]}
result = d.merge_schedule(prior_only, [], TODAY)
check("meeting missing from fresh scan is preserved, not dropped",
      any(m["id"] == "p1" for m in result["meetings"]))


# --- extract_candidates() ---------------------------------------------------
sample_output = (
    "some tool-call chatter\n"
    "⏺ Done with discovery.\n"
    'CANDIDATES_JSON: [{"id": "p1", "title": "Standup", "created_time": "x", "ready": true}]\n'
    "RESULT: SUCCESS\n"
)
result = d.extract_candidates(sample_output)
check("extract_candidates parses a real marker line", result == [{"id": "p1", "title": "Standup",
      "created_time": "x", "ready": True}])

check("extract_candidates returns None when marker is missing",
      d.extract_candidates("no marker here\nRESULT: SUCCESS\n") is None)

check("extract_candidates returns None on malformed JSON",
      d.extract_candidates("CANDIDATES_JSON: not valid json\n") is None)

check("extract_candidates handles the zero-candidates case",
      d.extract_candidates("CANDIDATES_JSON: []\nRESULT: SUCCESS\n") == [])

check("extract_candidates uses the LAST marker line (post-assistant-turn text)",
      d.extract_candidates(
          f"{d.CANDIDATES_MARKER} [\"echoed from the prompt instructions, not real\"]\n"
          f"⏺ actual work happens here\n"
          f'{d.CANDIDATES_MARKER} [{{"id": "real", "title": "T", "created_time": "x", "ready": true}}]\n'
      )[0]["id"] == "real")

# --- real regression: tmux hard-wraps long lines mid-string at the pty width ---
# Captured verbatim from a live run 2026-09-12 (220-col pty) -- the title value
# "Zoom Meeting" got split across the wrap with re-indented continuation spaces.
wrapped_real_output = (
    '⏺ CANDIDATES_JSON: [{"id":"https://app.notion.com/p/6038a3a3c4ff45379c0a22e8094adbc4","title":"Zoom\n'
    '  Meeting","created_time":"2026-09-12T11:27:56.557Z","ready":false},{"id":"https://app.notion.com/p/3d840e54ae3881a697ead5c8cf428d88","title":"BFCM peak readiness prep - potential\n'
    '  centralization","created_time":"2026-09-11T17:01:51.089Z","ready":true}]\n'
    '  RESULT: SUCCESS\n'
)
result = d.extract_candidates(wrapped_real_output)
check("extract_candidates survives a real tmux word-wrap mid-JSON", result is not None and len(result) == 2)
if result:
    check("wrapped title reconstructed correctly", result[0]["title"] == "Zoom Meeting")
    check("second wrapped title reconstructed correctly",
          result[1]["title"] == "BFCM peak readiness prep - potential centralization")
    check("wrapped ready flags parsed correctly",
          result[0]["ready"] is False and result[1]["ready"] is True)


if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("\nAll discover_meeting_notes tests passed.")
