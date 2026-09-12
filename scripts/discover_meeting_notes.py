#!/usr/bin/env python3
"""
Runs via launchd (com.galen.meeting-notes-scheduler, hourly 8am-5pm). Replaces
schedule_meeting_notes.py's Calendar-based discovery entirely -- see
~/.claude/plans/shiny-shimmying-popcorn.md for the "why": Notion AI already
creates a page per meeting from the live transcript, so that page's existence
(and whether its <summary> is populated yet) is a strictly better "did a
meeting happen and is it ready" signal than Calendar + a fixed 5-minute guess,
and it permanently removes the Google Calendar OAuth dependency (no admin
ticket, no fallback project, nothing to revisit).

Writes the exact same queue file (~/.meeting-notes-schedule.json, same shape)
that run_due_meeting_notes.py already reads -- that script needs ZERO changes;
it never cared how the queue got populated.

Why this isn't a plain Python-to-Notion API call: unlike `gws`, the Notion MCP
tools are only reachable from inside a live `claude` session -- there is no
installed Notion CLI and no NOTION_API_TOKEN configured anywhere in this
workspace for a standalone script to call directly (confirmed: even
run_due_meeting_notes.py, the existing "talk to Notion" script, never touches
Notion in Python -- it only ever spawns a real claude session for that). So
this script spawns one small, narrow discovery invocation via
run_due_meeting_notes.invoke_claude() (the same tmux mechanism already used
for firing) whose ONLY job is to query Notion and report back candidates as a
single JSON line -- never to write anything. The actual merge/dedup logic
below is pure Python and unit-tested without needing a live Notion call.

Safe to run more than once per day, same guarantee as schedule_meeting_notes.py's
merge_schedule() (see that file's docstring for the duplicate-fire bug this
prevents) -- plus one addition: a "skipped" entry (Notion's own
notion_only_prompt() tells the firing skill to output SKIPPED if the summary
isn't populated yet) gets retried on subsequent runs instead of being treated
as terminal, since "not ready yet" and "will never be ready" look identical
from the schedule file alone. Retries stop once the meeting falls outside
RETRY_WINDOW_DAYS of its own created_time -- avoids retrying forever for a
transcript that will genuinely never populate.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_due_meeting_notes import invoke_claude, CLAUDE_TIMEOUT_SECONDS  # noqa: E402

SCHEDULE_FILE = Path.home() / ".meeting-notes-schedule.json"
LOG_PREFIX = "[discover_meeting_notes]"
DISCOVERY_LOOKBACK_DAYS = 2   # how far back the Notion query looks each run
RETRY_WINDOW_DAYS = 2         # how long a "not ready yet" entry keeps getting retried
CANDIDATES_MARKER = "CANDIDATES_JSON:"
RETRYABLE_STATUSES = {"skipped", "fired_no_zoom"}


def log(msg):
    print(f"{datetime.now().isoformat()} {LOG_PREFIX} {msg}", flush=True)


def discovery_prompt():
    # Filter shape verified live 2026-09-12 against the real notion-query-meeting-notes
    # tool (two earlier hand-guessed shapes were rejected by its schema before this
    # one worked -- built with json.dumps here, not hand-written, to avoid a repeat).
    filter_json = json.dumps({
        "operator": "and",
        "filters": [{
            "property": "created_time",
            "filter": {
                "operator": "date_is_within",
                "value": {
                    "type": "relative",
                    "value": "custom",
                    "direction": "past",
                    "unit": "day",
                    "count": DISCOVERY_LOOKBACK_DAYS,
                },
            },
        }],
    })
    return (
        "You are ONLY discovering candidate meetings for later processing by a "
        "separate step -- do NOT write to Notion, do NOT run the meeting-notes "
        "synthesis workflow, do NOT use the Skill tool. "
        f"Call notion-query-meeting-notes with this exact filter argument: {filter_json} "
        "Each result row has fields \"Title\", \"Created time\" (ISO8601 UTC), and "
        "\"url\" -- url is the only real per-row identifier, use it as-is. "
        "For EVERY row returned, fetch it (notion-fetch) and judge its content using "
        "the exact rule from this directory's CLAUDE.md Step 1 (judge the <summary> "
        "block, never the raw transcript/<notes> block): a row is EMPTY/not ready "
        "only if there is no real synthesized or transcript-derived content yet -- "
        "missing/whitespace, or Notion's no-content boilerplate ('It looks like your "
        "transcript and notes are empty this time around' / 'could not be generated "
        "due to insufficient transcript'), or literally nothing but a 'Filing target: "
        "...' stub with no other content. A page that already has real content of any "
        "kind (a synthesized ## Action Items structure from an earlier run, a raw "
        "transcript, or a Notion AI summary) is ready=true -- being already-synthesized "
        "does not make it not-ready, that distinction is handled by a separate step, "
        "not by you. "
        "Then, as the very last lines of your final turn, print EXACTLY one line "
        f"starting with '{CANDIDATES_MARKER} ' followed by a single-line compact "
        "JSON array (no pretty-printing, no other text on that line), one object "
        "per row: {\"id\": <the row's url field, verbatim>, \"title\": <the row's "
        "Title field, with any trailing '@<Date> <Time>' suffix stripped if present, "
        "otherwise used as-is>, \"created_time\": <the row's Created time field, "
        "verbatim>, \"ready\": true or false}. If there are zero rows, print "
        f"'{CANDIDATES_MARKER} []'. "
        "Then print exactly one of: 'RESULT: SUCCESS' or 'RESULT: FAILED <reason>'."
    )


def extract_candidates(output):
    """Returns the parsed candidates list, or None if the marker is
    missing/unparseable (caller must treat None as "discovery failed, don't
    touch the schedule file" -- never as "zero candidates").

    Confirmed live 2026-09-12: tmux's captured pane hard-wraps long lines at
    the pty width (220 cols), splitting the JSON mid-string at a word boundary
    (e.g. "...\"title\":\"Zoom\\n  Meeting\",...") -- a naive single-line
    split/json.loads chokes on the fragment. Collapsing all whitespace
    (including the wrap-introduced newlines and tmux's re-indented
    continuation-line lead-in spaces) back to single spaces before parsing
    reconstructs the original line, since Claude Code's TUI wraps at word
    boundaries, never mid-token.
    """
    idx = output.rfind(CANDIDATES_MARKER)
    if idx == -1:
        return None
    tail = output[idx + len(CANDIDATES_MARKER):]
    result_idx = tail.find("RESULT:")
    if result_idx != -1:
        tail = tail[:result_idx]
    collapsed = " ".join(tail.split())
    start, end = collapsed.find("["), collapsed.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        candidates = json.loads(collapsed[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(candidates, list):
        return None
    return candidates


def parse_dt(dt_str):
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def load_existing_schedule():
    if not SCHEDULE_FILE.exists():
        return None
    try:
        return json.loads(SCHEDULE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def merge_schedule(existing, candidates, today_str, log_fn=None):
    """Pure merge logic (no I/O) -- mirrors schedule_meeting_notes.py's
    merge_schedule(), keyed by Notion page id instead of Calendar event id, plus
    the skipped/fired_no_zoom retry-with-expiry behavior described in the module
    docstring.

    existing: the previously-loaded schedule dict (or None), only honored if its
      "date" matches today_str.
    candidates: this run's discovery output -- list of
      {"id", "title", "created_time", "ready"} dicts.
    """
    log_fn = log_fn or (lambda msg: None)
    existing_by_id = {}
    if existing and existing.get("date") == today_str:
        existing_by_id = {m["id"]: m for m in existing.get("meetings", [])}

    now = datetime.now().astimezone()
    seen_ids = set()
    meetings = []
    new_count = 0
    updated_count = 0

    for cand in candidates:
        cid = cand["id"]
        prior = existing_by_id.get(cid)
        status = prior.get("status") if prior else None

        if status not in (None, "pending", *RETRYABLE_STATUSES):
            # Firmly done (fired/blocked/failed/stuck/missed) -- never touch again.
            meetings.append(prior)
            seen_ids.add(cid)
            continue

        if status in RETRYABLE_STATUSES:
            created = parse_dt(cand["created_time"])
            if (now - created) > timedelta(days=RETRY_WINDOW_DAYS):
                # Given up -- outside the retry window, leave it terminal.
                meetings.append(prior)
                seen_ids.add(cid)
                continue

        if not cand.get("ready"):
            log_fn(f"Not ready yet, will retry: {cand.get('title', 'Untitled')}")
            continue  # carried forward below if previously tracked; else just absent

        entry = {
            "id": cid,
            "title": cand.get("title", "Untitled"),
            # Named end_time for run_due_meeting_notes.py compatibility (it derives
            # meeting_date from this field) -- it's really the Notion page's
            # created_time, not a meeting end time; there's no calendar here.
            "end_time": cand["created_time"],
            # Readiness is already confirmed above, so fire on the very next poll.
            "trigger_time": now.isoformat(),
            "status": "pending",
        }
        if prior:
            updated_count += 1
        else:
            new_count += 1
        meetings.append(entry)
        seen_ids.add(cid)

    # A previously-tracked meeting missing from this scan (Notion query hiccup,
    # or it aged out of the lookback window) is carried forward, not dropped.
    for cid, prior in existing_by_id.items():
        if cid not in seen_ids:
            meetings.append(prior)

    return {
        "date": today_str,
        "meetings": meetings,
        "_new_count": new_count,
        "_updated_count": updated_count,
    }


def main():
    log("Starting Notion discovery scan")
    today_str = datetime.now().strftime("%Y-%m-%d")

    rc, output, reason = invoke_claude(discovery_prompt(), "discovery")
    if rc != 0:
        log(f"Discovery invocation did not complete cleanly (rc={rc}, {reason}); "
            f"leaving schedule file untouched, will retry next run")
        return

    candidates = extract_candidates(output)
    if candidates is None:
        log("Could not find/parse CANDIDATES_JSON in discovery output; "
            "leaving schedule file untouched, will retry next run")
        return

    log(f"Discovered {len(candidates)} candidate meeting note(s)")

    existing = load_existing_schedule()
    schedule = merge_schedule(existing, candidates, today_str, log_fn=log)
    new_count = schedule.pop("_new_count")
    updated_count = schedule.pop("_updated_count")

    SCHEDULE_FILE.write_text(json.dumps(schedule, indent=2))
    log(f"{new_count} new + {updated_count} updated meeting(s), "
        f"{len(schedule['meetings'])} total tracked for today; "
        f"runner will fire pending ones at trigger time")


if __name__ == "__main__":
    main()
