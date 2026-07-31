"""CLI for the OpenHands generation tasks (main.py only exposes the analysis
and evaluation modes).

Looks the commit up in results/dataset.csv so only --commit is required; every
field can still be overridden. The LLM comes from src/config.py -> openhands,
which defaults to Opus 4.8 through the Claude bridge (start it first with
scripts/start_claude_bridge.sh).

    python scripts/run_generation.py --task patch --commit <after_commit>
    python scripts/run_generation.py --task test  --commit <after_commit>
"""

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import src.config as conf
from src.openhands.run_openhands import OpenHandsRunner


def _clean(value):
    """Empty strings and NaN in the dataset both mean 'not recorded'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run OpenHands patch/test generation for one commit.")
    p.add_argument("--task", choices=["patch", "test"], default="patch")
    p.add_argument("--commit", required=True, help="after_commit hash (key into the dataset).")
    p.add_argument("--dataset", default=conf.data['dataset-path'])
    p.add_argument("--repo", help="Override owner/repo.")
    p.add_argument("--issue", type=int, help="Override the GitHub issue number used for the task prompt.")
    p.add_argument("--before-commit", help="Override the baseline commit.")
    p.add_argument("--pr-number", type=int, help="Override the PR number to fetch.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved task and exit, before any image pull or API call.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        filename='logs/generation_{:%Y-%m-%d-%H-%M}.log'.format(datetime.now()),
        filemode='a',
        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
        level=logging.INFO,
    )

    repo, issue, before_commit, pr_number = args.repo, args.issue, args.before_commit, args.pr_number

    rows = pd.read_csv(args.dataset, dtype=str)
    match = rows[rows['after_commit'] == args.commit]
    if match.empty and None in (repo, issue):
        print(
            f"commit {args.commit} not in {args.dataset}; pass --repo and --issue explicitly.",
            file=sys.stderr,
        )
        return 2
    if not match.empty:
        row = match.iloc[0]
        repo = repo or _clean(row['repo'])
        issue = issue if issue is not None else _clean(row['issue_number'])
        before_commit = before_commit or _clean(row['before_commit'])
        pr_number = pr_number if pr_number is not None else _clean(row['pr_number'])

    if issue is None:
        print(f"no issue_number recorded for {args.commit}; pass --issue.", file=sys.stderr)
        return 2

    issue = int(issue)
    pr_number = int(pr_number) if pr_number is not None else None

    print(f"task          : {args.task} generation")
    print(f"repo          : {repo}")
    print(f"after_commit  : {args.commit}")
    print(f"before_commit : {before_commit}")
    print(f"issue / pr    : {issue} / {pr_number}")
    print(f"model         : {conf.openhands['llm-model']} @ {conf.openhands['llm-base-url'] or 'provider default'}")

    if args.dry_run:
        print("dry run — stopping before the image pull.")
        return 0

    runner = OpenHandsRunner()
    if args.task == 'patch':
        runner.run_patch_generation(repo, before_commit, args.commit, issue, pr_number)
    else:
        runner.run_test_generation(repo, before_commit, args.commit, issue, pr_number)

    print(f"done — generated files are under {os.path.join(conf.openhands['working-dir'], f'workspace_{args.commit}')}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
