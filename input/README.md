# input/ — tasks the runner scores

Everything in here is a self-contained Repo2RLEnv `pr_diff` task, one directory
per task, **named by UUID**. `scripts/run_nemesis.py` reads this directory and
writes scored results to `../output/`.

These 30 tasks are a copy of [`nemesis-dataset`](../../nemesis-dataset); the
same tasks live under `../datasets/jeto-prdiff/tasks/` beside their original
`<org>__<repo>-<pr>` names. `uuid_map.json` maps one to the other.

## Layout

```
input/
  manifest.json                 per-task provenance, keyed by uuid
  uuid_map.json                 {original_task_id: uuid}
  <uuid>/
    task.toml                   name, repo, base commit, context files
    instruction.md              the issue the agent is asked to fix
    environment/Dockerfile      python:3.12-slim + repo @ base commit + /verifier
    tests/test.sh               diffs /workspace vs base, runs the verifier
    solution/patch.diff         the real merged PR diff (the oracle)
    solution/solve.sh           applies it
```

## Adding or replacing tasks

Drop in any directory containing a `task.toml` — discovery is just "has a
`task.toml`", so the folder name is free-form and the UUID convention is ours,
not a requirement. To re-sync from the dataset repo:

```bash
rsync -a --delete --exclude '.git' --exclude 'LICENSE' --exclude 'README.md' \
    ../nemesis-dataset/ input/
```

That mirrors deletions, so anything added here by hand is removed. Run against a
different directory instead with `--input` if you want to keep both.

## Note on task.toml names

`[task] name` still reads `jeto-bench/<org>__<repo>-<pr>` — the files are
byte-identical to the source export so their `content_hash` stays meaningful.
The UUID is the directory name and the primary key everywhere in `output/`;
the original id is carried alongside it as `source_task_id`.
