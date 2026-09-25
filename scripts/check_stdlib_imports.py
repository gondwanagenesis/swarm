#!/usr/bin/env python3
"""CI gate: agent-side packages must import stdlib only.

Walks every .py under the given roots with the AST (no imports executed),
collects top-level import roots, and fails if any root is not in
sys.stdlib_module_names plus the project's own 'swarm' package.

Usage: python scripts/check_stdlib_imports.py swarm/core swarm/probe ...
Exits non-zero with a report on violation.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import List, Set

ALLOWED_EXTRA = {"swarm", "__future__"}

# Baseline for interpreters without sys.stdlib_module_names (<3.10); unioned
# everywhere so behaviour is identical across the CI matrix.
BASELINE = {
    "__future__",
    "os",
    "sys",
    "json",
    "re",
    "io",
    "time",
    "math",
    "socket",
    "socketserver",
    "struct",
    "subprocess",
    "threading",
    "http",
    "urllib",
    "pathlib",
    "typing",
    "dataclasses",
    "enum",
    "hashlib",
    "uuid",
    "sqlite3",
    "statistics",
    "platform",
    "shutil",
    "importlib",
    "ctypes",
    "glob",
    "argparse",
    "contextlib",
    "collections",
    "functools",
    "itertools",
    "abc",
    "copy",
    "datetime",
    "errno",
    "inspect",
    "logging",
    "numbers",
    "operator",
    "queue",
    "random",
    "secrets",
    "select",
    "selectors",
    "signal",
    "ssl",
    "string",
    "tempfile",
    "textwrap",
    "traceback",
    "types",
    "unittest",
    "warnings",
    "weakref",
    "xml",
    "zipfile",
    "zlib",
    # added with the usable-fabric pass (all stdlib; absent from the list
    # the py3.9 legs fall back to)
    "atexit",
    "base64",
    "binascii",
    "bisect",
    "codecs",
    "fcntl",
    "gzip",
    "heapq",
    "hmac",
    "html",
    "ipaddress",
    "msvcrt",
    "pickletools",
    "shlex",
    "stat",
    "tarfile",
    "urllib",
    "winreg",
}


def imported_roots(path: Path) -> Set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def main(argv: List[str]) -> int:
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    allowed = stdlib | BASELINE | ALLOWED_EXTRA
    failures = []
    for root in argv[1:]:
        root_path = Path(root)
        if not root_path.exists():
            print(f"missing path: {root}")
            return 2
        for py in sorted(root_path.rglob("*.py")):
            for mod in sorted(imported_roots(py)):
                if mod not in allowed:
                    failures.append(f"{py}: non-stdlib import '{mod}'")
    if failures:
        print("STDLIB IMPORT VIOLATIONS (agent-side code must be stdlib-only):")
        for failure in failures:
            print("  " + failure)
        return 1
    print("stdlib import check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
