#!/usr/bin/env python3
"""Reward verifier for JETO-Bench Harbor tasks. Runs inside the task container.

Stdlib only — the task images carry a JDK and Maven, not numpy/scipy — so the
statistics from src/gh/commit_analysis/utils/mvn_log_analyzer.py are reproduced
here exactly:

  * per-test-class times come from the surefire "Time elapsed: X s -- in <class>"
    lines; a run's total is their sum,
  * the first execution of each version is discarded as warm-up,
  * significance is a one-sided exact binomial sign test over the paired runs of
    H1: patched < (1 - min_exec_time_improvement) x original,
  * improvement = (sum(original) - sum(patched)) / sum(original).

Reads tests/config.json, consumes the logs test.sh produced, writes reward.json
(full breakdown) and reward.txt (scalar).
"""

import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

_TIME_RE = re.compile(
    r'\[INFO\] Tests run:.*?Time elapsed:\s+([\d.]+)\s+(ms|s|min|h)\s+-+\s+in\s+(.+)'
)
_UNIT_SECONDS = {'ms': 0.001, 's': 1.0, 'min': 60.0, 'h': 3600.0}

_COMPILE_ERROR_PATTERNS = (
    r"(?im)^\[ERROR\]\s+Failed to execute goal .*maven-compiler-plugin.*:(compile|testCompile)\b",
    r"(?im)^\[ERROR\]\s+COMPILATION ERROR\s*:?",
    r"(?im)^\[ERROR\]\s+Compilation failure\b",
    r"(?im)^\[ERROR\]\s+Fatal error compiling\b",
    r"(?im)^\[ERROR\]\s+No compiler is provided in this environment\b",
)


def _read(path):
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        return f.read()


def has_compilation_error(log_path):
    content = _read(log_path)
    if any(re.search(p, content) for p in _COMPILE_ERROR_PATTERNS):
        return True
    goal_failed = re.search(
        r"(?is)Failed to execute goal .*maven-compiler-plugin.*:(compile|testCompile)\b", content
    )
    return bool(goal_failed and re.search(r"(?i)\bCompilation failure\b", content))


def is_exec_successful(log_path):
    content = _read(log_path)
    return not (
        "BUILD FAILURE" in content
        or "BUILD ERROR" in content
        or "BUILD SUCCESS" not in content
    )


def per_test_times(log_path):
    times = {}
    for m in _TIME_RE.finditer(_read(log_path)):
        times[m.group(3).strip()] = float(m.group(1)) * _UNIT_SECONDS.get(m.group(2), 1.0)
    return times


def total_time(log_path):
    return sum(per_test_times(log_path).values())


def binom_sf(wins, n):
    """P(X >= wins) for X ~ Binomial(n, 0.5) — scipy's binomtest(alternative='greater')."""
    return sum(math.comb(n, k) for k in range(wins, n + 1)) / (2.0 ** n)


def improvement_p_value(original, patched, min_improvement):
    """One-sided sign test of H1: patched < (1 - min_improvement) * original."""
    if len(original) != len(patched):
        raise ValueError("paired samples must have the same length")
    c = 1.0 - min_improvement
    diffs = [p - c * o for o, p in zip(original, patched)]
    wins = sum(1 for d in diffs if d <= 0)
    losses = sum(1 for d in diffs if d > 0)
    n = wins + losses
    if n == 0:
        raise ValueError("no comparable pairs after applying the margin")
    return binom_sf(wins, n)


def significant_class_improvements(orig_logs, patched_logs, expected_runs, min_p, min_improvement):
    """Per-class verdicts over the post-warm-up runs (mirrors MvnwExecResults)."""
    orig_by_class, patched_by_class = {}, {}
    for o_log, p_log in zip(orig_logs, patched_logs):
        for cls, t in per_test_times(o_log).items():
            orig_by_class.setdefault(cls, []).append(t)
        for cls, t in per_test_times(p_log).items():
            patched_by_class.setdefault(cls, []).append(t)

    out = {'patched_outperforms_original': [], 'original_outperforms_patched': []}
    expected = expected_runs - 1
    for cls, o_times in orig_by_class.items():
        p_times = patched_by_class.get(cls, [])
        if len(o_times) != expected or len(p_times) != expected:
            continue
        # Sub-10ms classes are noise-dominated; the harness skips them.
        if any(t < 0.01 for t in o_times + p_times):
            continue
        if improvement_p_value(o_times, p_times, min_improvement) < min_p:
            out['patched_outperforms_original'].append(cls)
        elif improvement_p_value(p_times, o_times, min_improvement) < min_p:
            out['original_outperforms_patched'].append(cls)
    return out


def main():
    cfg = json.load(open(os.path.join(HERE, 'config.json')))
    log_dir = cfg['log_dir']
    runs = int(cfg['exec_times'])
    min_p = float(cfg['min_p_value'])
    min_improvement = float(cfg['min_exec_time_improvement'])
    out_dir = os.environ.get('HARBOR_OUTPUT_DIR', cfg.get('output_dir', log_dir))
    os.makedirs(out_dir, exist_ok=True)

    baseline_logs = [os.path.join(log_dir, f"baseline_{i}.log") for i in range(runs)]
    candidate_logs = [os.path.join(log_dir, f"candidate_{i}.log") for i in range(runs)]

    result = {
        'reward': 0.0,
        'patch_applicable': True,
        'compile_success': False,
        'test_success': False,
        'exec_time_improvement': None,
        'p_value': None,
        'significant': False,
        'f2p_expected': cfg.get('f2p', []),
        'f2p_satisfied': [],
        'f2p_missing': [],
        'regressed_test_classes': [],
        'baseline_exec_times': [],
        'candidate_exec_times': [],
        'notes': [],
    }

    missing = [p for p in baseline_logs + candidate_logs if not os.path.isfile(p)]
    if missing:
        result['notes'].append(f"missing {len(missing)} log(s); test.sh did not complete")
        _write(out_dir, result)
        return 1

    if any(has_compilation_error(p) for p in candidate_logs):
        result['notes'].append("candidate failed to compile")
        _write(out_dir, result)
        return 0
    result['compile_success'] = True

    if not all(is_exec_successful(p) for p in baseline_logs + candidate_logs):
        result['notes'].append("test suite did not pass on every run (P2P gate)")
        _write(out_dir, result)
        return 0
    result['test_success'] = True

    baseline_totals = [total_time(p) for p in baseline_logs]
    candidate_totals = [total_time(p) for p in candidate_logs]
    result['baseline_exec_times'] = baseline_totals
    result['candidate_exec_times'] = candidate_totals

    # Discard the first run of each version (JIT / page-cache warm-up).
    valid_baseline, valid_candidate = baseline_totals[1:], candidate_totals[1:]
    if not valid_baseline or sum(valid_baseline) == 0:
        result['notes'].append("no usable timing data after dropping the warm-up run")
        _write(out_dir, result)
        return 0

    result['exec_time_improvement'] = (
        sum(valid_baseline) - sum(valid_candidate)
    ) / sum(valid_baseline)
    result['p_value'] = improvement_p_value(valid_baseline, valid_candidate, min_improvement)
    result['significant'] = (
        result['p_value'] < min_p and result['exec_time_improvement'] >= min_improvement
    )

    classes = significant_class_improvements(
        baseline_logs[1:], candidate_logs[1:], runs, min_p, min_improvement
    )
    improved = set(classes['patched_outperforms_original'])
    result['regressed_test_classes'] = classes['original_outperforms_patched']
    expected = cfg.get('f2p', [])
    result['f2p_satisfied'] = sorted(improved & set(expected))
    result['f2p_missing'] = sorted(set(expected) - improved)

    gate = result['compile_success'] and result['test_success'] and not result['f2p_missing']
    if gate and result['significant']:
        result['reward'] = 1.0
    elif gate and cfg.get('shaped', False):
        # Opt-in partial credit for a real but not-yet-significant speedup.
        ratio = max(0.0, min(result['exec_time_improvement'] / min_improvement, 1.0))
        result['reward'] = round(0.5 * ratio, 4)

    _write(out_dir, result)
    return 0


def _write(out_dir, result):
    with open(os.path.join(out_dir, 'reward.json'), 'w') as f:
        json.dump(result, f, indent=2)
    with open(os.path.join(out_dir, 'reward.txt'), 'w') as f:
        f.write(f"{result['reward']}\n")
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    sys.exit(main())
