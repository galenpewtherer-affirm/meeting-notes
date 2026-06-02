#!/usr/bin/env python3
"""
Get a Gmail compose token for writing drafts.
Saves gmail_drafts_token.json alongside gmail_token.json.

Run this once before using push_vendor_drafts.py.
Opens a browser for Google sign-in.
"""

import json
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

GMAIL_TOKEN  = Path(__file__).parent / "gmail_token.json"
OUTPUT_TOKEN = Path(__file__).parent / "gmail_drafts_token.json"

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]

with open(GMAIL_TOKEN) as f:
    existing = json.load(f)

client_config = {
    "installed": {
        "client_id":     existing["client_id"],
        "client_secret": existing["client_secret"],
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
        "token_uri":     "https://oauth2.googleapis.com/token",
    }
}

flow  = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0)

token_data = {
    "client_id":     existing["client_id"],
    "client_secret": existing["client_secret"],
    "token":         creds.token,
    "refresh_token": creds.refresh_token,
    "scopes":        list(creds.scopes),
    "expiry":        creds.expiry.isoformat() if creds.expiry else None,
    "token_uri":     "https://oauth2.googleapis.com/token",
}

with open(OUTPUT_TOKEN, "w") as f:
    json.dump(token_data, f, indent=2)

print(f"\nDone — saved to: {OUTPUT_TOKEN}")
print(f"Scopes: {token_data['scopes']}")
