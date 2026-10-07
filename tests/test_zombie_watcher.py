"""Watcher regression tests: vanished tasks, the strike cap, delivery text,
restart re-arming, and the identity a poll is issued under.

Run: python3 tests/test_zombie_watcher.py   # or `uv run python -m pytest -q`
"""
import asyncio
import builtins
import importlib.util
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import ClassVar

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
            return True

        self.plug._update_job = staticmethod(_update_job)
        self.plug._get_job = lambda tid: dict(self.job)
        self.plug._post_reply_sync = lambda job, text, stop: True


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


class TestCheckTaskContentType(Base):
    """The content-type guard must run before raise_for_status: a wrong base
    URL can answer 404 with an HTML page, and reading that as "task record
    gone" parks the job as lost instead of flagging the misconfiguration."""

    def _check_with(self, resp):
        self.plug._base_url = lambda: "http://x/api"
        saved = atc.httpx.get
        atc.httpx.get = lambda url, headers=None, timeout=None: resp
        try:
            return self.plug._check_task_sync(self.job)
        finally:
            atc.httpx.get = saved

    def test_json_404_is_gone(self):
        req = httpx.Request("GET", "http://x/api/console/chat/task/t1")
        resp = httpx.Response(404, request=req, json={"detail": "nope"})
        with self.assertRaises(httpx.HTTPStatusError):
            self._check_with(resp)

    def test_html_404_is_a_wrong_url_not_a_vanished_task(self):
        req = httpx.Request("GET", "http://x/api/console/chat/task/t1")
        resp = httpx.Response(404, request=req,
                              headers={"content-type": "text/html"},
                              text="<html>not found</html>")
        with self.assertRaises(ValueError):
            self._check_with(resp)


class TestAgeCap(Base):
    """MAX_JOB_AGE was only enforced at boot; a live watcher polled a
    never-terminal task forever."""

    def test_day_old_pending_job_is_abandoned_not_polled(self):
        self.job["registered_at"] = time.time() - 2 * atc.MAX_JOB_AGE
        polls = []

        def check(job):
            polls.append(1)
            raise ValueError("must not reach here")

        self.plug._check_task_sync = check
        atc.POLL_SECONDS = 0
        self.plug._watch_sync(dict(self.job), threading.Event())
        # The strike cap also ends in "abandoned", so the status alone cannot
        # tell the age path apart; only "never polled" can.
        self.assertEqual(polls, [], "an expired job must never be polled")
        self.assertEqual(self.updates[-1].get("status"), "abandoned")


class TestCancelBeforeDelivery(Base):
    """A cancel landing between the final poll and the POST must win."""

    def test_cancel_during_final_poll_blocks_delivery(self):
        stop = threading.Event()

        def check(job):
            stop.set()  # cancel lands while the terminal payload is in flight
            return {"status": "finished", "result": {}}

        self.plug._check_task_sync = check
        posts = []
        self.plug._post_reply_sync = lambda job, text, stop: posts.append(text) or True
        self.plug._watch_sync(dict(self.job), stop)
        self.assertEqual(posts, [], "cancel must win over a pending delivery")


class TestRespawnOnReregister(unittest.TestCase):
    """Re-registering a task from a different session must replace the live
    watcher; absorbing it would deliver the result to the old session."""

    JOB: ClassVar[dict] = {"task_id": "t1", "session_id": "s-old", "user_id": "u",
                           "channel": "console", "agent_id": "a", "target_agent": None}

    def _plug_with_live_watcher(self, job):
        plug = atc.AgentTaskCallbackPlugin()
        old = atc._WatcherThread(dict(job))
        old.is_alive = lambda: True
        plug._watchers[job["task_id"]] = old
        return plug, old

    def test_new_delivery_target_replaces_the_live_watcher(self):
        plug, old = self._plug_with_live_watcher(self.JOB)
        new_job = dict(self.JOB, session_id="s-new")
        saved_impl = atc._IMPL
        atc._IMPL = types.SimpleNamespace(
            _watch_sync=lambda job, stop: stop.wait(1))
        try:
            plug._spawn(new_job)
        finally:
            atc._IMPL = saved_impl
        self.assertTrue(old._stop_event.is_set(), "stale watcher must be stopped")
        self.assertIsNot(plug._watchers["t1"], old)
        self.assertIs(plug._watchers["t1"]._job, new_job)
        plug._watchers["t1"].stop()
        plug._watchers["t1"].join(timeout=3)

    def test_same_delivery_target_is_idempotent(self):
        plug, old = self._plug_with_live_watcher(self.JOB)
        plug._spawn(dict(self.JOB))
        self.assertIs(plug._watchers["t1"], old, "same target must not respawn")
        self.assertFalse(old._stop_event.is_set())


class TestStatePrune(unittest.TestCase):
    """Terminal jobs were kept forever; the state file grew without bound."""

    def test_old_terminal_jobs_are_pruned_on_save(self):
        now = time.time()
        state = {"jobs": [
            {"task_id": "old-done", "status": "done",
             "completed_at": now - 2 * atc.TERMINAL_JOB_KEEP},
            {"task_id": "new-done", "status": "done", "completed_at": now},
            {"task_id": "pending", "status": "pending", "registered_at": now},
        ]}
        with tempfile.TemporaryDirectory() as d:
            saved = atc.STATE_PATH
            atc.STATE_PATH = Path(d) / "state.json"
            try:
                atc._save_state(state)
                jobs = json.loads(atc.STATE_PATH.read_text())["jobs"]
            finally:
                atc.STATE_PATH = saved
        self.assertEqual([j["task_id"] for j in jobs], ["new-done", "pending"])


class TestSupersededWrite(unittest.TestCase):
    """A watcher's terminal write must never clobber a cancel or a
    re-registration that landed while it was delivering."""

    def _run_watch(self, store, watcher_job, post, stop=None):
        plug = atc.AgentTaskCallbackPlugin()
        plug._check_task_sync = lambda job: {"status": "finished", "result": {}}
        plug._post_reply_sync = post
        saved = atc._load_state, atc._save_state
        atc._load_state = lambda: store
        atc._save_state = lambda state: None  # store is mutated in place
        try:
            plug._watch_sync(watcher_job, stop or threading.Event())
        finally:
            atc._load_state, atc._save_state = saved
        return store["jobs"][0]

    def test_cancel_during_post_is_not_clobbered(self):
        """The POST physically went out (at-least-once window), but the
        cancel already owns the record: it must not become "done"."""
        stored = {"task_id": "t1", "job_id": "j1", "status": "pending",
                  "registered_at": time.time()}

        def post(job, text, stop):
            stored["status"] = "cancelled"  # cancel lands mid-delivery
            stop.set()
            return True

        job = self._run_watch({"jobs": [stored]}, dict(stored), post)
        self.assertEqual(job["status"], "cancelled")

    def test_interrupted_delivery_leaves_the_record_pending(self):
        """Shutdown during the 409 backoff must not write a terminal status
        for a result that was never even POSTed."""
        stored = {"task_id": "t1", "job_id": "j1", "status": "pending",
                  "registered_at": time.time()}
        stop = threading.Event()

        def post(job, text, stop_):
            stop.set()  # shutdown lands mid-delivery; implicit None = stopped
            # before any attempt

        job = self._run_watch({"jobs": [stored]}, dict(stored), post, stop)
        self.assertEqual(job["status"], "pending",
                         "an undelivered result must stay re-armable")

    def test_reregistered_job_is_not_clobbered_by_old_watcher(self):
        stored = {"task_id": "t1", "job_id": "j2", "status": "pending",
                  "registered_at": time.time()}
        old_job = dict(stored, job_id="j1")
        job = self._run_watch({"jobs": [stored]}, old_job,
                              lambda job, text, stop: True)
        self.assertEqual(job["status"], "pending",
                         "the new registration owns the state record now")

    def test_pending_job_accepts_the_terminal_write(self):
        stored = {"task_id": "t1", "job_id": "j1", "status": "pending",
                  "registered_at": time.time()}
        job = self._run_watch({"jobs": [stored]}, dict(stored),
                              lambda job, text, stop: True)
        self.assertEqual(job["status"], "done")


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
        self.plug._post_reply_sync = lambda job, text, stop: posts.append(text) or True

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
        self.plug._post_reply_sync = lambda job, text, stop: posts.append(text) or True
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
        original = atc._load_state, atc._save_state
        atc._load_state = lambda: {"jobs": jobs}
        atc._save_state = lambda state: None  # keep the real state file untouched
        try:
            asyncio.run(plug._boot())
        finally:
            atc._load_state, atc._save_state = original
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


class _FakeAgentContext:
    """Shadow the request-scoped context the registering tool reads its own
    identity from, which only exists inside a live app turn."""

    def install(self):
        names = ("qwenpaw.app", "qwenpaw.app.agent_context")
        self.saved = {n: sys.modules.get(n) for n in names}
        app = types.ModuleType("qwenpaw.app")
        app.__path__ = []
        ctx = types.ModuleType("qwenpaw.app.agent_context")
        ctx.get_current_agent_id = lambda: "default"
        ctx.get_current_session_id = lambda: "s-parent"
        ctx.get_current_user_id = lambda: "u-1"
        ctx.get_current_channel = lambda: "console"
        app.agent_context = ctx
        sys.modules["qwenpaw.app"] = app
        sys.modules["qwenpaw.app.agent_context"] = ctx

    def uninstall(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class TestTargetAgentIdentity(Base):
    """A background task belongs to the agent that ran it, and the framework's
    own check_agent_task polls as that agent. Polling as the submitting agent
    instead eventually reads as MAX_STRIKES failed checks and the job goes
    `abandoned` -- so the registered tool has to be able to carry the target."""

    def _register(self, *args, **kwargs):
        saved = (atc._IMPL, atc._load_state, atc._save_state)
        store = {"jobs": []}
        atc._IMPL = self.plug
        atc._load_state = lambda: store
        atc._save_state = lambda state: store.update(state)
        self.plug._spawn = lambda job: None
        fake = _FakeAgentContext()
        fake.install()
        try:
            out = atc.watch_agent_task(*args, **kwargs)
        finally:
            fake.uninstall()
            atc._IMPL, atc._load_state, atc._save_state = saved
        return out, store["jobs"][0]

    def test_named_target_agent_reaches_the_job(self):
        out, job = self._register("t1", target_agent="SE")
        self.assertTrue(out.startswith("Watching task t1"), out)
        self.assertEqual(job["target_agent"], "SE")

    def test_polling_asks_as_the_target_agent(self):
        _, job = self._register("t1", target_agent="SE")
        self.assertEqual(self.plug._query_headers(job)["X-Agent-Id"], "SE")

    def test_target_agent_stays_optional(self):
        _, job = self._register("t1")
        self.assertIsNone(job["target_agent"])
        self.assertEqual(self.plug._query_headers(job)["X-Agent-Id"], "default")


class TestReregister(unittest.TestCase):
    """Re-registration must keep the state record compatible with whoever
    owns the live watcher, and must never re-watch a delivered task."""

    def setUp(self):
        self.store = {"jobs": []}
        self.plug = atc.AgentTaskCallbackPlugin()
        self.plug._spawn = lambda job: None
        self.saved = (atc._IMPL, atc._load_state, atc._save_state)
        atc._IMPL = self.plug
        atc._load_state = lambda: self.store
        atc._save_state = lambda state: self.store.update(state)
        self.fake = _FakeAgentContext()
        self.fake.install()

    def tearDown(self):
        self.fake.uninstall()
        atc._IMPL, atc._load_state, atc._save_state = self.saved

    def test_same_identity_keeps_the_live_watchers_job_id(self):
        atc.watch_agent_task("t1")
        first_id = self.store["jobs"][0]["job_id"]
        atc.watch_agent_task("t1")
        self.assertEqual(len(self.store["jobs"]), 1)
        self.assertEqual(self.store["jobs"][0]["job_id"], first_id,
                         "the running watcher's guarded write must still fit")

    def test_new_session_gets_a_fresh_job_id(self):
        atc.watch_agent_task("t1")
        first_id = self.store["jobs"][0]["job_id"]
        ctx = sys.modules["qwenpaw.app.agent_context"]
        ctx.get_current_session_id = lambda: "s-other"
        atc.watch_agent_task("t1")
        self.assertEqual(len(self.store["jobs"]), 1)
        self.assertNotEqual(self.store["jobs"][0]["job_id"], first_id)
        self.assertEqual(self.store["jobs"][0]["session_id"], "s-other")

    def test_delivered_task_is_not_rewatched(self):
        self.store["jobs"].append({
            "task_id": "t1", "job_id": "j0", "status": "done",
            "registered_at": time.time(), "completed_at": time.time(),
        })
        out = atc.watch_agent_task("t1")
        self.assertIn("not re-watching", out)
        self.assertEqual(self.store["jobs"][0]["job_id"], "j0",
                         "the terminal record must be left alone")


if __name__ == "__main__":
    print("QwenPaw host:", "stubbed (delivery-text fallback)" if HOST_STUBBED
          else "real (delivery-text uses format_background_status_text)")
    unittest.main(verbosity=2)
