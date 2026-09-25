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
    import hashlib
    import json as _json

    pkg_root = root / "swarm"
    sources: List[tuple] = [("__main__.py", _MAIN.encode("utf-8"))]
    init_file = pkg_root / "__init__.py"
    if init_file.exists():
        sources.append(("swarm/__init__.py", init_file.read_bytes()))
    for pkg in _BUNDLE_PACKAGES:
        pkg_dir = pkg_root / pkg
        if not pkg_dir.is_dir():
            continue
        for py_file in sorted(pkg_dir.rglob("*.py")):
            if "__pycache__" in py_file.parts:
                continue
            arcname = "swarm/" + "/".join(py_file.relative_to(pkg_root).parts)
            sources.append((arcname, py_file.read_bytes()))
    # The CODE identity, independent of zip timestamps and of any per-invite
    # config: two bundles with the same code hash run the same agent. The
    # self-updater compares this, never the file hash (a per-invite bundle
    # always differs from the generic one byte-wise).
    digest = hashlib.sha256()
    for arcname, data in sources:
        digest.update(arcname.encode("utf-8") + b"\0" + data + b"\0")
    code_hash = digest.hexdigest()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcname, data in sources:
            zf.writestr(arcname, data)
        zf.writestr("swarm_build.json", _json.dumps({"code_hash": code_hash}, sort_keys=True))
        if config:
            zf.writestr("swarm_config.json", _json.dumps(config, sort_keys=True))
    payload = buf.getvalue()
    if output:
        Path(output).write_bytes(payload)
    return payload


def bundle_code_hash(payload: bytes) -> Optional[str]:
    """The code hash baked into a bundle, or None for pre-hash bundles."""
    import json as _json

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            return str(_json.loads(zf.read("swarm_build.json").decode("utf-8"))["code_hash"])
    except Exception:
        return None


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
