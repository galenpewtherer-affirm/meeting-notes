#!/usr/bin/env python3
"""Quick script to inspect the structure of a recent Zoom recording email."""

import os, sys, json, base64
from pathlib import Path

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "gmail_token.json")
SCOPES     = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_creds():
    import google.oauth2.credentials
    from google.auth.transport.requests import Request
    with open(TOKEN_FILE) as f:
        info = json.load(f)
    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def decode_body(data):
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def walk_parts(parts, depth=0):
    for part in parts:
        mime = part.get("mimeType", "")
        filename = part.get("filename", "")
        body = part.get("body", {})
        size = body.get("size", 0)
        prefix = "  " * depth
        print(f"{prefix}mimeType: {mime}  filename: {repr(filename)}  size: {size}")
        if mime in ("text/plain", "text/html") and body.get("data"):
            text = decode_body(body["data"])
            print(f"{prefix}  >> first 500 chars: {repr(text[:500])}")
        sub = part.get("parts", [])
        if sub:
            walk_parts(sub, depth + 1)


def main():
    from googleapiclient.discovery import build
    creds   = get_creds()
    service = build("gmail", "v1", credentials=creds)

    # Search for Zoom recording emails
    results = service.users().messages().list(
        userId="me",
        q='from:no-reply@zoom.us subject:"Cloud Recording"',
        maxResults=1
    ).execute()

    messages = results.get("messages", [])
    if not messages:
        print("No Zoom recording emails found.")
        return

    msg_id = messages[0]["id"]
    msg    = service.users().messages().get(userId="me", id=msg_id, format="full").execute()

    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    print(f"Subject: {headers.get('Subject')}")
    print(f"From:    {headers.get('From')}")
    print(f"Date:    {headers.get('Date')}")
    print(f"\nPayload structure:")
    parts = msg["payload"].get("parts", [])
    if parts:
        walk_parts(parts)
    else:
        body = msg["payload"].get("body", {})
        if body.get("data"):
            text = decode_body(body["data"])
            print(f"(single part, mimeType: {msg['payload'].get('mimeType')})")
            print(f"first 500 chars: {repr(text[:500])}")


if __name__ == "__main__":
    main()
