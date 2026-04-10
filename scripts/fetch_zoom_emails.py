#!/usr/bin/env python3
"""
scripts/fetch_zoom_emails.py — Fetch unprocessed Zoom meeting summary emails from Gmail.

Searches for Zoom "Meeting assets" emails, extracts the AI summary content,
and outputs JSON for Claude to use in the meeting-notes workflow.

Usage:
    python3 scripts/fetch_zoom_emails.py [--since YYYY-MM-DD] [--limit N] [--all]

Output (stdout): JSON array of meeting objects:
    [
      {
        "gmail_id": "...",
        "meeting_title": "...",
        "date": "YYYY-MM-DD",
        "zoom_summary": "full extracted text of Zoom AI summary"
      },
      ...
    ]

Processed emails are tracked in processed_ids.json so they are not returned again.
Pass --all to ignore the processed list and return everything.
"""

import os
import sys
import json
import base64
import re
import argparse
from datetime import datetime, timedelta
from pathlib import Path

TOKEN_FILE     = os.path.join(os.path.dirname(__file__), "gmail_token.json")
PROCESSED_FILE = os.path.join(os.path.dirname(__file__), "processed_ids.json")
SCOPES         = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_creds():
    import google.oauth2.credentials
    from google.auth.transport.requests import Request
    with open(TOKEN_FILE) as f:
        info = json.load(f)
    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    return set()


def save_processed(ids):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2)


def decode_part(data):
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def extract_text(payload):
    """Recursively extract plain text or stripped HTML from email payload."""
    mime = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return decode_part(data) if data else ""

    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if not data:
            return ""
        html = decode_part(data)
        # Remove <style> and <script> blocks
        html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        # Strip remaining HTML tags
        text = re.sub(r"<[^>]+>", " ", html)
        # Decode HTML entities
        import html as html_module
        text = html_module.unescape(text)
        # Collapse whitespace
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    if parts:
        # Prefer text/plain, fall back to text/html
        for preferred in ("text/plain", "text/html"):
            for part in parts:
                result = extract_text(part) if part.get("mimeType") == preferred else ""
                if result:
                    return result
        # Recurse into multipart
        for part in parts:
            result = extract_text(part)
            if result:
                return result

    return ""


def parse_meeting_title(subject):
    """Extract meeting name from subject like 'Meeting assets for X are ready!'"""
    m = re.search(r"Meeting assets for (.+?) are ready", subject, re.IGNORECASE)
    return m.group(1).strip() if m else subject


def parse_date(date_str):
    """Parse Gmail date header to YYYY-MM-DD."""
    # Normalize: remove parenthetical timezone like "(UTC)"
    cleaned = re.sub(r"\s*\([^)]+\)\s*$", "", date_str.strip())
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S", "%d %b %Y %H:%M:%S"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Fallback: extract YYYY-MM-DD or 4-digit year pattern
    m = re.search(r"\d{4}-\d{2}-\d{2}", date_str)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", date_str)
    if m:
        try:
            dt = datetime.strptime(m.group(0), "%d %b %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return date_str


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", help="Only emails on or after YYYY-MM-DD (default: 30 days ago)")
    parser.add_argument("--limit", type=int, default=20, help="Max emails to fetch (default: 20)")
    parser.add_argument("--all", action="store_true", dest="fetch_all", help="Ignore processed list")
    args = parser.parse_args()

    since = args.since or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    since_ts = datetime.strptime(since, "%Y-%m-%d").strftime("%Y/%m/%d")

    try:
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("Error: google-api-python-client not installed.\nRun: pip install google-api-python-client")

    creds   = get_creds()
    service = build("gmail", "v1", credentials=creds)

    query   = f'from:no-reply@zoom.us subject:"Meeting assets for" after:{since_ts}'
    results = service.users().messages().list(
        userId="me", q=query, maxResults=args.limit
    ).execute()

    messages  = results.get("messages", [])
    processed = load_processed() if not args.fetch_all else set()
    output    = []
    new_ids   = set()

    for msg_meta in messages:
        msg_id = msg_meta["id"]
        if msg_id in processed:
            continue

        msg     = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}

        subject       = headers.get("Subject", "")
        date_str      = headers.get("Date", "")
        meeting_title = parse_meeting_title(subject)
        meeting_date  = parse_date(date_str)
        zoom_summary  = extract_text(msg["payload"])

        output.append({
            "gmail_id":     msg_id,
            "meeting_title": meeting_title,
            "date":         meeting_date,
            "zoom_summary": zoom_summary,
        })
        new_ids.add(msg_id)

    # Update processed list
    if not args.fetch_all and new_ids:
        save_processed(processed | new_ids)

    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
