#!/usr/bin/env python3
"""Plain-assert tests for run_due_meeting_notes outcome classification.

No pytest dependency. Run: python3 test_run_due_meeting_notes.py
Exits non-zero on first failure.

These guard the fix for the silent-write-block bug: the headless skill exits 0
even when a Notion write is blocked pending permission, so the runner used to
mark such runs `fired` (false success). classify_outcome() inspects the skill's
stdout so blocked/skipped runs are distinguished from real successes.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run_due_meeting_notes as r

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")
        print(f"FAIL {name}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {name}")


# --- classify_outcome(rc, output) ---
# Non-zero exit always fails, regardless of text.
check("nonzero rc -> failed", r.classify_outcome(1, "anything"), "failed")
check("nonzero rc beats success marker", r.classify_outcome(2, "RESULT: SUCCESS"), "failed")

# Explicit sentinels (primary signal once prompts emit them).
check("explicit BLOCKED", r.classify_outcome(0, "stuff\nRESULT: BLOCKED notion write blocked"), "blocked")
check("explicit SKIPPED", r.classify_outcome(0, "RESULT: SKIPPED no page"), "skipped")
check("explicit SUCCESS", r.classify_outcome(0, "wrote page and filed\nRESULT: SUCCESS"), "success")
check("sentinel SUCCESS beats stray block word", r.classify_outcome(0, "the permission prompt earlier\nRESULT: SUCCESS"), "success")

# Real block phrasing pulled verbatim from /tmp/meeting-notes-poller.log (no sentinel).
block_adam = ("The Notion write is blocked - the `mcp__notion__notion-update-page` "
              "permission hasn't been granted, so I can't complete Step 4")
check("real Adam block phrase", r.classify_outcome(0, block_adam), "blocked")
check("permission-pending phrase", r.classify_outcome(0, "both write attempts came back as permission-pending"), "blocked")
check("denied phrase", r.classify_outcome(0, "The update was denied - permission to call notion-update-page hasn't been granted"), "blocked")
check("blocked pending permission grant", r.classify_outcome(0, "is blocked pending permission grant in this environment"), "blocked")

# Skip phrasing (no sentinel) - legitimate no-op, not an error, no alert.
check("no page found", r.classify_outcome(0, "No Notion meeting page was found for \"Greg Office Hours\""), "skipped")
check("skipping this meeting", r.classify_outcome(0, "Outcome - skipping this meeting. Likely causes:"), "skipped")

# Success fallback when neither sentinel nor failure phrases present.
check("plain success text", r.classify_outcome(0, "Updated the Notion page and re-parented it under the 1:1s index."), "success")

# Fail-safe documentation: a success run that omits the sentinel but narrates a
# stray "hasn't been granted" is classified blocked. This OVER-notifies (safe) and
# never reproduces the original silent-drop bug (which classified blocked as fired).
check("no-sentinel + stray grant phrase -> blocked (fail-safe)",
      r.classify_outcome(0, "calendar permission hasn't been granted, but the Notion page was written"),
      "blocked")

# Timeout path produces rc=124 -> failed (invoke_claude maps TimeoutExpired to rc 124).
check("timeout rc 124 -> failed", r.classify_outcome(124, "[runner] TIMEOUT after 900s; process killed."), "failed")

# Early-bail stuck-prompt path produces rc=125 -> stuck, regardless of text
# (invoke_claude only ever sets rc=125 for this case).
check("stuck rc 125 -> stuck", r.classify_outcome(125, "[runner] STUCK: stuck at an interactive prompt"), "stuck")
check("stuck rc 125 beats success marker", r.classify_outcome(125, "RESULT: SUCCESS"), "stuck")

# --- _pane_tail(text, max_lines, max_len) ---
check("_pane_tail empty", r._pane_tail(""), "")
check("_pane_tail none", r._pane_tail(None), "")
check("_pane_tail joins last lines", r._pane_tail("a\n\nb\nc\nd"), "b | c | d")
check("_pane_tail truncates", len(r._pane_tail("x" * 500, max_len=50)) <= 50, True)

# --- _describe_stuck_prompt(pane_text) ---
check("trust dialog named specifically",
      "trust-folder prompt" in r._describe_stuck_prompt("Quick safety check: Is this a project you created or one you trust?\n1. Yes, I trust this folder"),
      True)
check("generic stuck prompt falls back to tail",
      "some other menu" in r._describe_stuck_prompt("some other menu\nEnter to confirm"),
      True)

# --- _trust_dialog_accepted() ---
with tempfile.TemporaryDirectory() as td:
    cfg = Path(td) / "claude.json"
    orig_cfg, orig_dir = r.CLAUDE_CONFIG, r.MEETING_NOTES_DIR
    r.CLAUDE_CONFIG, r.MEETING_NOTES_DIR = cfg, "/some/dir"
    try:
        cfg.write_text('{"projects": {"/some/dir": {"hasTrustDialogAccepted": false}}}')
        check("trust false when explicitly false", r._trust_dialog_accepted(), False)
        cfg.write_text('{"projects": {"/some/dir": {"hasTrustDialogAccepted": true}}}')
        check("trust true when explicitly true", r._trust_dialog_accepted(), True)
        cfg.write_text('{"projects": {}}')
        check("trust true (fail-open) when project missing", r._trust_dialog_accepted(), True)
        check("trust true (fail-open) when config unreadable",
              (lambda: (cfg.unlink(), r._trust_dialog_accepted())[-1])(), True)
    finally:
        r.CLAUDE_CONFIG, r.MEETING_NOTES_DIR = orig_cfg, orig_dir

# --- _as_text(v) ---
check("_as_text None", r._as_text(None), "")
check("_as_text str", r._as_text("hi"), "hi")
check("_as_text bytes", r._as_text(b"hi"), "hi")

# --- status_for(outcome, no_zoom) ---
check("success normal -> fired", r.status_for("success", False), "fired")
check("success no_zoom -> fired_no_zoom", r.status_for("success", True), "fired_no_zoom")
check("blocked -> blocked", r.status_for("blocked", False), "blocked")
check("blocked no_zoom -> blocked", r.status_for("blocked", True), "blocked")
check("skipped -> skipped", r.status_for("skipped", False), "skipped")
check("failed -> failed", r.status_for("failed", False), "failed")
check("stuck -> stuck", r.status_for("stuck", False), "stuck")

# --- should_notify(outcome) ---
check("notify on blocked", r.should_notify("blocked"), True)
check("notify on failed", r.should_notify("failed"), True)
check("notify on stuck", r.should_notify("stuck"), True)
check("no notify on success", r.should_notify("success"), False)
check("no notify on skipped", r.should_notify("skipped"), False)

# --- maybe_notify fires only on blocked/failed/stuck, with a useful message
# and a one-click 'Open' action on every branch ---
_calls = []
_orig_notify = r.notify
r.notify = lambda title, message, **kw: _calls.append((title, message, kw))
try:
    r.maybe_notify("success", "Adam/Galen 1:1", "2026-06-01")
    r.maybe_notify("skipped", "Greg Office Hours", "2026-06-01")
    check("no notify on success/skipped", len(_calls), 0)

    r.maybe_notify("blocked", "Adam/Galen 1:1", "2026-06-01")
    check("notify fired on blocked", len(_calls), 1)
    check("blocked title", _calls[0][0], "Meeting notes: write blocked")
    check("blocked msg names meeting", "Adam/Galen 1:1" in _calls[0][1], True)
    check("blocked has Open action", _calls[0][2].get("action_label"), "Open")

    r.maybe_notify("failed", "TPM weekly", "2026-06-01", reason="timed out after 900s while still running")
    check("notify fired on failed", len(_calls), 2)
    check("failed title", _calls[1][0], "Meeting notes: run failed")
    check("failed msg includes reason", "timed out after 900s" in _calls[1][1], True)
    check("failed has Open action", _calls[1][2].get("action_label"), "Open")

    r.maybe_notify("failed", "TPM weekly", "2026-06-01")
    check("failed msg without reason still has a fallback", "run failed" in _calls[2][1], True)

    r.maybe_notify("stuck", "AI Enablement | ProdSec | IT", "2026-08-18",
                    reason="stuck at Claude Code's first-run trust-folder prompt (nobody there to accept it)")
    check("notify fired on stuck", len(_calls), 4)
    check("stuck title", _calls[3][0], "Meeting notes: stuck at a prompt")
    check("stuck msg includes reason", "trust-folder prompt" in _calls[3][1], True)
    check("stuck has Open action", _calls[3][2].get("action_label"), "Open")
finally:
    r.notify = _orig_notify

# --- _maybe_alert_trust_needed: fires once, then respects the cooldown ---
with tempfile.TemporaryDirectory() as td:
    marker = Path(td) / "trust-alert-last"
    orig_marker = r.TRUST_ALERT_MARKER
    r.TRUST_ALERT_MARKER = marker
    _trust_calls = []
    r.notify = lambda title, message, **kw: (_trust_calls.append((title, message, kw)), True)[-1]
    try:
        r._maybe_alert_trust_needed()
        check("trust alert fires first time", len(_trust_calls), 1)
        check("trust alert has Open action", _trust_calls[0][2].get("action_label"), "Open")
        r._maybe_alert_trust_needed()
        check("trust alert suppressed within cooldown", len(_trust_calls), 1)
    finally:
        r.notify = _orig_notify
        r.TRUST_ALERT_MARKER = orig_marker

# --- save_pending: mirrors blocked meetings into the durable pending_writes ledger ---
with tempfile.TemporaryDirectory() as td:
    orig_pending_dir = r.PENDING_DIR
    orig_record = r.record_pending
    r.PENDING_DIR = Path(td)
    _recorded = []
    r.record_pending = lambda **kw: (_recorded.append(kw), "fake-entry-id")[-1]
    try:
        r.save_pending({"title": "Peak Event planning"}, "2026-08-27", output="pane text")
        check("save_pending calls record_pending once", len(_recorded), 1)
        check("record_pending job is meeting-notes-runner", _recorded[0]["job"], "meeting-notes-runner")
        check("record_pending description names the meeting", "Peak Event planning" in _recorded[0]["description"], True)
        check("record_pending content is the pane output", _recorded[0]["content"], "pane text")
    finally:
        r.PENDING_DIR = orig_pending_dir
        r.record_pending = orig_record

# --- save_pending: a missing/broken pending_writes import must never crash the runner ---
with tempfile.TemporaryDirectory() as td:
    orig_pending_dir = r.PENDING_DIR
    orig_record = r.record_pending
    r.PENDING_DIR = Path(td)
    r.record_pending = lambda **kw: None  # mirrors the defensive no-op fallback import
    try:
        try:
            r.save_pending({"title": "Peak Event planning"}, "2026-08-27", output="pane text")
            check("save_pending tolerates a no-op record_pending", True, True)
        except Exception as e:
            check("save_pending tolerates a no-op record_pending", f"raised {e!r}", True)
    finally:
        r.PENDING_DIR = orig_pending_dir
        r.record_pending = orig_record

if failures:
    print(f"\n{len(failures)} FAILURE(S)")
    sys.exit(1)
print("\nALL PASS")
