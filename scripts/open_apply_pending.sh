#!/bin/bash
# Opened by terminal-notifier's -execute action for any meeting-notes alert
# (blocked write, stuck prompt, crash/timeout, or untrusted directory) — see
# maybe_notify/_maybe_alert_trust_needed in run_due_meeting_notes.py.
# Opens a new Terminal window with an interactive Claude Code session in the
# meeting-notes directory so the user can resolve whatever's stuck in person.
osascript <<'APPLESCRIPT'
tell application "Terminal"
    activate
    do script "cd ~/Claude/meeting-notes && /Users/galen.pewtherer/.local/bin/claude"
end tell
APPLESCRIPT
