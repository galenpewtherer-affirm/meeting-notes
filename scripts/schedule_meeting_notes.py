#!/usr/bin/env python3
"""
Runs via launchd (com.galen.meeting-notes-scheduler). Reads today's Google
Calendar events, finds Zoom meetings, and writes a queue file
(~/.meeting-notes-schedule.json) marking each meeting "pending" with a
trigger_time = end + TRIGGER_OFFSET_MINUTES.

A separate launchd job (com.galen.meeting-notes-runner) polls that queue every
few minutes and invokes the meeting-notes skill for any meeting whose trigger
time has arrived. See run_due_meeting_notes.py.

Safe to run more than once per day (added 2026-09-12, to support an hourly
cadence that catches meetings added/moved after the first scan): merges into
the existing same-day schedule file instead of overwriting it wholesale.
Previously-tracked meetings whose status is no longer "pending" (already
fired/blocked/skipped/missed/etc, per run_due_meeting_notes.py) are carried
forward completely untouched -- the earlier version rebuilt every meeting
fresh with status "pending" on every run, which silently reset that guard and
would have caused run_due_meeting_notes.py to re-invoke the whole
meeting-notes skill a second time for any meeting that had already fired
within roughly the last MAX_LATENESS_MINUTES. See merge_schedule().

This used to schedule via the Unix `at` command, but on modern macOS `at`
requires Full Disk Access for the launchd-spawned process and fails with
"cannot open lockfile /usr/lib/cron/jobs/.lockfile: Operation not permitted".
The polling-runner replacement avoids that permission entirely.
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS_DIR   = Path(__file__).parent
TOKEN_FILE    = SCRIPTS_DIR / "calendar_token.json"
CREDS_FILE    = SCRIPTS_DIR / "credentials.json"
SCHEDULE_FILE = Path.home() / '.meeting-notes-schedule.json'
SCOPES        = ["https://www.googleapis.com/auth/calendar.readonly"]
LOG_PREFIX    = '[schedule_meeting_notes]'
TRIGGER_OFFSET_MINUTES = 5


def log(msg):
    print(f"{datetime.now().isoformat()} {LOG_PREFIX} {msg}", flush=True)


def get_creds():
    import google.oauth2.credentials
    from google.auth.transport.requests import Request

    if not TOKEN_FILE.exists():
        log(f"No token found at {TOKEN_FILE}. Run setup_calendar_auth.py first.")
        sys.exit(1)

    with open(TOKEN_FILE) as f:
        info = json.load(f)
    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(info, SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds


def get_today_events(creds):
    from googleapiclient.discovery import build

    service = build("calendar", "v3", credentials=creds)

    local_now = datetime.now().astimezone()
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = local_now.replace(hour=23, minute=59, second=59, microsecond=0)

    result = service.events().list(
        calendarId="primary",
        timeMin=today_start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        timeMax=today_end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        singleEvents=True,
        orderBy="startTime",
        fields="items(id,summary,description,location,start,end,conferenceData,attendees)"
    ).execute()

    return result.get("items", [])


def is_zoom_meeting(event):
    conf = event.get("conferenceData", {})
    if "zoom" in conf.get("conferenceSolution", {}).get("name", "").lower():
        return True
    for entry in conf.get("entryPoints", []):
        if "zoom.us" in entry.get("uri", ""):
            return True
    for field in [event.get("description") or "", event.get("location") or ""]:
        if "zoom.us" in field.lower():
            return True
    return False


def user_declined(event):
    for att in event.get("attendees", []):
        if att.get("self") and att.get("responseStatus") == "declined":
            return True
    return False


def parse_dt(dt_str):
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def load_existing_schedule():
    """Returns the parsed schedule dict, or None if missing/unreadable/corrupt --
    callers should treat None the same as "no prior schedule for today"."""
    if not SCHEDULE_FILE.exists():
        return None
    try:
        return json.loads(SCHEDULE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def merge_schedule(existing, events, today_str, log_fn=None):
    """Pure merge logic (no I/O) -- see module docstring for why this must never
    just rebuild the meetings list from scratch.

    existing: the previously-loaded schedule dict (or None), only honored if its
      "date" matches today_str -- a schedule from a prior day is discarded, not
      merged, since each day starts fresh.
    events: today's raw Calendar events (get_today_events()'s return value).
    Returns the new schedule dict.
    """
    log_fn = log_fn or (lambda msg: None)
    existing_by_id = {}
    if existing and existing.get("date") == today_str:
        existing_by_id = {m["id"]: m for m in existing.get("meetings", [])}

    seen_ids = set()
    meetings = []
    new_count = 0
    updated_count = 0

    for event in events:
        if "dateTime" not in event.get("start", {}):
            continue

        eid = event["id"]
        prior = existing_by_id.get(eid)

        if prior and prior.get("status") != "pending":
            # Already processed by run_due_meeting_notes.py -- never touch again,
            # not even to refresh title/time, regardless of what Calendar says now.
            meetings.append(prior)
            seen_ids.add(eid)
            continue

        if user_declined(event):
            log_fn(f"Skipping declined: {event.get('summary', 'Untitled')}")
            continue
        if not is_zoom_meeting(event):
            log_fn(f"Skipping non-Zoom: {event.get('summary', 'Untitled')}")
            continue

        end_dt     = parse_dt(event["end"]["dateTime"])
        trigger_dt = end_dt + timedelta(minutes=TRIGGER_OFFSET_MINUTES)
        entry = {
            "id":           eid,
            "title":        event.get("summary", "Untitled"),
            "end_time":     end_dt.isoformat(),
            "trigger_time": trigger_dt.isoformat(),
            "status":       "pending",
        }
        if prior:
            updated_count += 1  # still-pending entry, refreshed (e.g. reschedule)
        else:
            new_count += 1
        meetings.append(entry)
        seen_ids.add(eid)

    # A previously-tracked meeting that doesn't appear in this scan at all (API
    # hiccup, or the event was deleted/moved off-calendar after being queued) is
    # carried forward as-is rather than silently dropped.
    for eid, prior in existing_by_id.items():
        if eid not in seen_ids:
            meetings.append(prior)

    return {
        "date": today_str,
        "meetings": meetings,
        "_new_count": new_count,
        "_updated_count": updated_count,
    }


def main():
    log("Starting calendar scan")
    today_str = datetime.now().strftime("%Y-%m-%d")

    creds = get_creds()
    events = get_today_events(creds)
    log(f"Found {len(events)} calendar events")

    existing = load_existing_schedule()
    schedule = merge_schedule(existing, events, today_str, log_fn=log)
    new_count = schedule.pop("_new_count")
    updated_count = schedule.pop("_updated_count")

    SCHEDULE_FILE.write_text(json.dumps(schedule, indent=2))
    log(f"{new_count} new + {updated_count} updated Zoom meeting(s), "
        f"{len(schedule['meetings'])} total tracked for today; "
        f"runner will fire pending ones at trigger time")


if __name__ == "__main__":
    main()
