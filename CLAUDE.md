# Meeting Notes Workflow

Combines Zoom AI meeting summaries (from email) with Notion AI meeting notes into
a single high-quality summary, written back to the Notion meeting page.

## Workflow

When asked to process meeting notes, execute all steps below in order.

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

### Step 2 — For each meeting, find the Notion meeting note

Use `mcp__notion__notion-query-meeting-notes` to find the matching page.

Filter by title (use the `meeting_title` from the Zoom email). If no exact match,
try a substring of the title. If still no match, skip and tell the user.

Fetch the full page content with `mcp__notion__notion-fetch` using the page URL.

Note: the URL returned by `notion-query-meeting-notes` may point to an internal block rather
than the writable page. If `notion-update-page` returns "not a page or database", fetch the
page first and use the parent page URL from the `<ancestor-path>` instead.

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
## Quick Recap
[2-3 sentence overview of what the meeting covered and what was decided]

## Key Discussion Points
[Bullet points covering the main topics discussed, with enough detail to be
useful without reading the full transcript. Combine and de-duplicate across
both sources. Preserve specific technical details, names, and decisions.]

## Action Items
[Numbered list. Owner in bold. Format: **Owner**: Action]

## Additional Context
[Any background, nuance, or detail from either source that doesn't fit above
but is worth preserving — e.g., constraints, open questions, alternatives
that were considered and rejected]
```

Synthesis guidelines:
- Zoom AI summary and Notion AI notes may overlap — merge, don't duplicate
- Related Notion pages (from Step 2b) provide background context — use them to enrich
  Key Discussion Points and Additional Context, not to repeat what's already in the meeting note
- Prefer specificity: keep concrete details (names, numbers, tool names, decisions)
- If sources contradict, note both versions
- Action items: consolidate from both sources, remove duplicates
- Write in past tense, third person

---

### Step 4 — Update the Notion meeting page

Use `mcp__notion__notion-update-page` with `replace_content` to replace the
existing page body with the synthesized summary.

Do NOT change the page title or any properties — only the content body.

After updating, confirm to the user: meeting title, date, and Notion page URL.

---

## Notes
- Processed Gmail IDs are stored in `scripts/processed_ids.json` — already-processed
  meetings won't be returned by the script on subsequent runs
- If a Notion page cannot be found for a meeting, skip it and report to the user
- Never modify Notion pages from other databases — only meeting note pages
