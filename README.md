# JETO-Bench Replication Package

This repository is the replication package for the paper **"JETO-Bench: A Reproducible Benchmark for Execution Time Improving Patches in Java"**.

It includes:
- data collection and filtering for executable ETIPs,
- dynamic analysis pipelines,
- evaluation harnesses for patch and test evaluation,
- scripts and data to reproduce reported tables and charts.

## Citation

If you use JETO-Mine or JETO-Bench in your research, please cite our paper:

```bibtex
@misc{etemadi2026jeto,
      title={JETO-Bench: A Reproducible Benchmark for Execution Time Improvement Patches in Java}, 
      author={Khashayar Etemadi and Zhendong Su},
      year={2026},
      eprint={2606.31767},
      archivePrefix={arXiv},
      primaryClass={cs.SE},
      url={https://arxiv.org/abs/2606.31767}, 
}
```

## Requirements

- Linux environment
- Python 3.11+
- [Poetry](https://python-poetry.org/)
- Docker (required for dynamic analysis and evaluation harness)

## Installation

From the project root:

```bash
poetry install
```

If you are running commands without `poetry run`, make sure dependencies are installed in your active environment.

## Environment Variables

Set the environment variables used by the code:

```bash
export workingdir="$(pwd)"
export github_access_token="<YOUR_GITHUB_TOKEN>"
export OPENROUTER_API_KEY="<YOUR_OPENROUTER_KEY>"
export OPENAI_API_KEY="<YOUR_OPENAI_KEY>"
```

Notes:
- `workingdir` is used by dynamic analysis and evaluation workflows.
- `github_access_token` is needed for GitHub API access.
- `OPENROUTER_API_KEY` / `OPENAI_API_KEY` are used by configured LLM-related components.

## Claude Code Bridge (Opus 4.8 on subscription)

`src/claude_oauth/` runs an Anthropic-compatible proxy backed by the local
Claude Code OAuth credentials, so LLM components can call `claude-opus-4-8`
without an Anthropic/OpenRouter key:

```bash
./scripts/start_claude_bridge.sh    # serves :8765, credentials from the Keychain
```

With it running, `src/llm/claude_bridge.py::Opus_4_8_Bridge` and the OpenHands
runner both route through it by default. Settings live in `.env` and
`claude_bridge` in `src/config.py`. Full details: `docs/CLAUDE_BRIDGE.md`.

## Running the Benchmark (input/ → output/)

`scripts/run_nemesis.py` is the one command that scores a whole task set. It
reads every task under `input/` and writes a run bundle to `output/`:

```bash
python scripts/run_nemesis.py                  # all tasks, Opus agent
python scripts/run_nemesis.py --agent oracle   # sanity check — expect ~1.0
python scripts/run_nemesis.py --limit 3 --jobs 3
python scripts/run_nemesis.py --task <uuid>    # one task; repeatable
```

`input/` holds 30 `pr_diff` tasks named by UUID (see `input/README.md`). Point
`--input` anywhere else to score a different set; `--output` moves the bundle.

Per task the runner builds `environment/Dockerfile` (repo cloned at the PR base
commit, verifier baked in), lets the agent edit `/workspace`, then runs
`tests/test.sh`, which diffs the workspace against the base commit and scores it
with the 6-component `diff_similarity` reward.

**Agents** (`--agent`)

| | what it does | expected reward |
|---|---|---|
| `opus` | single-shot: instruction + context files → Opus 4.8 over the Claude bridge → apply the diff it returns | varies |
| `oracle` | applies `solution/patch.diff` | ~1.0 |
| `none` | no edits; empty predicted diff | 0.0 |

`oracle` and `none` need no model and no bridge — run them first to prove the
plumbing. `opus` requires `./scripts/start_claude_bridge.sh` to be up.

**Output** — the Harbor/Repo2RLEnv trajectory layout, the same one
`run_harbor_task.py` writes for exec_time tasks (`scripts/harbor_bundle.py`),
rooted at `output/` so the input task tree stays untouched:

```
output/
  <uuid>/                                    one per task, mirroring input/
    trajectories/
      Claude Opus 4.8/                       one per model; oracle and none get their own
        pass_summary.json                    across every run_N of this task+model
        run_1/
          output.json      session_id, trajectory.meta_info, input_files,
                           output_artifacts, messages, usage
          report.json      model, run_index, reward, diff_similarity breakdown, patch status
          logs/verifier/
            reward.txt          the scalar Harbor reads
            reward.json         flat numeric map (Harbor's schema)
            reward-details.json nested sidecar: components, weights, judge status
            test.sh             the exact verifier that ran
            verify.log
          output_media/
            patch.diff     what the agent actually changed
          run.log
        run_2/ …                             repeats accumulate, never overwrite
  sweeps/
    sweep-0001.json                          cross-task roll-up for one invocation
```

`pass_summary.json` answers *how does this model do on this task across
repeats*; `sweeps/sweep-NNNN.json` answers *how does it do across the task set* —
average reward, solve rate, mean of each component, and breakdowns by difficulty
and by repo. That sweep number is the one to compare between agents.

Images are cached per task and base commit, so only the first run pays the clone
cost (`--rebuild` forces one).

**Disk.** Each task image carries its own repo clone — ~500MB, so a full 30-task
sweep parks roughly 15GB in Docker. Reclaim it with:

```bash
docker rmi $(docker images 'nemesis/*' -q)              # drop cached task images
find output -mindepth 1 -not -name .gitkeep -delete     # drop run bundles
```

**Notes**

- The `llm_judge` component (weight 0.5) posts to `api.anthropic.com` directly
  from inside the task container and ignores `ANTHROPIC_BASE_URL`, so the bridge
  cannot serve it. Without an `ANTHROPIC_API_KEY` in the environment it reports
  `no_api_key` and its weight is redistributed over the five deterministic
  components — a comparable score, just a differently weighted one. Export a
  real key to enable it.
- Models write diffs with correct content and wrong line numbers, and usually
  omit `diff --git` headers. The runner repairs the headers, then escalates
  `git apply` → `git apply -C1` → `scripts/nemesis_apply.py`, which locates each
  hunk by matching its context instead of trusting the `@@` offsets.

## Harbor Export (RL environments)

`scripts/export_harbor.py` emits the executable ETIPs as Harbor-format tasks
(the layout [Repo2RLEnv](https://github.com/huggingface/Repo2RLEnv) consumes),
using the prebuilt public per-commit images as environments and this harness's
own criterion — statistically significant execution-time improvement with the
module suite still green — as the reward:

```bash
python scripts/export_harbor.py --out datasets/jeto-bench
python scripts/export_harbor.py --out datasets/jeto-full --include-unverified
python scripts/export_harbor.py --out /tmp/one --commit <sha> --with-oracle
```

`scripts/harbor/verifier.py` is copied into every task and reproduces
`MvnwExecResults` in pure stdlib (no numpy/scipy inside the task image). The
export adds a `jeto_exec_time` pipeline name and an `exec_time_improvement`
reward kind, neither of which is registered upstream — see the generated
`README.md` in the output directory.

## Configuration and Filters

User-defined filters and analysis configuration for static and dynamic workflows can be set in:
- `src/config.py`

## Running the Main Entry Point

The main entry point is `main.py` and supports three modes via `--analysis-type`.

### 1) Static Analysis

Runs the commit collection pipeline (`CommitCollector`), which is the default mode.

```bash
poetry run python main.py
```

or explicitly:

```bash
poetry run python main.py --analysis-type static
```

### 2) Dynamic Analysis

Runs the dynamic analysis pipeline from `src/run_analysis.py`.

```bash
poetry run python main.py --analysis-type dynamic
```

### 3) Evaluation Harness

Runs evaluation via `src/evaluation/evaluators.py` and supports:
- `patch` evaluation (`PatchEvaluator`)
- `test` evaluation (`TestEvaluator`)

#### Patch Evaluation

Required arguments:
- `--evaluation-type patch`
- `--repo`
- `--after-commit`
- `--output-analysis-path`
- `--modified-modules` (comma-separated)
- `--patch-path`

Example:

```bash
poetry run python main.py \
  --analysis-type evaluation-harness \
  --evaluation-type patch \
  --repo owner/repo \
  --after-commit abc123 \
  --output-analysis-path results/eval_patch.json \
  --modified-modules module-a,module-b \
  --patch-path /path/to/fix.patch
```

#### Test Evaluation

Required arguments:
- `--evaluation-type test`
- `--repo`
- `--after-commit`
- `--output-analysis-path`
- `--modified-modules` (comma-separated)
- `--test-patch-path`
- `--tests` (comma-separated)

Example:

```bash
poetry run python main.py \
  --analysis-type evaluation-harness \
  --evaluation-type test \
  --repo owner/repo \
  --after-commit abc123 \
  --output-analysis-path results/eval_test.json \
  --modified-modules module-a,module-b \
  --test-patch-path /path/to/test.patch \
  --tests com.example.FooTest,com.example.BarTest
```

Optional evaluation arguments:
- `--exec-times`
- `--min-p-value`
- `--min-exec-time-improvement`
- `--working-dir`

## Dataset and Reproducibility Artifacts

- The list of identified and manually verified executable ETIPs is in:
  - `results/dataset.csv`
  - Note that the exec_time_improvement and p_value reported in this file are the overall numbers. To see which tests show statistically significant improvements, take a look at the `test_class_improvements` column.
- Tables and charts can be checked and reproduced using data and scripts in:
  - `results/`

Useful scripts include:
- `results/charts/stars_year.py`
- `results/charts/repos.py`
- `results/charts/executable_etips_stats.py`
- `results/tables/modified.py`