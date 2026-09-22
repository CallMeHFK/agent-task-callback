# Agent Task Callback

[![ci](https://github.com/CallMeHFK/agent-task-callback/actions/workflows/ci.yml/badge.svg)](https://github.com/CallMeHFK/agent-task-callback/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/CallMeHFK/agent-task-callback)](https://github.com/CallMeHFK/agent-task-callback/releases/latest)
[![license](https://img.shields.io/github/license/CallMeHFK/agent-task-callback)](LICENSE)

A QwenPaw plugin that turns inter-agent background tasks from **pull** into
**push**.

`submit_to_agent` returns a `task_id` and stops there: the result sits in the
child's record until some agent thinks to call `check_agent_task`. A session
that delegated work and moved on never learns that it finished. This plugin
registers a watcher per task and, when the task reaches a terminal state, posts
the result back into the **registering** session as a fresh turn — so the parent
agent picks the thread up again without polling.

| | |
| --- | --- |
| **Requires** | QwenPaw 2.2.0 – 2.3.0 (developed against 2.2.1) |
| **Adds** | tools `watch_agent_task`, `callback_task_status`, `cancel_task_callback` |
| **State** | `~/.qwenpaw/agent-task-callback.json` — jobs survive an app restart |
| **Cost** | one daemon thread per watched task, one `GET` every 20 s |
| **License** | MIT |

<details>
<summary>Contents</summary>

- [Install](#install)
- [Using it](#using-it)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Limitations](#limitations)
- [Development](#development)
- [Changelog](#changelog)

</details>

## Install

### 1. Get the plugin bundle

From the [latest release](https://github.com/CallMeHFK/agent-task-callback/releases/latest)
download **`agent-task-callback-qwenpaw-plugin-<version>.zip`**. It carries
exactly the three files the runtime loads (`plugin.json`, `backend/main.py`,
this README) under one `agent-task-callback/` directory.

The release also holds **`agent-task-callback-<tag>-source.zip`** — the whole
tagged tree, for reading and developing. Don't install that one. It contains a
`plugin.json` as well, so QwenPaw's installers accept it and drop `tests/`,
`packaging/` and `.github/` into `~/.qwenpaw/plugins/` next to the files they
actually import.

### 2. Install it

The first two routes hot-load the plugin — the install API runs its startup hook
and syncs its tools, so a running app needs **no restart**. Only the
copy-it-myself route does.

- **In the app** — the plugin screen accepts the zip as an upload or as a URL.
- **With the CLI** — `qwenpaw plugin install <path|url>` forwards to that same
  API while the app runs:

  ```bash
  qwenpaw plugin install \
    https://github.com/CallMeHFK/agent-task-callback/releases/latest/download/agent-task-callback-qwenpaw-plugin-0.1.2.zip
  ```

- **By hand** — unpack into `~/.qwenpaw/plugins/` (the archive already contains
  an `agent-task-callback/` directory) and *then* restart the app:

  ```bash
  unzip -o agent-task-callback-qwenpaw-plugin-0.1.2.zip -d ~/.qwenpaw/plugins/
  ```

  Do not leave a second copy under `~/.qwenpaw/plugins/` — every immediate
  subdirectory there is discovered as a plugin, so a `.bak/` folder with the same
  `id` gets loaded twice and the two instances race to register the same tools.
  Keep backups outside that directory.

### 3. Enable the tools for each agent

Installing writes a `builtin_tools` entry for every agent, **defaulted to
`enabled: false`**. Until an agent flips that to `true`, it is never offered the
tools. This is one-time and per-agent, and it is not something the plugin can do
for itself:

```python
from qwenpaw.config.config import load_agent_config, save_agent_config

for agent_id in ("default", "SE", ...):        # every agent that should watch tasks
    cfg = load_agent_config(agent_id)
    for name in ("watch_agent_task", "callback_task_status", "cancel_task_callback"):
        if name in cfg.tools.builtin_tools:
            cfg.tools.builtin_tools[name].enabled = True
    save_agent_config(agent_id, cfg)
```

Agents created afterwards start disabled too and need the same flip.

### 4. Verify

```bash
curl -sS http://127.0.0.1:19999/api/plugins/agent-task-callback/status
# {"id":"agent-task-callback","loaded":true,"enabled":true,"version":"0.1.2"}

python3 - <<'PY'
import json, pathlib
names = ("watch_agent_task", "callback_task_status", "cancel_task_callback")
for p in sorted(pathlib.Path.home().glob(".qwenpaw/workspaces/*/agent.json")):
    tools = (json.loads(p.read_text()).get("tools") or {}).get("builtin_tools") or {}
    print(p.parent.name, [(n, (tools.get(n) or {}).get("enabled")) for n in names])
PY
```

Port 19999 is the default; substitute your own if the app listens elsewhere.
`~/.qwenpaw/qwenpaw.log` records `✓ Loaded plugin 'agent-task-callback'
successfully` on every load.

## Using it

| Tool | Signature | Does |
| --- | --- | --- |
| `watch_agent_task` | `(task_id)` | Watch a task returned by `submit_to_agent`; deliver its result back to this session when it ends |
| `callback_task_status` | `()` | List the last 20 jobs: `task_id \| status \| agent=… session=… \| registered=…` |
| `cancel_task_callback` | `(task_id)` | Stop *this plugin's watcher* — the child task keeps running |

The parent agent calls `watch_agent_task` right after submitting. Tool
descriptions tell it when to, and asking in plain language ("watch that task,
ping me when it lands") works too:

```text
> have the SE agent audit the driver stack and report back

  submit_to_agent(...)                  -> task_id 0f9c1a…
  watch_agent_task("0f9c1a…")           -> Watching task 0f9c1a… for agent 'default'.
                                           Result will be posted back to session s-77…
  … the session finishes its turn, goes idle …

> [new turn, 20:14]                     [TASK_ID: 0f9c1a…]
                                        [STATUS: finished]

                                        Task completed.
                                        <the child's final reply>
```

The framing above the reply — `[TASK_ID: …]`, `[STATUS: …]`, `Task completed.` —
is the framework's own formatter, identical to what `check_agent_task` shows.
Context (agent, session, user, channel) is captured from the calling turn, so
the result always lands in the conversation that asked for it. Registering the
same `task_id` twice replaces the job record rather than starting a second
poller.

## How it works

1. `watch_agent_task` writes a `pending` job to the state file and starts a
   daemon thread for it.
2. Every **20 s** the thread reads `GET {base}/console/chat/task/{id}`, sending
   the same `X-Agent-Id` the framework's own `check_agent_task` uses — the
   recorded target agent if there is one, else the registering agent — and
   `X-Internal-Token` when `QWENPAW_RUNTIME_INTERNAL_TOKEN` is set. An answer
   that isn't JSON names the base URL as the suspect and counts as one failed
   check.
3. On a terminal status (`finished`, `failed`, `cancelled`, `timeout`, `error`)
   the result is rendered and posted as a user turn into the registering session.
4. The job becomes `done`.

**The delivered text is produced by the framework's own
`format_background_status_text`** — the same formatter `check_agent_task` uses,
truncated at 8000 characters. That matters: the payload has no
`final_response` field (the reply is the text of the *last* `output` item) and
the outer `status` reads `finished` even when `result.status` is `failed`, so
only the formatter turns a failed run into a failure sentence. An earlier
version JSON-dumped the whole payload instead, which posted the child's raw tool
output into the parent session and hid failures.

| Job status | Meaning | Re-armed on restart? |
| --- | --- | --- |
| `pending` | watching, or waiting to deliver | yes, if registered within the last 24 h |
| `done` | delivered | no |
| `unconfirmed` | terminal result captured in `final`, POST outcome unknown | **no** — re-watching risks a second copy of the same answer |
| `lost` | HTTP 404: the task record never returns | no |
| `abandoned` | 90 consecutive failed checks (~30 min of an unreachable or wrong API base URL) | no |
| `cancelled` | `cancel_task_callback` | no |

Two failure modes are deliberate stops rather than retries. The framework keeps
background tasks in a module-level dict that is never pruned, so a 404 means the
record died with an older process — polling it again only fills the log (this is
exactly how 0.1.1 produced 520 warnings/hour). And a delivery attempt that meets
**HTTP 409** ("a turn is already running for this chat") backs off and retries up
to 30 × 20 s instead of blindly re-posting, so a busy parent session never
receives duplicate turns.

## Configuration

No settings keys. Everything tunable is a constant at the top of
`backend/main.py`: `POLL_SECONDS` (20), `HTTP_TIMEOUT` (30 s), `MAX_STRIKES`
(90), `MAX_JOB_AGE` (24 h), `MAX_DELIVERY_CHARS` (8000).

**API base URL** resolves in this order — framework resolver, then environment,
then the default port:

1. `qwenpaw.agents.tools.agent_management._normalize_api_base_url(None)`
2. `QWENPAW_RUNTIME_API_URL`, or `QWENPAW_RUNTIME_HOST` + `QWENPAW_RUNTIME_PORT`
3. `http://127.0.0.1:19999/api`

```bash
export QWENPAW_RUNTIME_PORT=23456   # only consulted if the resolver is unavailable
export QWENPAW_RUNTIME_INTERNAL_TOKEN=…  # sent as X-Internal-Token, if your build needs it
```

Every branch is normalized so the required `/api` suffix survives. A wrong base
URL shows up as an `abandoned` job (90 checks that never returned JSON); a
mistyped or already-dead task id shows up as `lost`.

The state file is runtime state, not configuration: it holds session ids, user
ids and the text of delivered results. Treat it as private and don't paste from
it unredacted.

## Limitations

- One watcher per task id, one delivery per watcher. When a POST's outcome is
  unknown the job parks as `unconfirmed` with the rendered answer kept in its
  `final` field, and nothing is sent again — that one reply has to be fetched by
  hand with `check_agent_task`. Retrying automatically was rejected as the
  cheaper failure: a duplicate turn in the parent session.
- The result arrives as a **user** turn on the channel recorded at registration
  time (`console` when the framework reports none), so it follows whatever
  routing that channel has.
- `watch_agent_task` takes only `task_id`. `target_agent` exists on the internal
  implementation but is not exposed by the registered tool; identity is carried
  by the request path and headers.
- Jobs older than 24 h are dropped at startup rather than re-armed — their task
  record has almost certainly gone.
- The restart path is verified by invoking `_boot` directly in tests; it has not
  been exercised against a live `systemctl restart` on every deployment.
- The plugin talks to the app's own HTTP API, so that URL has to be reachable
  from the plugin process (it is by default, on loopback).

## Development

`backend/main.py` is one self-contained module — there is no Python package to
install; QwenPaw imports that file by path from `~/.qwenpaw/plugins/`.

```bash
uv sync
uv run python -m pytest -q          # 13 tests
uv run ruff check .
python3 packaging/build_plugin_zip.py && unzip -l dist/*.zip
```

The suite does not need QwenPaw installed: `backend/main.py` imports the host at
module scope, so the tests stand in for the one name they need and both branches
of the delivery-text path are covered. `python3 tests/test_zombie_watcher.py`
runs the same suite without pytest and reports which host mode it resolved to
(`QwenPaw host: stubbed …` / `real …`) — pytest's output capture hides that line.
Tests call `_watch_sync` and `_boot` directly and never POST, so a run cannot
deliver into a live chat.

Pushing a `v*` tag runs `.github/workflows/release.yml`, which attaches the
plugin bundle **and** a `-source.zip` of the tagged tree to the release.
[CONTRIBUTING.md](CONTRIBUTING.md) covers the edit → install → reload loop and
the framework behaviours the watcher depends on.

## Changelog

- **0.1.2** — Stop the zombie-watcher log flood. `_watch_sync` treated every
  exception as retryable, so a task id that 404s (record lost in a restart) was
  polled forever at `POLL_SECONDS`: 520 warnings/hour across 3 jobs in the
  2026-09-22 incident. 404 now parks the job as `lost`, `MAX_STRIKES`
  consecutive failures park it as `abandoned`, and `_boot` re-arms only fresh
  `pending` jobs (dropping `unconfirmed`, which risked duplicate delivery, and
  >24 h stale ones). Delivery text moved to the framework's
  `format_background_status_text` instead of a JSON dump of the whole result.
  Regression tests in `tests/test_zombie_watcher.py`.
- **0.1.1** — Version bumped to match the code; documented the two-copy install
  layout and the `enabled: false` tool-sync default.
- **0.1.0** — Initial release: watcher thread, three tools, persisted jobs,
  409-aware backoff delivery, API base URL resolution
  (framework resolver > environment > port 19999).

## License

MIT — see [LICENSE](LICENSE).
