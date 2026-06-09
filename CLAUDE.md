# Meeting Notes Workflow

This directory holds the local files for the meeting-notes pipeline. The
authoritative workflow lives in `~/.claude/skills/meeting-notes.md`; this file
mirrors it so that headless invocations (which auto-load CLAUDE.md from the
working directory) follow the same steps.

If you edit one, edit both — or delete this duplicate and instead reference the
skill explicitly.

---

Combines Zoom AI meeting summaries (from email) with Notion AI meeting notes into
a single high-quality summary, written back to the Notion meeting page.

## Workflow

Execute all steps below in order.

---

### Step 1 — Fetch unprocessed Zoom emails

```bash
python3 /Users/galen.pewtherer/Claude/meeting-notes/scripts/fetch_zoom_emails.py
```

This returns a JSON array. Each item has:
- `gmail_id` — Gmail message ID. **NOT yet marked processed** — you commit it in
  Step 6 only after the note is written + filed. Track each meeting's `gmail_id`
  so you can mark exactly the ones that succeed.
- `meeting_title` — extracted from email subject
- `date` — YYYY-MM-DD
- `zoom_summary` — full Zoom AI summary text

If the array is empty, tell the user there are no new meetings to process.

To reprocess all meetings from the past 30 days: add `--all`
To look further back: add `--since YYYY-MM-DD`

---

### Step 2 — For each meeting, find the Notion meeting note page

Notion AI auto-creates a page per calendar meeting. The title is
`<Meeting Title> @<Date> <Time>`, but **the `<Date>` is often a RELATIVE phrase**,
not an absolute date — e.g. "@Today", "@Yesterday", "@Last Tuesday 10:30 AM (PDT)",
"@Last Friday 9:00 AM (PDT)". Recent pages almost always use the relative form, so
**do not rely on the title containing an absolute date** (that was a prior bug).

Use `mcp__notion__notion-search` to locate it:

```json
{
  "query": "<meeting_title>",
  "query_type": "internal",
  "content_search_mode": "workspace_search",
  "page_size": 10,
  "max_highlight_length": 0,
  "filters": {
    "created_date_range": {
      "start_date": "<meeting_date - 1 day>",
      "end_date": "<meeting_date + 1 day>"
    }
  }
}
```

Filter results:
1. Keep only results where `type == "page"` (drop `google-calendar`, `gmail`, etc.)
2. Identify the candidate(s) whose base title (title with the `@...` suffix stripped)
   matches the meeting title. The search is date-windowed already, so usually only the
   right occurrence is in range.
3. **Confirm the date by the page's `mention-date`, NOT the title.** Fetch each candidate
   (next paragraph) and read the `<mention-date start="YYYY-MM-DD"/>` in its properties /
   `<meeting-notes>` body. The match is the page whose `mention-date start` equals the
   meeting date. This is the reliable check because titles use relative dates ("@Today").
4. If two or more pages share the same base title AND `mention-date` (true duplicates),
   pick the one with the most recent `timestamp` to write to, and **flag the duplicate(s)
   in your final report** so they can be deduped — do not silently ignore them.

The matched page's `id` is the **real, writable page ID** — use it for Step 4.

Fetch the page content with `mcp__notion__notion-fetch` using that `id` (you need it both
to confirm the `mention-date` above and to have the existing Notion AI auto-summary as a
synthesis input in Step 3).

**Detect the empty-Notion-AI case — judge the `<summary>`, never the `<notes>`.**
A populated Notion AI page has a rich `<summary>` block (headings, bullets, Action Items)
while its `<notes>` block is **almost always empty** (`<empty-block/>` or a bare
Agenda/Notes scaffold). That is the NORMAL, populated state — do **not** treat an empty
`<notes>` block as an empty page (doing so was a bug that overwrote good Notion AI
synthesis with Zoom-only content).

The page is empty ONLY if its `<summary>` has no real content, i.e. any of:
- The `<summary>` block is missing, or is itself `<empty-block/>` / whitespace.
- The body contains Notion's no-content boilerplate, e.g. "It looks like your transcript
  and notes are empty this time around" or "could not be generated due to insufficient
  transcript".
- There are no `### `/`## ` content headings and no Action Items anywhere in `<summary>`.

(An empty `<notes>` block alongside a full `<summary>` is POPULATED — keep the Notion AI
content as a synthesis input.)

When empty, the Zoom AI summary becomes the **sole synthesis input** for Step 3 — proceed
to Step 3 with Zoom-only content and add a leading callout to the output:

```markdown
> Notion AI did not generate a summary for this meeting — content below is sourced from the Zoom AI meeting recap email (no Notion AI synthesis input was available).
```

Do not skip the meeting, do not wait for Notion AI to populate later, and do not call
`notion-create-pages`. The Zoom AI summary alone is sufficient page content.

**Critical — do NOT use `mcp__notion__notion-query-meeting-notes` to find the page.**
That tool returns block-reference URLs which look like page IDs but are not writable;
`notion-update-page` on a block URL fails with "not a page or database." Using
`notion-search` with the `@<Date>` title pattern is the only reliable way to get a real
page ID for the update in Step 4.

**When search finds NO matching page.** Notion AI only auto-creates a page for calendar
meetings it transcribes; ad-hoc Zooms, PMI/"Zoom Meeting"-titled calls, and meetings the
notetaker missed produce a Zoom recap email but **no Notion page**. Decide as follows:

- **First, make sure the title isn't the problem.** If `meeting_title` is generic
  ("Zoom Meeting", "My Meeting", a bare PMI), the Notion title (if any) is named after the
  participants, not "Zoom Meeting". Re-search using participant names pulled from the Zoom
  recap's first "Quick recap" sentence (e.g. "Galen and Sarah discussed…" → search
  "Sarah Galen", "Galen / Sarah"). Widen the date window to ±2 days (recap emails can lag
  the meeting by a day).
- **If a page genuinely does not exist after that**, and the Zoom summary HAS usable
  content: **create the page** with `mcp__notion__notion-create-pages`, parented directly
  under the correct index subpage from the Step 5 classify table (so it has a durable home
  immediately). Title it `<Base Name> — YYYY-MM-DD`. Synthesize the body per Step 3 with
  the Zoom-only callout. Then do the Step 5 "Insert the link" sub-step (the page is already
  parented, so skip the re-parent). This is the supported path for meetings Notion AI never
  captured.
- **If the Zoom summary itself has no content** (e.g. "could not be generated due to
  insufficient transcript") AND no Notion page exists, there is nothing to file: **skip**
  and report (`RESULT: SKIPPED <reason>`). Do not create an empty page.

**Creating is allowed ONLY here — when search genuinely returns zero pages.** It is NEVER
a fallback for a failed `notion-update-page` (see Step 4 guardrail 3): an update failure
means you have a real page but used the wrong id, and creating then would duplicate it and
orphan the Zoom transcript link. Distinguish "no page exists" (create) from "update failed"
(re-search, never create).

---

### Step 2b — Search Notion for related documents (non-1:1 meetings only)

Skip this step if the meeting title contains "1:1" (case-insensitive).

Use `mcp__notion__notion-search` with `content_search_mode: workspace_search` to find
related Notion pages:

```json
{
  "query": "<meeting_title>",
  "query_type": "internal",
  "content_search_mode": "workspace_search",
  "page_size": 6,
  "max_highlight_length": 150
}
```

From the results:
- Keep only results with `type: "page"` (discard `google-calendar` and other types)
- Exclude the meeting's own Notion page (match by page ID)
- Take the top 2 remaining results

Fetch the content of each kept page using `mcp__notion__notion-fetch`.

These pages will be used as additional synthesis input in Step 3. If no relevant pages
are found, proceed to Step 3 without them.

---

### Step 3 — Synthesize a combined summary

Using the Zoom AI summary, the Notion AI meeting note, and any related Notion pages
fetched in Step 2b as inputs, write a single unified meeting summary following this structure:

```
## Action Items
[Numbered list. Owner in bold. Format: **Owner**: Action]

## Quick Recap
[2-3 sentence overview of what the meeting covered and what was decided]

## Key Discussion Points
[Bullet points covering the main topics discussed, with enough detail to be
useful without reading the full transcript. Combine and de-duplicate across
both sources. Preserve specific technical details, names, and decisions.]

## Additional Context
[Any background, nuance, or detail from either source that doesn't fit above
but is worth preserving - e.g., constraints, open questions, alternatives
that were considered and rejected]
```

Action Items lead because they're the highest-signal output for skim-reading. Everything below is supporting context.

Synthesis guidelines:
- Zoom AI summary and Notion AI notes may overlap — merge, don't duplicate
- **Zoom-only fallback (Notion AI empty):** if Step 2 detected an empty page, write the
  output using Zoom AI summary alone. Prefix the output with the empty-Notion-AI callout
  from Step 2. Action Items come from the Zoom "Next steps" section.
- Related Notion pages (from Step 2b) provide background context — use them to enrich
  Key Discussion Points and Additional Context, not to repeat what's already in the meeting note
- Prefer specificity: keep concrete details (names, numbers, tool names, decisions)
- If sources contradict, note both versions
- Action items: consolidate from both sources, remove duplicates
- Write in past tense, third person

---

### Step 4 — Update the Notion meeting page

Use `mcp__notion__notion-update-page` with `command: "replace_content"` and the
**page `id` from Step 2** to replace the existing page body with the synthesized summary.

```json
{
  "page_id": "<id from Step 2>",
  "command": "replace_content",
  "properties": {},
  "content_updates": [],
  "new_str": "<synthesized markdown from Step 3>"
}
```

Do NOT change the page title or any properties — only the content body.

**Critical guardrails:**

1. **If `notion-update-page` returns "not a page or database":** the page_id you have is
   actually a block reference, which means Step 2 used the wrong tool. Stop and
   re-run Step 2 using `notion-search` (do NOT fall back to writing to the page's
   parent — that overwrites the parent index).
2. **If `notion-update-page` returns "this operation would delete N child page(s)":**
   the existing body contains nested pages (e.g., child meeting notes). Include them
   in `new_str` as `<page url="...">Title</page>` blocks at the appropriate point,
   then retry. Do NOT set `allow_deleting_content: true` unless you're certain there's
   nothing to preserve.
3. **Never call `mcp__notion__notion-create-pages` as a fallback for a failed update.**
   An update failure means a real page exists but you have the wrong id — creating then
   produces a duplicate and orphans the original page (which still holds the underlying
   Zoom transcript link via its `<meeting-notes readOnlyViewMeetingNoteUrl>` wrapper). If
   update fails, fix Step 2 and retry — don't paper over by creating. (Creating is allowed
   ONLY in Step 2, when search genuinely returns zero pages — a distinct, deliberate path.)

After a successful update, confirm to the user: meeting title, date, Notion page URL,
and a one-line note that the page was updated (not created).

---

---

### Step 5 — Add to index and re-parent

After Step 4 succeeds, classify the meeting and place it under the matching
index subpage from the table below. Notion AI's auto-created meeting pages
have no parent — without this step they float at workspace root and are not
discoverable from the meeting-notes index.

Most indexes live under "Meeting notes — by meeting name"
(`bc31bdcce7e840efa21e41b40d7be735`). The Reliability Program weekly - Ops
and Reliability Program weekly - Product indexes were moved 2026-05-28 to
live under the Reliability Program Hub's Meetings sub-page
(`36e40e54ae388151b737ecca5bfa19e7`) so they inherit "Affirm all - edit"
sharing. The page IDs are unchanged; only the parents moved, so the lookup
table below still resolves correctly.

#### Classify

Strip the `@<Date> <Time> ...` suffix from the meeting title to get the base
name. Examples:
- `Mindy | Galen (Weekly) @Today 11:30 AM (PDT)` → `Mindy | Galen (Weekly)`
- `Reliability Program weekly - Ops @May 26, 2026 10:00 AM (PDT)` → `Reliability Program weekly - Ops`

Match the base name against the current index subpages (ordered by specificity —
first match wins):

| Base name pattern (case-insensitive)                          | Index subpage                              | Page ID                              |
|---------------------------------------------------------------|--------------------------------------------|--------------------------------------|
| starts with `Peak Event planning`                             | Peak Event planning                        | `34940e54ae388176b524c4ebb95dba24`   |
| starts with `TPM weekly`                                      | TPM weekly                                 | `d231650717c54dd2aef8723e216b40a1`   |
| starts with `Reliability Engineering Leadership`              | Reliability Engineering Leadership Weekly  | `95eb9d6808824fb98d13c753cd3acc05`   |
| starts with `Availability Program weekly`                     | Availability Program weekly                | `1d03268088814b329961ef5377a9f142`   |
| starts with `Reliability Program weekly - Product`            | Reliability Program weekly - Product       | `35f40e54ae38818a9584e55316bdd3e3`   |
| starts with `Reliability Program weekly - Ops`                | Reliability Program weekly - Ops           | `35f40e54ae3881199c16ecd0e3de482c`   |
| contains `/`, `\|`, `1:1`, `(Weekly)`, or `(Bi-Weekly)`       | 1:1s (various)                             | `121f6d7d163043a3a1ec7e0cab4b7351`   |
| anything else                                                 | Other meetings (2026)                      | `35e40e54ae38817890d1f97148c0951a`   |

If a new recurring series clearly merits its own subpage (e.g., 3+ past
occurrences scattered in "Other meetings"), flag to the user — do not create
new index subpages yourself.

#### Insert the link

Fetch the chosen index page. Find the markdown bullet list inside.

For indices with **monthly headers** (1:1s (various), Other meetings (2026)):
- Look for a header matching the current month: `**<Month> YYYY:**`
  (e.g., `**May 2026:**`).
- If found, insert the new bullet immediately after the header (becomes
  newest-first under that month).
- If not found, insert a new month header + bullet at the top of the list,
  above the most recent existing month header.

For **program-specific indices** (Reliability Program weekly - Ops, Reliability
Program weekly - Product, Peak Event planning, etc.):
- Look for a year header: `**<Year> (newest → oldest):**`
  (e.g., `**2026 (newest → oldest):**`).
- Insert the new bullet immediately after that header.

Bullet format:

```
- [<Base Name> — YYYY-MM-DD](<page_url>)
```

Escape `|` in the base name as `\|` (Notion markdown requirement, e.g.
`Mindy \| Galen (Weekly)`).

Use `update_content` with a precise `old_str`/`new_str` pair. **Notion's
markdown parser has a ~55-second response budget for large pages.** If
`update_content` returns `503 service_unavailable`, split into smaller
individual updates — do NOT retry the same payload.

#### Re-parent

Use `notion-move-pages` to move the meeting page under the index subpage:

```json
{
  "page_or_database_ids": ["<meeting_page_id_from_Step_2>"],
  "new_parent": {"type": "page_id", "page_id": "<index_subpage_id>"}
}
```

This is what gives the page a durable home. **Do NOT attempt to insert a
`<page url="...">Title</page>` block into the index** — that has failed in
practice (`validation_error: Failed to create block`). `notion-move-pages`
accomplishes the parenting reliably.

#### Failure mode

If any sub-step in Step 5 fails, do not undo Step 4 — the synthesis content on
the meeting page is still useful. Report to the user which sub-step failed
(e.g., "link inserted but re-parent failed") so they can complete it manually.

---

### Step 6 — Mark the email processed (commit)

**Only now**, after Step 4 wrote the page AND Step 5 filed it (or, for a created
page, after the index link is in), mark that meeting's Gmail id processed:

```bash
python3 /Users/galen.pewtherer/Claude/meeting-notes/scripts/fetch_zoom_emails.py --mark <gmail_id>
```

Mark **only** the `gmail_id`s that fully succeeded. Do this per-meeting (or pass
several ids at once at the end, but never mark a meeting you skipped, blocked on,
or only partially filed).

**Why this matters (audit C1):** fetching no longer marks emails processed. If you
mark before a successful write, a failed/blocked/skipped meeting is dropped forever
and never resurfaces. Leaving an unfiled meeting unmarked lets the next run retry it.
If a meeting was intentionally skipped (e.g. insufficient-transcript with no page),
do NOT mark it unless you want it to stop resurfacing — say so in your report.

---

## No-Zoom-assets mode (runner-invoked)

The polling runner (`run_due_meeting_notes.py`) may invoke the skill with a
prompt that includes:

> Run the meeting-notes skill for the meeting titled "..." on YYYY-MM-DD.
> There are NO Zoom AI assets in Gmail for this meeting (checked 3 times,
> 7 minutes apart). Skip Step 1 of the skill. ...

When you see that prompt, follow it exactly:

1. **Skip Step 1** (do not run `fetch_zoom_emails.py`). The runner has already
   confirmed there are no Zoom assets.
2. **Step 2 as usual** — find the Notion page by title + date.
3. **Step 2b as usual** — search for related pages if not a 1:1.
4. **Step 3** — synthesize from the Notion AI content (and any related pages)
   alone. If the Notion AI block is also empty, write a minimal page noting
   that no content sources were available.
5. **Step 4** — write the page with the no-Zoom callout the prompt provided
   prepended at the very top of `new_str`. Everything else (use `notion-search`
   page ID, use `replace_content`) is unchanged. Step 2's create-if-genuinely-absent
   path still applies if no Notion page exists at all.
6. **Skip Step 6** (no `gmail_id` to mark — Step 1 was skipped). The runner tracks
   this meeting's status via its own schedule queue, not `processed_ids.json`.

This mode is the planned exit when the runner has exhausted its Zoom-assets
retries; Zoom occasionally delays the recap email beyond the runner's polling
window, and adding an explicit callout to the Notion page is preferable to
silently dropping the synthesis.

---

## Notes

- Processed Gmail IDs are stored in `scripts/processed_ids.json`. Fetching does NOT
  add to it — only the Step 6 `--mark` commit does, after a successful write + file.
  Already-marked meetings won't be returned on subsequent runs.
- If a Notion page cannot be found, follow Step 2's no-page logic: re-search by
  participants for generic titles, then create-if-genuinely-absent, or skip only when
  there's no content in either source.
- Never modify Notion pages from other databases — only meeting note pages
- The script lives at `/Users/galen.pewtherer/Claude/meeting-notes/scripts/fetch_zoom_emails.py`
  and works from any working directory
