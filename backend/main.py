"""Agent Task Callback — opt-in watcher that resumes the registering session."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from qwenpaw.plugins.api import PluginApi

try:
    from agentscope_runtime.common.logger import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str):
        return logging.getLogger(name)


logger = get_logger("agent_task_callback")

STATE_PATH = Path.home() / ".qwenpaw" / "agent-task-callback.json"
POLL_SECONDS = 20
HTTP_TIMEOUT = 30.0

# A watcher must not outlive a problem it cannot fix. 90 consecutive failures is
# ~30 min of a broken base_url; a day-old pending job belongs to a task record
# that died with an earlier process.
MAX_STRIKES = 90
MAX_JOB_AGE = 24 * 3600
MAX_DELIVERY_CHARS = 8000

# Terminal jobs are kept for inspection via callback_task_status, then pruned so
# the state file cannot grow without bound over months of use.
TERMINAL_STATUSES = {"done", "unconfirmed", "cancelled", "lost", "abandoned",
                     "error", "expired"}
TERMINAL_JOB_KEEP = 7 * 24 * 3600

# Guards the load-modify-save sequences against watcher threads finishing at
# the same moment and clobbering each other's updates.
_STATE_LOCK = threading.Lock()


def _is_gone(exc: BaseException) -> bool:
    """Did this error mean the task record can never come back?

    The framework stores background tasks in ``_bg_tasks``, a module dict that
    is only ever written and read — never pruned. A 404 therefore means the id
    belonged to an earlier process (or to no process at all), not "not yet".
    """
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 404

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("callback state unreadable; starting fresh")
    return {"jobs": []}


def _save_state(state: dict) -> None:
    now = time.time()
    state["jobs"] = [
        job
        for job in state.get("jobs", [])
        if job.get("status") not in TERMINAL_STATUSES
        or now - job.get("completed_at", job.get("registered_at", 0))
        <= TERMINAL_JOB_KEEP
    ]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


# Module-level singleton so module-level tool functions can delegate to it
# (bound methods cannot receive the ``_tool_descriptor`` attribute that
# ``register_tool`` attaches, which raised AttributeError on ... ).
_IMPL = None


def watch_agent_task(task_id: str, target_agent: str = "") -> str:
    """Watch an inter-agent background task id and resume this session on completion.

    Args:
        task_id (`str`):
            The id returned by ``submit_to_agent``.
        target_agent (`str`, optional):
            The agent the task was submitted to. The watcher polls with that
            identity, exactly as ``check_agent_task`` does; omit it only when
            the task runs in this same agent.
    """
    if _IMPL is None:
        return "ERROR: plugin not initialised"
    return _IMPL.watch_agent_task(task_id, target_agent)


def callback_task_status() -> str:
    """List recent callback jobs recorded by this plugin."""
    if _IMPL is None:
        return "ERROR: plugin not initialised"
    return _IMPL.callback_task_status()


def cancel_task_callback(task_id: str) -> str:
    """Cancel a pending callback watcher (child task unaffected)."""
    if _IMPL is None:
        return "ERROR: plugin not initialised"
    return _IMPL.cancel_task_callback(task_id)


class AgentTaskCallbackMode:
    """Unconditional mode that publishes this plugin's tools into the workspace ToolRegistry."""

    name = "agent-task-callback"

    def setup(self, workspace: object) -> None:
        registry = workspace.plugins.tool_registry
        for desc in self.tools():
            if desc.name in registry:
                registry.unregister(desc.name)
            registry.register(desc)

    def commands(self) -> list:
        """No slash commands contributed."""
        return []

    def hooks(self) -> list:
        """No runtime hooks contributed."""
        return []

    def prompt_contributors(self) -> list:
        """No prompt sections contributed."""
        return []

    def tools(self) -> list:
        descs = []
        for fn in (watch_agent_task, callback_task_status, cancel_task_callback):
            desc = getattr(fn, "_tool_descriptor", None)
            if desc is not None:
                descs.append(desc)
        return descs

    def is_active(self, ctx: object) -> bool:
        return True

    async def on_turn_start(self, ctx: object) -> None:
        """No-op: the mode only publishes tools."""

    async def on_conversation_reset(self, ctx: object) -> None:
        """No-op: the mode only publishes tools."""


class _WatcherThread(threading.Thread):
    """Background thread that polls one task and re-injects the result."""

    daemon = True

    def __init__(self, job: dict) -> None:
        super().__init__(name=f"agent-task-callback-{job['task_id']}")
        self._job = job
        # Not ``_stop``: Thread reserves that name for join() bookkeeping.
        self._stop_event = threading.Event()
        self._impl = _IMPL

    def matches(self, job: dict) -> bool:
        """Same delivery target? A re-registration from another session must
        replace this watcher, not be silently absorbed by it."""
        keys = ("session_id", "user_id", "channel", "agent_id", "target_agent")
        return all(self._job.get(k) == job.get(k) for k in keys)

    def run(self) -> None:
        impl = self._impl or _IMPL
        if impl is None:
            return
        impl._watch_sync(self._job, self._stop_event)

    def stop(self) -> None:
        self._stop_event.set()


class AgentTaskCallbackPlugin:
    """Watches submitted agent tasks and re-injects results into the origin session."""

    def __init__(self) -> None:
        self._watchers: dict[str, _WatcherThread] = {}
        self._shutdown = False

    # ---- registration ---------------------------------------------------
    def register(self, api: PluginApi) -> None:
        api.register_tool(
            tool_name="watch_agent_task",
            tool_func=watch_agent_task,
            description=(
                "Watch an inter-agent background task id (returned by "
                "submit_to_agent). When the task finishes, its result is "
                "automatically sent back to the current session as a new "
                "user turn, so the parent agent resumes without polling. "
                "Context (agent/session/user/channel) is captured "
                "automatically. Pass target_agent — the agent the task was "
                "submitted to — so the watcher polls under that identity, the "
                "way check_agent_task does."
            ),
            icon="\u23f3",
            tool_type="network",
            enabled=True,
        )
        api.register_tool(
            tool_name="callback_task_status",
            tool_func=callback_task_status,
            description="List recent callback jobs recorded by this plugin.",
            icon="\U0001f4cb",
            tool_type="network",
            enabled=True,
        )
        api.register_tool(
            tool_name="cancel_task_callback",
            tool_func=cancel_task_callback,
            description=(
                "Cancel a pending callback watcher. Does NOT stop the child "
                "task itself, only this plugin's watcher for it."
            ),
            icon="\U0001f6ab",
            tool_type="network",
            enabled=True,
        )
        api.register_mode(AgentTaskCallbackMode)
        api.register_startup_hook(
            hook_name="agent_task_callback_boot",
            callback=self._boot,
            priority=10,
        )
        api.register_shutdown_hook(
            hook_name="agent_task_callback_halt",
            callback=self._halt,
            priority=10,
        )
        logger.info("agent-task-callback registered (3 tools)")

    # ---- lifecycle ------------------------------------------------------
    async def _boot(self) -> None:
        self._shutdown = False
        now = time.time()
        pending = [
            job
            for job in _load_state().get("jobs", [])
            if job.get("status") == "pending"
        ]
        # ``unconfirmed`` is deliberately not re-armed: it already holds a
        # terminal result in ``final`` (see the note written with it), so
        # re-watching risks a second delivery to the parent session.
        fresh = [
            job
            for job in pending
            if now - job.get("registered_at", 0) <= MAX_JOB_AGE
        ]
        # Stale pendings can never be re-armed, so leaving them "pending"
        # forever would misreport them in callback_task_status.
        for job in pending:
            if job not in fresh:
                self._update_job(
                    job["task_id"],
                    status="expired",
                    completed_at=now,
                    note="pending past MAX_JOB_AGE at boot; not re-armed",
                )
        for job in fresh:
            self._spawn(job)
        logger.info(
            "agent-task-callback boot: re-armed %d job(s), skipped %d stale",
            len(fresh), len(pending) - len(fresh),
        )

    async def _halt(self) -> None:
        self._shutdown = True
        watchers = list(self._watchers.values())
        for thread in watchers:
            thread.stop()
        for thread in watchers:
            thread.join(timeout=2)
        self._watchers.clear()

    # ---- internals ------------------------------------------------------
    def _spawn(self, job: dict) -> None:
        task_id = job["task_id"]
        self._watchers = {
            tid: t for tid, t in self._watchers.items() if t.is_alive()
        }
        existing = self._watchers.get(task_id)
        if existing is not None:
            if existing.matches(job):
                return  # idempotent: never double-watch the same task
            existing.stop()  # re-registered with a new target: the new job wins
        thread = _WatcherThread(job)
        self._watchers[task_id] = thread
        thread.start()

    @staticmethod
    def _update_job(task_id: str, expected_job: dict | None = None,
                    **fields: Any) -> bool:
        """Apply ``fields`` to the stored job; True when the write landed.

        With ``expected_job`` the write lands only while the stored job is
        still that exact pending registration, so a watcher's terminal write
        can never clobber a cancel or a re-registration that beat it.
        """
        with _STATE_LOCK:
            state = _load_state()
            updated = False
            for job in state.get("jobs", []):
                if job.get("task_id") == task_id:
                    if expected_job is None or (
                        job.get("job_id") == expected_job.get("job_id")
                        and job.get("status") == "pending"
                    ):
                        job.update(fields)
                        updated = True
                    break
            if updated:
                _save_state(state)
            return updated

    @staticmethod
    def _get_job(task_id: str) -> dict | None:
        return next(
            (j for j in _load_state().get("jobs", []) if j.get("task_id") == task_id),
            None,
        )

    @staticmethod
    def _base_url() -> str:
        """Resolve API URL: framework resolver > environment > port 19999.

        Environment fallback accepts QWENPAW_RUNTIME_API_URL, or
        QWENPAW_RUNTIME_HOST / QWENPAW_RUNTIME_PORT. All paths retain
        the required /api suffix. A nonempty framework result wins,
        including a default supplied by the framework itself.
        """
        import os

        def normalize(base: str) -> str:
            base = base.strip().rstrip("/")
            return base if base.endswith("/api") else f"{base}/api"

        try:
            from qwenpaw.agents.tools.agent_management import (
                _normalize_api_base_url,
            )

            resolved = _normalize_api_base_url(None)
            if resolved and resolved.strip():
                return normalize(resolved)
        except Exception:  # framework unavailable: fall back to env/port
            logger.debug("framework API resolver unavailable", exc_info=True)

        explicit = os.environ.get("QWENPAW_RUNTIME_API_URL", "").strip()
        if explicit:
            return normalize(explicit)
        host = os.environ.get("QWENPAW_RUNTIME_HOST", "").strip() or "127.0.0.1"
        port = os.environ.get("QWENPAW_RUNTIME_PORT", "").strip() or "19999"
        return normalize(f"http://{host}:{port}")

    def _headers(self, agent_id: str | None = None) -> dict:
        import os

        token = os.environ.get("QWENPAW_RUNTIME_INTERNAL_TOKEN", "")
        headers = {"X-Agent-Id": agent_id or "default"}
        if token:
            headers["X-Internal-Token"] = token
        return headers

    def _query_headers(self, job: dict) -> dict:
        """Poll as the agent ``check_agent_task`` would: the target when the
        caller named one, else this one.

        ``submit_to_agent`` forwards the request under the *target* agent
        identity and the framework's own poller passes that same id, so
        matching it keeps the watcher correct if task storage ever becomes
        per-agent. On 2.2.1 the store is one global dict, which is why
        watching has worked without naming the target.
        """
        return self._headers(job.get("target_agent") or job.get("agent_id"))

    def _check_task_sync(self, job: dict) -> dict:
        task_id = job["task_id"]
        resp = httpx.get(
            f"{self._base_url()}/console/chat/task/{task_id}",
            headers=self._query_headers(job),
            timeout=HTTP_TIMEOUT,
        )
        # Content-type first: a wrong base URL can answer 404 with an HTML
        # error page, and checking the status before the type would read that
        # as "the task record is gone" and park the job as lost.
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype.lower():
            raise ValueError(
                f"non-JSON response from {resp.url} (content-type={ctype!r}); "
                "the API base URL is probably wrong"
            )
        resp.raise_for_status()
        return resp.json()

    def _post_reply_sync(self, job: dict, text: str,
                         stop: threading.Event) -> bool | None:
        """True = delivered, False = attempted and failed, None = stopped
        before any attempt whose outcome the caller must not guess at."""
        payload = {
            "session_id": job["session_id"],
            "user_id": job["user_id"],
            "channel": job["channel"],
            "timeout": 240,
            "input": [
                {"role": "user", "content": [{"type": "text", "text": text}]}
            ],
        }
        # A 409 means the parent session is still mid-turn; that is a
        # "not yet" rather than a failure, so back off and retry. The stop
        # event is honoured between attempts so a cancel or shutdown during
        # the backoff does not deliver anyway.
        max_attempts = 30
        for attempt in range(max_attempts):
            if stop.is_set():
                logger.info("callback for %s abandoned (stopped)", job["task_id"])
                return None
            try:
                resp = httpx.post(
                    f"{self._base_url()}/console/chat/task",
                    headers=self._headers(job.get("agent_id")),
                    json=payload,
                    timeout=HTTP_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                logger.warning("callback post transport error: %s", exc)
                return False
            if 200 <= resp.status_code < 300:
                return True
            if resp.status_code == 409:
                logger.info(
                    "callback for %s deferred (session busy), retry %d/%d",
                    job["task_id"], attempt + 1, max_attempts,
                )
                if stop.wait(20):
                    logger.info("callback for %s abandoned (stopped)", job["task_id"])
                    return None
                continue
            logger.warning(
                "callback post HTTP %s: %s", resp.status_code, resp.text[:200],
            )
            return False
        logger.warning("callback for %s still blocked after retries", job["task_id"])
        return False

    @staticmethod
    def _delivery_text(task_id: str, data: dict) -> str:
        """Render a terminal task payload for the parent session.

        The reply is not a field on this payload -- it is the text of the last
        ``output`` item -- so this defers to the same formatter the framework's
        own ``check_agent_task`` uses, which also turns the inner
        ``result.status == "failed"`` into a failure sentence. The outer status
        is ``finished`` either way, so the plugin cannot tell those apart.
        """
        try:
            from qwenpaw.agents.tools.agent_management import (
                format_background_status_text,
            )

            return format_background_status_text(task_id, data)[:MAX_DELIVERY_CHARS]
        except Exception:  # helper moved or renamed: use the local extractor
            logger.debug("framework formatter unavailable", exc_info=True)

        # Fallback mirrors the framework's two decisions: the reply is the text
        # of the LAST output item, and a failed task is reported as a failure
        # even though the outer status is "finished" either way. Every step is
        # isinstance-guarded: a malformed payload must not kill the watcher and
        # orphan the job as pending forever.
        inner = data.get("result") or {}
        if not isinstance(inner, dict):
            return ""
        error = (inner.get("error") or {})
        if not isinstance(error, dict):
            error = {}
        if inner.get("status") == "failed" and error.get("message"):
            return f"Task failed.\n\nError: {error['message']}"[:MAX_DELIVERY_CHARS]
        output = inner.get("output")
        blocks = output[-1] if isinstance(output, list) and output else {}
        if not isinstance(blocks, dict):
            blocks = {}
        content = blocks.get("content")
        if not isinstance(content, list):
            content = []
        text = "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        return text[:MAX_DELIVERY_CHARS]

    def _watch_sync(self, job: dict, stop: threading.Event) -> None:
        task_id = job["task_id"]
        terminal = {"finished", "failed", "cancelled", "timeout", "error"}
        result = None

        strikes = 0
        while not stop.is_set():
            current = self._get_job(task_id)
            if current is None or current.get("status") == "cancelled":
                logger.info("watcher for %s stopped (cancelled/missing)", task_id)
                return
            if time.time() - current.get("registered_at", 0) > MAX_JOB_AGE:
                logger.warning("task %s still non-terminal after %dh; dropping "
                               "watcher", task_id, MAX_JOB_AGE // 3600)
                self._update_job(
                    task_id,
                    expected_job=job,
                    status="abandoned",
                    completed_at=time.time(),
                    note=f"still non-terminal after {MAX_JOB_AGE // 3600}h; "
                         "the task record belongs to a dead process",
                )
                return
            try:
                data = self._check_task_sync(job)
            except Exception as exc:  # noqa: BLE001 - keep watcher alive
                if _is_gone(exc):
                    logger.warning("task %s vanished (404); dropping watcher",
                                   task_id)
                    self._update_job(
                        task_id,
                        expected_job=job,
                        status="lost",
                        completed_at=time.time(),
                        note="HTTP 404 from the task endpoint: record not "
                             "found, lost on a restart, or the task id / base "
                             "URL is wrong",
                    )
                    return
                strikes += 1
                if strikes >= MAX_STRIKES:
                    logger.warning(
                        "task %s failed %d consecutive checks; dropping watcher"
                        " (last error: %s)", task_id, strikes, exc)
                    self._update_job(
                        task_id,
                        expected_job=job,
                        status="abandoned",
                        completed_at=time.time(),
                        note=f"gave up after {strikes} failed checks; "
                             f"last error: {exc}",
                    )
                    return
                logger.warning("check %s failed: %s", task_id, exc)
            else:
                strikes = 0
                if data.get("status") in terminal:
                    result = data
                    break
            stop.wait(POLL_SECONDS)

        if result is None:
            return

        # A cancel or shutdown can land between the last poll and now; the
        # delivery below must not fire for a watcher that was already stopped.
        if stop.is_set():
            logger.info("watcher for %s stopped before delivery", task_id)
            return
        current = self._get_job(task_id)
        if current is None or current.get("status") == "cancelled":
            logger.info("watcher for %s cancelled before delivery", task_id)
            return

        try:
            status = result.get("status", "unknown")
            text = self._delivery_text(task_id, result)
            if not text:
                text = f"(agent task {task_id} {status}, no text content)"
        except Exception as exc:  # a rendering bug must not orphan the job
            # as pending and re-arm it on every restart
            logger.exception("rendering callback for %s failed", task_id)
            self._update_job(
                task_id,
                expected_job=job,
                status="error",
                completed_at=time.time(),
                note=f"delivery render failed: {exc}",
            )
            return

        sent = self._post_reply_sync(job, text, stop)
        if sent is None:
            # Interrupted (shutdown or cancel) before any delivery happened:
            # leave the record alone — a cancel already rewrote it, and a
            # pending record can still be re-armed at the next boot.
            logger.info("delivery for %s interrupted; record untouched", task_id)
            return
        fields: dict[str, Any] = {
            "expected_job": job,
            "completed_at": time.time(),
            "final": text[:2000],
        }
        if sent:
            fields["status"] = "done"
        else:
            fields["status"] = "unconfirmed"
            fields["note"] = ("POST outcome unknown; not auto-retried to "
                              "avoid duplicate turns")
        if self._update_job(task_id, **fields):
            if sent:
                logger.info("callback delivered for %s", task_id)
            else:
                logger.warning("callback delivery unconfirmed for %s", task_id)
        else:
            logger.info("watcher for %s superseded before the final write; "
                        "leaving the newer record alone", task_id)

    # ---- tools ----------------------------------------------------------
    def watch_agent_task(self, task_id: str, target_agent: str = "") -> str:
        """Register a callback watcher for a task id (see submit_to_agent).

        Call automatically right after submitting a background task that
        you want to be resumed on. It records the current agent, session,
        user and channel, then polls until the task reaches a terminal
        state and re-injects the result into this same session.

        ``target_agent`` names the agent the task was submitted to, which is
        the identity the poll is issued under; empty means poll as this agent.
        """
        from qwenpaw.app.agent_context import (
            get_current_agent_id,
            get_current_channel,
            get_current_session_id,
            get_current_user_id,
        )

        task_id = (task_id or "").strip()
        if not task_id:
            return "ERROR: task_id is required."

        agent_id = get_current_agent_id()
        session_id = get_current_session_id()
        if not session_id:
            return "ERROR: no active session in context; cannot register callback."

        job = {
            "task_id": task_id,
            "job_id": uuid.uuid4().hex[:12],
            "target_agent": (target_agent or "").strip() or None,
            "agent_id": agent_id,
            "session_id": session_id,
            "user_id": get_current_user_id(),
            "channel": get_current_channel() or "console",
            "registered_at": time.time(),
            "status": "pending",
        }
        with _STATE_LOCK:
            state = _load_state()
            old = next(
                (j for j in state.get("jobs", []) if j.get("task_id") == task_id),
                None,
            )
            if old is not None:
                if old.get("status") in ("done", "unconfirmed"):
                    # The result already went out (or tried to); the task
                    # record can never produce a new one, so re-watching
                    # would only re-inject the old result.
                    return (
                        f"Task {task_id} already has a terminal callback "
                        f"record ({old.get('status')}); not re-watching."
                    )
                if old.get("status") == "pending":
                    # Same registration again: keep the job_id the live
                    # watcher holds, or its guarded terminal write would be
                    # rejected and the record stuck at pending.
                    keys = ("session_id", "user_id", "channel",
                            "agent_id", "target_agent")
                    if all(old.get(k) == job.get(k) for k in keys):
                        job["job_id"] = old.get("job_id") or job["job_id"]
            state["jobs"] = [j for j in state.get("jobs", []) if j.get("task_id") != task_id]
            state["jobs"].append(job)
            _save_state(state)
            # Inside the lock: the state record and the live watcher must be
            # swapped atomically, or two concurrent registrations of one task
            # can leave the record owned by a watcher that was just stopped.
            self._spawn(job)
        return (
            f"Watching task {task_id} for agent '{agent_id}'. "
            f"Result will be posted back to session {session_id} on completion."
        )

    def callback_task_status(self) -> str:
        """Report recent callback jobs and their delivery state."""
        jobs = _load_state().get("jobs", [])[-20:]
        if not jobs:
            return "No callback jobs recorded."
        lines = []
        for job in reversed(jobs):
            lines.append(
                f"{job.get('task_id')} | {job.get('status')} | "
                f"agent={job.get('agent_id')} session={job.get('session_id')} | "
                f"registered={job.get('registered_at', 0):.0f}"
            )
        return "\n".join(lines)

    def cancel_task_callback(self, task_id: str) -> str:
        """Cancel the watcher for a task; the child task keeps running."""
        job = self._get_job(task_id)
        if job is None:
            return f"No callback job found for task {task_id}."
        if job.get("status") == "done":
            return f"Task {task_id} already delivered; nothing to cancel."
        self._update_job(task_id, status="cancelled", completed_at=time.time())
        watcher = self._watchers.pop(task_id, None)
        if watcher is not None:
            watcher.stop()
        return f"Callback for {task_id} cancelled (child task unaffected)."


plugin = AgentTaskCallbackPlugin()
_IMPL = plugin
