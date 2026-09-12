#!/usr/bin/env python3
"""Quick script to inspect the structure of a recent Zoom recording email.

Auth: gws_google_client.py (the `gws` CLI) -- migrated 2026-09-12 off the personal
gmail_token.json OAuth token, whose backing GCP project ("Peak Events") is being
decommissioned 2026-09-30. See ~/.claude/plans/shiny-shimmying-popcorn.md.
"""

import base64

from gws_google_client import gmail_messages_list, gmail_messages_get


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
    # Search for Zoom recording emails
    results = gmail_messages_list(q='from:no-reply@zoom.us subject:"Cloud Recording"', max_results=1)

    messages = results.get("messages", [])
    if not messages:
        print("No Zoom recording emails found.")
        return

    msg_id = messages[0]["id"]
    msg    = gmail_messages_get(msg_id, format="full")

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
