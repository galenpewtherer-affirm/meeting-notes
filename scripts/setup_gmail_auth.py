#!/usr/bin/env python3
"""
scripts/setup_gmail_auth.py — One-time Gmail OAuth setup for fetch_zoom_emails.py.

Run this once to authorize Gmail read access. Saves gmail_token.json next to
this script. fetch_zoom_emails.py will use it automatically on future runs.

Prerequisites:
    pip install google-auth-oauthlib google-auth-httplib2

Reuses the same credentials.json from the peak-events project.
The Gmail API must be enabled in that project first:
    https://console.cloud.google.com/apis/library?project=peak-events-491423
"""

import os
import sys
import json

SCOPES     = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "gmail_token.json")


def main():
    creds_path = os.path.realpath(CREDS_FILE)
    if not os.path.exists(creds_path):
        sys.exit(
            f"Error: {creds_path} not found.\n\n"
            "The credentials.json from the peak-events project is required.\n"
            "Check that peak-events/scripts/credentials.json exists."
        )

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        import google.oauth2.credentials
    except ImportError:
        sys.exit(
            "Error: required packages not installed.\n"
            "Run: pip install google-auth-oauthlib google-auth-httplib2"
        )

    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            info = json.load(f)
        creds = google.oauth2.credentials.Credentials.from_authorized_user_info(info, SCOPES)

    if creds and creds.valid:
        print(f"Already authorized. Token is valid.")
        print(f"Token file: {TOKEN_FILE}")
        return

    if creds and creds.expired and creds.refresh_token:
        print("Token expired — refreshing...")
        creds.refresh(Request())
    else:
        print("Opening browser for Gmail authorization...")
        flow  = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"\nAuthorization complete. Token saved to: {TOKEN_FILE}")
    print("You can now run: python3 scripts/fetch_zoom_emails.py")


if __name__ == "__main__":
    main()
