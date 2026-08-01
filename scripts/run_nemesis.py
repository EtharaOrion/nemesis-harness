"""Run the tasks in input/ and write scored results to output/.

    python scripts/run_nemesis.py                    # every task, Opus agent
    python scripts/run_nemesis.py --agent oracle     # sanity check: expect ~1.0
    python scripts/run_nemesis.py --limit 3 --jobs 3

Each task under the input directory is a self-contained Repo2RLEnv `pr_diff`
environment: `environment/Dockerfile` clones the repo at the PR base commit and
bakes in `/verifier/{verifier.py,oracle.patch,instruction.md}`. This runner
builds that image, lets an agent edit `/workspace`, then runs `tests/test.sh`,
which diffs the workspace against the base commit and scores the result with
the 6-component diff-similarity reward.

Agents (--agent):
  opus    (default) single-shot patcher — sends instruction.md plus the task's
          context files to Opus 4.8 through the Claude bridge and applies the
          unified diff it returns. One turn, no feedback loop; a baseline, not
          a strong agent.
  oracle  applies solution/patch.diff. The reward should land at ~1.0; use it
          to prove the plumbing before spending model tokens.
  none    no edits. The predicted diff is empty, so the reward floors out.
          Use it to see what a do-nothing run scores on this dataset.

Output follows the Harbor trajectory layout (scripts/harbor_bundle.py), rooted
at output/ so the input tree stays untouched:

    output/<uuid>/trajectories/<Pretty Model>/pass_summary.json
                                             /run_N/{output.json,report.json,
                                                     logs/verifier/,output_media/}
    output/sweeps/sweep-NNNN.json     cross-task roll-up for one invocation

The verifier's llm_judge carries half the reward weight; see --judge for how it
is served.
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harbor_bundle

WORKSPACE = "/workspace"
VERIFIER_LOGS = "/logs/verifier"
PREDICTED_PATCH = "/tmp/predicted.patch"

DIFF_PROMPT = """{instruction}

## Files you may change

{files}

Reply with ONE unified diff and nothing else — no prose, no markdown fences.
Start each file with a `diff --git a/<path> b/<path>` header followed by its
`---`/`+++` pair, using paths relative to the repository root, so that
`git apply -p1` applies it cleanly. If the change needs a test, include the
test file in the same diff.
"""

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
If the change needs a test, update the test file listed above as well.
"""


def run(cmd, **kwargs):
    return subprocess.run(cmd, text=True, **kwargs)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class Log:
    """Per-task logger: prefixes stdout and tees everything into the bundle."""

    def __init__(self, label, path=None):
        self.label = label
        self.lines = []
        self.path = path

    def __call__(self, stage, message):
        line = f"[{self.label}] [{stage}] {message}"
        self.lines.append(line)
        print(line, flush=True)

    def flush(self):
        if self.path:
            with open(self.path, 'w') as f:
                f.write("\n".join(self.lines) + "\n")


# --------------------------------------------------------------------------
# task discovery
# --------------------------------------------------------------------------

def load_task(task_dir):
    with open(os.path.join(task_dir, 'task.toml'), 'rb') as f:
        meta = tomllib.load(f)
    repo2env = meta['metadata']['repo2env']
    return {
        'dir': task_dir,
        'uuid': os.path.basename(task_dir.rstrip('/')),
        'name': meta['task']['name'],
        'description': meta['task'].get('description', ''),
        'repo': repo2env['repo'],
        'base_ref': repo2env['ref'],
        'reference': repo2env.get('reference'),
        'difficulty': meta.get('metadata', {}).get('difficulty'),
        'context_files': repo2env.get('pr_diff', {}).get('context_files', []),
        'image_ref': repo2env.get('reproducibility', {}).get('image_ref'),
    }


def discover(input_dir, only=None, limit=None):
    tasks = []
    for name in sorted(os.listdir(input_dir)):
        task_dir = os.path.join(input_dir, name)
        if not os.path.isfile(os.path.join(task_dir, 'task.toml')):
            continue
        if only and name not in only:
            continue
        tasks.append(load_task(task_dir))
    if limit:
        tasks = tasks[:limit]
    return tasks


# --------------------------------------------------------------------------
# container plumbing
# --------------------------------------------------------------------------

def build_image(task, image, platform, rebuild, log):
    """Build the task image, reusing an existing one unless --rebuild."""
    if not rebuild:
        exists = run(["docker", "image", "inspect", image], capture_output=True)
        if exists.returncode == 0:
            log('build', f"reusing {image}")
            return True

    cmd = ["docker", "build", "--platform", platform, "-t", image]
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('github_access_token')
    if token:
        cmd += ["--build-arg", f"GITHUB_TOKEN={token}"]
    cmd.append(os.path.join(task['dir'], 'environment'))

    log('build', f"cloning {task['repo']} @ {task['base_ref'][:12]} (first build is slow)")
    started = time.time()
    built = run(cmd, capture_output=True)
    if built.returncode != 0:
        tail = (built.stderr or '').strip().splitlines()[-5:]
        log('build', "FAILED: " + " / ".join(tail))
        return False
    log('build', f"image ready in {time.time() - started:.0f}s")
    return True


# --- runtime patches to the in-image verifier -----------------------------
#
# Two edits, both applied inside the container so input/ stays byte-identical
# to the dataset:
#
#  1. URL. The stock file hardcodes api.anthropic.com, so the Claude bridge
#     cannot serve the judge. Rewritten to honour ANTHROPIC_BASE_URL.
#  2. Score extraction. The stock regex `\\{[^{}]*"score"[^{}]*\\}` cannot match
#     a JSON object whose reasoning string contains braces — and on a Java
#     corpus the judge quotes braces constantly ("${aesh.version}", code
#     snippets). The judge then returns missing_score and its 0.50 weight is
#     silently redistributed, so affected tasks are scored with no semantic
#     component at all. Replaced with a brace-balanced scan.
#
# Both are bug fixes, not scoring changes: they alter whether the judge's
# answer is received, never how the reward is computed from it.

_JUDGE_URL_LITERAL = '"https://api.anthropic.com/v1/messages",'
_JUDGE_URL_PATCHED = (
    'os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")'
    '.rstrip("/") + "/v1/messages",'
)

_SCORE_LITERAL = '''    m = re.search(r"\\{[^{}]*\\"score\\"[^{}]*\\}", text, re.DOTALL)
    if not m:
        return None, "missing_score"
    try:
        obj = json.loads(m.group(0))
        score = float(obj.get("score", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, "missing_score"'''

_SCORE_PATCHED = '''    obj = None
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        depth, instr, esc = 0, False, False
        for end in range(start, len(text)):
            ch = text[end]
            if esc:
                esc = False
                continue
            if ch == "\\\\" and instr:
                esc = True
                continue
            if ch == '"':
                instr = not instr
            elif not instr and ch == "{":
                depth += 1
            elif not instr and ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        cand = json.loads(text[start:end + 1])
                    except (json.JSONDecodeError, ValueError):
                        cand = None
                    if isinstance(cand, dict) and "score" in cand:
                        obj = cand
                    break
        if obj is not None:
            break
    if obj is None:
        return None, "missing_score"
    try:
        score = float(obj.get("score", 0))
    except (ValueError, TypeError):
        return None, "missing_score"'''

_PATCH_SCRIPT = f'''
import io, sys
p = "/verifier/verifier.py"
src = io.open(p, encoding="utf-8").read()
done = []
url_old = {_JUDGE_URL_LITERAL!r}
if url_old in src:
    src = src.replace(url_old, {_JUDGE_URL_PATCHED!r}, 1)
    done.append("url")
elif "ANTHROPIC_BASE_URL" in src:
    done.append("url-already")
score_old = {_SCORE_LITERAL!r}
if score_old in src:
    src = src.replace(score_old, {_SCORE_PATCHED!r}, 1)
    done.append("score")
io.open(p, "w", encoding="utf-8").write(src)
compile(src, p, "exec")          # refuse to ship a verifier that will not import
print(",".join(done) or "none")
'''


def start_container(image, container, platform, judge, log):
    """Start the task container, wiring the verifier's LLM judge per `judge`.

    judge['mode'] is one of:
      off     no key reaches the container; llm_judge reports no_api_key and
              its 0.50 weight is redistributed over the deterministic parts.
      api     stock behaviour — a real key, straight to api.anthropic.com.
      bridge  route the judge through the Claude bridge on the subscription.
              Needs a one-line runtime patch of /verifier/verifier.py, since
              the stock file ignores ANTHROPIC_BASE_URL. input/ is untouched.
    """
    run(["docker", "rm", "-f", container], capture_output=True)
    cmd = [
        "docker", "run", "-d", "--platform", platform, "--name", container,
        "--add-host", "host.docker.internal:host-gateway",
    ]
    if judge['mode'] != 'off':
        cmd += ["-e", f"ANTHROPIC_API_KEY={judge['key']}",
                "-e", f"R2E_JUDGE_MODEL={judge['model']}"]
    if judge['mode'] == 'bridge':
        cmd += ["-e", f"ANTHROPIC_BASE_URL={judge['base_url']}"]
    cmd += [image, "tail", "-f", "/dev/null"]
    started = run(cmd, capture_output=True)
    if started.returncode != 0:
        log('run', f"container start failed: {(started.stderr or '').strip()[:200]}")
        return False

    if judge['mode'] != 'off':
        patched = run(["docker", "exec", container, "python3", "-c", _PATCH_SCRIPT],
                      capture_output=True)
        if patched.returncode != 0:
            log('judge', "verifier patch failed — judge may report missing_score: "
                         + (patched.stderr or '').strip()[:160])
        else:
            log('judge', f"verifier patched ({patched.stdout.strip()})")
    return True


def read_context_files(container, paths, log):
    """Return (existing_file_blocks, paths_to_create).

    A context file that is absent at the base commit is one the PR *creates*.
    Four of the thirty tasks are new-file-only, so treating "unreadable" as
    fatal would hand them a 0.0 for a reason that has nothing to do with the
    model. They are listed to the agent as files to create instead.
    """
    files, to_create = [], []
    for path in paths:
        r = run(["docker", "exec", container, "cat", f"{WORKSPACE}/{path}"],
                capture_output=True)
        if r.returncode != 0:
            log('agent', f"{path} absent at base — listing as a file to create")
            to_create.append(path)
            continue
        content = r.stdout
        if len(content) > 120_000:
            content = content[:120_000] + "\n... [truncated]\n"
        files.append(f"### {path}\n```java\n{content}\n```")
    return files, to_create


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


def extract_diff(text):
    """Pull a unified diff out of the model's reply."""
    text = text.strip()
    fence = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = re.search(r"^(diff --git |--- )", text, re.M)
    return normalize_diff(text[start.start():].strip() + "\n") if start else ""


def normalize_diff(diff):
    """Insert the `diff --git` header git needs to see a multi-file patch.

    Without it, `git apply --recount` reads the second file's `--- a/...` as a
    removal line belonging to the previous hunk and rejects the whole patch as
    corrupt. Models omit the header constantly; adding it back is free.
    """
    lines = diff.split('\n')
    out = []
    for i, line in enumerate(lines):
        if (line.startswith('--- ')
                and i + 1 < len(lines) and lines[i + 1].startswith('+++ ')
                and not any(p.startswith('diff --git') for p in out[-1:])):
            new = lines[i + 1][4:].split('\t')[0].strip()
            path = re.sub(r'^[ab]/', '', new)
            if path and path != '/dev/null':
                out.append(f"diff --git a/{path} b/{path}")
        out.append(line)
    return '\n'.join(out)


def write_files_into_container(container, files, log):
    for path, body in files.items():
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write(body)
            local = f.name
        copied = run(["docker", "cp", local, f"{container}:{WORKSPACE}/{path}"],
                     capture_output=True)
        os.unlink(local)
        if copied.returncode != 0:
            return False, f"could not write {path}: {(copied.stderr or '').strip()[:200]}"
        log('agent', f"wrote {path} ({len(body)} bytes)")
    return True, None


def apply_diff_in_container(container, diff, log):
    """Apply the model's diff, escalating through progressively looser tools.

    `git apply --recount` fixes wrong hunk *counts*; nothing in git fixes wrong
    hunk *line numbers*, which is what a model actually gets wrong, and the
    slim task image has no patch(1). scripts/nemesis_apply.py closes that gap
    by locating each hunk by its context.
    """
    with tempfile.NamedTemporaryFile('w', suffix='.patch', delete=False) as f:
        f.write(diff)
        local = f.name
    run(["docker", "cp", local, f"{container}:/tmp/fix.patch"], capture_output=True)
    os.unlink(local)
    run(["docker", "cp", os.path.join(ROOT, 'scripts', 'nemesis_apply.py'),
         f"{container}:/tmp/nemesis_apply.py"], capture_output=True)

    attempts = (
        ("git apply", ["git", "apply", "-p1", "--recount",
                       "--ignore-whitespace", "/tmp/fix.patch"]),
        ("git apply -C1", ["git", "apply", "-p1", "--recount", "-C1",
                           "--ignore-whitespace", "/tmp/fix.patch"]),
        ("context match", ["python3", "/tmp/nemesis_apply.py", "/tmp/fix.patch"]),
    )
    errors = []
    for label, cmd in attempts:
        r = run(["docker", "exec", "--workdir", WORKSPACE, container, *cmd],
                capture_output=True)
        if r.returncode == 0:
            if label != "git apply":
                log('agent', f"applied via {label} fallback")
            if r.stdout.strip():
                log('agent', r.stdout.strip().splitlines()[-1])
            return True, None
        errors.append(f"{label}: {((r.stderr or '') + (r.stdout or '')).strip()[:150]}")
    return False, "patch did not apply — " + " | ".join(errors)


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------

def agent_opus(container, task, bundle, log, protocol='diff'):
    from src.llm.claude_bridge import Opus_4_8_Bridge
    from src.llm.invocation import Prompt
    import src.config as conf

    instruction = open(os.path.join(task['dir'], 'instruction.md')).read()
    files, to_create = read_context_files(container, task['context_files'], log)
    if not files and not to_create:
        return False, "the task lists no context files"

    context = "\n\n".join(files)
    if to_create:
        listed = "\n".join(f"- `{p}`" for p in to_create)
        context += ("\n\n### Files to create\n\nThese do not exist yet — your diff "
                    "should add them, with `--- /dev/null` as the source side:\n\n"
                    + listed + "\n")
    template = DIFF_PROMPT if protocol == 'diff' else FILES_PROMPT
    prompt = template.format(instruction=instruction, files=context)
    bundle['prompt'] = prompt
    log('agent', f"asking {conf.claude_bridge['model']} for a {protocol} "
                 f"({len(prompt)} chars over {len(files)} file(s)"
                 f"{f', {len(to_create)} to create' if to_create else ''})")

    adapter = Opus_4_8_Bridge()
    started = time.time()
    try:
        reply = adapter.get_response(Prompt([Prompt.Message('user', prompt)])).first_content
    except Exception as exc:
        return False, f"bridge call failed: {exc}"
    bundle['reply'] = reply
    bundle['latency_s'] = round(time.time() - started, 2)
    bundle['usages'] = adapter.usages
    bundle['stop_reasons'] = adapter.stop_reasons
    log('agent', f"model replied in {bundle['latency_s']:.0f}s ({len(reply)} chars)")

    # A max_tokens stop means the reply is cut off mid-file/mid-hunk, so
    # whatever we parse out of it is incomplete. Say so plainly rather than
    # reporting the downstream "no diff found" symptom.
    truncated = 'max_tokens' in bundle['stop_reasons']

    written = extract_files(reply)
    if written and protocol == 'files':
        if truncated:
            return False, (f"reply truncated at max_tokens "
                           f"({conf.claude_bridge['max-tokens']}) — the whole-file "
                           f"protocol does not fit these files; use --protocol diff")
        log('agent', f"model returned {len(written)} whole file(s)")
        ok, reason = write_files_into_container(container, written, log)
        return (True, None) if ok else (False, reason)

    diff = extract_diff(reply)
    if diff:
        if truncated:
            log('agent', "WARNING: reply hit max_tokens; the diff may be incomplete")
        log('agent', "model returned a diff; applying with --recount")
        ok, reason = apply_diff_in_container(container, diff, log)
        return (True, None) if ok else (False, reason)

    if written:
        log('agent', f"model returned {len(written)} whole file(s)")
        ok, reason = write_files_into_container(container, written, log)
        return (True, None) if ok else (False, reason)

    if truncated:
        return False, (f"reply truncated at max_tokens "
                       f"({conf.claude_bridge['max-tokens']}) before a usable patch — "
                       f"raise CLAUDE_BRIDGE_MAX_TOKENS or use --protocol diff")
    return False, "model reply contained neither a unified diff nor FILE: blocks"


def agent_oracle(container, task, bundle, log, protocol=None):
    """Apply the task's own solution/patch.diff — the reward should be ~1.0."""
    solution = os.path.join(task['dir'], 'solution')
    copied = run(["docker", "cp", solution, f"{container}:/solution"], capture_output=True)
    if copied.returncode != 0:
        return False, f"could not stage solution/: {(copied.stderr or '').strip()[:200]}"
    applied = run(["docker", "exec", container, "bash", "/solution/solve.sh"],
                  capture_output=True)
    if applied.returncode != 0:
        tail = ((applied.stderr or '') + (applied.stdout or '')).strip()[-300:]
        return False, f"solve.sh failed: {tail}"
    log('agent', "oracle patch applied")
    return True, None


def agent_none(container, task, bundle, log, protocol=None):
    log('agent', "none — workspace left untouched (empty predicted diff)")
    return True, None


AGENTS = {'opus': agent_opus, 'oracle': agent_oracle, 'none': agent_none}


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def verify(container, task, run_dir, log):
    """Run tests/test.sh in the container and pull back what it produced.

    Returns (reward_details, seconds, patch_path, test_script).
    """
    verifier_dir = os.path.join(run_dir, 'logs', 'verifier')
    os.makedirs(verifier_dir, exist_ok=True)

    staged = tempfile.mkdtemp(prefix='nemesis-tests-')
    tests = os.path.join(staged, 'tests')
    shutil.copytree(os.path.join(task['dir'], 'tests'), tests)
    run(["docker", "cp", tests, f"{container}:/tests"], capture_output=True)
    # Keep the exact verifier script that ran, the way the exec_time bundles do.
    shutil.copy(os.path.join(tests, 'test.sh'), os.path.join(verifier_dir, 'test.sh'))
    shutil.rmtree(staged, ignore_errors=True)

    started = time.time()
    verified = run(["docker", "exec", container, "bash", "/tests/test.sh"],
                   capture_output=True)
    seconds = round(time.time() - started, 1)
    log('verify', f"finished in {seconds:.0f}s (exit {verified.returncode})")
    with open(os.path.join(verifier_dir, 'verify.log'), 'w') as f:
        f.write((verified.stdout or '') + (verified.stderr or ''))

    # Staged to a temp file; write_bundle files it as output_media/patch.diff,
    # the name the delivery layout uses for the agent's change.
    with tempfile.NamedTemporaryFile('w', suffix='.diff', delete=False) as f:
        patch_path = f.name
    pulled = run(["docker", "cp", f"{container}:{PREDICTED_PATCH}", patch_path],
                 capture_output=True)
    if pulled.returncode != 0 or not os.path.getsize(patch_path):
        os.unlink(patch_path)
        patch_path = None

    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        details_path = f.name
    fetched = run(["docker", "cp", f"{container}:{VERIFIER_LOGS}/reward-details.json",
                   details_path], capture_output=True)
    if fetched.returncode != 0:
        os.unlink(details_path)
        log('verify', f"no reward produced: {(fetched.stderr or '').strip()[:200]}")
        return None, seconds, patch_path
    with open(details_path) as f:
        details = json.load(f)
    os.unlink(details_path)
    return details, seconds, patch_path


# --------------------------------------------------------------------------
# one task, end to end
# --------------------------------------------------------------------------

def run_task(task, args, output_dir, index, total, judge, model_label):
    """Run one task and write it as a Harbor trajectory bundle under

        output/<uuid>/trajectories/<Pretty Model>/run_N/

    which is the delivery layout run_harbor_task.py already produces for
    exec_time tasks, rooted at output/ instead of inside the task tree.
    """
    short = task['uuid'][:8]
    task_root = os.path.join(output_dir, task['uuid'])
    os.makedirs(task_root, exist_ok=True)
    run_dir, run_index = harbor_bundle.next_run_dir(task['dir'], model_label,
                                                    root=task_root)
    log = Log(f"{index}/{total} {short}", os.path.join(run_dir, 'run.log'))

    image = f"nemesis/{task['uuid']}:{task['base_ref'][:12]}"
    container = f"nemesis-run-{short}"
    bundle = {'prompt': None, 'reply': None, 'latency_s': None,
              'usages': [], 'stop_reasons': []}
    started_at = harbor_bundle._now()
    details, patch_path, seconds = None, None, None
    applied, agent_error = False, None

    log('plan', f"{task['name']}  ({task['repo']}, {task['difficulty']})")
    log('bundle', f"run {run_index} -> {os.path.relpath(run_dir, ROOT)}")

    try:
        if not build_image(task, image, args.platform, args.rebuild, log):
            agent_error = 'image build failed'
        elif not start_container(image, container, args.platform, judge, log):
            agent_error = 'container start failed'
        else:
            applied, agent_error = AGENTS[args.agent](container, task, bundle,
                                                      log, args.protocol)
            if not applied:
                log('agent', f"failed: {agent_error}")
                # Score it anyway: an empty diff is a legitimate (bad)
                # prediction, and the run stays comparable across tasks.
            details, seconds, patch_path = verify(container, task, run_dir, log)
            if details:
                log('reward', f"{details.get('final_reward'):.4f}  "
                              f"(judge: {details.get('judge_status')})")
            else:
                agent_error = agent_error or 'verifier produced no reward'
    finally:
        harbor_bundle.write_bundle(
            task_dir=task['dir'],
            run_dir=run_dir,
            run_index=run_index,
            model_id=model_id_for(args.agent),
            agent=args.agent,
            reward_kind='diff_similarity',
            meta_info={
                'task_id': task['uuid'],
                'source_task_id': task['name'],
                'repo': task['repo'],
                'reference': task['reference'],
                'base_ref': task['base_ref'],
                'difficulty': task['difficulty'],
                'image_ref': task['image_ref'],
                'platform': args.platform,
                'agent': args.agent,
                'protocol': args.protocol if args.agent == 'opus' else None,
                'agent_error': agent_error,
                'patch_applied': applied,
                'judge_transport': judge['mode'],
                'judge_model': judge['model'] if judge['mode'] != 'off' else None,
                'model_latency_s': bundle['latency_s'],
                'verify_seconds': seconds,
            },
            prompt_text=bundle['prompt'],
            reply_text=bundle['reply'],
            usages=bundle['usages'],
            stop_reasons=bundle['stop_reasons'],
            reward=details,
            patch_path=patch_path,
            started_at=started_at,
        )
        if patch_path and os.path.isfile(patch_path):
            os.unlink(patch_path)          # write_bundle copied it into the bundle
        model_root = os.path.dirname(run_dir)
        harbor_bundle.rebuild_pass_summary(model_root, model_label,
                                           reward_kind='diff_similarity')
        log.flush()
        if not args.keep:
            run(["docker", "rm", "-f", container], capture_output=True)
        else:
            log('run', f"container {container} left running (--keep)")

    return {
        'uuid': task['uuid'],
        'source_task_id': task['name'],
        'repo': task['repo'],
        'reference': task['reference'],
        'difficulty': task['difficulty'],
        'run_index': run_index,
        'trajectory': os.path.relpath(run_dir, output_dir),
        'reward': details.get('final_reward') if details else None,
        'components': details.get('components') if details else None,
        'judge_status': details.get('judge_status') if details else None,
        'patch_applied': applied,
        'agent_error': agent_error,
        'verify_seconds': seconds,
        'status': 'ok' if details else 'error',
    }


# --------------------------------------------------------------------------

def resolve_judge(choice, log_lines):
    """Decide how the in-container llm_judge (weight 0.50) gets served.

    'auto' prefers a real API key (stock behaviour), falls back to the Claude
    bridge on the subscription, and only then gives up.
    """
    import src.config as conf
    key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    model = os.environ.get('R2E_JUDGE_MODEL', 'claude-haiku-4-5-20251001')
    off = {'mode': 'off', 'key': None, 'model': model, 'base_url': None}

    if choice == 'off':
        return off
    if choice in ('api', 'auto') and key:
        log_lines.append(f"judge   {model} via api.anthropic.com (ANTHROPIC_API_KEY)")
        return {'mode': 'api', 'key': key, 'model': model, 'base_url': None}
    if choice == 'api':
        log_lines.append("judge   disabled — --judge api needs ANTHROPIC_API_KEY")
        return off

    # bridge (explicit, or auto with no key)
    if not check_bridge():
        log_lines.append(f"judge   disabled — bridge not answering at "
                         f"{conf.claude_bridge['url']}")
        return off
    log_lines.append(f"judge   {model} via the Claude bridge "
                     f"({conf.claude_bridge['docker-url']}, subscription)")
    log_lines.append("        note: verifier.py is patched in-container to honour "
                     "ANTHROPIC_BASE_URL; input/ is untouched")
    return {
        'mode': 'bridge',
        'key': conf.claude_bridge['secret'] or 'nemesis-bridge-stub',
        'model': model,
        'base_url': conf.claude_bridge['docker-url'],
    }


def model_id_for(agent):
    """The model string the bundle records. Only `opus` actually calls one."""
    if agent != 'opus':
        return agent
    import src.config as conf
    return conf.claude_bridge['model']


def next_sweep_path(output_dir):
    """Sweep roll-ups live beside the per-task trajectories, not inside them:
    one file per invocation, numbered, never overwritten."""
    sweeps = os.path.join(output_dir, 'sweeps')
    os.makedirs(sweeps, exist_ok=True)
    used = [int(m.group(1)) for name in os.listdir(sweeps)
            if (m := re.fullmatch(r'sweep-(\d{4})\.json', name))]
    return os.path.join(sweeps, f"sweep-{max(used, default=0) + 1:04d}.json")


def check_bridge():
    import httpx
    import src.config as conf
    url = conf.claude_bridge['url'].rstrip('/')
    try:
        r = httpx.get(f"{url}/healthz", timeout=5)
        return r.status_code == 200 and r.json().get('ok') is True
    except Exception:
        return False


def write_sweep(path, records, args, model_label, started_at):
    """Cross-task roll-up for one invocation.

    pass_summary.json answers "how does this model do on this task across
    repeats"; this answers "how does it do across the task set", which is the
    number you actually compare between agents.
    """
    scored = [r for r in records if r['reward'] is not None]
    rewards = [r['reward'] for r in scored]
    by_difficulty, by_repo = {}, {}
    for r in scored:
        by_difficulty.setdefault(r['difficulty'] or 'unknown', []).append(r['reward'])
        by_repo.setdefault(r['repo'], []).append(r['reward'])

    names = [n for r in scored for n in (r.get('components') or {})]
    summary = {
        'sweep': os.path.basename(path).removesuffix('.json'),
        'started_at': started_at,
        'finished_at': utcnow(),
        'model': model_label,
        'agent': args.agent,
        'reward_kind': 'diff_similarity',
        'input': os.path.relpath(args.input, ROOT),
        'tasks': len(records),
        'scored': len(scored),
        'failed': len(records) - len(scored),
        'average_reward': round(sum(rewards) / len(rewards), 4) if rewards else None,
        'solve_rate_percentage': (round(100 * sum(1 for r in rewards if r >= 1.0)
                                        / len(rewards), 2) if rewards else None),
        'average_components': {
            name: round(sum(vals) / len(vals), 4)
            for name in dict.fromkeys(names)
            if (vals := [r['components'][name] for r in scored
                         if isinstance((r.get('components') or {}).get(name), (int, float))])
        },
        'average_reward_by_difficulty': {
            k: round(sum(v) / len(v), 4) for k, v in sorted(by_difficulty.items())
        },
        'average_reward_by_repo': {
            k: round(sum(v) / len(v), 4) for k, v in sorted(by_repo.items())
        },
        'judge_status': sorted({r['judge_status'] for r in scored if r['judge_status']}),
        'results': sorted(records, key=lambda r: (r['reward'] is None, -(r['reward'] or 0))),
    }
    with open(path, 'w') as f:
        json.dump(summary, f, indent=2)
    return summary


def main():
    p = argparse.ArgumentParser(
        description=__doc__.split('\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('--input', default=os.path.join(ROOT, 'input'),
                   help="Directory of task folders (default: input/).")
    p.add_argument('--output', default=os.path.join(ROOT, 'output'),
                   help="Where run bundles are written (default: output/).")
    p.add_argument('--agent', choices=sorted(AGENTS), default='opus')
    p.add_argument(
        '--protocol', choices=['diff', 'files'], default='diff',
        help=(
            "How the opus agent returns its change. 'diff' (default) asks for a "
            "unified diff applied with git apply --recount; 'files' asks for "
            "whole files, which only fits tasks whose context files are small "
            "enough to reprint inside the bridge's max_tokens budget."
        ),
    )
    p.add_argument('--task', action='append', dest='tasks',
                   help="Run only this task UUID. Repeatable.")
    p.add_argument('--limit', type=int, help="Run only the first N tasks.")
    p.add_argument('--jobs', type=int, default=1,
                   help="Tasks to run concurrently (default: 1).")
    p.add_argument(
        '--judge', choices=['auto', 'bridge', 'api', 'off'], default='auto',
        help=("How to serve the verifier's llm_judge component (weight 0.50). "
              "'auto' uses ANTHROPIC_API_KEY if set, else the Claude bridge; "
              "'bridge' forces the subscription route; 'api' requires a real "
              "key; 'off' leaves the judge disabled and redistributes its "
              "weight over the deterministic components."))
    p.add_argument('--platform', default='linux/amd64')
    p.add_argument('--rebuild', action='store_true',
                   help="Rebuild task images even if they already exist.")
    p.add_argument('--keep', action='store_true', help="Leave containers running.")
    p.add_argument('--dry-run', action='store_true', help="List the plan and exit.")
    args = p.parse_args()

    args.input = os.path.abspath(args.input)
    args.output = os.path.abspath(args.output)
    if not os.path.isdir(args.input):
        print(f"no input directory at {args.input}", file=sys.stderr)
        return 2

    tasks = discover(args.input, only=set(args.tasks) if args.tasks else None,
                     limit=args.limit)
    if not tasks:
        print(f"no tasks with a task.toml under {args.input}", file=sys.stderr)
        return 2

    if args.agent == 'opus' and not args.dry_run and not check_bridge():
        import src.config as conf
        print(f"claude bridge not answering at {conf.claude_bridge['url']} — "
              f"start it with ./scripts/start_claude_bridge.sh", file=sys.stderr)
        return 2

    judge_lines = []
    judge = resolve_judge(args.judge, judge_lines)
    started_at = utcnow()
    model_label = harbor_bundle.pretty_model(model_id_for(args.agent), args.agent)

    print(f"input   {args.input}")
    print(f"output  {args.output}")
    print(f"agent   {args.agent}  ({model_label})")
    print(f"tasks   {len(tasks)}"
          f"{f' ({args.jobs} at a time)' if args.jobs > 1 else ''}")
    for line in judge_lines:
        print(line)
    if judge['mode'] == 'off':
        print("        llm_judge weight (0.50) is redistributed over the "
              "deterministic components")
    print()

    if args.dry_run:
        for i, t in enumerate(tasks, 1):
            print(f"[{i}/{len(tasks)} {t['uuid'][:8]}] [plan] {t['name']}  "
                  f"({t['repo']}, {t['difficulty']})")
        return 0

    total = len(tasks)
    if args.jobs > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(run_task, t, args, args.output, i, total,
                                   judge, model_label)
                       for i, t in enumerate(tasks, 1)]
            records = [f.result() for f in futures]
    else:
        records = [run_task(t, args, args.output, i, total, judge, model_label)
                   for i, t in enumerate(tasks, 1)]

    sweep_path = next_sweep_path(args.output)
    summary = write_sweep(sweep_path, records, args, model_label, started_at)
    print("\n=== summary ===")
    for r in summary['results']:
        reward = f"{r['reward']:.4f}" if r['reward'] is not None else "  --  "
        note = "" if r['status'] == 'ok' else f"  ({r['agent_error']})"
        print(f"  {reward}  {r['uuid'][:8]}  {r['source_task_id']}{note}")
    print(f"\nscored {summary['scored']}/{summary['tasks']}, "
          f"average reward {summary['average_reward']}")
    print(f"trajectories under {args.output}/<uuid>/trajectories/{model_label}/")
    print(f"sweep roll-up      {os.path.relpath(sweep_path, ROOT)}")
    return 0 if summary['scored'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
