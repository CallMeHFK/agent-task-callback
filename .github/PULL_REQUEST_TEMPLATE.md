## Summary

<!-- What does this PR change, and why? Link the incident or issue if there is
one. -->

## How it was tested

<!-- e.g. `uv run pytest -q`, plus a live session if the delivery path changed -->

## Checklist

- [ ] `uv run pytest -q` passes locally
- [ ] `uv run ruff check .` passes locally
- [ ] `python3 packaging/build_plugin_zip.py` still produces an installable zip
- [ ] No real session ids, user ids, task results or secrets added (the state file
      is gitignored for a reason)
- [ ] README updated if user-facing behavior changed
- [ ] `plugin.json` version bumped, and the changelog entry says which measured
      symptom the change addresses
