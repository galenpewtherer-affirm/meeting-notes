#!/usr/bin/env python3
"""
One-time Google Calendar OAuth setup for schedule_meeting_notes.py.

Run this once to authorize Calendar read access. Saves calendar_token.json
next to this script. schedule_meeting_notes.py will use it automatically.

Prerequisites:
    pip install google-auth-oauthlib google-auth-httplib2

The Calendar API must be enabled in the peak-events project first:
    https://console.cloud.google.com/apis/library/calendar-json.googleapis.com?project=peak-events-491423
"""

import os
import sys
import json

SCOPES     = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "calendar_token.json")


def main():
    creds_path = os.path.realpath(CREDS_FILE)
    if not os.path.exists(creds_path):
        sys.exit(f"Error: {creds_path} not found.")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        import google.oauth2.credentials
    except ImportError:
        sys.exit("Error: run: pip install google-auth-oauthlib google-auth-httplib2")

    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            info = json.load(f)
        creds = google.oauth2.credentials.Credentials.from_authorized_user_info(info, SCOPES)

    if creds and creds.valid:
        print(f"Already authorized. Token: {TOKEN_FILE}")
        return

    if creds and creds.expired and creds.refresh_token:
        print("Refreshing token...")
        creds.refresh(Request())
    else:
        print("Opening browser for Google Calendar authorization...")
        flow  = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"\nDone. Token saved to: {TOKEN_FILE}")
    print("You can now run: python3 scripts/schedule_meeting_notes.py")


if __name__ == "__main__":
    main()
