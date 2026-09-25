"""Inference-runtime discovery: what can this node already RUN, today?

Law 3 says enumerate before you generate, and the cheapest compute on an old
machine is the runtime somebody already installed there. This module asks —
never assumes — three questions, each answered by the runtime itself:

1. **Ollama.** Is a server answering on this box, and which models does it
   hold? Each model is classed ``chat`` or ``embed`` from the capabilities
   Ollama reports (``/api/tags``, falling back to ``/api/show``). A model
   with no reported capability is left out, never guessed.
2. **llama.cpp.** Are ``llama-server`` and an RPC worker (``rpc-server`` /
   ``ggml-rpc-server``) on PATH or in ``SWARM_LLAMA_DIR``? If so, what
   devices does llama.cpp itself see, with how much *free* memory? That
   comes from ``llama-server --list-devices`` — the runtime's own
   measurement, the number the pooled-model planner is allowed to use.
3. **Model files.** Which GGUF files sit in ``~/.swarm/models`` (or
   ``SWARM_MODELS_DIR``)? Size is read from disk; architecture and layer
   count from the GGUF header. Nothing about a model is taken from its
   filename except its name.

Never raises, never hangs: every subprocess is bounded, every network call
has a timeout, and a missing runtime is an empty answer, not an error.
Stdlib only (this package is scanned by the import gate).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import struct
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import Anomaly
from ._proc import run_bounded

OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"

LLAMA_SERVER_NAMES = ("llama-server",)
LLAMA_RPC_NAMES = ("rpc-server", "ggml-rpc-server", "llama-rpc-server")

# "  Vulkan0: Intel(R) Iris(R) Xe Graphics (16235 MiB, 15467 MiB free)"
_DEVICE_LINE = re.compile(
    r"^\s*(?P<id>[A-Za-z][A-Za-z0-9_]*\d+):\s*(?P<name>.+?)\s*\((?P<total>\d+)\s*MiB,\s*(?P<free>\d+)\s*MiB free\)\s*$"
)


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def ollama_url() -> str:
    return (os.environ.get("SWARM_OLLAMA_URL") or OLLAMA_DEFAULT_URL).rstrip("/")


def _get_json(url: str, timeout: float, body: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"} if data else {}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def ollama_models(timeout: float = 2.0) -> Tuple[List[Dict[str, Any]], List[Anomaly]]:
    """Models the local Ollama reports, classed by ITS capability list."""
    anomalies: List[Anomaly] = []
    base = ollama_url()
    tags = _get_json(base + "/api/tags", timeout)
    if not isinstance(tags, dict):
        return [], anomalies
    out: List[Dict[str, Any]] = []
    for m in tags.get("models") or []:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        caps = m.get("capabilities")
        if not isinstance(caps, list):
            shown = _get_json(base + "/api/show", timeout, {"model": name})
            caps = shown.get("capabilities") if isinstance(shown, dict) else None
        if not isinstance(caps, list):
            anomalies.append(
                Anomaly("runtimes.ollama", f"model {name!r} reported no capabilities; not offered")
            )
            continue
        kinds = []
        if "embedding" in caps:
            kinds.append("embed")
        if "completion" in caps:
            kinds.append("chat")
        for kind in kinds:
            out.append(
                {
                    "name": str(name),
                    "kind": kind,
                    "runtime": "ollama",
                    "size_bytes": m.get("size"),
                }
            )
    return out, anomalies


# ---------------------------------------------------------------------------
# llama.cpp
# ---------------------------------------------------------------------------


def _search_dirs() -> List[str]:
    """Where llama.cpp may live beyond PATH: SWARM_LLAMA_DIR, then the places
    the joiners install it (``~/.swarm/llama`` release trees; winget's links
    and package dirs, which a freshly started agent may not have on PATH)."""
    dirs: List[str] = []
    extra = os.environ.get("SWARM_LLAMA_DIR")
    if extra:
        dirs.extend(d for d in extra.split(os.pathsep) if d)
    home = Path(os.environ.get("USERPROFILE") or str(Path.home())) if os.name == "nt" else Path.home()
    own = home / ".swarm" / "llama"
    if own.is_dir():
        dirs.append(str(own))
        try:
            # newest build first, so a fleet-wide bump wins over an old copy
            for depth1 in sorted(own.iterdir(), key=lambda d: llama_build("build " + d.name.rsplit("b", 1)[-1]) or 0, reverse=True):
                if depth1.is_dir():
                    dirs.append(str(depth1))
                    for depth2 in ("bin", "build/bin"):
                        if (depth1 / depth2).is_dir():
                            dirs.append(str(depth1 / depth2))
        except OSError:
            pass
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local:
        winget = Path(local) / "Microsoft" / "WinGet"
        dirs.append(str(winget / "Links"))
        with contextlib.suppress(OSError):
            dirs.extend(str(d) for d in (winget / "Packages").glob("ggml.llamacpp*") if d.is_dir())
    return dirs


def _which(names: Tuple[str, ...]) -> Optional[str]:
    for d in _search_dirs():
        for n in names:
            for candidate in (n, n + ".exe"):
                path = os.path.join(d, candidate)
                if os.path.isfile(path):
                    return path
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def find_llama_binaries() -> Dict[str, str]:
    out: Dict[str, str] = {}
    server = _which(LLAMA_SERVER_NAMES)
    if server:
        out["llama_server"] = server
    rpc = _which(LLAMA_RPC_NAMES)
    if rpc:
        out["llama_rpc"] = rpc
    return out


def parse_device_list(text: str) -> List[Dict[str, Any]]:
    """Parse ``llama-server --list-devices`` output into measured devices."""
    devices: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        m = _DEVICE_LINE.match(line)
        if not m:
            continue
        devices.append(
            {
                "id": m.group("id"),
                "name": m.group("name").strip(),
                "total_bytes": int(m.group("total")) * 1024 * 1024,
                "free_bytes": int(m.group("free")) * 1024 * 1024,
            }
        )
    return devices


def llama_devices(server_bin: str, timeout: float = 30.0) -> List[Dict[str, Any]]:
    out = run_bounded([server_bin, "--list-devices"], timeout=timeout, merge_stderr=True)
    return parse_device_list(out or "")


def llama_version(binary: str, timeout: float = 30.0) -> Optional[str]:
    out = run_bounded([binary, "--version"], timeout=timeout, merge_stderr=True) or ""
    m = re.search(r"version:\s*(.+)", out)
    return m.group(1).strip() if m else None


def llama_build(version: Optional[str]) -> Optional[int]:
    """``0.5.0-dev (build 11190, commit fcc891545)`` -> 11190. Pooling across
    nodes needs matching builds: llama.cpp's RPC protocol changes between
    releases, and a mismatched helper is silently dropped by the head."""
    m = re.search(r"build\s+(\d+)", version or "")
    return int(m.group(1)) if m else None


def rpc_supports_cache(rpc_bin: str, timeout: float = 15.0) -> bool:
    out = run_bounded([rpc_bin, "--help"], timeout=timeout, merge_stderr=True) or ""
    return "--cache" in out


# ---------------------------------------------------------------------------
# GGUF model files
# ---------------------------------------------------------------------------

_GGUF_SCALARS = {
    0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d",
}
_GGUF_STRING = 8
_GGUF_ARRAY = 9
_MAX_HEADER_BYTES = 256 * 1024 * 1024


class _Reader:
    def __init__(self, fh: Any) -> None:
        self.fh = fh
        self.read_bytes = 0

    def take(self, n: int) -> bytes:
        self.read_bytes += n
        if self.read_bytes > _MAX_HEADER_BYTES:
            raise ValueError("GGUF header larger than the sanity bound")
        data = self.fh.read(n)
        if len(data) != n:
            raise ValueError("truncated GGUF header")
        return data

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def string(self) -> str:
        return self.take(self.u64()).decode("utf-8", errors="replace")

    def skip_string(self) -> None:
        n = self.u64()
        self.read_bytes += n
        if self.read_bytes > _MAX_HEADER_BYTES:
            raise ValueError("GGUF header larger than the sanity bound")
        self.fh.seek(n, os.SEEK_CUR)

    def value(self, vtype: int, keep: bool) -> Any:
        if vtype in _GGUF_SCALARS:
            fmt = _GGUF_SCALARS[vtype]
            return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]
        if vtype == _GGUF_STRING:
            if keep:
                return self.string()
            self.skip_string()
            return None
        if vtype == _GGUF_ARRAY:
            etype = self.u32()
            count = self.u64()
            if etype in _GGUF_SCALARS:
                size = struct.calcsize(_GGUF_SCALARS[etype]) * count
                self.read_bytes += size
                if self.read_bytes > _MAX_HEADER_BYTES:
                    raise ValueError("GGUF header larger than the sanity bound")
                self.fh.seek(size, os.SEEK_CUR)
                return None
            for _ in range(count):
                self.value(etype, keep=False)
            return None
        raise ValueError(f"unknown GGUF value type {vtype}")


def gguf_metadata(path: Path) -> Dict[str, Any]:
    """Read architecture, layer count and context length from a GGUF header.
    Returns {} on anything unexpected — a file we cannot parse is still a
    file, it just gets no layer count."""
    wanted_suffixes = (".block_count", ".context_length", ".embedding_length")
    meta: Dict[str, Any] = {}
    try:
        with open(path, "rb") as fh:
            r = _Reader(fh)
            if r.take(4) != b"GGUF":
                return {}
            version = r.u32()
            if version < 2:
                return {}
            r.u64()  # tensor count
            kv_count = r.u64()
            for _ in range(kv_count):
                key = r.string()
                vtype = r.u32()
                keep = key == "general.architecture" or key == "general.name" or key.endswith(wanted_suffixes)
                val = r.value(vtype, keep)
                if keep and val is not None:
                    meta[key] = val
                arch = meta.get("general.architecture")
                if arch and f"{arch}.block_count" in meta and f"{arch}.context_length" in meta:
                    break
    except Exception:
        return {}
    arch = meta.get("general.architecture")
    out: Dict[str, Any] = {}
    if arch:
        out["architecture"] = str(arch)
        if f"{arch}.block_count" in meta:
            out["n_layers"] = int(meta[f"{arch}.block_count"])
        if f"{arch}.context_length" in meta:
            out["context_length"] = int(meta[f"{arch}.context_length"])
    if meta.get("general.name"):
        out["display_name"] = str(meta["general.name"])
    return out


def models_dirs() -> List[Path]:
    """``SWARM_MODELS_DIR`` (os.pathsep-separated) REPLACES the default
    ``~/.swarm/models`` — an explicit choice, not an addition."""
    env = os.environ.get("SWARM_MODELS_DIR")
    if env:
        return [Path(d) for d in env.split(os.pathsep) if d]
    home = Path(os.environ.get("USERPROFILE") or str(Path.home())) if os.name == "nt" else Path.home()
    return [home / ".swarm" / "models"]


def local_gguf_models() -> List[Dict[str, Any]]:
    """GGUF files this node could serve. Split files (``-00001-of-00003``)
    are offered once, by their first shard."""
    seen: Dict[str, Dict[str, Any]] = {}
    for d in models_dirs():
        try:
            entries = sorted(d.glob("*.gguf"))
        except OSError:
            continue
        for path in entries:
            stem = path.stem
            if stem.lower().startswith("mmproj"):
                continue  # vision projector, not a model on its own
            shard = re.match(r"^(?P<base>.+)-(?P<i>\d{5})-of-(?P<n>\d{5})$", stem)
            if shard and shard.group("i") != "00001":
                continue
            name = shard.group("base") if shard else stem
            if name in seen:
                continue
            try:
                size = path.stat().st_size
                if shard:
                    size = sum(
                        p.stat().st_size for p in d.glob(shard.group("base") + "-*-of-" + shard.group("n") + ".gguf")
                    )
            except OSError:
                continue
            entry: Dict[str, Any] = {
                "name": name,
                "kind": "chat",
                "runtime": "llama_cpp",
                "size_bytes": size,
                "file": path.name,
            }
            entry.update(gguf_metadata(path))
            seen[name] = entry
    return list(seen.values())


def resolve_local_model(name: str) -> Optional[Path]:
    """Map a model NAME (never a path the hub supplied) to a local GGUF."""
    for entry_dir in models_dirs():
        for candidate in (entry_dir / (name + ".gguf"), entry_dir / (name + "-00001-of-*.gguf")):
            if "*" in candidate.name:
                try:
                    hits = sorted(entry_dir.glob(candidate.name))
                except OSError:
                    hits = []
                if hits:
                    return hits[0]
            elif candidate.is_file():
                return candidate
    return None


# ---------------------------------------------------------------------------
# the whole answer
# ---------------------------------------------------------------------------


def discover_inference(list_devices: bool = True) -> Tuple[Dict[str, Any], List[Anomaly]]:
    """Everything this node can already serve. Shape::

        {"runtimes": ["ollama", "llama_server", "llama_rpc"],
         "models":   [{"name", "kind", "runtime", "size_bytes", ...}],
         "llama":    {"llama_server": path, "llama_rpc": path, "version": str,
                      "devices": [{"id", "name", "total_bytes", "free_bytes"}],
                      "rpc_cache": bool}}
    """
    anomalies: List[Anomaly] = []
    runtimes: List[str] = []
    models: List[Dict[str, Any]] = []
    llama: Dict[str, Any] = {}

    try:
        found, anoms = ollama_models()
        anomalies.extend(anoms)
        if found or _get_json(ollama_url() + "/api/version", 1.0) is not None:
            runtimes.append("ollama")
        models.extend(found)
    except Exception as exc:
        anomalies.append(Anomaly("runtimes.ollama", str(exc)[:200]))

    try:
        bins = find_llama_binaries()
        llama.update(bins)
        if "llama_server" in bins:
            runtimes.append("llama_server")
            llama["version"] = llama_version(bins["llama_server"])
            llama["build"] = llama_build(llama["version"])
            if list_devices:
                llama["devices"] = llama_devices(bins["llama_server"])
        if "llama_rpc" in bins:
            runtimes.append("llama_rpc")
            llama["rpc_cache"] = rpc_supports_cache(bins["llama_rpc"])
        if "llama_server" in bins:
            models.extend(local_gguf_models())
    except Exception as exc:
        anomalies.append(Anomaly("runtimes.llama", str(exc)[:200]))

    return {"runtimes": runtimes, "models": models, "llama": llama}, anomalies
