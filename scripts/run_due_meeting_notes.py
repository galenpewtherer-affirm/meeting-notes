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
  pending → blocked            (skill ran but a Notion write was blocked/denied on
                                a permission grant; nothing filed; macOS alert fired)
  pending → skipped            (skill ran but found no Notion page or empty summary)
  pending → stuck              (claude never produced an assistant turn and the pane
                                stopped changing — parked at an interactive prompt
                                nobody answered; bailed early, macOS alert fired)
  pending → failed             (claude invoked, non-zero exit some other way — tmux
                                session died or ran the full CLAUDE_TIMEOUT_SECONDS
                                still working; macOS alert fired)
  pending (unchanged)          (meeting-notes dir isn't trusted — see
                                _trust_dialog_accepted — so this cycle's due meetings
                                are deferred rather than attempted; retried once trust
                                is re-accepted)
  pending → missed             (trigger more than MAX_LATENESS_MINUTES late;
                                never attempted)

NOTE: a 0 exit code no longer implies success — the headless skill exits 0 even
when blocked on permission, so outcomes are classified from its output (see
classify_outcome). failed/stuck/blocked alerts all carry a one-click 'Open'
action (see maybe_notify) and, for failed/stuck, a `reason` string describing
what the captured pane showed.

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
# Early-bail thresholds for "stuck at an interactive prompt" detection (see
# invoke_claude): a session that has never printed an assistant turn (no
# '⏺') and whose pane content has stopped changing is almost always sitting
# at a prompt with nobody there to answer it (first-run trust dialog, a
# permission menu the hook didn't catch, etc). No point burning the full
# CLAUDE_TIMEOUT_SECONDS on that — bail after GRACE + STABLE seconds instead.
STUCK_PROMPT_GRACE_SECONDS  = 30
STUCK_PROMPT_STABLE_SECONDS = 15
GMAIL_SCOPES  = ["https://www.googleapis.com/auth/gmail.readonly"]
CLAUDE_CONFIG = Path.home() / ".claude.json"
# Cooldown so a persistently-untrusted directory alerts once per window
# instead of spamming every poll cycle until Galen fixes it.
TRUST_ALERT_COOLDOWN_MINUTES = 30
TRUST_ALERT_MARKER = Path("/tmp/meeting-notes-trust-alert-last")

# macOS notification helper (dependency-free osascript) and the durable
# pending-writes ledger both live with the peak-events launchd tooling.
# Import defensively so a missing helper never breaks the runner.
ALERT_DIR = SCRIPTS_DIR.parent.parent / "peak-events" / "scripts"
sys.path.insert(0, str(ALERT_DIR))
try:
    from alert import notify
except Exception:
    def notify(title, message):  # no-op fallback
        pass
try:
    from pending_writes import record as record_pending
except Exception:
    def record_pending(*a, **k):  # no-op fallback
        return None

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
    """Map (exit code, skill stdout+stderr) to: stuck | failed | blocked | skipped | success.

    rc == 125 (invoke_claude's early-bail code) is always 'stuck'. Any other
    rc != 0 is 'failed'. Otherwise prefer the explicit `RESULT:` sentinel the
    skill is asked to print, then fall back to known block/skip phrasing.
    Defaults to 'success' so a clean run with no markers is still recorded fired."""
    if rc == 125:
        return "stuck"
    if rc != 0:
        return "failed"
    text = _last_assistant_text(output).lower()
    if "result: success" in text:
        return "success"
    if "result: blocked" in text:
        return "blocked"
    if "result: skipped" in text:
        return "skipped"
    if any(p in text for p in BLOCK_PHRASES):
        return "blocked"
    if any(p in text for p in SKIP_PHRASES):
        return "skipped"
    return "success"


def status_for(outcome, no_zoom=False):
    """Queue status string for a classified outcome."""
    if outcome == "success":
        return "fired_no_zoom" if no_zoom else "fired"
    return outcome  # blocked | skipped | failed


def should_notify(outcome):
    """Whether to fire a macOS notification so Galen can finish the run manually."""
    return outcome in ("blocked", "failed", "stuck")


def save_pending(meeting, date_str, output=None):
    """Save blocked meeting metadata to PENDING_DIR for interactive retry, and
    mirror it into the durable pending_writes ledger (data/pending_writes/ in
    peak-events, not /tmp) so an unresolved block re-surfaces every day via
    check_pending_writes.py instead of relying solely on the one-shot alert
    below. The source (Notion AI's own summary block) persists independently
    and can be re-synthesized on retry, so this is a visibility safety net,
    not the only copy of any content."""
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
    entry_id = record_pending(
        job="meeting-notes-runner",
        description=f"'{title}' ({date_str}): Notion write blocked - rerun via {APPLY_SCRIPT}",
        content=output,
    )
    if entry_id:
        log(f"Recorded pending-write entry {entry_id}")


def maybe_notify(outcome, title, date_str, meeting=None, reason=None, output=None):
    """Fire a macOS alert for an outcome that needs Galen's attention.

    Every branch gets a one-click 'Open' action (APPLY_SCRIPT: opens Terminal
    into the meeting-notes dir with an interactive `claude` session already
    running) — the fix for a blocked write, a stuck prompt, and a generic
    crash/timeout all start with "open an interactive session and look."
    failed/stuck messages also carry `reason` (built in invoke_claude from the
    captured pane, e.g. which prompt it's stuck at, or a tail of the last
    output) so the notification itself says why, not just that it happened.
    """
    if not should_notify(outcome):
        return
    if outcome == "blocked" and meeting is not None:
        save_pending(meeting, date_str, output)

    if outcome == "blocked":
        head = "Meeting notes: write blocked"
        msg = f"'{title}' ({date_str}): click Open to finish the write"
    elif outcome == "stuck":
        head = "Meeting notes: stuck at a prompt"
        detail = reason or "no assistant activity yet"
        msg = f"'{title}' ({date_str}): {detail} — click Open to resolve"
    else:  # failed
        head = "Meeting notes: run failed"
        detail = reason or "run failed"
        msg = f"'{title}' ({date_str}): {detail} — click Open to investigate"

    notify(head, msg, execute=APPLY_SCRIPT, action_label="Open")


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


def _pane_tail(text, max_lines=3, max_len=160):
    """Last few non-blank lines of captured pane text, joined and truncated
    to a length that fits in a macOS notification body."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    tail = " | ".join(lines[-max_lines:])
    if len(tail) > max_len:
        tail = tail[: max_len - 1] + "…"
    return tail


def _describe_stuck_prompt(pane_text):
    """Human-readable reason for an early-bail 'stuck' classification.
    Names the known first-run trust dialog specifically (the actual cause
    behind the 2026-08-18 timeouts); falls back to a generic description
    plus a tail excerpt for anything else that presents the same way."""
    lower = pane_text.lower()
    if "trust this folder" in lower or "trust the files in this folder" in lower:
        return "stuck at Claude Code's first-run trust-folder prompt (nobody there to accept it)"
    tail = _pane_tail(pane_text)
    if tail:
        return f"stuck at an interactive prompt with no activity yet: \"{tail}\""
    return "stuck at an interactive prompt with no activity yet"


def _trust_dialog_accepted():
    """Best-effort read of Claude Code's per-project trust state for the
    meeting-notes directory. Returns True if unknown/unreadable — a failed
    *check* must never itself block a real run, only a confirmed False does."""
    try:
        data = json.loads(CLAUDE_CONFIG.read_text())
        project = data.get("projects", {}).get(MEETING_NOTES_DIR, {})
        return bool(project.get("hasTrustDialogAccepted", True))
    except Exception:
        return True


def _maybe_alert_trust_needed():
    """Cooldown-limited alert for a confirmed-untrusted meeting-notes dir.
    Checked once per poll cycle (see main) before attempting any due meeting,
    so a whole cycle's worth of meetings gets one alert instead of one per
    meeting, and doesn't waste 15 minutes per meeting discovering it the hard
    way like the 2026-08-18 timeouts did."""
    now = time.time()
    if TRUST_ALERT_MARKER.exists():
        try:
            last = float(TRUST_ALERT_MARKER.read_text().strip())
            if now - last < TRUST_ALERT_COOLDOWN_MINUTES * 60:
                return
        except Exception:
            pass
    notify(
        "Meeting notes: trust dialog needed",
        "Claude Code needs the folder-trust prompt re-accepted before any "
        "meeting can run — click Open to accept it.",
        execute=APPLY_SCRIPT, action_label="Open",
    )
    TRUST_ALERT_MARKER.write_text(str(now))


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
        return 1, f"[runner] tmux new-session failed: {create_out}", f"tmux new-session failed: {create_out}"

    # Keep the pane alive after claude exits (crash, early error) so the final
    # screen can still be captured instead of vanishing with the session.
    _tmux("set-option", "-t", session, "remain-on-exit", "on")
    # Explicit history-limit, not just tmux's default (2000 lines unless
    # configured otherwise) — a long-running synthesis with a lot of tool-call
    # chatter can scroll the actual synthesized content out of a too-small
    # scrollback before the RESULT sentinel appears, silently truncating what
    # gets captured/persisted (pending_writes.record() via save_pending(),
    # RUN_LOG, etc). Same fix applied to the sibling claude_tmux.py helper.
    _tmux("set-option", "-t", session, "history-limit", "10000")

    output = ""
    rc = 0
    reason = None
    start_ts = time.time()
    seen_assistant_marker = False
    last_pane = None
    last_change_ts = None
    deadline = start_ts + CLAUDE_TIMEOUT_SECONDS
    try:
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            has_session_rc, _ = _tmux("has-session", "-t", session)
            _, pane = _tmux("capture-pane", "-t", session, "-p", "-S", "-5000")
            output = pane
            if pane.rfind("⏺") != -1:
                seen_assistant_marker = True
            if _has_result_sentinel(pane):
                # Debounce: grab one more capture in case trailing UI chrome
                # (footer redraw) is still settling.
                time.sleep(1.5)
                _, pane2 = _tmux("capture-pane", "-t", session, "-p", "-S", "-5000")
                output = pane2
                rc = 0
                break
            if has_session_rc != 0:
                # Session ended without ever printing a RESULT: sentinel.
                rc = 1
                reason = "tmux session ended without printing a RESULT: sentinel"
                tail = _pane_tail(output)
                if tail:
                    reason += f" — last output: \"{tail}\""
                output += f"\n[runner] {reason}"
                break
            if not seen_assistant_marker:
                # No assistant turn has happened yet. If the pane content has
                # stopped changing, claude is almost certainly parked at an
                # interactive prompt (trust dialog, an un-hooked permission
                # menu, etc) with nobody there to answer it — bail early
                # instead of waiting out the full timeout to discover that.
                if pane != last_pane:
                    last_pane = pane
                    last_change_ts = time.time()
                elif (
                    last_change_ts is not None
                    and time.time() - start_ts >= STUCK_PROMPT_GRACE_SECONDS
                    and time.time() - last_change_ts >= STUCK_PROMPT_STABLE_SECONDS
                ):
                    rc = 125
                    reason = _describe_stuck_prompt(pane)
                    output += f"\n[runner] STUCK: {reason}"
                    log(f"claude appears stuck at an interactive prompt for '{title}': {reason}")
                    break
        else:
            rc = 124
            reason = f"timed out after {CLAUDE_TIMEOUT_SECONDS}s while still running"
            tail = _pane_tail(output)
            if tail:
                reason += f" — last output: \"{tail}\""
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
    return rc, output, reason


RESULT_SENTINEL_INSTRUCTION = (
    " When you are completely done, print as the very last line exactly one of: "
    "'RESULT: SUCCESS' if the synthesis was written to the Notion page and the page "
    "was filed/re-parented; 'RESULT: BLOCKED <reason>' if any Notion write was "
    "blocked, denied, or left pending on a permission grant; or "
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
    counters = {"fired": 0, "blocked": 0, "skipped": 0, "missed": 0, "failed": 0, "stuck": 0}
    # Checked lazily (once) on the first due meeting rather than up front, so
    # a schedule with nothing due yet never pays for a config read.
    trust_checked = False
    trusted = True

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
            changed = True
            continue

        if not trust_checked:
            trusted = _trust_dialog_accepted()
            trust_checked = True
            if not trusted:
                log("Meeting-notes directory not trusted; deferring due meetings this cycle and alerting")
                _maybe_alert_trust_needed()

        if not trusted:
            # Leave as pending — every due meeting would hit the same wall,
            # so skip invoking claude for the rest of this cycle and retry
            # once the trust dialog is re-accepted.
            continue

        rc, output, reason = invoke_claude(notion_only_prompt(title, meeting_date.isoformat()), title)
        outcome = classify_outcome(rc, output)
        status = status_for(outcome)
        m["status"] = status
        counters[status] = counters.get(status, 0) + 1
        maybe_notify(outcome, title, meeting_date.isoformat(), m, reason, output)
        changed = True

    if changed:
        SCHEDULE_FILE.write_text(json.dumps(schedule, indent=2))
        log("Updated queue: " + ", ".join(f"{k}={v}" for k, v in counters.items() if v))


if __name__ == "__main__":
    main()
