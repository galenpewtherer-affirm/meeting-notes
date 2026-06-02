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
- `gmail_id` — Gmail message ID (already marked processed)
- `meeting_title` — extracted from email subject
- `date` — YYYY-MM-DD
- `zoom_summary` — full Zoom AI summary text

If the array is empty, tell the user there are no new meetings to process.

To reprocess all meetings from the past 30 days: add `--all`
To look further back: add `--since YYYY-MM-DD`

---

### Step 2 — For each meeting, find the Notion meeting note page

Notion AI auto-creates a page per calendar meeting with title format
`<Meeting Title> @<Date> <Time>` (e.g., "Peak Event planning @April 21, 2026 9:00 AM (PDT)").

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
2. Match the result whose title contains the meeting's date (e.g., "@April 21, 2026"
   matches `meeting_date = 2026-04-21`)
3. If multiple pages match (rare), pick the one with the most recent `timestamp`

The matched result's `id` is the **real, writable page ID** — use it for Step 4.

Then fetch the page content with `mcp__notion__notion-fetch` using that `id` so you have
the existing body (Notion AI's auto-summary) as one of the synthesis inputs in Step 3.

**Detect the empty-Notion-AI case.** Inspect the fetched content. The page is considered
empty if the body matches any of these patterns:
- Contains `<empty-block/>` inside a `<notes>` block
- Contains only a `<meeting-notes>` wrapper with no real content (just attendees +
  `<mention-date>` + empty notes)
- Has no `## Quick Recap` / `## Summary` / `## Key Discussion Points` style headings

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

If no page is found via search, skip the meeting and report to the user. Do **not** call
`notion-create-pages` — the page should already exist (Notion AI creates it when the
calendar event runs). Creating a new page produces duplicates and orphans the underlying
Zoom transcript link.

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
3. **Never call `mcp__notion__notion-create-pages` as a fallback.** Creating a new
   page produces a duplicate and orphans the original page (which still holds the
   underlying Zoom transcript link via its `<meeting-notes readOnlyViewMeetingNoteUrl>`
   wrapper). If update fails, fix Step 2 and retry — don't paper over by creating.

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
   page ID, use `replace_content`, do not call `notion-create-pages`) is
   unchanged.

This mode is the planned exit when the runner has exhausted its Zoom-assets
retries; Zoom occasionally delays the recap email beyond the runner's polling
window, and adding an explicit callout to the Notion page is preferable to
silently dropping the synthesis.

---

## Notes

- Processed Gmail IDs are stored in `scripts/processed_ids.json` — already-processed
  meetings won't be returned by the script on subsequent runs
- If a Notion page cannot be found for a meeting, skip it and report to the user
- Never modify Notion pages from other databases — only meeting note pages
- The script lives at `/Users/galen.pewtherer/Claude/meeting-notes/scripts/fetch_zoom_emails.py`
  and works from any working directory
