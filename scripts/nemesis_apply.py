"""Apply a unified diff by matching context, not by trusting line numbers.

    python3 nemesis_apply.py /tmp/fix.patch [repo_root]

`git apply` needs a hunk's @@ header to point at (near) the right place. LLMs
reliably get the *content* of a hunk right and the *line numbers* wrong — they
are counting lines in a file they only read once. GNU `patch -F3` would absorb
that, but the pr_diff task images are `python:3.12-slim`, which ships git and
no patch(1). This is the stand-in: locate each hunk by searching for its
context/removed lines and splice in the replacement wherever they actually are.

Deliberately dependency free — it is copied into the task container and run by
the interpreter that is already there. Exits 0 when every hunk applied.
"""

import os
import re
import sys

HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


class Hunk:
    def __init__(self, start):
        self.start = start      # 1-based line the diff claims the hunk begins at
        self.before = []        # context + removed lines (what must be on disk)
        self.after = []         # context + added lines (what replaces it)


def parse(text):
    """Return {path: [Hunk, ...]} for a unified diff."""
    files, path, hunk = {}, None, None
    for line in text.splitlines():
        if line.startswith('--- '):
            hunk = None
            continue
        if line.startswith('+++ '):
            raw = line[4:].split('\t')[0].strip()
            path = None if raw == '/dev/null' else re.sub(r'^[ab]/', '', raw)
            if path:
                files.setdefault(path, [])
            hunk = None
            continue
        if line.startswith('diff --git'):
            hunk = None
            continue
        m = HUNK_RE.match(line)
        if m and path:
            hunk = Hunk(int(m.group(1)))
            files[path].append(hunk)
            continue
        if hunk is None:
            continue
        if line.startswith('\\'):        # "\ No newline at end of file"
            continue
        tag, body = (line[0], line[1:]) if line else (' ', '')
        if tag == ' ':
            hunk.before.append(body)
            hunk.after.append(body)
        elif tag == '-':
            hunk.before.append(body)
        elif tag == '+':
            hunk.after.append(body)
    return {p: h for p, h in files.items() if h}


def find(lines, block, hint):
    """Index where `block` occurs in `lines`, nearest to `hint`. -1 if absent."""
    if not block:
        return max(0, min(hint, len(lines)))
    span = len(block)
    candidates = [i for i in range(len(lines) - span + 1)
                  if lines[i:i + span] == block]
    if not candidates:
        # Second pass: ignore indentation differences, which is the other thing
        # a model gets wrong when it retypes a hunk from memory.
        stripped = [b.strip() for b in block]
        candidates = [i for i in range(len(lines) - span + 1)
                      if [l.strip() for l in lines[i:i + span]] == stripped]
    if not candidates:
        return -1
    return min(candidates, key=lambda i: abs(i - hint))


def apply_file(path, hunks):
    """Apply every hunk for one file. Returns (applied, failed, note)."""
    if os.path.exists(path):
        with open(path, encoding='utf-8', errors='surrogateescape') as f:
            text = f.read()
        lines = text.split('\n')
        trailing = lines and lines[-1] == ''
        if trailing:
            lines.pop()
    else:
        lines, trailing = [], True
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)

    applied = failed = 0
    note = None
    # Apply in reverse file order so earlier hunks' offsets stay valid.
    for hunk in sorted(hunks, key=lambda h: h.start, reverse=True):
        at = find(lines, hunk.before, hunk.start - 1)
        if at < 0:
            failed += 1
            if note is None:
                head = next((b for b in hunk.before if b.strip()), '')
                note = f"{path}: no match for hunk @@ -{hunk.start} ({head.strip()[:60]!r})"
            continue
        lines[at:at + len(hunk.before)] = hunk.after
        applied += 1

    if applied:
        with open(path, 'w', encoding='utf-8', errors='surrogateescape') as f:
            f.write('\n'.join(lines) + ('\n' if trailing else ''))
    return applied, failed, note


def main():
    if len(sys.argv) < 2:
        print("usage: nemesis_apply.py <patch> [repo_root]", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding='utf-8', errors='surrogateescape') as f:
        files = parse(f.read())
    if len(sys.argv) > 2:
        os.chdir(sys.argv[2])
    if not files:
        print("no file hunks found in patch", file=sys.stderr)
        return 1

    total_applied = total_failed = 0
    notes = []
    for path, hunks in files.items():
        applied, failed, note = apply_file(path, hunks)
        total_applied += applied
        total_failed += failed
        if note:
            notes.append(note)
        print(f"{path}: {applied} hunk(s) applied, {failed} failed")

    if total_failed:
        for n in notes:
            print(n, file=sys.stderr)
    # Partial success still counts: a patch that lands 3 of 4 hunks is a better
    # prediction than no patch at all, and the verifier scores what is on disk.
    return 0 if total_applied else 1


if __name__ == '__main__':
    raise SystemExit(main())
