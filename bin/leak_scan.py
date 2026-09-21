#!/usr/bin/env python3
"""Scan for values that *look* like secrets or organisation identifiers, without naming any.

Two rules, both learned the hard way in sibling projects:

1. A gate written as a literal list of the things it hunts IS a disclosure. Everything below is a
   shape. The private literal denylist lives outside the repo and is passed with --denylist.
2. A gate that can fail open is worse than none. This is Python `re`, not `grep -P`.

Modes:
    leak_scan.py --git [root]     scan git-tracked files (or the working tree if not a git repo)
    leak_scan.py --tree <dir>     scan every file under a directory
    --denylist <file>             one regex per line, kept OUT of the repo; matches are never echoed
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# (label, pattern). Shapes only.
SHAPES: list[tuple[str, str]] = [
    ("cloud account ID", r"(?<![\d-])[0-9]{12}(?![\d-])"),
    ("AWS access key ID", r"AKIA[0-9A-Z]{16}"),
    ("Slack token", r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ("Slack channel/user ID", r"\bC0[0-9A-Z]{8,}\b"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("local home path", r"/Users/[a-z][a-z0-9._-]*/"),
    # A real workspace host. `example.cloud.databricks.com` and `<...>` placeholders are fine.
    ("workspace host", r"\b(?!example\.)(?!your-workspace)[a-z0-9][a-z0-9-]{3,}\.(?:cloud\.databricks\.com|azuredatabricks\.net|gcp\.databricks\.com)\b"),
    ("Snowflake account locator", r"\b[a-z0-9-]+\.(?:us|eu|ap|ca)-(?:east|west|central|north|south)-[0-9]\.snowflakecomputing\.com\b"),
    # RFC 2606 reserved domains are legitimate in docs and fixtures.
    (
        "email address",
        r"[a-zA-Z0-9._%+-]+@(?!example\.(?:com|org|net)\b)(?!test\b)(?!invalid\b)"
        r"(?!localhost\b)(?!noreply\.)(?!users\.noreply\.)[a-zA-Z0-9-]+\.[a-zA-Z]{2,}",
    ),
    # Ticket keys whose prefix is not a documented placeholder.
    ("non-placeholder ticket key", r"\b(?!JOB-|DAG-|ABC-|ENG-|PROJ-|TICKET-|ISSUE-|ISO-|UTF-|RFC-|SHA-|CVE-|AES-|RSA-|MD-|TLS-|HTTP-|SQL-)[A-Z]{2,6}-[0-9]{1,5}\b"),
    # GitHub org/repo slugs are allowed only for this project and RFC-style placeholders.
    ("github repo slug", r"(?<![\w/-])(?!kyle-chalmers/|your-org/|YOUR-ORG/|example-org/|owner/|OWNER/|anthropics/|actions/|astral-sh/|zizmorcore/)[A-Za-z0-9-]+/[A-Za-z0-9._-]+(?=\.git\b|#[0-9]| repo\b)"),
]

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules", ".healwright-backups"}
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".whl", ".gz", ".zip", ".db"}


def _files_from_git(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=root, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _files_from_tree(root)
    return [root / p for p in out.split("\0") if p]


def _files_from_tree(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and not (_SKIP_DIRS & set(p.relative_to(root).parts))]


def _read(path: Path) -> str | None:
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def scan(files: list[Path], root: Path, denylist: list[str]) -> int:
    compiled = [(label, re.compile(pat)) for label, pat in SHAPES]
    hits = 0
    for path in files:
        text = _read(path)
        if text is None or not path.exists():
            continue
        rel = path.relative_to(root) if path.is_relative_to(root) else path
        for lineno, line in enumerate(text.splitlines(), 1):
            if "leak-scan-ok" in line:
                continue
            for label, rx in compiled:
                if rx.search(line):
                    print(f"FAIL [{label}] {rel}:{lineno}: {line.strip()[:120]}")
                    hits += 1
            for rx in denylist:
                if re.search(rx, line, re.IGNORECASE):
                    print(f"FAIL [private denylist] {rel}:{lineno} (term and line redacted)")
                    hits += 1
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--git", metavar="ROOT", nargs="?", const=".")
    g.add_argument("--tree", metavar="DIR")
    ap.add_argument("--denylist")
    args = ap.parse_args()

    denylist: list[str] = []
    if args.denylist:
        dl = Path(args.denylist)
        if not dl.is_file():
            print(f"FAIL: denylist is not a readable file: {dl}")
            return 1
        denylist = [ln.strip() for ln in dl.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]

    root = Path(args.git if args.git is not None else args.tree).resolve()
    files = _files_from_git(root) if args.git is not None else _files_from_tree(root)
    hits = scan(files, root, denylist)
    if hits:
        print(f"\nFAIL: {hits} sensitive-shape hit(s)")
        return 1
    print(f"leak scan: {len(files)} files clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
