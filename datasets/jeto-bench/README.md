# JETO-Bench — Harbor export

91 execution-time-improvement tasks in Java, exported from
`results/dataset.csv` by `scripts/export_harbor.py` at 2026-07-31T11:44:50+00:00.

Each task hands the agent a Java repository at a commit that a real
performance patch later improved, and rewards a *measured* speedup:

- **P2P gate** — the modified modules' test suite must compile and pass on
  every run, for both the baseline and the agent's version.
- **Reward 1.0** — total test execution time improves by at least
  1% and survives a one-sided
  sign test at p < 0.1 over 11
  alternating runs per version (first run of each discarded as warm-up).
- Otherwise 0.0 (set `shaped: true` in a task's `tests/config.json` for partial
  credit on sub-threshold speedups).

`tests/verifier.py` reproduces `MvnwExecResults` from the harness in pure
stdlib: first run of each version dropped as warm-up, per-test-class times from
surefire output, exact binomial sign test.

## Deviations from the Repo2RLEnv spec

- `pipeline = "jeto_exec_time"` is not a registered Repo2RLEnv pipeline, so
  `repo2rlenv validate` will not recognise these tasks.
- `reward_kinds` includes `exec_time_improvement`, which the upstream reward
  schema does not define — upstream has no timing signal.
- Environments are `mode = "registry"` and pull the benchmark's prebuilt public
  images. Those images are **linux/amd64 only**; timing measured under
  emulation is not meaningful.
- The images ship the reference fix at `/app/patched_repo`; each task's
  Dockerfile deletes it so the solution cannot be read out of the environment.
