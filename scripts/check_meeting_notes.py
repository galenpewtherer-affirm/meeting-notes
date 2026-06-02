#!/usr/bin/env python3
"""
Runs every 5 minutes via launchd. Checks the meeting schedule and triggers
'claude -p run the meeting notes skill' for any meetings past their trigger time.
"""
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCHEDULE_FILE = Path.home() / '.meeting-notes-schedule.json'
MEETING_NOTES_DIR = Path.home() / 'Claude/meeting-notes'
CLAUDE = '/Users/galen.pewtherer/.local/bin/claude'
LOG_PREFIX = '[check_meeting_notes]'
# If a trigger is missed, still process it up to 60 minutes after the meeting ended
CATCH_UP_WINDOW = timedelta(minutes=60)


def log(msg):
    print(f"{datetime.now().isoformat()} {LOG_PREFIX} {msg}", flush=True)


def main():
    if not SCHEDULE_FILE.exists():
        log("No schedule file, skipping")
        return

    try:
        data = json.loads(SCHEDULE_FILE.read_text())
    except Exception as e:
        log(f"Error reading schedule: {e}")
        return

    today = datetime.now().strftime('%Y-%m-%d')
    if data.get('date') != today:
        log(f"Schedule date is {data.get('date')}, not today — skipping")
        return

    now = datetime.now(timezone.utc)
    updated = False

    for meeting in data.get('meetings', []):
        if meeting.get('processed'):
            continue

        trigger_dt = datetime.fromisoformat(meeting['trigger_time'].replace('Z', '+00:00'))
        deadline_dt = trigger_dt + CATCH_UP_WINDOW

        if now < trigger_dt:
            log(f"Not yet: '{meeting['title']}' triggers at {trigger_dt.strftime('%H:%M %Z')}")
            continue

        if now > deadline_dt:
            log(f"Past catch-up window for '{meeting['title']}', skipping")
            meeting['processed'] = True
            updated = True
            continue

        log(f"Triggering meeting notes for: '{meeting['title']}'")

        result = subprocess.run(
            [CLAUDE, '-p', 'run the meeting notes skill', '--dangerously-skip-permissions'],
            cwd=str(MEETING_NOTES_DIR),
            capture_output=True, text=True,
            timeout=300
        )

        if result.returncode == 0:
            log(f"Done: '{meeting['title']}'")
            if result.stdout.strip():
                log(f"Output: {result.stdout.strip()[:300]}")
        else:
            log(f"Error for '{meeting['title']}': {result.stderr.strip()[:300]}")

        # Mark processed regardless of success to avoid hammering on error
        meeting['processed'] = True
        updated = True

    if updated:
        SCHEDULE_FILE.write_text(json.dumps(data, indent=2))
        log("Schedule updated")


if __name__ == '__main__':
    main()
