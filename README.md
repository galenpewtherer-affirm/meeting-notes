# Meeting Notes Workflow — Setup Guide

Automatically combines your Zoom AI meeting summaries (from email) with Notion AI
meeting notes into a single high-quality summary, written back to your Notion page.

You run it by telling Claude: **"Process my meeting notes"** (or use `/meeting-notes`).
Claude handles the rest.

---

## What it does

After every meeting, Zoom emails you an AI summary. Notion also captures AI meeting
notes. This workflow pulls both together into one clean, structured summary —
Quick Recap, Key Discussion Points, Action Items, and Additional Context — and
writes it directly onto your Notion meeting page.

---

## One-time setup

### Step 1 — Install Claude Code

If you don't have it already:

```
npm install -g @anthropic/claude-code
```

Or download the desktop app from [claude.ai/code](https://claude.ai/code).

### Step 2 — Install Python dependencies

Open Terminal and run:

```
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
```

If `pip` isn't found, try `pip3` instead.

### Step 3 — Connect Gmail

This is a one-time step that lets the workflow read your Zoom summary emails.

1. Make sure `scripts/credentials.json` exists (copied from the peak-events project)
2. Run:
   ```
   python3 scripts/setup_gmail_auth.py
   ```
3. A browser window will open — sign in with your Google account and click Allow
4. Done. A `gmail_token.json` file is saved automatically and used from now on

> The script only requests **read-only** access to Gmail. It cannot send, delete,
> or modify anything.

### Step 4 — Connect Notion

The workflow writes summaries back to your Notion pages via the Notion MCP server.

In Claude Code, run:
```
/mcp
```
and follow the prompts to add the Notion integration if it isn't already connected.
You'll need a Notion API token from [notion.so/my-integrations](https://www.notion.so/my-integrations).

---

## How to run it

Open this folder in Claude Code and say:

> **"Process my meeting notes"**

Or use the slash command:

> **`/meeting-notes`**

Claude will:
1. Fetch any unprocessed Zoom summary emails from the past 30 days
2. Find the matching Notion meeting page for each one
3. Write a combined summary back to each Notion page
4. Tell you what was updated and flag anything it couldn't find

### Options

| What you want | What to say |
|---|---|
| Process new meetings only | "Process my meeting notes" |
| Reprocess everything from the past month | "Process all meeting notes from the past month" |
| Go further back | "Process meeting notes since March 1" |

---

## Troubleshooting

**"No new meetings to process"**
Either there are no unprocessed Zoom emails in the past 30 days, or the Gmail
token has expired. Re-run `setup_gmail_auth.py` to refresh it.

**"Could not find a Notion page for [meeting]"**
The meeting title in the Zoom email didn't match any Notion page. This usually
happens with generic titles like "Zoom Meeting". Tell Claude the correct Notion
page URL and it can process it directly.

**Python not found**
Try `python3` instead of `python`, or install Python from [python.org](https://python.org).

**Gmail token expired**
Tokens expire after 30 days of inactivity. Re-run:
```
python3 scripts/setup_gmail_auth.py
```

---

## Files in this project

```
meeting-notes/
├── README.md              ← you are here
├── CLAUDE.md              ← instructions Claude follows (don't edit)
└── scripts/
    ├── fetch_zoom_emails.py    ← fetches Zoom summaries from Gmail
    ├── setup_gmail_auth.py     ← one-time Gmail authentication
    ├── credentials.json        ← Google API credentials (keep private)
    ├── gmail_token.json        ← your Gmail auth token (keep private)
    └── processed_ids.json      ← tracks which emails have been processed
```

> **Keep `credentials.json` and `gmail_token.json` private.** Don't share or
> commit them — they give access to your Gmail account.
