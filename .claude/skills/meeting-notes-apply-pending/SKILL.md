---
name: meeting-notes-apply-pending
description: "Re-run meeting notes that were blocked during a headless launchd run, in an interactive session where the Notion write permission can be approved. Use when: 'apply pending meeting notes', 'finish the blocked meeting note', 'process pending notes', or after a 'Meeting notes: write blocked' notification."
---

# Meeting Notes: Apply Pending

Process meeting notes that were blocked during headless launchd runs. The runner
saves metadata to `/tmp/meeting-notes-pending/` when a Notion write is denied; this
skill re-runs those meetings in the current interactive session where you can approve
the Notion write permission.

---

## Step 1 — Scan for pending notes

```bash
python3 -c "
import json
from pathlib import Path
d = Path('/tmp/meeting-notes-pending')
if not d.exists() or not list(d.glob('*.json')):
    print('NONE')
else:
    for f in sorted(d.glob('*.json')):
        data = json.loads(f.read_text())
        print(f'{f.name}  |  {data[\"title\"]}  |  {data[\"date\"]}')
"
```

If output is `NONE`, report "No pending meeting notes." and stop.

Otherwise list what was found and proceed.

---

## Step 2 — Process each pending note

Work through the pending files one at a time. For each, read the JSON to get
`title` and `date`.

A pending file only ever means one thing under the current (Notion-only) runner:
the skill ran and a Notion write was blocked on a permission prompt it couldn't
answer headlessly, so nothing was filed. Nothing about *which* step it reached is
recorded, and none of the earlier steps have any lasting side effect worth
skipping, so simply re-run the meeting from the top:

- Follow the full meeting-notes workflow defined in this project's CLAUDE.md,
  starting at **Step 1** (Notion-page search) using the pending file's `title`
  and `date`.
- Execute Steps 1 through 5 in order, same as a normal invocation. In this
  interactive session you can approve the Notion write permission prompt when
  it appears, so the run should now get past whatever previously blocked it.
- If it stops again at an earlier step for a different reason (e.g. Step 1 finds
  no matching Notion page, or the `<summary>` is empty), that's a real outcome —
  report it per CLAUDE.md's `RESULT: SKIPPED` / `RESULT: BLOCKED` / `RESULT: PARTIAL`
  conventions rather than treating it as an error in this skill.

After each meeting's Step 4 (Notion write) succeeds, delete its pending file:

```bash
python3 -c "from pathlib import Path; Path('/tmp/meeting-notes-pending/<FILENAME>').unlink(missing_ok=True)"
```

---

## Step 3 — Report

Summarize results: how many notes were applied, skipped, or failed, and list
any that need follow-up.
