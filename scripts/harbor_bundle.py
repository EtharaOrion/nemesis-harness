"""Write a run bundle for a Harbor task, in the delivery layout used by the
sibling benchmarks (mikazuki-delivery / WildClawBench):

    <root>/trajectories/<Pretty Model>/
        pass_summary.json
        run_N/
            output.json          session_id, timestamp, trajectory, input_files,
                                 output_artifacts, messages, usage
            report.json          model, run_index, reward + verifier breakdown
            logs/verifier/       reward.txt, reward.json, test.sh, config.json,
                                 maven/*.log (pulled out of the container)
            output_media/        the agent's patch

`<root>` defaults to the task directory, which is where JETO-Bench keeps its
trajectories. run_nemesis.py passes an explicit root so a sweep writes under
output/<uuid>/ and leaves the input task tree untouched.

Two reward kinds share the layout, differing only in the report.json body:

  exec_time        JETO-Bench's timing criterion (run_harbor_task.py).
  diff_similarity  Repo2RLEnv's pr_diff 6-component score (run_nemesis.py).

Kept separate from run_harbor_task.py so it can be exercised without Docker or
an LLM.
"""

import json
import mimetypes
import os
import uuid
from datetime import datetime, timezone

PRETTY_MODEL = {
    'claude-opus-4-8': 'Claude Opus 4.8',
    'claude-opus-4-7': 'Claude Opus 4.7',
    'claude-sonnet-4-6': 'Claude Sonnet 4.6',
}


def pretty_model(model_id, agent):
    if agent == 'none':
        return 'No Agent (baseline)'
    if agent == 'oracle':
        return 'Oracle (gold patch)'
    return PRETTY_MODEL.get(model_id, model_id)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _file_entry(ref_id, path, source, role=None):
    entry = {
        'ref_id': ref_id,
        'filename': os.path.basename(path),
        'mime_type': mimetypes.guess_type(path)[0] or 'application/octet-stream',
        'size_bytes': os.path.getsize(path) if os.path.isfile(path) else 0,
        'source': source,
    }
    if role:
        entry['role'] = role
    return entry


def next_run_dir(task_dir, model_dir_name, root=None):
    root = os.path.join(root or task_dir, 'trajectories', model_dir_name)
    os.makedirs(root, exist_ok=True)
    used = [
        int(name.split('_')[1])
        for name in os.listdir(root)
        if name.startswith('run_') and name.split('_')[1].isdigit()
    ]
    index = max(used) + 1 if used else 1
    run_dir = os.path.join(root, f'run_{index}')
    os.makedirs(os.path.join(run_dir, 'logs', 'verifier', 'maven'), exist_ok=True)
    os.makedirs(os.path.join(run_dir, 'output_media'), exist_ok=True)
    return run_dir, index


def build_messages(prompt_text, reply_text, started_at, finished_at):
    """Two-turn transcript for the single-shot agent; empty for --agent none."""
    if prompt_text is None:
        return []
    return [
        {
            'type': 'message',
            'id': uuid.uuid4().hex[:8],
            'timestamp': started_at,
            'message': {'role': 'user', 'content': [{'type': 'text', 'text': prompt_text}]},
        },
        {
            'type': 'message',
            'id': uuid.uuid4().hex[:8],
            'timestamp': finished_at,
            'message': {'role': 'assistant',
                        'content': [{'type': 'text', 'text': reply_text or ''}]},
        },
    ]


def merge_usage(usages):
    """Anthropic usage blocks -> the delivery bundle's usage shape."""
    total = {
        'input_tokens': 0,
        'output_tokens': 0,
        'cached_input_tokens': 0,
        'cache_read_tokens': 0,
        'cache_write_tokens': 0,
        # Subscription-billed through the Claude bridge: no per-call price.
        'cost_usd': 0.0,
    }
    for u in usages or []:
        total['input_tokens'] += u.get('input_tokens', 0)
        total['output_tokens'] += u.get('output_tokens', 0)
        read = u.get('cache_read_input_tokens', 0)
        total['cache_read_tokens'] += read
        total['cached_input_tokens'] += read
        total['cache_write_tokens'] += u.get('cache_creation_input_tokens', 0)
    return total


def reward_value(reward, reward_kind='exec_time'):
    """The scalar Harbor scores on, whichever verifier produced it."""
    if not reward:
        return 0.0
    if reward_kind == 'diff_similarity':
        return reward.get('final_reward', 0.0)
    return reward.get('reward', 0.0)


def flat_reward_json(reward, reward_kind='exec_time'):
    """Harbor's reward.json must be a flat map of floats/ints, so nested
    breakdowns and status strings are dropped here and live in the sidecar."""
    flat = {'reward': reward_value(reward, reward_kind)}
    if reward_kind == 'diff_similarity':
        for name, value in (reward.get('components') or {}).items():
            if isinstance(value, (int, float)):
                flat[name] = value
        return flat
    for name, value in (reward or {}).items():
        if isinstance(value, bool):
            flat[name] = int(value)
        elif isinstance(value, (int, float)):
            flat[name] = value
    return flat


def write_bundle(
    task_dir,
    run_dir,
    run_index,
    model_id,
    agent,
    meta_info,
    prompt_text,
    reply_text,
    usages,
    stop_reasons,
    reward,
    patch_path=None,
    verifier_files=(),
    maven_logs=(),
    started_at=None,
    finished_at=None,
    reward_kind='exec_time',
):
    started_at = started_at or _now()
    finished_at = finished_at or _now()
    verifier_dir = os.path.join(run_dir, 'logs', 'verifier')

    # --- artifacts -------------------------------------------------------
    artifacts = []
    if patch_path and os.path.isfile(patch_path):
        target = os.path.join(run_dir, 'output_media', 'patch.diff')
        with open(patch_path) as src, open(target, 'w') as dst:
            dst.write(src.read())
        artifacts.append(_file_entry('artifact_0', target, 'agent_patch'))

    for i, src_path in enumerate(verifier_files):
        if not os.path.isfile(src_path):
            continue
        target = os.path.join(verifier_dir, os.path.basename(src_path))
        if os.path.abspath(src_path) != os.path.abspath(target):
            with open(src_path, 'rb') as src, open(target, 'wb') as dst:
                dst.write(src.read())
        artifacts.append(_file_entry(f'artifact_{len(artifacts)}', target, 'verifier'))

    if reward is not None:
        with open(os.path.join(verifier_dir, 'reward.txt'), 'w') as f:
            f.write(f"{reward_value(reward, reward_kind)}\n")
        # exec_time tasks already have the container's own reward.json copied
        # in by the caller; only pr_diff needs one synthesized from the
        # breakdown, alongside the nested sidecar the flat schema can't hold.
        if reward_kind == 'diff_similarity':
            with open(os.path.join(verifier_dir, 'reward.json'), 'w') as f:
                json.dump(flat_reward_json(reward, reward_kind), f, indent=2)
            with open(os.path.join(verifier_dir, 'reward-details.json'), 'w') as f:
                json.dump(reward, f, indent=2)

    # --- inputs ----------------------------------------------------------
    input_files = []
    for i, name in enumerate(('instruction.md', 'task.toml')):
        path = os.path.join(task_dir, name)
        if os.path.isfile(path):
            input_files.append(_file_entry(f'input_{i}', path, f'task/{name}', role='primary'))

    # --- output.json -----------------------------------------------------
    output = {
        'session_id': uuid.uuid4().hex,
        'timestamp': started_at,
        'trajectory': {
            'meta_info': dict(meta_info, stop_reasons=list(stop_reasons or [])),
            'input_modalities': ['text'],
            'output_modalities': ['text', 'patch'],
        },
        'input_files': input_files,
        'output_artifacts': artifacts,
        'messages': build_messages(prompt_text, reply_text, started_at, finished_at),
        'usage': merge_usage(usages),
    }
    with open(os.path.join(run_dir, 'output.json'), 'w') as f:
        json.dump(output, f, indent=2)

    # --- report.json -----------------------------------------------------
    reward = reward or {}
    report = {
        'model': pretty_model(model_id, agent),
        'run_index': run_index,
        'agent': agent,
        'reward': reward_value(reward, reward_kind),
    }
    if reward_kind == 'diff_similarity':
        components = reward.get('components', {})
        report['diff_similarity'] = {
            'components': components,
            'weights': reward.get('weights', {}),
            'judge_model': reward.get('judge_model'),
            'judge_status': reward.get('judge_status'),
        }
        report['patch'] = {
            'applied': meta_info.get('patch_applied', False),
            'error': meta_info.get('agent_error'),
        }
        report['notes'] = reward.get('notes', [])
    else:
        # The perf analogue of the delivery bundle's `pytest` block: the gate is
        # the module suite, the score is the timing result.
        report['exec_time'] = {
            'compile_success': reward.get('compile_success', False),
            'test_success': reward.get('test_success', False),
            'exec_time_improvement': reward.get('exec_time_improvement'),
            'p_value': reward.get('p_value'),
            'significant': reward.get('significant', False),
            'baseline_exec_times': reward.get('baseline_exec_times', []),
            'candidate_exec_times': reward.get('candidate_exec_times', []),
        }
        report['tests'] = {
            'f2p_expected': reward.get('f2p_expected', []),
            'f2p_satisfied': reward.get('f2p_satisfied', []),
            'f2p_missing': reward.get('f2p_missing', []),
            'regressed_test_classes': reward.get('regressed_test_classes', []),
        }
        report['notes'] = reward.get('notes', [])
        report['maven_logs'] = [os.path.basename(p) for p in maven_logs]

    with open(os.path.join(run_dir, 'report.json'), 'w') as f:
        json.dump(report, f, indent=2)

    return output, report


def rebuild_pass_summary(model_root, model_label, reward_kind='exec_time'):
    """Recompute pass_summary.json across every run_N present."""
    runs = []
    for name in sorted(os.listdir(model_root)):
        report_path = os.path.join(model_root, name, 'report.json')
        if not name.startswith('run_') or not os.path.isfile(report_path):
            continue
        report = json.load(open(report_path))
        entry = {'run_index': report['run_index'], 'reward': report.get('reward', 0.0)}
        if reward_kind == 'diff_similarity':
            block = report.get('diff_similarity', {})
            entry['components'] = block.get('components', {})
            entry['judge_status'] = block.get('judge_status')
            entry['patch_applied'] = report.get('patch', {}).get('applied', False)
        else:
            entry['exec_time_improvement'] = report.get('exec_time', {}).get('exec_time_improvement')
            entry['p_value'] = report.get('exec_time', {}).get('p_value')
            entry['test_success'] = report.get('exec_time', {}).get('test_success', False)
        runs.append(entry)

    runs.sort(key=lambda r: r['run_index'])
    rewards = [r['reward'] for r in runs]
    summary = {
        'model': model_label,
        'runs': len(runs),
        'average_reward': round(sum(rewards) / len(rewards), 4) if rewards else 0.0,
        'solve_rate_percentage': round(100 * sum(1 for r in rewards if r >= 1.0) / len(rewards), 2)
                                 if rewards else 0.0,
    }
    if reward_kind == 'diff_similarity':
        # Mean of each component across runs, skipping the ones a run left null
        # (llm_judge when no key was available).
        names = [n for r in runs for n in (r.get('components') or {})]
        summary['average_components'] = {
            name: round(sum(vals) / len(vals), 4)
            for name in dict.fromkeys(names)
            if (vals := [r['components'][name] for r in runs
                         if isinstance((r.get('components') or {}).get(name), (int, float))])
        }
    else:
        improvements = [r['exec_time_improvement'] for r in runs
                        if r.get('exec_time_improvement') is not None]
        summary['average_exec_time_improvement'] = (
            round(sum(improvements) / len(improvements), 4) if improvements else None)
    summary['per_run'] = runs

    with open(os.path.join(model_root, 'pass_summary.json'), 'w') as f:
        json.dump(summary, f, indent=2)
    return summary
