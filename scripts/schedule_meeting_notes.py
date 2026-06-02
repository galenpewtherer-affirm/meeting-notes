#!/usr/bin/env python3
"""
Runs daily at 7am via launchd (com.galen.meeting-notes-scheduler). Reads today's
Google Calendar events, finds Zoom meetings, and writes a queue file
(~/.meeting-notes-schedule.json) marking each meeting "pending" with a
trigger_time = end + TRIGGER_OFFSET_MINUTES.

A separate launchd job (com.galen.meeting-notes-runner) polls that queue every
few minutes and invokes the meeting-notes skill for any meeting whose trigger
time has arrived. See run_due_meeting_notes.py.

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


def main():
    log("Starting daily calendar scan")

    creds = get_creds()
    events = get_today_events(creds)
    log(f"Found {len(events)} calendar events")

    meetings = []
    for event in events:
        if "dateTime" not in event.get("start", {}):
            continue
        if user_declined(event):
            log(f"Skipping declined: {event.get('summary', 'Untitled')}")
            continue
        if not is_zoom_meeting(event):
            log(f"Skipping non-Zoom: {event.get('summary', 'Untitled')}")
            continue

        end_dt     = parse_dt(event["end"]["dateTime"])
        trigger_dt = end_dt + timedelta(minutes=TRIGGER_OFFSET_MINUTES)

        meetings.append({
            "id":           event["id"],
            "title":        event.get("summary", "Untitled"),
            "end_time":     end_dt.isoformat(),
            "trigger_time": trigger_dt.isoformat(),
            "status":       "pending",
        })

    schedule = {
        "date":     datetime.now().strftime("%Y-%m-%d"),
        "meetings": meetings,
    }
    SCHEDULE_FILE.write_text(json.dumps(schedule, indent=2))
    log(f"Queued {len(meetings)} Zoom meetings; runner will fire them at trigger time")


if __name__ == "__main__":
    main()
