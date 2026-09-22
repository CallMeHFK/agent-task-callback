"""Agent Task Callback — opt-in watcher that resumes the registering session.

Full lifecycle traceability (kept verbatim from the prior conversation):
  parent check token PARENT-5027570671
  child  check token CHILD-5027570671
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

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
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


# Module-level singleton so module-level tool functions can delegate to it
# (bound methods cannot receive the ``_tool_descriptor`` attribute that
# ``register_tool`` attaches, which raised AttributeError on ... ).
_IMPL = None


def watch_agent_task(task_id: str) -> str:
    """Watch an inter-agent background task id and resume this session on completion."""
    if _IMPL is None:
        return "ERROR: plugin not initialised"
    return _IMPL.watch_agent_task(task_id)


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
        self._stop = threading.Event()
        self._impl = _IMPL

    def run(self) -> None:
        impl = self._impl or _IMPL
        if impl is None:
            return
        impl._watch_sync(self._job, self._stop)

    def stop(self) -> None:
        self._stop.set()


class AgentTaskCallbackPlugin:
    """Watches submitted agent tasks and re-injects results into the origin session."""

    def __init__(self) -> None:
        self._watchers: dict[str, "_WatcherThread"] = {}
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
                "automatically."
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
        for job in fresh:
            self._spawn(job)
        logger.info(
            "agent-task-callback boot: re-armed %d job(s), skipped %d stale",
            len(fresh), len(pending) - len(fresh),
        )

    async def _halt(self) -> None:
        self._shutdown = True
        for thread in list(self._watchers.values()):
            thread.stop()
        self._watchers.clear()

    # ---- internals ------------------------------------------------------
    def _spawn(self, job: dict) -> None:
        task_id = job["task_id"]
        existing = self._watchers.get(task_id)
        if existing is not None and existing.is_alive():
            return  # idempotent: never double-watch the same task
        thread = _WatcherThread(job)
        self._watchers[task_id] = thread
        thread.start()

    @staticmethod
    def _update_job(task_id: str, **fields: Any) -> dict:
        state = _load_state()
        for job in state.get("jobs", []):
            if job.get("task_id") == task_id:
                job.update(fields)
                break
        _save_state(state)
        return state

    @staticmethod
    def _get_job(task_id: str) -> Optional[dict]:
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
        except Exception:  # noqa: BLE001 - framework unavailable: use fallback
            logger.debug("framework API resolver unavailable", exc_info=True)

        explicit = os.environ.get("QWENPAW_RUNTIME_API_URL", "").strip()
        if explicit:
            return normalize(explicit)
        host = os.environ.get("QWENPAW_RUNTIME_HOST", "").strip() or "127.0.0.1"
        port = os.environ.get("QWENPAW_RUNTIME_PORT", "").strip() or "19999"
        return normalize(f"http://{host}:{port}")

    def _headers(self, agent_id: Optional[str] = None) -> dict:
        import os

        token = os.environ.get("QWENPAW_RUNTIME_INTERNAL_TOKEN", "")
        headers = {"X-Agent-Id": agent_id or "default"}
        if token:
            headers["X-Internal-Token"] = token
        return headers

    def _query_headers(self, job: dict) -> dict:
        """The task lives in the child agent's namespace, so query as it.

        ``submit_to_agent`` forwards the request under the *target* agent
        identity; polling with any other identity yields a non-JSON error
        page ("Expecting value: line 1 column 1").
        """
        return self._headers(job.get("target_agent") or job.get("agent_id"))

    def _check_task_sync(self, job: dict) -> dict:
        task_id = job["task_id"]
        resp = httpx.get(
            f"{self._base_url()}/console/chat/task/{task_id}",
            headers=self._query_headers(job),
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype.lower():
            raise ValueError(
                f"non-JSON response from {resp.url} (content-type={ctype!r}); "
                "the API base URL is probably wrong"
            )
        return resp.json()

    def _post_reply_sync(self, job: dict, text: str) -> bool:
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
        # "not yet" rather than a failure, so back off and retry.
        max_attempts = 30
        for attempt in range(max_attempts):
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
                time.sleep(20)
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
        except Exception:  # noqa: BLE001 - helper moved or renamed
            logger.debug("framework formatter unavailable", exc_info=True)

        blocks = ((data.get("result") or {}).get("output") or [{}])[-1]
        text = "\n".join(
            item.get("text", "")
            for item in (blocks.get("content") or [])
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        return text[:MAX_DELIVERY_CHARS]

    def _watch_sync(self, job: dict, stop: "threading.Event") -> None:
        task_id = job["task_id"]
        terminal = {"finished", "failed", "cancelled", "timeout", "error"}
        result = None

        strikes = 0
        while not stop.is_set():
            current = self._get_job(task_id)
            if current is None or current.get("status") == "cancelled":
                logger.info("watcher for %s stopped (cancelled/missing)", task_id)
                return
            try:
                data = self._check_task_sync(job)
            except Exception as exc:  # noqa: BLE001 - keep watcher alive
                if _is_gone(exc):
                    logger.warning("task %s vanished (404); dropping watcher",
                                   task_id)
                    self._update_job(
                        task_id,
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

        status = result.get("status", "unknown")
        text = self._delivery_text(task_id, result)
        if not text:
            text = f"(agent task {task_id} {status}, no text content)"

        sent = self._post_reply_sync(job, text)
        if sent:
            self._update_job(task_id, status="done", completed_at=time.time(), final=text[:2000])
            logger.info("callback delivered for %s", task_id)
        else:
            self._update_job(
                task_id,
                status="unconfirmed",
                completed_at=time.time(),
                final=text[:2000],
                note="POST outcome unknown; not auto-retried to avoid duplicate turns",
            )
            logger.warning("callback delivery unconfirmed for %s", task_id)

    # ---- tools ----------------------------------------------------------
    def watch_agent_task(self, task_id: str, target_agent: str = "") -> str:
        """Register a callback watcher for a task id (see submit_to_agent).

        Call automatically right after submitting a background task that
        you want to be resumed on. It records the current agent, session,
        user and channel, then polls until the task reaches a terminal
        state and re-injects the result into this same session.
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
        state = _load_state()
        state["jobs"] = [j for j in state.get("jobs", []) if j.get("task_id") != task_id]
        state["jobs"].append(job)
        _save_state(state)
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
        self._update_job(task_id, status="cancelled")
        watcher = self._watchers.pop(task_id, None)
        if watcher is not None:
            watcher.stop()
        return f"Callback for {task_id} cancelled (child task unaffected)."


plugin = AgentTaskCallbackPlugin()
_IMPL = plugin
