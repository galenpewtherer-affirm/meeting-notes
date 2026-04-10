# Meeting Notes

Combine Zoom AI meeting summaries with Notion AI meeting notes into a single
high-quality summary, written back to the Notion meeting page.

Follow the full workflow in `CLAUDE.md` (Steps 1–4):
1. Run fetch_zoom_emails.py to get unprocessed Zoom summaries
2. Find matching Notion meeting note for each meeting
3. Synthesize both sources into a unified summary
4. Replace the Notion meeting page content with the combined output

Pass `--all` to reprocess all meetings, or `--since YYYY-MM-DD` to look further back.
