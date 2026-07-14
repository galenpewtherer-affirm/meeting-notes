#!/bin/bash
# Opened by terminal-notifier's -execute action when a meeting-notes write is blocked.
# Opens a new Terminal window with an interactive Claude Code session in the
# meeting-notes directory so the user can approve the Notion write.
osascript <<'APPLESCRIPT'
tell application "Terminal"
    activate
    do script "cd ~/Claude/meeting-notes && /Users/galen.pewtherer/.local/bin/claude"
end tell
APPLESCRIPT
