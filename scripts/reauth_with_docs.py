#!/usr/bin/env python3
"""
Re-authorize OAuth token to include Google Docs readonly scope.
Run interactively — opens a browser for Google sign-in.
Saves updated token back to gmail_token.json.
"""

import json, os
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

TOKEN_FILE = Path(__file__).parent / "gmail_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Load existing token to get client_id and client_secret
with open(TOKEN_FILE) as f:
    existing = json.load(f)

client_config = {
    "installed": {
        "client_id": existing["client_id"],
        "client_secret": existing["client_secret"],
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0)

# Merge back into existing token format
existing["token"] = creds.token
existing["refresh_token"] = creds.refresh_token
existing["scopes"] = list(creds.scopes)
existing["expiry"] = creds.expiry.isoformat() if creds.expiry else None

with open(TOKEN_FILE, "w") as f:
    json.dump(existing, f, indent=2)

print(f"\nDone — token updated with scopes: {existing['scopes']}")
