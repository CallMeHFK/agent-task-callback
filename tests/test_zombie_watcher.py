"""Regression tests for the agent-task-callback zombie-watcher bug (0.1.2).

Run: python3 tests/test_zombie_watcher.py   # or `uv run python -m pytest -q`
"""
import asyncio
import builtins
import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path

import httpx

SRC = str(Path(__file__).resolve().parent.parent / "backend" / "main.py")

# backend/main.py imports the host app at module scope, and QwenPaw is not on
# PyPI. Without the host the test stands in for the one name it needs, so the
# suite runs anywhere; with it, nothing is replaced.
try:
    import qwenpaw.plugins.api  # noqa: F401

    HOST_STUBBED = False
except ImportError:
    HOST_STUBBED = True
    _qwenpaw = types.ModuleType("qwenpaw")
    _qwenpaw.__path__ = []
    _plugins = types.ModuleType("qwenpaw.plugins")
    _plugins.__path__ = []
    _api = types.ModuleType("qwenpaw.plugins.api")

    class PluginApi:
        pass

    _api.PluginApi = PluginApi
    _qwenpaw.plugins = _plugins
    _plugins.api = _api
    sys.modules.setdefault("qwenpaw", _qwenpaw)
    sys.modules["qwenpaw.plugins"] = _plugins
    sys.modules["qwenpaw.plugins.api"] = _api

spec = importlib.util.spec_from_file_location("atc", SRC)
atc = importlib.util.module_from_spec(spec)
sys.modules["atc"] = atc
spec.loader.exec_module(atc)


def http_status_error(code, detail="boom"):
    req = httpx.Request("GET", "http://127.0.0.1:19999/api/console/chat/task/t1")
    resp = httpx.Response(code, request=req, json={"detail": detail})
    return httpx.HTTPStatusError(f"{code} error", request=req, response=resp)


class Base(unittest.TestCase):
    def setUp(self):
        self.plug = atc.AgentTaskCallbackPlugin()
        self.updates = []
        self.calls = []
        self.job = {"task_id": "t1", "status": "pending", "registered_at": time.time()}

        def _update_job(task_id, **fields):
            self.updates.append(fields)
            return {}

        self.plug._update_job = staticmethod(_update_job)
        self.plug._get_job = lambda tid: dict(self.job)
        self.plug._post_reply_sync = lambda job, text: True


class TestGone404(Base):
    """A 404 is a deterministic, permanent failure: the framework never deletes
    _bg_tasks, so a missing record can only mean the process restarted."""

    def _run_watch(self, exc_builder):
        def fake_check(job):
            self.calls.append(1)
            raise exc_builder()

        self.plug._check_task_sync = fake_check
        stop = threading.Event()
        atc.POLL_SECONDS = 0
        t0 = time.time()
        self.plug._watch_sync(dict(self.job), stop)
        return time.time() - t0

    def test_404_parks_job_and_stops_looping(self):
        elapsed = self._run_watch(
            lambda: http_status_error(404, "Task not found: t1"))
        self.assertEqual(len(self.calls), 1,
                         "watcher must not re-poll a vanished task")
        self.assertTrue(self.updates, "job status must be written")
        self.assertEqual(self.updates[-1].get("status"), "lost")
        self.assertLess(elapsed, 2.0, "must return without waiting a poll cycle")

    def test_404_note_names_the_ambiguous_causes(self):
        self._run_watch(lambda: http_status_error(404, "Task not found: t1"))
        note = str(self.updates[-1].get("note", ""))
        self.assertIn("404", note)

    def test_500_stays_retryable(self):
        """A 5xx is transient: it must NOT be parked as lost on the first hit."""
        seen = {"n": 0}

        def fake_check(job):
            seen["n"] += 1
            if seen["n"] == 1:
                raise http_status_error(500, "gateway hiccup")
            return {"status": "finished", "result": {"ok": True}}

        self.plug._check_task_sync = fake_check
        atc.POLL_SECONDS = 0
        self.plug._watch_sync(dict(self.job), threading.Event())
        self.assertEqual(seen["n"], 2, "500 must be retried, not abandoned")
        self.assertNotIn("lost", [u.get("status") for u in self.updates])

    def test_transport_error_is_not_lost(self):
        seen = {"n": 0}

        def fake_check(job):
            seen["n"] += 1
            if seen["n"] == 1:
                raise httpx.ConnectError("connection refused")
            return {"status": "finished", "result": {"ok": True}}

        self.plug._check_task_sync = fake_check
        atc.POLL_SECONDS = 0
        self.plug._watch_sync(dict(self.job), threading.Event())
        self.assertEqual(seen["n"], 2)


class TestStrikeCap(Base):
    """方案C: any permanent non-404 failure (wrong base_url -> ValueError, 401)
    used to loop forever too."""

    def test_repeated_failures_stop_at_a_cap(self):
        self.plug._check_task_sync = lambda job: (_ for _ in ()).throw(
            ValueError("non-JSON response; base URL is probably wrong"))
        atc.POLL_SECONDS = 0
        t0 = time.time()
        self.plug._watch_sync(dict(self.job), threading.Event())
        self.assertLess(time.time() - t0, 5.0, "watcher must give up eventually")
        self.assertTrue(self.updates, "giving up must be recorded on the job")
        self.assertNotEqual(self.updates[-1].get("status"), "pending")


class TestHappyPath(Base):
    """Guard the main path against the new early-returns."""

    def test_finished_task_is_delivered_once(self):
        self.plug._check_task_sync = lambda job: {
            "status": "submitted"}
        posts = []
        self.plug._post_reply_sync = lambda job, text: posts.append(text) or True

        calls = {"n": 0}

        def check(job):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"status": "submitted"}
            return {"status": "finished", "final_response": "the answer"}

        self.plug._check_task_sync = check
        atc.POLL_SECONDS = 0
        self.plug._watch_sync(dict(self.job), threading.Event())
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.updates[-1].get("status"), "done")


class TestDeliveryText(Base):
    """The GET payload has no `final_response` key: the reply is the text of
    the last `output` item. The old code therefore POSTed a JSON dump of the
    whole result -- reasoning included, truncated mid-JSON at 4000 chars."""

    def _deliver(self, payload):
        posts = []
        self.plug._post_reply_sync = lambda job, text: posts.append(text) or True
        self.plug._check_task_sync = lambda job: payload
        atc.POLL_SECONDS = 0
        self.plug._watch_sync(dict(self.job), threading.Event())
        self.assertEqual(len(posts), 1, "result must be delivered exactly once")
        return posts[0]

    def test_completed_task_delivers_the_answer_text(self):
        text = self._deliver({
            "status": "finished",
            "result": {
                "status": "completed",
                "session_id": "s9",
                "output": [
                    {"type": "reasoning", "role": "assistant",
                     "content": [{"type": "text", "text": "SECRETISH chain of thought"}]},
                    {"type": "plugin_call_output", "role": "tool",
                     "content": [{"type": "text", "text": "raw tool blob"}]},
                    {"type": "message", "role": "assistant",
                     "content": [{"type": "text", "text": "final answer here"}]},
                ],
            },
        })
        self.assertIn("final answer here", text)
        self.assertNotIn("raw tool blob", text)
        self.assertNotIn("SECRETISH", text)
        self.assertNotIn('"output"', text, "must not POST a JSON dump")

    def test_failed_task_says_so(self):
        """Inner status carries the failure; the outer one is just 'finished'."""
        text = self._deliver({
            "status": "finished",
            "result": {"status": "failed",
                       "error": {"message": "fork finalize blew up"}},
        })
        self.assertIn("Task failed", text)
        self.assertIn("fork finalize blew up", text)
        self.assertFalse(text.lstrip().startswith("{"), "not a JSON dump")

    def test_fallback_when_framework_unavailable(self):
        real = sys.modules.get("qwenpaw.agents.tools.agent_management")
        blocker = _BlockAgentManagement()
        blocker.install()
        try:
            text = self._deliver({
                "status": "finished",
                "result": {"status": "completed", "output": [
                    {"type": "message", "content": [
                        {"type": "text", "text": "fallback answer"}]}]},
            })
        finally:
            blocker.uninstall(real)
        self.assertIn("fallback answer", text)
        self.assertNotIn('"output"', text)


class _BlockAgentManagement:
    """Make `from qwenpaw.agents.tools.agent_management import X` fail, the way
    it would on a QwenPaw version that moved or renamed the helper."""

    def install(self):
        self.saved = {k: v for k, v in sys.modules.items()
                      if k.startswith("qwenpaw.agents.tools")}
        for k in self.saved:
            sys.modules.pop(k, None)
        self.orig_import = builtins.__import__

        def fake(name, *a, **kw):
            if name == "qwenpaw.agents.tools.agent_management":
                raise ImportError("blocked for test")
            return self.orig_import(name, *a, **kw)

        builtins.__import__ = fake

    def uninstall(self, real):
        builtins.__import__ = self.orig_import
        sys.modules.update(self.saved)


class TestBootRearm(unittest.TestCase):
    def _boot_with(self, jobs):
        plug = atc.AgentTaskCallbackPlugin()
        spawned = []
        plug._spawn = lambda job: spawned.append(job["task_id"])
        original = atc._load_state
        atc._load_state = lambda: {"jobs": jobs}
        try:
            asyncio.run(plug._boot())
        finally:
            atc._load_state = original
        return spawned

    def test_unconfirmed_is_not_rearmed(self):
        """unconfirmed already holds a terminal result in `final`; re-watching
        risks a duplicate injection and, after a restart, an endless 404."""
        got = self._boot_with([
            {"task_id": "a", "status": "unconfirmed", "registered_at": time.time()},
        ])
        self.assertEqual(got, [])

    def test_fresh_pending_is_rearmed(self):
        got = self._boot_with([
            {"task_id": "b", "status": "pending", "registered_at": time.time()},
        ])
        self.assertEqual(got, ["b"])

    def test_done_and_cancelled_and_lost_are_left_alone(self):
        got = self._boot_with([
            {"task_id": "c", "status": "done", "registered_at": time.time()},
            {"task_id": "d", "status": "cancelled", "registered_at": time.time()},
            {"task_id": "e", "status": "lost", "registered_at": time.time()},
        ])
        self.assertEqual(got, [])

    def test_stale_pending_is_not_revived(self):
        old = time.time() - 3 * 24 * 3600
        got = self._boot_with([
            {"task_id": "f", "status": "pending", "registered_at": old},
        ])
        self.assertEqual(got, [])


if __name__ == "__main__":
    print("QwenPaw host:", "stubbed (delivery-text fallback)" if HOST_STUBBED
          else "real (delivery-text uses format_background_status_text)")
    unittest.main(verbosity=2)
