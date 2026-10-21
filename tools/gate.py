#!/usr/bin/env python3
"""Publication gate: refuses a repository that carries disallowed content or identities.

Scans every commit reachable from the given revisions (default: HEAD), the working tree
when asked, and fails when any of these is found:

  * a forbidden term (case-insensitive regular expressions) in a file path, in file
    content, in a commit message, or in an author/committer name or address;
  * a commit whose author or committer is not on the allowed list;
  * a Co-authored-by trailer: this project has one author, and trailers are how a second
    name gets attached to a commit.

The terms and the allowed identities are deliberately not kept in the repository. They
come from the environment (GATE_TERMS, GATE_AUTHORS: one entry per line) or from files
(--terms-file, --authors-file). With no terms the gate refuses to pass, so a missing
secret in CI cannot silently turn it into a no-op.

    python3 tools/gate.py --terms-file ../gate-terms.txt --authors-file ../gate-authors.txt
    GATE_TERMS=... GATE_AUTHORS=... python3 tools/gate.py --all --worktree

Exit status 0 means clean, 1 means findings, 2 means the gate could not run.
"""
import argparse
import os
import re
import subprocess
import sys


def git(*args, repo='.', binary=False):
    out = subprocess.run(['git', '-C', repo, *args], capture_output=True, check=True).stdout
    return out if binary else out.decode('utf-8', 'replace')


def load_list(value, path):
    text = ''
    if path:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    elif value:
        text = value
    return [l.strip() for l in text.replace(';', '\n').splitlines() if l.strip() and not l.strip().startswith('#')]


def compile_terms(terms):
    return [re.compile(t.encode(), re.IGNORECASE) for t in terms]


def find_terms(patterns, data):
    """All (pattern, line number, line) hits in a bytes blob."""
    hits = []
    for pat in patterns:
        for m in pat.finditer(data):
            line_no = data.count(b'\n', 0, m.start()) + 1
            start = data.rfind(b'\n', 0, m.start()) + 1
            end = data.find(b'\n', m.end())
            line = data[start:end if end >= 0 else None].decode('utf-8', 'replace').strip()
            hits.append((pat.pattern.decode(), line_no, line[:160]))
    return hits


def scan_commits(repo, revs, patterns, authors, findings):
    fmt = '%H%x00%an <%ae>%x00%cn <%ce>%x00%B%x00%x01'
    raw = git('log', '--format=' + fmt, *revs, repo=repo, binary=True)
    commits = [c for c in raw.split(b'\x01') if c.strip()]
    for c in commits:
        parts = c.lstrip(b'\n').split(b'\x00')
        sha, author, committer, message = parts[0].decode(), parts[1].decode(), parts[2].decode(), parts[3]
        short = sha[:10]
        if authors:
            for role, ident in (('author', author), ('committer', committer)):
                if ident not in authors:
                    findings.append(f'commit {short}: {role} {ident!r} is not an allowed identity')
        for role, ident in (('author', author), ('committer', committer)):
            for pat, _, _ in find_terms(patterns, ident.encode()):
                findings.append(f'commit {short}: {role} {ident!r} matches {pat!r}')
        for pat, line_no, line in find_terms(patterns, message):
            findings.append(f'commit {short}: message line {line_no} matches {pat!r}: {line}')
        if re.search(rb'^\s*co-authored-by\s*:', message, re.IGNORECASE | re.MULTILINE):
            findings.append(f'commit {short}: carries a Co-authored-by trailer')
    return len(commits)


def scan_history_blobs(repo, revs, patterns, findings):
    """Every path and blob reachable from revs, each blob scanned once."""
    raw = git('rev-list', '--objects', *revs, repo=repo)
    seen_paths, blobs = set(), {}
    for line in raw.splitlines():
        sha, _, path = line.partition(' ')
        if not path:
            continue
        if path not in seen_paths:
            seen_paths.add(path)
            for pat, _, _ in find_terms(patterns, path.encode()):
                findings.append(f'path {path!r} (history) matches {pat!r}')
        blobs.setdefault(sha, path)
    if not blobs:
        return 0
    proc = subprocess.run(['git', '-C', repo, 'cat-file', '--batch'], input='\n'.join(blobs).encode() + b'\n', capture_output=True, check=True)
    data = proc.stdout
    pos, scanned = 0, 0
    while pos < len(data):
        nl = data.index(b'\n', pos)
        header = data[pos:nl].decode()
        pos = nl + 1
        fields = header.split()
        if len(fields) != 3:          # "<sha> missing"
            continue
        sha, kind, size = fields[0], fields[1], int(fields[2])
        body = data[pos:pos + size]
        pos += size + 1
        if kind != 'blob':
            continue
        scanned += 1
        for pat, line_no, line in find_terms(patterns, body):
            findings.append(f'blob {sha[:10]} {blobs[sha]!r} (history) line {line_no} matches {pat!r}: {line}')
    return scanned


def scan_worktree(repo, patterns, findings):
    files = git('ls-files', '-co', '--exclude-standard', '-z', repo=repo).split('\0')
    n = 0
    for path in files:
        if not path:
            continue
        full = os.path.join(repo, path)
        if not os.path.isfile(full):
            continue
        n += 1
        for pat, _, _ in find_terms(patterns, path.encode()):
            findings.append(f'path {path!r} (worktree) matches {pat!r}')
        with open(full, 'rb') as f:
            body = f.read()
        for pat, line_no, line in find_terms(patterns, body):
            findings.append(f'file {path!r} (worktree) line {line_no} matches {pat!r}: {line}')
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', default='.')
    ap.add_argument('--terms-file', help='regular expressions, one per line (else $GATE_TERMS)')
    ap.add_argument('--authors-file', help='allowed "Name <email>" identities, one per line (else $GATE_AUTHORS)')
    ap.add_argument('--all', action='store_true', help='scan every ref, not only HEAD')
    ap.add_argument('--worktree', action='store_true', help='also scan the working tree (tracked and untracked, not ignored)')
    ap.add_argument('revs', nargs='*', help='revisions to scan (default HEAD)')
    a = ap.parse_args()
    try:
        terms = load_list(os.environ.get('GATE_TERMS'), a.terms_file)
        authors = load_list(os.environ.get('GATE_AUTHORS'), a.authors_file)
    except OSError as e:
        print(f'gate: cannot read list: {e}', file=sys.stderr)
        return 2
    if not terms:
        print('gate: no forbidden terms given (GATE_TERMS or --terms-file); refusing to pass an empty gate', file=sys.stderr)
        return 2
    patterns = compile_terms(terms)
    revs = ['--all'] if a.all else (a.revs or ['HEAD'])
    findings = []
    try:
        commits = scan_commits(a.repo, revs, patterns, authors, findings)
        blobs = scan_history_blobs(a.repo, revs, patterns, findings)
        files = scan_worktree(a.repo, patterns, findings) if a.worktree else 0
    except subprocess.CalledProcessError as e:
        print(f'gate: git failed: {e.stderr.decode(errors="replace").strip()}', file=sys.stderr)
        return 2
    for f in findings:
        print('FAIL ' + f)
    summary = f'{commits} commits, {blobs} blobs, {files} worktree files, {len(terms)} terms, {len(authors)} allowed identities'
    if findings:
        print(f'gate: {len(findings)} finding(s) in {summary}')
        return 1
    print(f'gate: clean ({summary})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
