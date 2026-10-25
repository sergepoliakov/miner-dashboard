#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Extra release checks; run gitleaks as well. Prints locations, never values."""
import re, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PATTERNS={
 'private home path':re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
 'private LAN address':re.compile(r'\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
 'real GPU identity':re.compile(r'GPU-(?!00000000-0000-0000-0000-\d{12})[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'),
 'private key':re.compile(r'-{5}BEGIN (?:(?:OPENSSH|RSA|EC|DSA) )?PRIVATE KEY-{5}'),
 'GitHub token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
}
DENY_SUFFIXES={'.sqlite','.sqlite3','.db','.pem','.key','.p12','.log','.jsonl'}
def scan(path):
    failures=[]
    for p in sorted(path.rglob('*')):
        rel=p.relative_to(path)
        if any(part in ('.git','__pycache__','.venv') for part in rel.parts) or not p.is_file():continue
        if p.is_symlink():failures.append((str(rel),0,'symlink'));continue
        if p.suffix in DENY_SUFFIXES:failures.append((str(rel),0,'private runtime file'))
        if p.suffix in {'.png','.jpg','.jpeg','.webp'}:continue # Pixels require manual DEMO review.
        try:s=p.read_text()
        except UnicodeDecodeError:failures.append((str(rel),0,'unexpected binary'));continue
        for kind,pattern in PATTERNS.items():
            for m in pattern.finditer(s):failures.append((str(rel),s.count('\n',0,m.start())+1,kind))
    return failures
if __name__=='__main__':
    failures=scan(ROOT)
    for name,line,kind in failures:print(f'{name}:{line}: {kind}')
    print(f'Privacy patterns: {len(failures)} finding(s). Screenshots and history require separate review.')
    raise SystemExit(bool(failures))
