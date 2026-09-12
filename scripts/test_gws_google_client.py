"""Tests for gws_google_client.py -- mocks subprocess.run, no live gws call needed."""
import json
import subprocess
import unittest
from unittest import mock

import gws_google_client as gc


def _ok(stdout_obj):
    return mock.Mock(returncode=0, stdout=json.dumps(stdout_obj), stderr="")


class TestGwsCall(unittest.TestCase):
    def test_parses_json_stdout(self):
        with mock.patch.object(subprocess, "run", return_value=_ok({"a": 1})):
            self.assertEqual(gc.gws_call(["x"]), {"a": 1})

    def test_nonzero_exit_raises(self):
        bad = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch.object(subprocess, "run", return_value=bad):
            with self.assertRaises(RuntimeError):
                gc.gws_call(["x"])

    def test_auth_error_translated(self):
        bad = mock.Mock(returncode=1, stdout="", stderr="not authenticated, run login")
        with mock.patch.object(subprocess, "run", return_value=bad):
            with self.assertRaises(RuntimeError) as ctx:
                gc.gws_call(["x"])
            self.assertIn("gws auth login", str(ctx.exception))

    def test_timeout_raises(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("gws", 30)):
            with self.assertRaises(RuntimeError):
                gc.gws_call(["x"])

    def test_binary_not_found_raises(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(RuntimeError):
                gc.gws_call(["x"])

    def test_non_json_stdout_raises(self):
        bad = mock.Mock(returncode=0, stdout="not json", stderr="")
        with mock.patch.object(subprocess, "run", return_value=bad):
            with self.assertRaises(RuntimeError):
                gc.gws_call(["x"])

    def test_expect_json_false_returns_raw_stdout(self):
        with mock.patch.object(subprocess, "run", return_value=_ok("ignored")) as m:
            m.return_value = mock.Mock(returncode=0, stdout="raw text", stderr="")
            self.assertEqual(gc.gws_call(["x"], expect_json=False), "raw text")


class TestGmailWrappers(unittest.TestCase):
    def test_messages_list_builds_params(self):
        with mock.patch.object(gc, "gws_call", return_value={}) as call:
            gc.gmail_messages_list(q="in:sent", max_results=50)
        args = call.call_args[0][0]
        self.assertEqual(args[:4], ["gmail", "users", "messages", "list"])
        params = json.loads(args[-1])
        self.assertEqual(params, {"userId": "me", "q": "in:sent", "maxResults": 50})

    def test_messages_get_includes_metadata_headers(self):
        with mock.patch.object(gc, "gws_call", return_value={}) as call:
            gc.gmail_messages_get("m1", format="metadata", metadata_headers=["Subject"])
        params = json.loads(call.call_args[0][0][-1])
        self.assertEqual(params["metadataHeaders"], ["Subject"])

    def test_threads_get_shape(self):
        with mock.patch.object(gc, "gws_call", return_value={}) as call:
            gc.gmail_threads_get("t1", format="full")
        args = call.call_args[0][0]
        self.assertEqual(args[:4], ["gmail", "users", "threads", "get"])
        self.assertEqual(json.loads(args[-1])["id"], "t1")

    def test_drafts_create_includes_thread_id_when_given(self):
        with mock.patch.object(gc, "gws_call", return_value={"id": "d1"}) as call:
            result = gc.gmail_drafts_create("cmF3", thread_id="t1")
        self.assertEqual(result, {"id": "d1"})
        args = call.call_args[0][0]
        self.assertEqual(args[:4], ["gmail", "users", "drafts", "create"])
        body = json.loads(args[args.index("--json") + 1])
        self.assertEqual(body, {"message": {"raw": "cmF3", "threadId": "t1"}})

    def test_drafts_create_omits_thread_id_when_absent(self):
        with mock.patch.object(gc, "gws_call", return_value={"id": "d1"}) as call:
            gc.gmail_drafts_create("cmF3")
        args = call.call_args[0][0]
        body = json.loads(args[args.index("--json") + 1])
        self.assertEqual(body, {"message": {"raw": "cmF3"}})

    def test_get_profile_shape(self):
        with mock.patch.object(gc, "gws_call", return_value={"emailAddress": "a@x.com"}) as call:
            result = gc.gmail_get_profile()
        self.assertEqual(result["emailAddress"], "a@x.com")
        self.assertEqual(call.call_args[0][0][:3], ["gmail", "users", "getProfile"])

    def test_no_delete_function_exists(self):
        # SECREV-443 hard-blocks Gmail/Drive/Calendar permanent-delete -- confirmed live
        # 2026-09-12 (gws itself refuses `drafts delete`). This module must never grow one.
        self.assertFalse(hasattr(gc, "gmail_drafts_delete"))
        self.assertFalse(any("delete" in name.lower() for name in dir(gc) if not name.startswith("_")))


class TestDocsWrappers(unittest.TestCase):
    def test_docs_get_shape(self):
        with mock.patch.object(gc, "gws_call", return_value={}) as call:
            gc.docs_get("doc1")
        args = call.call_args[0][0]
        self.assertEqual(args[:3], ["docs", "documents", "get"])
        self.assertEqual(json.loads(args[-1])["documentId"], "doc1")

    def test_docs_batch_update_shape(self):
        reqs = [{"insertText": {"location": {"index": 1}, "text": "hi"}}]
        with mock.patch.object(gc, "gws_call", return_value={}) as call:
            gc.docs_batch_update("doc1", reqs)
        args = call.call_args[0][0]
        self.assertEqual(args[:3], ["docs", "documents", "batchUpdate"])
        self.assertEqual(json.loads(args[args.index("--params") + 1])["documentId"], "doc1")
        self.assertEqual(json.loads(args[args.index("--json") + 1]), {"requests": reqs})


if __name__ == "__main__":
    unittest.main()
