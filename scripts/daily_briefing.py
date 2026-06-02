#!/usr/bin/env python3
"""
Runs daily at 7am via launchd. Fetches recent relevant Gmail messages,
writes them to /tmp/gmail_briefing.json, then calls claude -p to run
the daily briefing skill and create a Notion page.
"""
import json
import sys
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR   = Path(__file__).parent
TOKEN_FILE    = SCRIPTS_DIR / "gmail_token.json"
SCOPES        = ["https://www.googleapis.com/auth/gmail.readonly"]
OUTPUT_FILE   = Path("/tmp/gmail_briefing.json")
CLAUDE        = Path.home() / ".local/bin/claude"
BRIEFING_DIR  = Path.home() / "Claude/daily-briefing"
LOG_PREFIX    = "[daily_briefing]"

KEYWORDS = [
    "peak event", "peak-event", "0% day", "0 percent day",
    "reliability", "availability", "incident", "tpm", "program manager",
    "orion", "runbook", "vendor", "Jira", "PEAK-", "on-call", "oncall",
    "scale up", "scale down", "freeze",
]

IMPORTANT_SENDERS = [
    "vivek", "geddes", "jeffery.kline", "andrew.cheng",
    "jumio", "lnrs", "stripe", "telesign", "twilio", "transunion",
]


def log(msg):
    print(f"{datetime.now().isoformat()} {LOG_PREFIX} {msg}", flush=True)


def is_monday():
    return datetime.now().weekday() == 0


def get_creds():
    import google.oauth2.credentials
    from google.auth.transport.requests import Request

    with open(TOKEN_FILE) as f:
        info = json.load(f)
    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def build_query(lookback_hours):
    cutoff = datetime.now() - timedelta(hours=lookback_hours)
    after_epoch = int(cutoff.timestamp())
    keyword_part = " OR ".join(f'"{k}"' for k in KEYWORDS[:10])  # Gmail query length limit
    sender_part  = " OR ".join(f"from:{s}" for s in IMPORTANT_SENDERS)
    return f"({keyword_part} OR {sender_part}) after:{after_epoch} -category:promotions -category:social"


def fetch_emails(service, query, max_results=40):
    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()
    return result.get("messages", [])


def get_message_detail(service, msg_id):
    msg = service.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "gmail_id": msg_id,
        "date": headers.get("Date", ""),
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", "(no subject)"),
        "snippet": msg.get("snippet", ""),
    }


def main():
    monday = is_monday()
    lookback_hours = 72 if monday else 24
    log(f"Starting daily briefing — lookback {lookback_hours}h (Monday={monday})")

    try:
        from googleapiclient.discovery import build
        creds = get_creds()
        service = build("gmail", "v1", credentials=creds)

        query = build_query(lookback_hours)
        log(f"Gmail query: {query[:120]}...")

        messages = fetch_emails(service, query)
        log(f"Found {len(messages)} matching emails")

        emails = []
        for m in messages:
            try:
                detail = get_message_detail(service, m["id"])
                emails.append(detail)
            except Exception as e:
                log(f"Warning: could not fetch {m['id']}: {e}")

    except Exception as e:
        log(f"Gmail fetch failed: {e}. Writing empty email list.")
        emails = []

    payload = {
        "lookback_hours": lookback_hours,
        "generated_at": datetime.now().isoformat(),
        "emails": emails,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2))
    log(f"Wrote {len(emails)} emails to {OUTPUT_FILE}")

    log("Calling claude -p to run daily briefing skill")
    result = subprocess.run(
        [str(CLAUDE), "-p", "run the daily briefing skill", "--dangerously-skip-permissions"],
        cwd=str(BRIEFING_DIR),
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "HOME": str(Path.home())},
    )

    if result.returncode != 0:
        log(f"claude -p failed (exit {result.returncode}):")
        log(result.stderr[-2000:] if result.stderr else "(no stderr)")
        _notify("Daily Briefing Failed", f"claude -p exited {result.returncode} — check /tmp/daily-briefing.log")
        sys.exit(1)

    output = result.stdout or ""
    log("Daily briefing complete")
    if output:
        log(output[-500:])

    if "BRIEFING_CREATED:" not in output:
        log("WARNING: No BRIEFING_CREATED sentinel in output — page may not have been created")
        _notify("Daily Briefing: Notion page missing", "Briefing ran but no Notion page was created. Notion MCP may have dropped — rerun manually.")


def _notify(title, message):
    """Send a macOS notification. Fails silently if osascript is unavailable."""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
