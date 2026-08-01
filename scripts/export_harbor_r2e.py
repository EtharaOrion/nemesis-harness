"""Export JETO-Bench tasks using Repo2RLEnv's extraction + Harbor emitter.

Same 91 rows as scripts/export_harbor.py, but the task text and metadata come
from Repo2RLEnv's own machinery instead of my templates:

  * problem statement  — its `_SYNTH_SYSTEM` prompt, run on Opus 4.8 through the
    Claude bridge, rewriting the commit into a clean leak-free issue report
  * patch / test_patch — its SWE-bench split heuristic
  * difficulty, LOC, touched files — its bucketing helpers
  * task.toml + layout — its `write_harbor_task`, so the output is emitted by
    the same code path its own pipelines use

What we deliberately do NOT take from it: the bootstrap. Its `generate` CLI runs
an LLM agent that builds each repo from scratch in `eclipse-temurin:21-jdk`; on
this benchmark that is both redundant (JETO ships prebuilt per-commit images)
and unreliable (it exhausted 20 iterations on mybatis discovering the container
has no python and no system maven). We keep the `khesoem` images and JETO's
timing verifier, and use Repo2RLEnv purely for extraction.

Commit text and diff come from GitHub's public `.patch` endpoint — mbox format,
so one unauthenticated fetch yields subject, body and diff together.

    python scripts/export_harbor_r2e.py --out datasets/jeto-r2e --limit 3
    python scripts/export_harbor_r2e.py --out datasets/jeto-r2e
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pandas as pd

import src.config as conf
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.llm import complete
from repo2rlenv.pipelines.commit_runtime import _SYNTH_SYSTEM
from repo2rlenv.pipelines.pr_runtime import (
    _diff_loc_changed,
    _difficulty_bucket,
    _files_in_patch,
    _strip_info_leak,
    split_patch_and_test_patch,
)
from repo2rlenv.spec.input import LLMSpec

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_harbor as jeto  # reuse the row parsing + environment/test templates

PIPELINE = "jeto_exec_time"
PIPELINE_VERSION = "0.2.0"


def fetch_commit_patch(repo: str, sha: str, timeout=60.0):
    """GitHub's .patch endpoint: mbox headers + unified diff, no auth needed."""
    url = f"https://github.com/{repo}/commit/{sha}.patch"
    r = httpx.get(url, timeout=timeout, follow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"{url} -> HTTP {r.status_code}")
    text = r.text
    subject, body = "", ""
    m = re.search(r"^Subject: (?:\[PATCH[^\]]*\] )?(.*?)(?=^\w+:|^---$|\ndiff --git )",
                  text, re.S | re.M)
    if m:
        block = m.group(1).strip()
        parts = block.split("\n\n", 1)
        subject = " ".join(parts[0].split())
        body = parts[1].strip() if len(parts) > 1 else ""
        # The mbox trailer (`---` + diffstat) is not part of the message.
        body = re.split(r"^---\s*$", body, maxsplit=1, flags=re.M)[0].strip()
    start = text.find("diff --git ")
    diff = text[start:] if start != -1 else ""
    return subject, body, diff


def synthesize(llm_spec, subject, body, issue_number, repo):
    """Repo2RLEnv's own synthesis prompt, verbatim, via the Claude bridge."""
    src = f"Commit subject: {subject}\n\nCommit body:\n{body}\n"
    if issue_number:
        src += f"\nLinked issue: {repo}#{issue_number}\n"
    resp = complete(llm_spec, system=_SYNTH_SYSTEM, user=src,
                    max_tokens=1024, temperature=0.2)
    return (resp.content or "").strip()


def build_task(row, llm_spec, exec_times, org, built_at, no_llm=False):
    repo = row['repo']
    after = row['after_commit']
    modules = jeto.parse_list(row['modified_modules'])
    if not modules:
        return None, "no modified_modules recorded"

    subject, body, diff = fetch_commit_patch(repo, after)
    if not diff:
        return None, "no diff returned by the .patch endpoint"

    patch, test_patch = split_patch_and_test_patch(diff)
    loc = _diff_loc_changed(patch)
    files = _files_in_patch(patch)
    improvements = jeto.parse_improvements(row['test_class_improvements'])
    f2p = improvements.get('patched_outperforms_original', [])
    issue_number = jeto.clean(row['issue_number'])

    statement = ""
    if not no_llm:
        try:
            statement = synthesize(llm_spec, subject, body, issue_number, repo)
        except Exception as exc:  # noqa: BLE001 — fall back to raw commit text
            statement = ""
            print(f"  synthesis failed ({exc}); falling back to commit text", file=sys.stderr)
    if not statement:
        statement = _strip_info_leak(f"**{subject}**\n\n{body}".strip())

    module_arg = ",".join(modules)
    instruction = (
        f"# Issue\n\n{statement}\n\n"
        "## Task\n\n"
        f"The repository is checked out at `{jeto.CANDIDATE_REPO}`. Make the "
        f"`{module_arg}` module(s) execute measurably faster without changing "
        "behaviour.\n\n"
        "```bash\n"
        f"cd {jeto.CANDIDATE_REPO}\n"
        f"./mvnw -pl {module_arg} -am test -DfailIfNoTests=false\n"
        "```\n\n"
        "You are scored on measured execution time, not on matching any "
        "particular patch: the module suite must still compile and pass on "
        f"every run, total test time must improve by at least "
        f"{conf.evaluation['min-exec-time-improvement']:.0%} against the "
        "untouched baseline, and that improvement must survive a one-sided "
        f"sign test at p < {conf.evaluation['min-p-value']} over {exec_times} "
        "alternating runs per version. Do not edit tests — the baseline is "
        "restored from a pristine copy before timing.\n"
    )

    image = jeto.image_ref(repo, after)
    base_ref = jeto.clean(row['before_commit']) or f"{after}^"
    tests_dir = Path(jeto.TEMPLATE_DIR)
    aux_files = {
        'tests/verifier.py': (tests_dir / 'verifier.py').read_text(),
        'tests/f2p.json': json.dumps(f2p, indent=2),
        'tests/p2p.json': json.dumps([], indent=2),
        'tests/config.json': json.dumps({
            'log_dir': jeto.LOG_DIR,
            'output_dir': jeto.LOG_DIR,
            'exec_times': exec_times,
            'min_p_value': conf.evaluation['min-p-value'],
            'min_exec_time_improvement': conf.evaluation['min-exec-time-improvement'],
            'modules': modules,
            'f2p': f2p,
            'p2p_policy': 'all-tests-in-modified-modules',
            'shaped': False,
        }, indent=2),
    }

    dataset_improvement = jeto.clean(row['exec_time_improvement'])
    dataset_p = jeto.clean(row['p_value'])
    task = HarborTask(
        name=jeto.task_id(repo, after),
        org=org,
        description=f"Reduce execution time of {','.join(modules)} in {repo}",
        instruction=instruction,
        oracle_diff=patch,
        difficulty=_difficulty_bucket(len(f2p), loc),
        category="performance",
        keywords=["performance", "java", "maven", "execution-time"],
        environment_dockerfile=jeto.render_dockerfile(image),
        test_script=jeto.render_test_sh(modules, exec_times),
        aux_files=aux_files,
        repo2env={
            'spec_version': '0.2.0',
            'pipeline': PIPELINE,
            'pipeline_version': PIPELINE_VERSION,
            'repo': repo,
            'ref': base_ref,
            'reference': f"https://github.com/{repo}/commit/{after}",
            'source_access': 'public',
            'built_at': built_at,
            'synthesis_llm': f"anthropic/{conf.claude_bridge['model']}" if not no_llm else 'none',
            'reward_kinds': ['test_execution', 'exec_time_improvement'],
            'jeto_exec_time': {
                'after_commit': after,
                'before_commit': jeto.clean(row['before_commit']) or '',
                'issue_number': issue_number or '',
                'modified_modules': modules,
                'changed_files': files,
                'test_patch_present': bool(test_patch.strip()),
                'oracle_loc_changed': loc,
                'exec_times': exec_times,
                'min_p_value': conf.evaluation['min-p-value'],
                'min_exec_time_improvement': conf.evaluation['min-exec-time-improvement'],
                'reference_exec_time_improvement': float(dataset_improvement) if dataset_improvement else 0.0,
                'reference_p_value': float(dataset_p) if dataset_p else 0.0,
                'manually_verified_improvement': jeto.clean(row['is_improvement_per_manual_analysis']) == 'True',
                'oracle_solution_stripped_from_image': True,
            },
            'reproducibility': {
                'mode': 'registry',
                'image_ref': image,
                'image_tag': image,
                'image_visibility': 'public',
            },
        },
    )
    return task, None


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--out', required=True)
    p.add_argument('--dataset', default=conf.data['dataset-path'])
    p.add_argument('--org', default='jeto-bench')
    p.add_argument('--limit', type=int)
    p.add_argument('--commit')
    p.add_argument('--exec-times', type=int)
    p.add_argument('--include-unverified', action='store_true')
    p.add_argument('--no-llm', action='store_true',
                   help="Skip synthesis; use the raw commit text (offline).")
    args = p.parse_args()

    floor = jeto.min_exec_times(conf.evaluation['min-p-value'])
    exec_times = args.exec_times or max(11, floor)
    built_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    df = pd.read_csv(args.dataset, dtype=str).fillna('')
    rows = df[df['exec_status'] == 'maven_execution_successful']
    if not args.include_unverified:
        rows = rows[rows['is_improvement_per_manual_analysis'] == 'True']
    if args.commit:
        rows = rows[rows['after_commit'] == args.commit]
    if args.limit:
        rows = rows.head(args.limit)
    if rows.empty:
        print("no matching rows", file=sys.stderr)
        return 2

    llm_spec = LLMSpec(
        provider='anthropic',
        model=conf.claude_bridge['model'],
        api_key_env='ANTHROPIC_API_KEY',
        endpoint=conf.claude_bridge['url'],
        timeout_sec=600,
    )
    os.environ.setdefault('ANTHROPIC_API_KEY', conf.claude_bridge['secret'])

    tasks_dir = Path(args.out) / 'tasks'
    tasks_dir.mkdir(parents=True, exist_ok=True)
    manifest, skipped = [], []
    for i, (_, row) in enumerate(rows.iterrows(), start=1):
        label = f"{row['repo']}@{row['after_commit'][:12]}"
        print(f"[{i}/{len(rows)}] {label}", flush=True)
        started = time.time()
        try:
            task, reason = build_task(row, llm_spec, exec_times, args.org, built_at, args.no_llm)
        except Exception as exc:  # noqa: BLE001
            task, reason = None, str(exc)
        if task is None:
            print(f"  skipped: {reason}", file=sys.stderr)
            skipped.append((row['after_commit'], reason))
            continue
        path = write_harbor_task(task, tasks_dir)
        manifest.append({
            'task_id': task.name,
            'repo': row['repo'],
            'path': f"tasks/{task.name}",
            'difficulty': task.difficulty,
            'image': task.repo2env['reproducibility']['image_ref'],
            'f2p_count': len(json.loads(task.aux_files['tests/f2p.json'])),
            'oracle_loc_changed': task.repo2env['jeto_exec_time']['oracle_loc_changed'],
        })
        print(f"  -> {path.name}  difficulty={task.difficulty} "
              f"loc={task.repo2env['jeto_exec_time']['oracle_loc_changed']} "
              f"({time.time() - started:.1f}s)", flush=True)

    (Path(args.out) / 'tasks' / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    (Path(args.out) / 'registry.json').write_text(json.dumps({
        'version': '1.0', 'org': args.org, 'built_at': built_at,
        'spec_version': '0.2.0', 'pipeline': PIPELINE,
        'tasks': [{'task_id': m['task_id'], 'path': m['path'], 'image': m['image']}
                  for m in manifest],
    }, indent=2))

    print(f"\nexported {len(manifest)} task(s) to {args.out}")
    if skipped:
        print(f"skipped {len(skipped)}:")
        for sha, reason in skipped[:10]:
            print(f"  {sha[:12]}: {reason}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
