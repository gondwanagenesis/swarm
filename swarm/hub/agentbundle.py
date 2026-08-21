"""Build the single-file agent (swarm-agent.pyz): a stdlib zipapp bundling
core+probe+bench+transport+agent. One file, bare Python 3.9+, zero deps:

    python swarm-agent.pyz --hub http://hub:8777

This is the M4.5-lite enrollment path: the hub serves the file, the node runs
it. Signing and fleet tokens land with full M4.5; the bundle is content in
plain HTTP for now — deploy it over your mesh (Tailscale) or pin it by hash.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import List, Optional, Union

_BUNDLE_PACKAGES = ["core", "probe", "bench", "transport", "agent"]

_MAIN = '''"""swarm-agent pyz entrypoint."""
import sys

from swarm.agent.daemon import main

if __name__ == "__main__":
    sys.exit(main())
'''


def build_agent_pyz(output: Union[str, Path, None] = None, source_root: Optional[Path] = None, config: Optional[dict] = None) -> bytes:
    """Build the single-file agent. Returns the pyz bytes; also writes to
    `output` if a path is given. Only stdlib packages are bundled."""
    if source_root:
        root = Path(source_root)
    else:
        root = next(
            (p for p in Path(__file__).resolve().parents if (p / "swarm" / "core").is_dir()),
            Path(__file__).resolve().parents[2],
        )
    buf = io.BytesIO()
    files: List[str] = []
    files.append("__main__.py")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("__main__.py", _MAIN)
        pkg_root = root / "swarm"
        init_file = pkg_root / "__init__.py"
        if init_file.exists():
            zf.write(init_file, "swarm/__init__.py")
        for pkg in _BUNDLE_PACKAGES:
            pkg_dir = pkg_root / pkg
            if not pkg_dir.is_dir():
                continue
            for py_file in sorted(pkg_dir.rglob("*.py")):
                if "__pycache__" in py_file.parts:
                    continue
                arcname = "swarm/" + "/".join(py_file.relative_to(pkg_root).parts)
                zf.write(py_file, arcname)
                files.append(arcname)
        if config:
            import json as _json

            zf.writestr("swarm_config.json", _json.dumps(config, sort_keys=True))
    payload = buf.getvalue()
    if output:
        Path(output).write_bytes(payload)
    return payload


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build the single-file swarm agent")
    parser.add_argument("--out", default="swarm-agent.pyz")
    args = parser.parse_args()
    payload = build_agent_pyz(output=args.out)
    print(f"built {args.out} ({len(payload)} bytes) — run: python {args.out} --hub http://host:8777")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
