# Meeting Notes Workflow

Synthesizes Notion AI meeting notes into a single high-quality summary, written
back to the Notion meeting page.

## Workflow

Execute all steps below in order.

---

### Step 1 — Find the Notion meeting note page

Notion AI auto-creates a page per calendar meeting. The title is
`<Meeting Title> @<Date> <Time>`, but **the `<Date>` is often a RELATIVE phrase**,
not an absolute date — e.g. "@Today", "@Yesterday", "@Last Tuesday 10:30 AM (PDT)".
Recent pages almost always use the relative form, so **do not rely on the title
containing an absolute date**.

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
   matches the meeting title.
3. **Confirm the date by the page's `mention-date`, NOT the title.** Fetch each
   candidate and read the `<mention-date start="YYYY-MM-DD"/>` in its
   `<meeting-notes>` body. The match is the page whose `mention-date start` equals
   the meeting date.
4. If two or more pages share the same base title AND `mention-date` (true duplicates),
   pick the one with the most recent `timestamp` and **flag the duplicate(s)** so they
   can be deduped.

The matched page's `id` is the **real, writable page ID** — use it for Step 3.

Fetch the page content with `mcp__notion__notion-fetch` using that `id`.

**Judge the `<summary>` block, never the `<notes>` block.** A populated Notion AI
page has a rich `<summary>` (headings, bullets, Action Items) while its `<notes>`
block is almost always empty — that is the normal state. An empty `<notes>` block
alongside a full `<summary>` is POPULATED.

The page is empty ONLY if its `<summary>` has no real content:
- The `<summary>` block is missing, or is `<empty-block/>` / whitespace.
- Notion's no-content boilerplate: "It looks like your transcript and notes are
  empty this time around" or "could not be generated due to insufficient transcript".
- No headings and no Action Items anywhere in `<summary>`.

**If the `<summary>` is empty: output `RESULT: SKIPPED <reason>` and stop.** Do not
create a page and do not write a placeholder — there is no content to synthesize.

**Critical — do NOT use `mcp__notion__notion-query-meeting-notes` to find the page.**
That tool returns block-reference URLs which are not writable; `notion-update-page`
on a block URL fails with "not a page or database."

**When search finds NO matching page:** output `RESULT: SKIPPED no Notion page found
for "<title>" on <date>` and stop. Do not create a page — Notion AI can only populate
pages it created itself from a live transcript.

---

### Step 2 — Search Notion for related documents (non-1:1 meetings only)

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

These pages provide additional background context for Step 3. If none are found,
proceed to Step 3 without them.

---

### Step 3 — Synthesize

Using the Notion AI `<summary>` and any related pages from Step 2 as inputs, write
a single unified meeting summary following this structure:

```
## Action Items
[Numbered list. Owner in bold. Format: **Owner**: Action]

## Quick Recap
[2-3 sentence overview of what the meeting covered and what was decided]

## Key Discussion Points
[Bullet points covering main topics with enough detail to be useful without
reading the full transcript. Preserve specific technical details, names, decisions.]

## Additional Context
[Background, nuance, or detail worth preserving — constraints, open questions,
alternatives considered and rejected]
```

Action Items lead because they're the highest-signal output for skim-reading.

Synthesis guidelines:
- Notion AI summary is the primary source; related pages provide background context
- Use related pages to enrich Key Discussion Points and Additional Context — not to
  repeat what's already in the meeting note
- Prefer specificity: keep concrete details (names, numbers, tool names, decisions)
- Write in past tense, third person

---

### Step 4 — Write to Notion and file

#### 4a — Update the page

Use `mcp__notion__notion-update-page` with `command: "replace_content"` and the
**page `id` from Step 1** to replace the existing page body with the synthesized summary.

Do NOT change the page title or any properties — only the content body.

**Critical guardrails:**

1. **If `notion-update-page` returns "not a page or database":** you have a block
   reference ID. Stop, re-run Step 1 using `notion-search`, and retry with the real
   page ID. Do NOT write to the parent.
2. **If `notion-update-page` returns "this operation would delete N child page(s)":**
   include them in `new_str` as `<page url="...">Title</page>` blocks, then retry.
   Do NOT set `allow_deleting_content: true` unless certain there's nothing to preserve.
3. **Never call `mcp__notion__notion-create-pages` as a fallback for a failed update.**
   An update failure means the page exists but you have the wrong id — fix Step 1
   and retry, don't create a duplicate.

#### 4b — Add to index and re-parent

After 4a succeeds, classify the meeting and place it under the matching index subpage.
Notion AI's auto-created pages have no parent — without this step they float at workspace
root and are not discoverable from the meeting-notes index.

Most indexes live under "Meeting notes — by meeting name"
(`bc31bdcce7e840efa21e41b40d7be735`). The Reliability Program weekly - Ops and
Reliability Program weekly - Product indexes were moved 2026-05-28 to live under the
Reliability Program Hub's Meetings sub-page (`36e40e54ae388151b737ecca5bfa19e7`).

**Classify** — strip the `@<Date> <Time>` suffix from the title to get the base name,
then match (first match wins):

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

**Insert the link** — fetch the chosen index page, then use `update_content` to
insert a bullet. Always re-fetch before editing; do not guess the current content.

For indices with **monthly headers** (1:1s (various), Other meetings (2026)):
- Find `**<Month> YYYY:**`; insert the new bullet immediately after it (newest-first).
- If the month header doesn't exist, insert a new header + bullet at the top of the list.

For **program-specific indices**:
- Find `**<Year> (newest → oldest):**`; insert the new bullet immediately after it.

Bullet format: `- [<Base Name> — YYYY-MM-DD](<page_url>)`

Escape `|` as `\|` in the base name.

If `update_content` returns `503 service_unavailable`, split into smaller updates —
do NOT retry the same payload.

**Re-parent** — use `notion-move-pages` to move the meeting page under the index subpage:

```json
{
  "page_or_database_ids": ["<meeting_page_id_from_Step_1>"],
  "new_parent": {"type": "page_id", "page_id": "<index_subpage_id>"}
}
```

Do NOT insert `<page url="...">` blocks manually — that fails with `validation_error`.
`notion-move-pages` is the only reliable parenting method.

If any sub-step in 4b fails, do not undo 4a. Report which sub-step failed so it can
be completed manually.

---

## Notes

- Notion AI populates the `<summary>` block automatically from the meeting transcript.
  If `<summary>` is empty the meeting had no usable transcript — skip it.
- Never modify Notion pages from other databases — only meeting note pages.
- The runner (`run_due_meeting_notes.py`) triggers this skill headlessly via launchd.
  Print `RESULT: SUCCESS`, `RESULT: BLOCKED <reason>`, or `RESULT: SKIPPED <reason>`
  as the very last line so the runner can classify the outcome.
- **Never call `notion-create-pages` at any point in this workflow.** Only update
  pages that Notion AI created from a live transcript. If no page exists or the
  summary is empty, skip — there is nothing to synthesize.
- Legacy Zoom scripts (`fetch_zoom_emails.py`, `processed_ids.json`) are kept in
  `scripts/` for reference but are no longer part of this workflow.
