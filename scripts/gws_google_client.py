#!/usr/bin/env python3
"""Thin Gmail/Docs client backed by the `gws` CLI (Affirm's managed Google Workspace
CLI, Jamf-provisioned, org-approved credential) instead of a per-user Google Cloud
project + OAuth client.

Added 2026-09-12 to retire this repo's personal-OAuth Gmail/Docs tokens (all minted
from the "Peak Events" GCP project, decommissioning 2026-09-30 -- see
~/.claude/plans/shiny-shimmying-popcorn.md). Every function here returns the RAW parsed
JSON from the real Gmail/Docs REST API (gws is a faithful passthrough, not a reshaping
layer), so callers' existing response parsing (draft["id"], msg["payload"]["headers"],
etc.) needs no changes -- only the credential-loading + service-build call site at each
caller changes.

Scope note: `gws`'s own service-account credential is restricted to the read/list/
draft-create surface approved by SECREV-443. Verified live 2026-09-12: `gws gmail users
drafts delete --help` is hard-blocked by an actual enforced guardrail, not just a
documentation convention -- "SECREV-443 approved Google Drive/Calendar write & edit and
Gmail draft only -- sending email, permanently deleting Drive/Gmail/Calendar data,
mailbox auto-forward/delegate/sendAs changes, external sharing, and Chat sends are
blocked." So this module deliberately has NO delete function -- an earlier plan draft
assumed draft-delete was in-bounds (it isn't) before this was checked against the real
CLI. Never add `messages.send`, `drafts.send`, or any delete operation here.

CLI is not owned by this module -- never build a gws subcommand from config or
model-generated content; every function below issues one of a small, fixed set of
subcommands with caller-supplied *data* (message bodies, doc IDs), never caller-supplied
*commands*.
"""
import base64
import json
import shutil
import subprocess

GWS = shutil.which("gws") or "/usr/local/bin/gws"


def gws_call(args, expect_json=True):
    """Run `gws <args>` and return parsed JSON stdout (or raw stdout if
    expect_json=False). Raises RuntimeError with a translated message on failure.
    Mirrors daily_briefing.py's gws_call() -- same subprocess boundary, same
    auth-error translation."""
    try:
        result = subprocess.run(
            [GWS, *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError(f"gws binary not found at {GWS}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gws {' '.join(args)} timed out after 30s")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        low = stderr.lower()
        if "auth" in low or "login" in low or "credential" in low or "keychain" in low:
            raise RuntimeError(
                f"gws {' '.join(args[:3])} failed - looks like an auth problem "
                f"(run: gws auth login --full). Raw error: {stderr}"
            )
        raise RuntimeError(f"gws {' '.join(args[:3])} failed (exit {result.returncode}): {stderr}")

    if not expect_json:
        return result.stdout
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gws {' '.join(args[:3])} returned non-JSON stdout: {e}")


# ---------- Gmail ----------

def gmail_get_profile():
    return gws_call(["gmail", "users", "getProfile", "--params", json.dumps({"userId": "me"})])


def gmail_messages_list(q=None, label_ids=None, max_results=None):
    params = {"userId": "me"}
    if q:
        params["q"] = q
    if label_ids:
        params["labelIds"] = label_ids
    if max_results:
        params["maxResults"] = max_results
    return gws_call(["gmail", "users", "messages", "list", "--params", json.dumps(params)])


def gmail_messages_get(msg_id, format="full", metadata_headers=None):
    params = {"userId": "me", "id": msg_id, "format": format}
    if metadata_headers:
        params["metadataHeaders"] = metadata_headers
    return gws_call(["gmail", "users", "messages", "get", "--params", json.dumps(params)])


def gmail_threads_get(thread_id, format="full", metadata_headers=None):
    params = {"userId": "me", "id": thread_id, "format": format}
    if metadata_headers:
        params["metadataHeaders"] = metadata_headers
    return gws_call(["gmail", "users", "threads", "get", "--params", json.dumps(params)])


def gmail_drafts_create(raw_b64, thread_id=None):
    """raw_b64: base64url-encoded RFC 2822 message (e.g. base64.urlsafe_b64encode(
    msg.as_bytes()).decode()), same input every caller already builds today."""
    message = {"raw": raw_b64}
    if thread_id:
        message["threadId"] = thread_id
    body = {"message": message}
    return gws_call(["gmail", "users", "drafts", "create",
                      "--params", json.dumps({"userId": "me"}),
                      "--json", json.dumps(body)])


# ---------- Docs ----------

def docs_get(doc_id):
    return gws_call(["docs", "documents", "get", "--params", json.dumps({"documentId": doc_id})])


def docs_batch_update(doc_id, requests):
    return gws_call(["docs", "documents", "batchUpdate",
                      "--params", json.dumps({"documentId": doc_id}),
                      "--json", json.dumps({"requests": requests})])
