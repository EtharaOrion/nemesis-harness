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