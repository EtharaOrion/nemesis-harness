"""Run one Harbor task end to end: build environment, patch it, score it.

    python scripts/run_harbor_task.py mybatis__mybatis-3-ddd931bd0959

Stages: build the task image from environment/Dockerfile, start a container,
let an agent edit /app/original_repo, then run tests/test.sh and print the
reward.

Agents (--agent):
  opus  (default) single-shot patcher — sends instruction.md plus the task's
        target files to Opus 4.8 through the Claude bridge and applies the diff
        it returns. A baseline, not a strong agent: one turn, no test feedback,
        no iteration. For a real agentic run use scripts/run_generation.py
        (OpenHands), which drives the same model through the same bridge.
  none  no edits — the candidate stays identical to the baseline. Use this to
        validate the plumbing: a healthy setup scores 0.0 with
        exec_time_improvement ~ 0 and test_success true.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

import src.config as conf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harbor_bundle

CANDIDATE_REPO = "/app/original_repo"
BASELINE_REPO = "/app/baseline_repo"
LOG_DIR = "/logs/harbor"
REWARD_PATH = f"{LOG_DIR}/reward.json"

FILES_PROMPT = """{instruction}

## Files you may change

{files}

Reply with the COMPLETE new contents of every file you changed, and nothing
else. For each one, emit exactly:

FILE: <path relative to the repository root>
```java
<the entire file, from its first line to its last>
```

Repeat that block per changed file. Do not abbreviate, do not write "unchanged"
or "...", and do not emit a diff — the whole file is written to disk verbatim.
Change only production code; do not touch tests.
"""

DIFF_PROMPT = """{instruction}

## Files you may change

{files}

Reply with ONE unified diff and nothing else — no prose, no markdown fences.
Use a/ and b/ prefixes and paths relative to the repository root, so that
`git apply -p1` applies it cleanly. Change only production code; do not touch
tests.
"""


def run(cmd, **kwargs):
    return subprocess.run(cmd, text=True, **kwargs)


def log(stage, message):
    print(f"[{stage}] {message}", flush=True)


def load_task(task_dir):
    import tomllib
    with open(os.path.join(task_dir, 'task.toml'), 'rb') as f:
        meta = tomllib.load(f)
    jeto = meta['metadata']['repo2env']['jeto_exec_time']
    return meta, jeto


def check_bridge():
    url = conf.claude_bridge['url'].rstrip('/')
    try:
        r = httpx.get(f"{url}/healthz", timeout=5)
        return r.status_code == 200 and r.json().get('ok') is True
    except Exception:
        return False


def stage_tests(task_dir, exec_times):
    """Copy tests/ to a temp dir, overriding the run count if asked."""
    staged = tempfile.mkdtemp(prefix='harbor-tests-')
    tests = os.path.join(staged, 'tests')
    shutil.copytree(os.path.join(task_dir, 'tests'), tests)
    if exec_times:
        script = os.path.join(tests, 'test.sh')
        body = open(script).read()
        body = re.sub(r'^EXEC_TIMES=\d+$', f'EXEC_TIMES={exec_times}', body, flags=re.M)
        open(script, 'w').write(body)
        os.chmod(script, 0o755)
        cfg_path = os.path.join(tests, 'config.json')
        cfg = json.load(open(cfg_path))
        cfg['exec_times'] = exec_times
        json.dump(cfg, open(cfg_path, 'w'), indent=2)
    return staged, tests


def read_target_files(container, paths):
    files = []
    for path in paths:
        r = run(["docker", "exec", container, "cat", f"{CANDIDATE_REPO}/{path}"],
                capture_output=True)
        if r.returncode != 0:
            log('agent', f"skipping unreadable {path}")
            continue
        content = r.stdout
        # Keep the prompt bounded; these are single source files, not modules.
        if len(content) > 120_000:
            content = content[:120_000] + "\n... [truncated]\n"
        files.append(f"### {path}\n```java\n{content}\n```")
    return files


def extract_diff(text):
    """Pull a unified diff out of the model's reply."""
    text = text.strip()
    fence = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = re.search(r"^(diff --git |--- )", text, re.M)
    return text[start.start():].strip() + "\n" if start else ""


def extract_files(text):
    """Pull `FILE: <path>` + fenced whole-file blocks out of the model's reply."""
    pattern = re.compile(
        r"FILE:\s*(?P<path>[^\n`]+?)\s*\n+```[a-zA-Z]*\n(?P<body>.*?)\n?```",
        re.S,
    )
    files = {}
    for m in pattern.finditer(text):
        path = m.group('path').strip().strip('`').lstrip('./')
        if path:
            files[path] = m.group('body') + "\n"
    return files


def write_files_into_container(container, files):
    for path, body in files.items():
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write(body)
            local = f.name
        copied = run(["docker", "cp", local, f"{container}:{CANDIDATE_REPO}/{path}"],
                     capture_output=True)
        os.unlink(local)
        if copied.returncode != 0:
            return False, f"could not write {path}: {(copied.stderr or '').strip()[:200]}"
        log('agent', f"wrote {path} ({len(body)} bytes)")
    return True, None


def apply_diff_in_container(container, diff):
    """Fallback path. --recount rescues the usual LLM error: wrong hunk counts."""
    with tempfile.NamedTemporaryFile('w', suffix='.patch', delete=False) as f:
        f.write(diff)
        local = f.name
    run(["docker", "cp", local, f"{container}:/app/fix.patch"], capture_output=True)
    os.unlink(local)
    attempts = (
        ["git", "apply", "-p1", "--recount", "--ignore-whitespace", "/app/fix.patch"],
        ["patch", "-p1", "-l", "-F", "3", "-i", "/app/fix.patch"],
    )
    last = ""
    for cmd in attempts:
        r = run(["docker", "exec", "--workdir", CANDIDATE_REPO, container, *cmd],
                capture_output=True)
        if r.returncode == 0:
            return True, None
        last = ((r.stderr or '') + (r.stdout or '')).strip()[:300]
    return False, f"patch did not apply: {last}"


def capture_patch(container):
    """The actual applied change, diffed against the image's pristine baseline."""
    r = run(["docker", "exec", container, "diff", "-ruN", BASELINE_REPO, CANDIDATE_REPO],
            capture_output=True)
    # diff exits 1 when files differ, which is the expected case here.
    if r.returncode not in (0, 1) or not r.stdout.strip():
        return None
    with tempfile.NamedTemporaryFile('w', suffix='.diff', delete=False) as f:
        f.write(r.stdout)
        return f.name


class AgentRun:
    """Everything the trajectory bundle needs to record about the agent turn."""

    def __init__(self):
        self.prompt = None
        self.reply = None
        self.usages = []
        self.stop_reasons = []
        self.patch_path = None
        self.applied = False
        self.error = None
        self.latency_s = None


def run_opus_agent(container, task_dir, jeto, result, protocol='files'):
    from src.llm.claude_bridge import Opus_4_8_Bridge
    from src.llm.invocation import Prompt

    instruction = open(os.path.join(task_dir, 'instruction.md')).read()
    files = read_target_files(container, jeto.get('changed_files', []))
    if not files:
        result.error = "none of the task's target files could be read from the container"
        return False

    template = FILES_PROMPT if protocol == 'files' else DIFF_PROMPT
    result.prompt = template.format(instruction=instruction, files="\n\n".join(files))
    log('agent', f"asking {conf.claude_bridge['model']} for a patch "
                 f"({len(result.prompt)} chars over {len(files)} file(s))")
    adapter = Opus_4_8_Bridge()
    started = time.time()
    result.reply = adapter.get_response(
        Prompt([Prompt.Message('user', result.prompt)])
    ).first_content
    result.latency_s = round(time.time() - started, 2)
    result.usages = adapter.usages
    result.stop_reasons = adapter.stop_reasons
    log('agent', f"model replied in {result.latency_s:.0f}s ({len(result.reply)} chars)")

    # Whole-file protocol first; fall back to a diff if that's what came back.
    written = extract_files(result.reply)
    if written:
        log('agent', f"model returned {len(written)} whole file(s)")
        ok, reason = write_files_into_container(container, written)
        if not ok:
            result.error = reason
            return False
    else:
        diff = extract_diff(result.reply)
        if not diff:
            result.error = "model reply contained neither FILE: blocks nor a unified diff"
            return False
        log('agent', "model returned a diff; applying with --recount")
        ok, reason = apply_diff_in_container(container, diff)
        if not ok:
            result.error = reason
            return False

    result.applied = True
    result.patch_path = capture_patch(container)
    if result.patch_path:
        log('agent', "changes applied; diff captured against the pristine baseline")
    else:
        log('agent', "changes applied, but the baseline diff came back empty")
    return True


def collect_maven_logs(container, run_dir):
    """Pull the per-run Maven logs out before the container is torn down."""
    target = os.path.join(run_dir, 'logs', 'verifier', 'maven')
    os.makedirs(target, exist_ok=True)
    fetched = run(["docker", "cp", f"{container}:{LOG_DIR}/.", target], capture_output=True)
    if fetched.returncode != 0:
        log('bundle', f"could not copy maven logs: {(fetched.stderr or '').strip()[:200]}")
        return []
    return sorted(
        os.path.join(target, name)
        for name in os.listdir(target)
        if name.endswith('.log')
    )


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('task', help="Task id under datasets/<set>/tasks, or a path to a task dir.")
    p.add_argument('--dataset', default='datasets/jeto-bench')
    p.add_argument('--agent', choices=['opus', 'none'], default='opus')
    p.add_argument(
        '--protocol',
        choices=['files', 'diff'],
        default='files',
        help=(
            "How the agent returns its change. 'files' asks for whole files "
            "(no hunk arithmetic to get wrong); 'diff' asks for a unified diff "
            "and applies it with git apply --recount."
        ),
    )
    p.add_argument('--exec-times', type=int, help="Override timed runs per version.")
    p.add_argument('--platform', default='linux/amd64')
    p.add_argument('--keep', action='store_true', help="Leave the container running.")
    p.add_argument('--dry-run', action='store_true', help="Print the plan and exit.")
    args = p.parse_args()

    task_dir = args.task if os.path.isdir(args.task) else os.path.join(
        args.dataset, 'tasks', args.task)
    if not os.path.isfile(os.path.join(task_dir, 'task.toml')):
        print(f"no task.toml under {task_dir}", file=sys.stderr)
        return 2

    meta, jeto = load_task(task_dir)
    task_id = meta['task']['name']
    image = f"jeto/{task_id}".lower()
    container = f"jeto-run-{task_id}".lower()[:60]
    exec_times = args.exec_times or jeto['exec_times']

    log('plan', f"task      {task_id}  ({jeto['modified_modules']})")
    log('plan', f"base      {meta['metadata']['repo2env']['reproducibility']['image_ref']}")
    log('plan', f"agent     {args.agent}")
    log('plan', f"runs      {exec_times} per version ({2 * exec_times} Maven runs total)")
    if args.dry_run:
        return 0

    if args.agent == 'opus' and not check_bridge():
        print(f"claude bridge not answering at {conf.claude_bridge['url']} — "
              f"start it with ./scripts/start_claude_bridge.sh", file=sys.stderr)
        return 2

    log('build', f"docker build --platform {args.platform} -t {image} (first run pulls ~2.7GB)")
    started = time.time()
    if run(["docker", "build", "--platform", args.platform, "-t", image,
            os.path.join(task_dir, 'environment')]).returncode != 0:
        print("image build failed", file=sys.stderr)
        return 1
    log('build', f"image ready in {time.time() - started:.0f}s")

    run(["docker", "rm", "-f", container], capture_output=True)
    started_container = run([
        "docker", "run", "-d", "--platform", args.platform, "--name", container,
        "--add-host", "host.docker.internal:host-gateway",
        "-e", f"ANTHROPIC_BASE_URL={conf.claude_bridge['docker-url']}",
        "-e", f"ANTHROPIC_API_KEY={conf.claude_bridge['secret']}",
        image, "tail", "-f", "/dev/null",
    ], capture_output=True)
    if started_container.returncode != 0:
        print(f"container start failed: {started_container.stderr}", file=sys.stderr)
        return 1
    log('run', f"container {container} up")

    model_label = harbor_bundle.pretty_model(conf.claude_bridge['model'], args.agent)
    run_dir, run_index = harbor_bundle.next_run_dir(task_dir, model_label)
    log('bundle', f"run {run_index} -> {run_dir}")
    agent_run = AgentRun()
    started_at = harbor_bundle._now()
    verify_seconds = None

    try:
        if args.agent == 'opus':
            if not run_opus_agent(container, task_dir, jeto, agent_run, args.protocol):
                log('agent', f"failed: {agent_run.error}")
        else:
            log('agent', "none — candidate is identical to baseline (plumbing check)")

        reward = None
        staged_dir = None
        if args.agent == 'none' or agent_run.applied:
            staged, tests = stage_tests(task_dir, args.exec_times)
            staged_dir = tests
            run(["docker", "cp", tests, f"{container}:/tests"], capture_output=True)

            log('verify', f"running {2 * exec_times} Maven runs — this is the long part")
            started = time.time()
            verified = run(["docker", "exec", container, "bash", "/tests/test.sh"])
            verify_seconds = round(time.time() - started, 1)
            log('verify', f"finished in {verify_seconds:.0f}s (exit {verified.returncode})")

            reward_path = os.path.join(run_dir, 'logs', 'verifier', 'reward.json')
            fetched = run(["docker", "cp", f"{container}:{REWARD_PATH}", reward_path],
                          capture_output=True)
            if fetched.returncode == 0:
                reward = json.load(open(reward_path))
            else:
                log('verify', f"no reward.json produced: {(fetched.stderr or '').strip()[:200]}")

        maven_logs = collect_maven_logs(container, run_dir)
        verifier_files = [os.path.join(staged_dir, name)
                          for name in ('test.sh', 'config.json')] if staged_dir else []

        harbor_bundle.write_bundle(
            task_dir=task_dir,
            run_dir=run_dir,
            run_index=run_index,
            model_id=conf.claude_bridge['model'],
            agent=args.agent,
            meta_info={
                'task_id': task_id,
                'repo': meta['metadata']['repo2env']['repo'],
                'after_commit': jeto['after_commit'],
                'image_ref': meta['metadata']['repo2env']['reproducibility']['image_ref'],
                'platform': args.platform,
                'exec_times': exec_times,
                'modules': jeto['modified_modules'],
                'agent': args.agent,
                'protocol': args.protocol,
                'agent_error': agent_run.error,
                'patch_applied': agent_run.applied,
                'model_latency_s': agent_run.latency_s,
                'verify_seconds': verify_seconds,
                'bridge_url': conf.claude_bridge['url'],
            },
            prompt_text=agent_run.prompt,
            reply_text=agent_run.reply,
            usages=agent_run.usages,
            stop_reasons=agent_run.stop_reasons,
            reward=reward,
            patch_path=agent_run.patch_path,
            verifier_files=verifier_files,
            maven_logs=maven_logs,
            started_at=started_at,
        )
        if staged_dir:
            shutil.rmtree(os.path.dirname(staged_dir), ignore_errors=True)

        summary = harbor_bundle.rebuild_pass_summary(os.path.dirname(run_dir), model_label)
        log('bundle', f"{len(maven_logs)} maven log(s), pass_summary over "
                      f"{summary['runs']} run(s), avg reward {summary['average_reward']}")

        if reward is None:
            print(f"\nno reward produced — see {run_dir}", file=sys.stderr)
            return 1
        print("\n=== reward ===")
        print(json.dumps(reward, indent=2))
        print(f"\nreward {reward['reward']} — bundle at {run_dir}")
        return 0
    finally:
        if not args.keep:
            run(["docker", "rm", "-f", container], capture_output=True)
        else:
            log('run', f"container {container} left running (--keep)")


if __name__ == '__main__':
    raise SystemExit(main())
