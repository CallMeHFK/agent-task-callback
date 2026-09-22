# Agent Task Callback (QwenPaw plugin)

Opt-in persistent watcher for **inter-agent background tasks**. QwenPaw's
`submit_to_agent` is pull-only: it returns a `task_id` and the result stays in
memory until something calls `check_agent_task`. When a sub-agent finishes,
nothing notifies the caller — so a console session that submitted work and
moved on never learns it completed.

This plugin adds the missing **push** side: a dedicated watcher thread polls the
child task, and on completion delivers the result back into the *registering*
session as a fresh turn.

## Install

QwenPaw keeps **two copies** of a plugin and they are *not* symlinked:

| Path | Role |
| --- | --- |
| `~/.qwenpaw/plugins/<id>/` | **Loaded at runtime** — this is the code that actually runs |
| `~/.qwenpaw/workspaces/<agent>/plugin-dev/<id>/` | Development source tree |

Editing only the dev tree does **nothing** until the plugin is reinstalled.
Keep both in sync, or copy the source over the installed copy and reload:

```bash
ID=agent-task-callback
SRC=~/.qwenpaw/workspaces/default/plugin-dev/$ID
DST=~/.qwenpaw/plugins/$ID

cp "$SRC/plugin.json" "$DST/plugin.json"
mkdir -p "$DST/backend" && cp "$SRC/backend/main.py" "$DST/backend/main.py"
rm -rf "$DST/backend/__pycache__"

# then reload from the console, or:
curl -sS -X POST http://127.0.0.1:19999/api/plugins/install \
  -H 'Content-Type: application/json' \
  -d "{\"source\":\"$SRC\",\"force\":true}"
```

Always verify both copies agree afterwards:

```bash
diff -q "$SRC/backend/main.py" "$DST/backend/main.py" && echo IN SYNC
```

### Which agents see the tools

QwenPaw's plugin router syncs `meta.tools` from `plugin.json` into **every**
agent's `builtin_tools` config on install/reload — that part is automatic, so
the manifest must list the tools:

```json
"meta": { "tools": [ { "name": "watch_agent_task" }, ... ] }
```

The catch: the sync writes entries with **`enabled: false`**. Until an agent's
config flips that to `true`, the agent will not be offered the tool. Enabling
is per-agent and is **not** covered by the plugin:

```python
from qwenpaw.config.config import load_agent_config, save_agent_config

cfg = load_agent_config(agent_id)          # via qwenpaw.config.utils.load_config
for n in ("watch_agent_task", "callback_task_status", "cancel_task_callback"):
    if n in cfg.tools.builtin_tools:
        cfg.tools.builtin_tools[n].enabled = True
save_agent_config(agent_id, cfg)
```

Note this means **newly created agents start disabled** and need the same
one-time flip. `manifest.version` is reported at load time but is *not* used by
the framework for upgrade decisions — bump it anyway so consumers can tell
revisions apart.

## Tools

| Tool | Purpose |
| --- | --- |
| `watch_agent_task` | Watch a submitted task and resume this session on completion |
| `callback_task_status` | Inspect this session's callback jobs |
| `cancel_task_callback` | Cancel a pending callback (not the child task) |

## API base URL resolution

Priority: **framework resolver > environment > default port**.

1. `qwenpaw.agents.tools.agent_management._normalize_api_base_url(None)`
2. Environment — `QWENPAW_RUNTIME_API_URL`, or `QWENPAW_RUNTIME_HOST` +
   `QWENPAW_RUNTIME_PORT`
3. Default — `http://127.0.0.1:19999/api`

```bash
export QWENPAW_RUNTIME_PORT=23456   # only used if the resolver is unavailable
```

Every branch is normalized to keep the required `/api` suffix.

## Delivery semantics

- **409 means "a turn is already running for this chat"** — the plugin retries
  with backoff (30 x 20s) rather than blindly re-delivering, so no duplicate
  turns are produced.
- Jobs are persisted to `~/.qwenpaw/agent-task-callback.json`, so a watcher
  survives a process restart: `on_start` re-arms `pending` jobs that are less
  than `MAX_JOB_AGE` (24 h) old. It does **not** re-arm `unconfirmed` — that
  status already holds a terminal result in `final`, and re-watching it risks a
  second delivery to the parent session (see `0.1.2`).
- The delivered text is rendered by the framework's own
  `format_background_status_text` — the same formatter `check_agent_task` uses.
  The task payload has no `final_response` field; the reply is the text of the
  last `output` item. Before 0.1.2 the plugin JSON-dumped the whole result
  instead, which posted the child's reasoning and raw tool output into the
  parent session, truncated mid-JSON, and hid failures (the outer status is
  `finished` even when `result.status` is `failed`).
- A watcher stops instead of looping when the task record can never come back:
  HTTP 404 parks the job as `lost`, and `MAX_STRIKES` (90) consecutive failed
  checks park it as `abandoned`. The framework keeps background tasks in a
  module-level dict that is never pruned, so a 404 means the record died with
  an older process — polling it again only fills the journal.

## Notes / limitations

- Requires QwenPaw 2.2.0–2.3.0.
- The restart path has not been exercised against a live `systemctl restart`
  in every environment; the recovery logic itself is verified by directly
  invoking `on_start`.
- `target_agent` is not accepted by `watch_agent_task`; identity is carried via
  the request path and headers instead.

## Development

`backend/main.py` is a single self-contained module. There is no package to
install: QwenPaw loads that file by path from `~/.qwenpaw/plugins/`.

The test suite runs without QwenPaw installed -- `backend/main.py` imports the
host at module scope, so the tests stand in for the one name they need:

```bash
uv sync
uv run python -m pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the edit → install → restart loop,
why a second copy of the plugin directory must not sit in `plugins/`, and the
framework behaviours the watcher depends on. Release tags (`v*`) build the
installable zip via `packaging/build_plugin_zip.py`.

When changing code, do all three or the change will not take effect:

1. edit this repository,
2. copy it over `~/.qwenpaw/plugins/agent-task-callback/`,
3. bump `version` in `plugin.json` and restart the app.

## Changelog

- **0.1.2** — stop the zombie-watcher log flood. `_watch_sync` treated every
  exception as retryable, so a task id that 404s (its record lost in a restart)
  was polled forever at `POLL_SECONDS`: 520 warnings/hour over 3 jobs in the
  2026-09-22 incident. 404 now parks the job as `lost`, `MAX_STRIKES` consecutive
  failures park it as `abandoned`, and `_boot` re-arms only fresh `pending` jobs
  (dropping `unconfirmed`, which risked duplicate delivery, and >24 h stale ones).
  Delivery text now comes from the framework's `format_background_status_text`
  instead of a JSON dump of the whole result.
  Regression tests in `tests/test_zombie_watcher.py`.
- **0.1.1** — bump version to match code; document the two-copy install layout
  and the `enabled: false` tool-sync default

- **0.1.0** — initial release: watcher thread, three tools, persisted jobs,
  409-aware backoff delivery, API base URL resolution
  (framework resolver > environment > port 19999).
