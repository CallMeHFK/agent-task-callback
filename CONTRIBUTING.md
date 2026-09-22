# Contributing

## What this is

A single-file QwenPaw plugin: `backend/main.py` registers three tools and a
startup hook that resumes a console session when an inter-agent background task
it is watching reaches a terminal state.

There is no Python package to install. QwenPaw imports `backend/main.py` by path
from `~/.qwenpaw/plugins/agent-task-callback/`.

## Development loop

The app loads the plugin at startup and keeps it in memory, so editing files on
disk changes nothing until the process reloads:

```bash
# 1. edit here
# 2. push the change into the location the app reads
rsync -a --exclude __pycache__ --exclude tests --exclude packaging ./ \
  ~/.qwenpaw/plugins/agent-task-callback/
# 3. restart the app -- the boot hook only runs on startup
sudo systemctl restart qwenpaw
```

Do not leave a second copy of the plugin directory inside
`~/.qwenpaw/plugins/`: every immediate subdirectory there is discovered as a
plugin, so a `agent-task-callback.bak/` next to the real one gets loaded twice
under the same id and the two instances race to register the same tools.

## Running the tests

The suite does not need QwenPaw. `backend/main.py` imports the host at module
scope, so the test file stands in for the one name it uses and reports which
mode it resolved to:

```bash
uv sync
uv run python -m pytest -q             # 13 tests
uv run ruff check .
python3 tests/test_zombie_watcher.py   # same suite, and prints the host mode
```

`pytest -q` captures the host line; the direct run shows it
(`QwenPaw host: stubbed (…)` / `real (…)`), which is the one thing worth knowing
about a local result.

Both branches of the delivery-text path are real code and both are covered:
against a live app it defers to the framework's `format_background_status_text`;
without one it falls back to its own extractor. Change that function and run the
suite both ways -- with and without the app importable.

The tests exercise `_watch_sync` and `_boot` directly. They never POST to a
session, so a test run cannot deliver a message into a live chat.

## Conventions worth knowing before you change the watcher

- `~/.qwenpaw/agent-task-callback.json` is runtime state, not configuration, and
  contains session ids, user ids and delivered result text. It is gitignored.
  Never paste from it without redacting.
- The framework keeps background tasks in a module dict that is never pruned.
  A 404 from `GET /console/chat/task/{id}` therefore means the record died with
  an earlier process. It is permanent. Code that treats it as transient produced
  the 0.1.1 log flood.
- `unconfirmed` is a terminal state, not a pending one: the watcher already has
  the result in the job's `final` field and only the delivery is unknown.
  Re-watching it risks delivering the same result to the parent session twice.
- Polling happens on a plain thread per job with a shared state file. Keep the
  loop's exits cheap to reason about: every `return` from `_watch_sync` should
  leave the job in a status that `_boot` will not pick up again.

## Release

Tag `v*` and the release workflow attaches two assets: the installable plugin
zip and a `-source.zip` of the tagged tree (`git archive`, so tracked files only
and no `dist/`). GitHub generates its own "Source code" link for every tag
anyway; the explicit asset exists so the release page and `gh release download`
name the two kinds of archive unambiguously. Both carry `plugin.json`, so both
*can* be installed — only the `-qwenpaw-plugin-` one should be, because the
source tree also drags `tests/`, `packaging/` and `.github/` into
`~/.qwenpaw/plugins/`.

Bump `version` in `plugin.json` **before** tagging: it is both the asset name and
what `GET /api/plugins/<id>/status` reports, and the tag is only a tag. Move
`pyproject.toml` with it, and the version inside the README's install commands.

To check the bundle locally:

```bash
python3 packaging/build_plugin_zip.py
unzip -l dist/agent-task-callback-qwenpaw-plugin-*.zip
```

The archive must contain exactly one top-level directory holding `plugin.json`;
QwenPaw's installers reject anything else, and the builder refuses to ship
symlinks because extraction would silently turn them into their target paths.
