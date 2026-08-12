#!/usr/bin/env python3
"""
Polled by launchd (com.galen.meeting-notes-runner) every few minutes. Reads the
queue file written by schedule_meeting_notes.py and invokes the meeting-notes
skill for any meeting whose trigger_time has arrived.

Synthesizes from Notion AI content only — no Zoom email check or retry loop.
Each due meeting gets one Claude invocation via notion_only_prompt(); outcome is
classified from the skill's stdout sentinel line.

Claude runs inside a detached tmux session (see invoke_claude), not `claude -p`.
`-p` print mode auto-denies every tool in the permissions 'ask' list (Notion
writes included) with no hook consultation — verified 2026-07-16. A real
interactive session, even fully unattended inside tmux, keeps the
PermissionRequest hook path alive, so ~/.claude/scripts/auto-approve-notion.py
can actually auto-approve the Notion write tools that were blocking every run.

Replaces the previous `at`-based scheduling which fails on modern macOS without
Full Disk Access.

Meeting status transitions:
  pending → fired              (skill wrote + filed the note)
  pending → partial            (note written + filed (Steps 1-4 OK) but Step 5's
                                1:1 hub cross-post failed; macOS alert fired)
  pending → blocked            (skill ran but a Notion write was blocked/denied on
                                a permission grant; nothing filed; macOS alert fired)
  pending → skipped            (skill ran but found no Notion page or empty summary)
  pending → failed             (claude invoked, non-zero exit; macOS alert fired)
  pending → missed             (trigger more than MAX_LATENESS_MINUTES late;
                                never attempted; macOS alert fired)

NOTE: a 0 exit code no longer implies success — the headless skill exits 0 even
when blocked on permission, so outcomes are classified from its output (see
classify_outcome).

Legacy Zoom functions (gmail_service, zoom_asset_exists, reschedule,
normal_prompt, no_zoom_prompt) are kept below for reference but are not called.
"""
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS_DIR   = Path(__file__).parent
SCHEDULE_FILE = Path.home() / ".meeting-notes-schedule.json"
GMAIL_TOKEN   = SCRIPTS_DIR / "gmail_token.json"
LOG_PREFIX    = "[run_due_meeting_notes]"
CLAUDE        = "/Users/galen.pewtherer/.local/bin/claude"
TMUX          = "/opt/homebrew/bin/tmux"
MEETING_NOTES_DIR = "/Users/galen.pewtherer/Claude/meeting-notes"
RUN_LOG       = "/tmp/meeting-notes-poller.log"
PENDING_DIR   = Path("/tmp/meeting-notes-pending")
APPLY_SCRIPT  = str(SCRIPTS_DIR / "open_apply_pending.sh")
MAX_LATENESS_MINUTES = 60
ZOOM_RETRY_MINUTES   = 7
ZOOM_MAX_ATTEMPTS    = 3
# Hard cap on a single run. Without this a wedged `claude` invocation blocks
# the poller indefinitely and starves every later meeting in the queue. A
# timeout kills the tmux session, classified `failed`, and alerts (see main).
CLAUDE_TIMEOUT_SECONDS = 900
# How often to poll the tmux pane for the RESULT: sentinel.
POLL_INTERVAL_SECONDS = 3
GMAIL_SCOPES  = ["https://www.googleapis.com/auth/gmail.readonly"]

# macOS notification helper (dependency-free osascript) lives with the peak-events
# launchd tooling. Import defensively so a missing helper never breaks the runner.
ALERT_DIR = SCRIPTS_DIR.parent.parent / "peak-events" / "scripts"
sys.path.insert(0, str(ALERT_DIR))


def _fallback_notify(title, message, execute=None, action_label=None):
    """No-op stand-in used when peak-events' alert.notify can't be imported.

    The signature MUST mirror alert.notify exactly — maybe_notify's blocked path
    calls notify(..., execute=..., action_label=...), and a stub that only accepts
    (title, message) raises TypeError there. That TypeError propagates out of
    maybe_notify, out of main()'s per-meeting loop, and past the
    SCHEDULE_FILE.write_text() that persists the queue — so every already-processed
    meeting silently reverts to 'pending' and every later due meeting in the batch
    is never attempted, with nothing surfaced. Returns False (never delivered) so
    _notify() logs the lost banner, matching alert.notify's bool contract.
    """
    return False


try:
    from alert import notify
except Exception:
    notify = _fallback_notify

# Phrases indicating the skill could NOT complete the Notion write. The headless
# skill exits 0 even when a write is blocked pending a permission grant, so exit
# code alone is not trustworthy. Lowercase for case-insensitive matching; sourced
# from real /tmp/meeting-notes-poller.log entries.
BLOCK_PHRASES = (
    "permission hasn't been granted",
    "permission has not been granted",
    "permission-pending",
    "permission pending",
    "blocked pending permission",
    "blocked on write permission",
    "the notion write is blocked",
    "awaiting permission",
    "awaiting your permission",
    "hasn't been granted",
    "denied - permission",
    "denied — permission",
)
SKIP_PHRASES = (
    "no notion meeting page was found",
    "no notion meeting page exists",
    "no notion ai page exists",
    "skipping this meeting",
    "skipping the meeting",
    "skipped - no notion page",
    "skipped — no notion page",
)


def _as_text(v):
    """Coerce subprocess stdout/stderr (str | bytes | None) to str. TimeoutExpired
    may carry bytes even under text mode, so normalize defensively."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def _last_assistant_text(output):
    """Return the tail of `output` after the last '⏺' (Claude Code's assistant-
    turn marker). Everything before it is either the echoed user prompt (tmux
    pty captures echo the input line, unlike the old `-p` capture) or an
    earlier turn — and RESULT_SENTINEL_INSTRUCTION itself contains the literal
    strings 'RESULT: SUCCESS' / 'BLOCKED' / 'SKIPPED', so scanning the whole
    blob false-matches before the agent has done anything. Falls back to the
    full text when no marker is present (e.g. old `-p`-captured logs, which
    never echoed the prompt in the first place)."""
    text = output or ""
    last_bullet = text.rfind("⏺")
    return text[last_bullet:] if last_bullet != -1 else text


def classify_outcome(rc, output):
    """Map (exit code, skill stdout+stderr) to: failed | blocked | skipped | partial | success.

    rc != 0 is always 'failed'. Otherwise prefer the explicit `RESULT:` sentinel
    the skill is asked to print, then fall back to known block/skip phrasing.
    Defaults to 'success' so a clean run with no markers is still recorded fired.

    'partial' covers Step 5 (hub cross-post) failing after Steps 1-4 succeeded —
    the meeting note itself was written and filed, but the skill's own contract
    was not fully satisfied, so this must still alert (see should_notify).

    Sentinel precedence is deliberately incomplete-work-first (blocked > partial >
    skipped > success), NOT success-first. An LLM final turn routinely narrates the
    sentinel it is *not* printing ("normally I'd print RESULT: SUCCESS, but the hub
    write failed, so: RESULT: PARTIAL ..."), and a success-first check matches that
    stray mention and silently records a fully successful run — reintroducing exactly
    the silent-drop bug this function exists to prevent. Biasing toward the
    alerting outcome can only over-notify, which is recoverable; under-notifying in
    an unattended launchd run is not."""
    if rc != 0:
        return "failed"
    text = _last_assistant_text(output).lower()
    if "result: blocked" in text:
        return "blocked"
    if "result: partial" in text:
        return "partial"
    if "result: skipped" in text:
        return "skipped"
    if "result: success" in text:
        return "success"
    if any(p in text for p in BLOCK_PHRASES):
        return "blocked"
    if any(p in text for p in SKIP_PHRASES):
        return "skipped"
    return "success"


def status_for(outcome, no_zoom=False):
    """Queue status string for a classified outcome."""
    if outcome == "success":
        return "fired_no_zoom" if no_zoom else "fired"
    return outcome  # blocked | skipped | failed | partial


def should_notify(outcome):
    """Whether to fire a macOS notification so Galen can finish the run manually.

    'partial' (Step 5 hub cross-post failed after Steps 1-4 succeeded) must alert
    too — otherwise a Step-5-only failure prints RESULT: SUCCESS-equivalent silence
    and nobody knows the hub update needs to be finished by hand.

    'missed' (trigger more than MAX_LATENESS_MINUTES late, so never attempted) must
    alert as well: the note is simply never written and, without a banner, nothing
    ever says so. That is the same silent-drop failure class as a blocked write.

    'skipped' does NOT alert — no Notion page / empty summary is a legitimate no-op
    with nothing for a human to finish."""
    return outcome in ("blocked", "failed", "partial", "missed")


def save_pending(meeting, date_str):
    """Save blocked meeting metadata to PENDING_DIR for interactive retry."""
    PENDING_DIR.mkdir(exist_ok=True)
    title = meeting.get("title", "Untitled")
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in title).strip()[:50]
    fname = f"{date_str}_{safe.replace(' ', '_')}.json"
    payload = {
        "title": title,
        "date": date_str,
        # no_zoom is always False in the Notion-only flow (zoom_check_attempts is
        # never set); kept in the payload for schema compatibility with older files.
        "no_zoom": int(meeting.get("zoom_check_attempts", 0)) >= ZOOM_MAX_ATTEMPTS,
        "blocked_at": datetime.now().isoformat(),
    }
    (PENDING_DIR / fname).write_text(json.dumps(payload, indent=2))
    log(f"Saved pending note: {fname}")


def _notify(head, msg, **kwargs):
    """Call notify() and log when nothing was actually delivered.

    alert.notify returns False rather than raising when terminal-notifier and
    osascript both fail, and _fallback_notify always returns False. Every alert here
    exists because a human has to finish the run by hand, so a swallowed banner means
    total silence — log it so /tmp/meeting-notes-poller.log still carries the signal."""
    delivered = notify(head, msg, **kwargs)
    if not delivered:
        log(f"NOTIFICATION NOT DELIVERED: {head} — {msg}")
    return delivered


def maybe_notify(outcome, title, date_str, meeting=None):
    if not should_notify(outcome):
        return
    if outcome == "blocked" and meeting is not None:
        save_pending(meeting, date_str)
    if outcome == "blocked":
        head = "Meeting notes: write blocked"
        msg = f"'{title}' ({date_str}): click Apply to open Claude and finish the write"
        _notify(head, msg, execute=APPLY_SCRIPT, action_label="Apply")
    elif outcome == "partial":
        # Steps 1-4 succeeded (note written + filed) but Step 5's hub cross-post
        # failed — distinct from a real run failure, so the message says so.
        head = "Meeting notes: hub update failed"
        msg = f"'{title}' ({date_str}): note filed, but the 1:1 hub cross-post failed — finish it manually"
        _notify(head, msg)
    elif outcome == "missed":
        # Never attempted: the trigger was already stale when the poller reached it.
        head = "Meeting notes: meeting missed"
        msg = (f"'{title}' ({date_str}): trigger was over {MAX_LATENESS_MINUTES}m late, "
               f"never attempted — process it manually")
        _notify(head, msg)
    else:
        head = "Meeting notes: run failed"
        msg = f"'{title}' ({date_str}): run failed"
        _notify(head, msg)


def log(msg):
    print(f"{datetime.now().isoformat()} {LOG_PREFIX} {msg}", flush=True)


def parse_dt(dt_str):
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def gmail_service():
    import google.oauth2.credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    with open(GMAIL_TOKEN) as f:
        info = json.load(f)
    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(info, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def zoom_asset_exists(service, meeting_title, meeting_date):
    """Read-only Gmail check: is there a 'Meeting assets for <title>' email from
    no-reply@zoom.us dated on or after meeting_date? Returns bool."""
    quoted_title = meeting_title.replace('"', '\\"')
    after = meeting_date.strftime("%Y/%m/%d")
    query = (
        f'from:no-reply@zoom.us '
        f'subject:"Meeting assets for {quoted_title}" '
        f'after:{after}'
    )
    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=1
        ).execute()
        return bool(result.get("messages"))
    except Exception as e:
        log(f"Gmail check failed for '{meeting_title}': {e}")
        return False


# Dedicated socket so this runner's sessions never collide with (or get
# accidentally killed alongside) any tmux server Galen runs interactively.
TMUX_SOCKET = "meeting_notes_runner"


def _tmux(*args):
    """Run a tmux subcommand on the dedicated socket. Returns (returncode, stdout+stderr)."""
    result = subprocess.run(
        [TMUX, "-L", TMUX_SOCKET, *args], capture_output=True, text=True,
    )
    return result.returncode, _as_text(result.stdout) + _as_text(result.stderr)


def _has_result_sentinel(pane_text):
    """True if the RESULT: sentinel appears in the most recent assistant turn
    (see _last_assistant_text) rather than merely echoed back as part of the
    prompt instructions Claude Code displays above the input box."""
    if pane_text.rfind("⏺") == -1:
        return False
    return "result:" in _last_assistant_text(pane_text).lower()


def invoke_claude(prompt, title):
    """Run the skill in a detached tmux session (a real pty) rather than `claude -p`.

    `-p` print mode auto-denies any tool in the permissions 'ask' list with no
    hook consultation — verified empirically 2026-07-16 (see meeting-notes
    CLAUDE.md history / recent_activity.md). A real interactive session, even
    fully unattended inside tmux, keeps the PermissionRequest hook path alive:
    ~/.claude/scripts/auto-approve-notion.py auto-approves the Notion write
    tools that were blocking every headless run. Returns (returncode,
    combined_output) in the same shape callers already expect from the old
    subprocess-based implementation.
    """
    log(f"Invoking claude (tmux) for '{title}'")
    session = f"meeting_notes_{os.getpid()}_{int(time.time())}"

    # Single shell string so the prompt is submitted as claude's first message
    # at launch (avoids a separate readiness-detection step before send-keys).
    # shlex.quote handles any quotes/special characters in the meeting title.
    shell_cmd = (
        f"cd {shlex.quote(MEETING_NOTES_DIR)} && "
        f"HOME={shlex.quote(str(Path.home()))} "
        f"{shlex.quote(CLAUDE)} {shlex.quote(prompt)}"
    )

    rc_create, create_out = _tmux(
        "new-session", "-d", "-s", session, "-x", "220", "-y", "50", shell_cmd
    )
    if rc_create != 0:
        log(f"tmux new-session failed for '{title}': {create_out}")
        return 1, f"[runner] tmux new-session failed: {create_out}"

    # Keep the pane alive after claude exits (crash, early error) so the final
    # screen can still be captured instead of vanishing with the session.
    _tmux("set-option", "-t", session, "remain-on-exit", "on")

    output = ""
    rc = 0
    deadline = time.time() + CLAUDE_TIMEOUT_SECONDS
    try:
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            has_session_rc, _ = _tmux("has-session", "-t", session)
            _, pane = _tmux("capture-pane", "-t", session, "-p", "-S", "-500")
            output = pane
            if _has_result_sentinel(pane):
                # Debounce: grab one more capture in case trailing UI chrome
                # (footer redraw) is still settling.
                time.sleep(1.5)
                _, pane2 = _tmux("capture-pane", "-t", session, "-p", "-S", "-500")
                output = pane2
                rc = 0
                break
            if has_session_rc != 0:
                # Session ended without ever printing a RESULT: sentinel.
                rc = 1
                output += "\n[runner] tmux session ended without a RESULT: sentinel."
                break
        else:
            rc = 124
            output += f"\n[runner] TIMEOUT after {CLAUDE_TIMEOUT_SECONDS}s; session killed."
            log(f"claude timed out after {CLAUDE_TIMEOUT_SECONDS}s for '{title}'")
    finally:
        _tmux("kill-session", "-t", session)

    with open(RUN_LOG, "a") as runlog:
        runlog.write(f"\n===== {datetime.now().isoformat()} '{title}' =====\n")
        runlog.write(output)
        if not output.endswith("\n"):
            runlog.write("\n")
        runlog.flush()
    return rc, output


# The tmux session below is a real interactive pty, so EVERY environmental signal the
# agent can observe says "a user is here" — there is no env var, no --headless flag, no
# tty check that would reveal otherwise. Headlessness therefore has to be stated in the
# prompt itself, or CLAUDE.md's "in a headless run, skip instead of asking" branch can
# never fire: the agent calls AskUserQuestion, blocks on a prompt nobody will ever
# answer, and the run burns CLAUDE_TIMEOUT_SECONDS before being killed as `failed`.
HEADLESS_INSTRUCTION = (
    " IMPORTANT: this is an unattended headless run launched by launchd — there is no "
    "user present to answer prompts, and any question you ask will simply time out. "
    "Never call AskUserQuestion (or any other interactive prompt) at any point. Where the "
    "workflow says to ask Galen — in particular Step 5's ambiguous stakeholder match — "
    "skip that step instead and print 'RESULT: PARTIAL ambiguous stakeholder match for "
    "\"<first name>\" (<candidate hub page titles>)' so the runner alerts him to finish "
    "it by hand."
)

RESULT_SENTINEL_INSTRUCTION = (
    " When you are completely done, print as the very last line exactly one of: "
    "'RESULT: SUCCESS' if the synthesis was written to the Notion page and the page "
    "was filed/re-parented; 'RESULT: PARTIAL <reason>' if that succeeded but Step 5 "
    "(the 1:1 hub cross-post) did not complete — whether it failed or you skipped it "
    "because it needed a human; 'RESULT: BLOCKED <reason>' if any Notion write "
    "was blocked, denied, or left pending on a permission grant; or "
    "'RESULT: SKIPPED <reason>' if there was no Notion page to write to."
)


def notion_only_prompt(title, date_str):
    return (
        f"Follow the meeting-notes workflow already loaded from this directory's "
        f"CLAUDE.md — it is in your context, execute its steps directly. Do NOT "
        f"invoke any Skill tool (in particular, do not use the tpm:meeting-notes "
        f"plugin skill — that is a different, unrelated workflow). "
        f"Process the meeting titled \"{title}\" on {date_str}. "
        f"Use the Notion AI content as the sole synthesis input. "
        f"Do not create a page if no Notion page exists or the summary is empty — "
        f"stop and output the SKIPPED sentinel instead. "
        f"Process ONLY this one meeting — do not process other meetings you may find."
        + HEADLESS_INSTRUCTION
        + RESULT_SENTINEL_INSTRUCTION
    )


# Legacy prompts kept for reference — superseded by notion_only_prompt above.
def normal_prompt(title, date_str):
    return (
        f"Run the meeting-notes skill for the meeting titled \"{title}\" "
        f"on {date_str}. Zoom AI assets are available in Gmail for this "
        f"meeting. In Step 1, run fetch_zoom_emails.py and select the email "
        f"that matches this meeting title and date. Process ONLY this one "
        f"meeting — do not process other unprocessed meetings you may find."
        + RESULT_SENTINEL_INSTRUCTION
    )


def no_zoom_prompt(title, date_str):
    return (
        f"Run the meeting-notes skill for the meeting titled \"{title}\" "
        f"on {date_str}. There are NO Zoom AI assets in Gmail for this "
        f"meeting (checked {ZOOM_MAX_ATTEMPTS} times, {ZOOM_RETRY_MINUTES} "
        f"minutes apart). Skip Step 1 of the skill. In Step 2, find the "
        f"Notion meeting page by title and date as usual. In Step 4, write "
        f"the synthesis from the Notion AI content alone and prepend this "
        f"callout at the very top of the page body:\n\n"
        f"> ⚠️ No Zoom AI assets were available for this meeting "
        f"(no recap email in Gmail after {ZOOM_MAX_ATTEMPTS} checks). Synthesis below is "
        f"sourced from the Notion AI content only."
        + RESULT_SENTINEL_INSTRUCTION
    )


def reschedule(meeting, attempts):
    new_trigger = datetime.now().astimezone() + timedelta(minutes=ZOOM_RETRY_MINUTES)
    meeting["trigger_time"] = new_trigger.isoformat()
    meeting["zoom_check_attempts"] = attempts


def main():
    if not SCHEDULE_FILE.exists():
        log("No schedule file; nothing to do")
        return

    try:
        schedule = json.loads(SCHEDULE_FILE.read_text())
    except json.JSONDecodeError as e:
        log(f"Failed to parse {SCHEDULE_FILE}: {e}")
        return

    meetings = schedule.get("meetings", [])
    if not meetings:
        return

    now = datetime.now().astimezone()
    cutoff = now - timedelta(minutes=MAX_LATENESS_MINUTES)
    changed = False
    counters = {"fired": 0, "blocked": 0, "skipped": 0, "partial": 0, "missed": 0, "failed": 0}

    for m in meetings:
        if m.get("status") != "pending":
            continue

        trigger_dt = parse_dt(m["trigger_time"])
        if trigger_dt > now:
            continue

        title = m.get("title", "Untitled")
        end_dt = parse_dt(m["end_time"])
        meeting_date = end_dt.astimezone().date()

        if trigger_dt < cutoff:
            log(f"Skipping '{title}' — past max lateness ({MAX_LATENESS_MINUTES}m)")
            m["status"] = "missed"
            counters["missed"] += 1
            # A dropped meeting is never written up at all — alert, or it vanishes
            # silently into the queue file (see should_notify).
            maybe_notify("missed", title, meeting_date.isoformat())
            changed = True
            continue

        rc, output = invoke_claude(notion_only_prompt(title, meeting_date.isoformat()), title)
        outcome = classify_outcome(rc, output)
        status = status_for(outcome)
        m["status"] = status
        counters[status] = counters.get(status, 0) + 1
        maybe_notify(outcome, title, meeting_date.isoformat(), m)
        changed = True

    if changed:
        SCHEDULE_FILE.write_text(json.dumps(schedule, indent=2))
        log("Updated queue: " + ", ".join(f"{k}={v}" for k, v in counters.items() if v))


if __name__ == "__main__":
    main()
