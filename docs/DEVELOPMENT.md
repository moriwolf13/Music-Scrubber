# Development

## Working tree

Run commands from the `Scrubber` directory. `mixie.py` is the entry point, and all implementation modules are in `src/`. The repository's parent directory is the default location for Music Library.

Do not use the production library, Inbox, staging or archive folders as test fixtures. Isolated tests must have their own temporary workspace, HOME and synthetic provider responses.

## Tests

```sh
.venv/bin/python -I -B tests/run_quick.py
.venv/bin/python -I -B tests/run_isolated.py
```

The quick suite covers commonly modified ingestion, review and recovery behavior. Run the complete suite before merging changes that affect source cleanup, ZIP integrity, import receipts or library commits. The runners maintain diagnostic logs and deliberately isolate test data.

Check for unintended whitespace errors before committing:

```sh
git diff --check
git diff --cached --check
```

## Git hygiene

The source repository should contain application code, dependencies, tests and maintainable documentation. It should not contain audio, downloaded ZIPs, backup archives, API keys, personal paths from operational logs or the JSON records generated during ingestion.

Review `git status --short` and `git diff --cached --name-only` before pushing. Verify that new files under `docs/` and `tests/` are fixtures or source documentation rather than production artifacts. Do not use `git push --force` to reconcile divergent branches.

## Project notes

Historical implementation reports and dated acceptance logs may remain in `docs/` as reference material. They are not a substitute for the current README, architecture description or fresh test results. Keep safety-focused code comments that explain why a check or invariant exists.
