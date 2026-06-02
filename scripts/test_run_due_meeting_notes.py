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

# --- should_notify(outcome) ---
check("notify on blocked", r.should_notify("blocked"), True)
check("notify on failed", r.should_notify("failed"), True)
check("no notify on success", r.should_notify("success"), False)
check("no notify on skipped", r.should_notify("skipped"), False)

# --- maybe_notify fires only on blocked/failed, with a useful message ---
_calls = []
_orig_notify = r.notify
r.notify = lambda title, message: _calls.append((title, message))
try:
    r.maybe_notify("success", "Adam/Galen 1:1", "2026-06-01")
    r.maybe_notify("skipped", "Greg Office Hours", "2026-06-01")
    check("no notify on success/skipped", len(_calls), 0)
    r.maybe_notify("blocked", "Adam/Galen 1:1", "2026-06-01")
    check("notify fired on blocked", len(_calls), 1)
    check("blocked title", _calls[0][0], "Meeting notes: write blocked")
    check("blocked msg names meeting", "Adam/Galen 1:1" in _calls[0][1], True)
    r.maybe_notify("failed", "TPM weekly", "2026-06-01")
    check("notify fired on failed", len(_calls), 2)
    check("failed title", _calls[1][0], "Meeting notes: run failed")
finally:
    r.notify = _orig_notify

if failures:
    print(f"\n{len(failures)} FAILURE(S)")
    sys.exit(1)
print("\nALL PASS")
