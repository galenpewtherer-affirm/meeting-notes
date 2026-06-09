#!/usr/bin/env python3
"""Plain-assert tests for fetch_zoom_emails deferred-marking behavior.

No pytest dependency. Run: python3 test_fetch_zoom_emails.py
Exits non-zero on first failure.

These guard the fix for the silent data-loss bug (audit finding C1): an email
used to be marked processed at FETCH time, so when the Notion write later failed
or was skipped, the meeting was dropped permanently and never resurfaced. Marking
is now deferred to an explicit `--mark` commit that the skill issues only after a
successful write + file.
"""
import io
import json
import sys
import types
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetch_zoom_emails as f

failures = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        failures.append(name)


def with_temp_processed(initial):
    """Point the module at a fresh temp processed_ids.json seeded with `initial`."""
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(sorted(initial), tmp)
    tmp.close()
    f.PROCESSED_FILE = tmp.name
    return tmp.name


def run_main(argv):
    """Invoke f.main() with a synthetic argv, capturing stdout."""
    old = sys.argv
    sys.argv = ["fetch_zoom_emails.py", *argv]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            f.main()
    finally:
        sys.argv = old
    return buf.getvalue()


# --- C1a: --mark commits IDs to the processed list -------------------------
with_temp_processed([])
out = run_main(["--mark", "aaa", "bbb"])
saved = set(json.load(open(f.PROCESSED_FILE)))
check("--mark adds the given ids", saved == {"aaa", "bbb"})
check("--mark reports added count", json.loads(out)["added"] == 2)

# --- C1b: --mark unions with existing + is idempotent ----------------------
with_temp_processed(["aaa"])
run_main(["--mark", "aaa", "ccc"])
saved = set(json.load(open(f.PROCESSED_FILE)))
check("--mark unions, no dupes", saved == {"aaa", "ccc"})
out = run_main(["--mark", "aaa"])
check("--mark of existing id adds 0", json.loads(out)["added"] == 0)

# --- C1c: fetching does NOT mark (the core regression) ---------------------
# Stub out auth + the Gmail client so main() runs offline and returns one msg.
f.get_creds = lambda: object()

fake_msg = {
    "payload": {
        "headers": [
            {"name": "Subject", "value": "Meeting assets for Foo / Galen are ready!"},
            {"name": "Date", "value": "Mon, 02 Jun 2026 18:00:00 +0000"},
        ],
        "mimeType": "text/plain",
        "body": {"data": ""},
    }
}


class _Messages:
    def list(self, userId, q, maxResults):
        return types.SimpleNamespace(execute=lambda: {"messages": [{"id": "newid123"}]})

    def get(self, userId, id, format):
        return types.SimpleNamespace(execute=lambda: fake_msg)


class _Users:
    def messages(self):
        return _Messages()


class _Service:
    def users(self):
        return _Users()


fake_discovery = types.ModuleType("googleapiclient.discovery")
fake_discovery.build = lambda *a, **k: _Service()
sys.modules["googleapiclient"] = types.ModuleType("googleapiclient")
sys.modules["googleapiclient.discovery"] = fake_discovery

with_temp_processed([])
out = run_main(["--since", "2026-06-01"])
items = json.loads(out)
check("fetch returns the unprocessed email", len(items) == 1 and items[0]["gmail_id"] == "newid123")
saved = set(json.load(open(f.PROCESSED_FILE)))
check("fetch does NOT mark the email processed", saved == set())

# --- C1d: already-marked ids are filtered out of fetch ---------------------
with_temp_processed(["newid123"])
out = run_main(["--since", "2026-06-01"])
check("already-marked id is filtered from fetch", json.loads(out) == [])


if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("\nAll fetch_zoom_emails tests passed.")
