"""Export JETO-Bench tasks as Harbor-format RL environments.

Reads results/dataset.csv and emits one Harbor task directory per executable
ETIP, using the prebuilt public per-commit images as the environment and the
harness's own criterion — statistically significant execution-time improvement
with the module test suite still green — as the reward.

    python scripts/export_harbor.py --out datasets/jeto-bench
    python scripts/export_harbor.py --out datasets/jeto-verified --only-verified
    python scripts/export_harbor.py --out /tmp/one --limit 1 --with-oracle

Layout follows Repo2RLEnv's spec (docs/reference/SPEC.md, spec_version 0.2.0):

    <out>/registry.json
    <out>/README.md
    <out>/tasks/manifest.json
    <out>/tasks/<task_id>/task.toml
    <out>/tasks/<task_id>/instruction.md
    <out>/tasks/<task_id>/environment/Dockerfile
    <out>/tasks/<task_id>/tests/{test.sh,verifier.py,config.json,f2p.json,p2p.json}
    <out>/tasks/<task_id>/solution/patch.diff        (--with-oracle only)

Two deliberate extensions to the spec, both recorded in the task's metadata:

  * pipeline = "jeto_exec_time" — not one of Repo2RLEnv's registered pipelines,
    so `repo2rlenv validate` will not recognise it.
  * reward_kinds includes "exec_time_improvement" alongside "test_execution".
    Repo2RLEnv only defines diff_similarity and test_execution; neither carries
    a timing signal, which is the entire point of this benchmark.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import src.config as conf

SPEC_VERSION = "0.2.0"
PIPELINE = "jeto_exec_time"
PIPELINE_VERSION = "0.1.0"
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'harbor')

# Inside the task image: the agent edits the repo, test.sh reconstructs the
# baseline beside it. /app/patched_repo (the reference solution) is stripped.
CANDIDATE_REPO = "/app/original_repo"
BASELINE_REPO = "/app/baseline_repo"
LOG_DIR = "/logs/harbor"


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    text = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return f'"{text}"'


def toml_table(name, rows):
    lines = [f"[{name}]"] if name else []
    for key, value in rows.items():
        if value is None or (isinstance(value, (list, dict)) and not value):
            continue
        lines.append(f"{key} = {toml_value(value)}")
    return "\n".join(lines)


def clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def parse_list(value):
    """modified_modules / changed_files are JSON lists stored as CSV strings."""
    raw = clean(value)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [part.strip() for part in raw.split(',') if part.strip()]
    return [str(p) for p in parsed] if isinstance(parsed, list) else []


def parse_improvements(value):
    raw = clean(value)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def image_ref(repo, after_commit):
    """Prebuilt public image, same naming src/utils.py::pull_image_install_git uses."""
    return f"ghcr.io/khesoem/{repo.split('/')[-1]}-{after_commit}:latest"


def task_id(repo, after_commit):
    return f"{repo.replace('/', '__')}-{after_commit[:12]}"


def content_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def render_instruction(row, modules, files, issue_text, exec_times):
    module_arg = ",".join(modules)
    lines = [
        f"# Speed up `{row['repo']}`",
        "",
        f"The repository is checked out at `{CANDIDATE_REPO}` and builds with the Maven",
        f"wrapper. The test suite for the affected module(s) runs with:",
        "",
        "```bash",
        f"cd {CANDIDATE_REPO}",
        f"./mvnw -pl {module_arg} -am test -DfailIfNoTests=false",
        "```",
        "",
        "## Task",
        "",
        f"There is an execution-time performance problem in the `{module_arg}` module(s).",
        "Change the production code so the same test suite runs measurably faster, without",
        "altering behaviour.",
        "",
    ]
    if issue_text:
        lines += ["## Reported issue", "", issue_text, ""]
    if files:
        lines += [
            "## Files changed by the reference fix",
            "",
            *[f"- `{path}`" for path in files],
            "",
        ]
    lines += [
        "## How you are scored",
        "",
        f"1. The module test suite must still compile and pass — every run, both versions.",
        f"2. Total test execution time must improve by at least"
        f" {conf.evaluation['min-exec-time-improvement']:.0%} against the unmodified baseline.",
        f"3. That improvement must hold under a one-sided sign test at"
        f" p < {conf.evaluation['min-p-value']} over {exec_times} alternating runs"
        f" per version (the first run of each is discarded as warm-up).",
        "",
        "Do not edit tests, and do not weaken assertions — the baseline is reconstructed",
        "from your repository's git state, so test edits are reverted before timing.",
    ]
    return "\n".join(lines) + "\n"


def render_dockerfile(image):
    return f"""# JETO-Bench task environment.
# The upstream image ships both /app/original_repo (pre-improvement) and
# /app/patched_repo (the real performance commit). The reference solution is
# removed here so the task is not gameable by reading it.
FROM {image}

# git: for the agent and for reverting edits. python3: the base image is a bare
# JDK, and tests/verifier.py (stdlib only) needs an interpreter.
RUN apt-get update && apt-get install -y --no-install-recommends git python3 \\
    && rm -rf /var/lib/apt/lists/* || true

RUN rm -rf /app/patched_repo

# Pristine baseline captured before the agent can touch anything, so timing does
# not depend on the repo carrying a usable .git.
RUN cp -r {CANDIDATE_REPO} {BASELINE_REPO}

WORKDIR {CANDIDATE_REPO}
"""


def render_test_sh(modules, exec_times):
    template = open(os.path.join(TEMPLATE_DIR, 'test.sh')).read()
    return (
        template
        .replace('{{CANDIDATE_REPO}}', CANDIDATE_REPO)
        .replace('{{BASELINE_REPO}}', BASELINE_REPO)
        .replace('{{MODULES}}', ",".join(modules))
        .replace('{{EXEC_TIMES}}', str(exec_times))
        .replace('{{LOG_DIR}}', LOG_DIR)
    )


def fetch_issue_and_oracle(gh, repo_name, row, want_oracle):
    """Optional GitHub enrichment: issue prose and the reference diff."""
    issue_text, oracle = None, None
    repo = gh.get_repo(repo_name)
    issue_number = clean(row['issue_number'])
    if issue_number:
        issue = repo.get_issue(int(float(issue_number)))
        issue_text = f"**{issue.title}**\n\n{issue.body or ''}".strip()
    if want_oracle:
        before = clean(row['before_commit'])
        after = row['after_commit']
        comparison = repo.compare(before, after) if before else None
        if comparison is not None:
            oracle = "\n".join(
                f"--- a/{f.filename}\n+++ b/{f.filename}\n{f.patch}"
                for f in comparison.files if f.patch
            )
        else:
            commit = repo.get_commit(after)
            oracle = "\n".join(
                f"--- a/{f.filename}\n+++ b/{f.filename}\n{f.patch}"
                for f in commit.files if f.patch
            )
    return issue_text, oracle


def min_exec_times(min_p_value):
    """Runs needed for the reward to be reachable at all.

    The sign test's smallest attainable p over n post-warm-up pairs is 0.5**n,
    so n must satisfy 0.5**n < min_p_value; +1 for the discarded warm-up run.
    At the default p < 0.1 that is 5 runs per version — conf.evaluation's
    exec-times of 2 leaves n=1, where the best possible p is 0.5 and no patch
    can ever score.
    """
    n = 1
    while 0.5 ** n >= min_p_value:
        n += 1
    return n + 1


def export_task(row, out_root, org, built_at, gh, want_oracle, exec_times):
    repo_name = row['repo']
    after = row['after_commit']
    modules = parse_list(row['modified_modules'])
    files = parse_list(row['changed_files'])
    if not modules:
        return None, "no modified_modules recorded"

    improvements = parse_improvements(row['test_class_improvements'])
    f2p = improvements.get('patched_outperforms_original', [])
    base_ref = clean(row['before_commit']) or f"{after}^"

    issue_text, oracle = (None, None)
    if gh is not None:
        try:
            issue_text, oracle = fetch_issue_and_oracle(gh, repo_name, row, want_oracle)
        except Exception as exc:  # noqa: BLE001 — enrichment is best effort
            return None, f"github enrichment failed: {exc}"

    tid = task_id(repo_name, after)
    task_dir = os.path.join(out_root, 'tasks', tid)
    for sub in ('environment', 'tests'):
        os.makedirs(os.path.join(task_dir, sub), exist_ok=True)

    instruction = render_instruction(row, modules, files, issue_text, exec_times)
    min_p = conf.evaluation['min-p-value']
    min_improvement = conf.evaluation['min-exec-time-improvement']

    digest = content_hash({
        'repo': repo_name, 'ref': base_ref, 'after_commit': after,
        'instruction': instruction, 'modules': modules, 'f2p': f2p,
        'pipeline': PIPELINE, 'pipeline_version': PIPELINE_VERSION,
        'reward': {'exec_times': exec_times, 'min_p_value': min_p,
                   'min_exec_time_improvement': min_improvement},
    })

    with open(os.path.join(task_dir, 'instruction.md'), 'w') as f:
        f.write(instruction)

    with open(os.path.join(task_dir, 'environment', 'Dockerfile'), 'w') as f:
        f.write(render_dockerfile(image_ref(repo_name, after)))

    tests_dir = os.path.join(task_dir, 'tests')
    test_sh = os.path.join(tests_dir, 'test.sh')
    with open(test_sh, 'w') as f:
        f.write(render_test_sh(modules, exec_times))
    os.chmod(test_sh, 0o755)
    shutil.copy(os.path.join(TEMPLATE_DIR, 'verifier.py'), os.path.join(tests_dir, 'verifier.py'))

    json.dump({
        'log_dir': LOG_DIR,
        'output_dir': LOG_DIR,
        'exec_times': exec_times,
        'min_p_value': min_p,
        'min_exec_time_improvement': min_improvement,
        'modules': modules,
        'f2p': f2p,
        # The suite of the modified modules is the P2P set; the dataset does not
        # enumerate individual passing tests, so the gate is "the whole suite".
        'p2p_policy': 'all-tests-in-modified-modules',
        'shaped': False,
    }, open(os.path.join(tests_dir, 'config.json'), 'w'), indent=2)
    json.dump(f2p, open(os.path.join(tests_dir, 'f2p.json'), 'w'), indent=2)
    json.dump([], open(os.path.join(tests_dir, 'p2p.json'), 'w'), indent=2)

    if oracle:
        os.makedirs(os.path.join(task_dir, 'solution'), exist_ok=True)
        with open(os.path.join(task_dir, 'solution', 'patch.diff'), 'w') as f:
            f.write(oracle + "\n")

    dataset_improvement = clean(row['exec_time_improvement'])
    dataset_p = clean(row['p_value'])
    task_toml = "\n\n".join([
        'version = "1.0"',
        toml_table('task', {
            'name': tid,
            'org': org,
            'description': f"Reduce execution time of {','.join(modules)} in {repo_name}",
        }),
        toml_table('metadata', {'difficulty': 'hard', 'category': 'performance'}),
        toml_table('metadata.repo2env', {
            'spec_version': SPEC_VERSION,
            'pipeline': PIPELINE,
            'pipeline_version': PIPELINE_VERSION,
            'repo': repo_name,
            'ref': base_ref,
            'reference': f"https://github.com/{repo_name}/commit/{after}",
            'source_access': 'public',
            'built_at': built_at,
            'synthesis_llm': 'none (deterministic export from results/dataset.csv)',
            'content_hash': digest,
            'reward_kinds': ['test_execution', 'exec_time_improvement'],
        }),
        toml_table('metadata.repo2env.jeto_exec_time', {
            'after_commit': after,
            'before_commit': clean(row['before_commit']),
            'issue_number': clean(row['issue_number']),
            'pr_number': clean(row['pr_number']),
            'modified_modules': modules,
            'changed_files': files,
            'exec_times': exec_times,
            'min_p_value': min_p,
            'min_exec_time_improvement': min_improvement,
            'p2p_policy': 'all-tests-in-modified-modules',
            'reference_exec_time_improvement': float(dataset_improvement) if dataset_improvement else None,
            'reference_p_value': float(dataset_p) if dataset_p else None,
            'manually_verified_improvement': clean(row['is_improvement_per_manual_analysis']) == 'True',
            'oracle_solution_stripped_from_image': True,
        }),
        toml_table('metadata.repo2env.reproducibility', {
            'mode': 'registry',
            'image_ref': image_ref(repo_name, after),
            'image_tag': image_ref(repo_name, after),
            'image_visibility': 'public',
        }),
        toml_table('agent', {'timeout_sec': 3600.0}),
        # Two versions x exec_times full Maven runs; generous by construction.
        toml_table('verifier', {'timeout_sec': float(1800 * exec_times)}),
    ]) + "\n"
    with open(os.path.join(task_dir, 'task.toml'), 'w') as f:
        f.write(task_toml)

    return {
        'task_id': tid,
        'repo': repo_name,
        'ref': base_ref,
        'after_commit': after,
        'reference': f"https://github.com/{repo_name}/commit/{after}",
        'image': image_ref(repo_name, after),
        'reward_kinds': ['test_execution', 'exec_time_improvement'],
        'f2p_count': len(f2p),
        'p2p_count': 0,
        'modules': modules,
        'content_hash': digest,
        'difficulty': 'hard',
        'manually_verified_improvement': clean(row['is_improvement_per_manual_analysis']) == 'True',
        'has_oracle': bool(oracle),
    }, None


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--out', required=True, help="Output dataset directory.")
    p.add_argument('--dataset', default=conf.data['dataset-path'])
    p.add_argument('--org', default='jeto-bench', help="Harbor [task].org value.")
    p.add_argument('--limit', type=int, help="Export at most N tasks.")
    p.add_argument('--commit', help="Export a single after_commit.")
    p.add_argument(
        '--include-unverified',
        action='store_true',
        help=(
            "Also export rows the manual analysis rejected as improvements "
            "(default: only is_improvement_per_manual_analysis == True, since a "
            "rejected row has no demonstrated speedup for an agent to find)."
        ),
    )
    p.add_argument(
        '--exec-times',
        type=int,
        help=(
            "Timed runs per version. Default is the smallest statistically "
            "usable value for the configured p threshold, rounded up to 11."
        ),
    )
    p.add_argument(
        '--with-issues',
        action='store_true',
        help="Fetch issue title/body from GitHub for the instruction (needs a token).",
    )
    p.add_argument(
        '--with-oracle',
        action='store_true',
        help="Also fetch the reference diff into solution/patch.diff (implies --with-issues).",
    )
    return p.parse_args()


def main():
    args = parse_args()
    built_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    floor = min_exec_times(conf.evaluation['min-p-value'])
    exec_times = args.exec_times if args.exec_times else max(11, floor)
    if exec_times < floor:
        print(
            f"--exec-times {exec_times} cannot reach p < {conf.evaluation['min-p-value']}: "
            f"the sign test needs at least {floor} runs per version.",
            file=sys.stderr,
        )
        return 2

    df = pd.read_csv(args.dataset, dtype=str).fillna('')
    # Only these rows have a working prebuilt image and recorded timings.
    rows = df[df['exec_status'] == 'maven_execution_successful']
    if not args.include_unverified:
        rows = rows[rows['is_improvement_per_manual_analysis'] == 'True']
    if args.commit:
        rows = rows[rows['after_commit'] == args.commit]
    if args.limit:
        rows = rows.head(args.limit)
    if rows.empty:
        print("no matching rows to export", file=sys.stderr)
        return 2

    gh = None
    if args.with_issues or args.with_oracle:
        from github import Auth, Github
        gh = Github(auth=Auth.Token(conf.github['access-token']))

    os.makedirs(os.path.join(args.out, 'tasks'), exist_ok=True)
    manifest, skipped = [], []
    for _, row in rows.iterrows():
        entry, reason = export_task(
            row, args.out, args.org, built_at, gh, args.with_oracle, exec_times
        )
        if entry is None:
            skipped.append((row['after_commit'], reason))
            continue
        manifest.append(entry)

    json.dump(manifest, open(os.path.join(args.out, 'tasks', 'manifest.json'), 'w'), indent=2)
    json.dump({
        'version': '1.0',
        'org': args.org,
        'built_at': built_at,
        'spec_version': SPEC_VERSION,
        'pipeline': PIPELINE,
        'tasks': [
            {'task_id': e['task_id'], 'path': f"tasks/{e['task_id']}",
             'content_hash': e['content_hash'], 'image': e['image']}
            for e in manifest
        ],
    }, open(os.path.join(args.out, 'registry.json'), 'w'), indent=2)

    with open(os.path.join(args.out, 'README.md'), 'w') as f:
        f.write(f"""# JETO-Bench — Harbor export

{len(manifest)} execution-time-improvement tasks in Java, exported from
`results/dataset.csv` by `scripts/export_harbor.py` at {built_at}.

Each task hands the agent a Java repository at a commit that a real
performance patch later improved, and rewards a *measured* speedup:

- **P2P gate** — the modified modules' test suite must compile and pass on
  every run, for both the baseline and the agent's version.
- **Reward 1.0** — total test execution time improves by at least
  {conf.evaluation['min-exec-time-improvement']:.0%} and survives a one-sided
  sign test at p < {conf.evaluation['min-p-value']} over {exec_times}
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
""")

    print(f"exported {len(manifest)} task(s) to {args.out}")
    if skipped:
        print(f"skipped {len(skipped)}:")
        for commit, reason in skipped[:10]:
            print(f"  {commit}: {reason}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
