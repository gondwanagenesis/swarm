"""Long-lived services: the one thing a pure op cannot be.

A bag task is a pure function — params in, payload out. Pooling one model
across several machines needs something else: a process that stays up
(llama.cpp's ``rpc-server`` on each helper, ``llama-server`` on the head)
for as long as the hub wants that model served.

The contract is declarative, like the rest of the organism's metabolism:

- The hub owns **desired state**: "node X should run these services".
- The agent owns **actual state**: it reconciles — starts what is missing,
  stops what is no longer wanted — and reports every service's real status
  (``starting`` / ``running`` / ``failed`` / ``parked``) on the next sync.
- Nothing is pushed. The agent asks (``POST /api/services/sync``); the hub
  answers. A node the hub cannot reach still converges the next time it
  can reach the hub.

Safety, because a hub is a remote party telling this machine to run things:

- Only binaries **discovered on this node** run (``probe.runtimes``), and
  only two kinds: ``llama_rpc`` and ``llama_server``. The hub never names a
  path or a binary.
- The command line is built **here**, from a validated spec: ports are ints
  in range, addresses are literals, a model is a NAME resolved against this
  node's own models directory, never a path. No shell, ever.
- ``rpc-server`` has no authentication (llama.cpp says so). It is bound to
  the specific address the hub reaches this node on, never ``0.0.0.0``
  unless the owner sets ``SWARM_ALLOW_WILDCARD_BIND=1``.
- Services run at below-normal priority, and a new one is not started while
  the welfare gate says the host is busy (unless the node is dedicated).
  The host is yours first.
"""

from __future__ import annotations

import atexit
import contextlib
import ipaddress
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

KINDS = ("llama_rpc", "llama_server")

# Lines llama-server prints when it could not use a remote device. It then
# carries on WITHOUT that device — "healthy", but not the deployment the hub
# planned. The service manager treats these as failures so nobody reports a
# pooled model that is really running alone.
_RPC_REJECTED = re.compile(r"(RPC server version mismatch[^\r\n]*|Failed to connect to [^\r\n]+)")
_HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")
_DEVICE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,199}$")
_SERVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
LOG_TAIL_CHARS = 1500

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000


class SpecError(ValueError):
    pass


def _state_dir() -> Path:
    base = Path(os.environ.get("USERPROFILE") or str(Path.home())) if os.name == "nt" else Path.home()
    return base / ".swarm"


def _port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise SpecError(f"bad port {value!r}") from exc
    if not 1024 <= port <= 65535:
        raise SpecError(f"port {port} out of range 1024-65535")
    return port


def _bind(value: Any) -> str:
    text = str(value or "127.0.0.1").strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError as exc:
        raise SpecError(f"bind address must be an IP literal, got {text!r}") from exc
    if addr.is_unspecified and os.environ.get("SWARM_ALLOW_WILDCARD_BIND") != "1":
        raise SpecError(
            "refusing to bind a service to every interface (rpc-server has no auth); "
            "set SWARM_ALLOW_WILDCARD_BIND=1 to override"
        )
    return text


def _endpoint(value: Any) -> str:
    text = str(value or "").strip()
    host, sep, port = text.rpartition(":")
    if not sep:
        raise SpecError(f"rpc endpoint must be host:port, got {text!r}")
    host = host.strip("[]")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME.match(host):
            raise SpecError(f"bad rpc host {host!r}") from None
    return f"{host}:{_port(port)}"


def validate_spec(spec: Dict[str, Any], resolve_model: Callable[[str], Optional[Path]]) -> Dict[str, Any]:
    """Normalize a hub-supplied spec or raise SpecError. The output is the
    only thing `build_command` ever reads."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    sid = str(spec.get("service_id") or "")
    if not _SERVICE_ID.match(sid):
        raise SpecError(f"bad service_id {sid!r}")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise SpecError(f"unknown service kind {kind!r}")
    out: Dict[str, Any] = {
        "service_id": sid,
        "kind": kind,
        "port": _port(spec.get("port")),
        "bind": _bind(spec.get("bind")),
    }
    if kind == "llama_rpc":
        device = spec.get("device")
        if device is not None:
            if not _DEVICE.match(str(device)):
                raise SpecError(f"bad device {device!r}")
            out["device"] = str(device)
        out["cache"] = bool(spec.get("cache"))
        threads = spec.get("threads")
        if threads is not None:
            out["threads"] = max(1, min(256, int(threads)))
        return out

    model = str(spec.get("model") or "")
    if not _MODEL_NAME.match(model) or "/" in model or "\\" in model or ".." in model:
        raise SpecError(f"bad model name {model!r}")
    path = resolve_model(model)
    if path is None:
        raise SpecError(f"model {model!r} is not in this node's models directory")
    out["model"] = model
    out["model_path"] = str(path)
    rpc = spec.get("rpc") or []
    if not isinstance(rpc, list) or len(rpc) > 32:
        raise SpecError("rpc must be a list of at most 32 endpoints")
    out["rpc"] = [_endpoint(e) for e in rpc]
    split = spec.get("tensor_split")
    if split is not None:
        if not isinstance(split, list) or len(split) > 64:
            raise SpecError("tensor_split must be a list of at most 64 numbers")
        vals = [float(v) for v in split]
        if any(v < 0 for v in vals) or not any(v > 0 for v in vals):
            raise SpecError("tensor_split needs non-negative numbers, at least one positive")
        out["tensor_split"] = vals
    out["n_gpu_layers"] = max(0, min(999, int(spec.get("n_gpu_layers", 999))))
    out["ctx"] = max(256, min(262144, int(spec.get("ctx", 4096))))
    devices = spec.get("devices")
    if devices is not None:
        if not isinstance(devices, list) or not all(_DEVICE.match(str(d)) for d in devices):
            raise SpecError("devices must be a list of device ids")
        out["devices"] = [str(d) for d in devices]
    alias = spec.get("alias") or model
    if not _MODEL_NAME.match(str(alias)):
        raise SpecError(f"bad alias {alias!r}")
    out["alias"] = str(alias)
    parallel = spec.get("parallel")
    if parallel is not None:
        out["parallel"] = max(1, min(64, int(parallel)))
    return out


def build_command(spec: Dict[str, Any], binaries: Dict[str, str]) -> List[str]:
    """argv for a VALIDATED spec. No shell; every element is ours."""
    kind = spec["kind"]
    binary = binaries.get(kind)
    if not binary:
        raise SpecError(f"this node has no {kind} binary")
    if kind == "llama_rpc":
        cmd = [binary, "--host", spec["bind"], "--port", str(spec["port"])]
        if spec.get("device"):
            cmd += ["--device", spec["device"]]
        if spec.get("threads"):
            cmd += ["--threads", str(spec["threads"])]
        if spec.get("cache"):
            cmd.append("--cache")
        return cmd
    cmd = [
        binary,
        "--model", spec["model_path"],
        "--host", spec["bind"],
        "--port", str(spec["port"]),
        "--alias", spec["alias"],
        "--ctx-size", str(spec["ctx"]),
        "--n-gpu-layers", str(spec["n_gpu_layers"]),
    ]
    if spec.get("rpc"):
        cmd += ["--rpc", ",".join(spec["rpc"])]
    if spec.get("devices"):
        cmd += ["--device", ",".join(spec["devices"])]
    if spec.get("tensor_split"):
        cmd += ["--tensor-split", ",".join(_fmt(v) for v in spec["tensor_split"])]
    if spec.get("parallel"):
        cmd += ["--parallel", str(spec["parallel"])]
    return cmd


def _fmt(v: float) -> str:
    return ("%.4f" % v).rstrip("0").rstrip(".") or "0"


def _popen_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW}

    def lower_priority() -> None:  # runs in the child, before exec
        with contextlib.suppress(Exception):
            os.nice(10)

    return {"preexec_fn": lower_priority, "start_new_session": True}


def _tcp_ready(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ready(host: str, port: int, timeout: float = 2.0) -> bool:
    shown = f"[{host}]" if ":" in host else host
    try:
        with urllib.request.urlopen(f"http://{shown}:{port}/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


class _Running:
    def __init__(self, spec: Dict[str, Any], proc: subprocess.Popen, log_path: Path) -> None:
        self.spec = spec
        self.proc = proc
        self.log_path = log_path
        self.started_at = time.time()
        self.ready_at: Optional[float] = None
        self.rejected: Optional[str] = None


class ServiceManager:
    """Owns this node's service processes. Thread-safe; never raises out of
    `reconcile`. Every problem becomes a status the hub can read."""

    def __init__(
        self,
        binaries: Dict[str, str],
        resolve_model: Callable[[str], Optional[Path]],
        welfare: Optional[Callable[[], Dict[str, Any]]] = None,
        log_dir: Optional[Path] = None,
    ) -> None:
        self.binaries = dict(binaries)
        self.resolve_model = resolve_model
        self.welfare = welfare
        self.log_dir = log_dir or (_state_dir() / "logs")
        self._lock = threading.RLock()
        self._running: Dict[str, _Running] = {}
        self._problems: Dict[str, Dict[str, Any]] = {}
        atexit.register(self.stop_all)

    # -- reconcile ----------------------------------------------------------

    def reconcile(self, desired: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self._lock:
            wanted: Dict[str, Dict[str, Any]] = {}
            for raw in desired or []:
                sid = str((raw or {}).get("service_id") or "")
                try:
                    spec = validate_spec(raw, self.resolve_model)
                except (SpecError, TypeError, ValueError) as exc:
                    self._problems[sid or "?"] = {
                        "service_id": sid,
                        "kind": (raw or {}).get("kind"),
                        "state": "failed",
                        "error": f"rejected spec: {exc}",
                    }
                    continue
                wanted[spec["service_id"]] = spec

            for sid in list(self._running):
                if sid not in wanted:
                    self._stop(sid)
            for sid in list(self._problems):
                if sid not in wanted and not any(str((r or {}).get("service_id")) == sid for r in desired or []):
                    del self._problems[sid]

            for sid, spec in wanted.items():
                if sid in self._running or self._problems.get(sid, {}).get("state") == "failed":
                    continue
                self._start(spec)
            return self.status()

    def _start(self, spec: Dict[str, Any]) -> None:
        sid = spec["service_id"]
        if self.welfare is not None:
            try:
                verdict = self.welfare()
            except Exception:
                verdict = {"allowed": True}
            if not verdict.get("allowed", True):
                self._problems[sid] = {
                    "service_id": sid,
                    "kind": spec["kind"],
                    "state": "parked",
                    "error": str(verdict.get("reason") or "host busy"),
                }
                return
        try:
            cmd = build_command(spec, self.binaries)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / f"{sid}.log"
            log = open(log_path, "wb")  # noqa: SIM115 - handed to the child, closed below
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    **_popen_kwargs(),
                )
            finally:
                log.close()
        except Exception as exc:
            self._problems[sid] = {
                "service_id": sid,
                "kind": spec["kind"],
                "state": "failed",
                "error": f"could not start: {exc}"[:300],
            }
            return
        self._problems.pop(sid, None)
        self._running[sid] = _Running(spec, proc, log_path)

    def _stop(self, sid: str) -> None:
        run = self._running.pop(sid, None)
        if run is None:
            return
        with contextlib.suppress(Exception):
            run.proc.terminate()
            try:
                run.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                run.proc.kill()
                run.proc.wait(timeout=5.0)

    def stop_all(self) -> None:
        with self._lock:
            for sid in list(self._running):
                self._stop(sid)

    # -- status ---------------------------------------------------------------

    def status(self) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = list(self._problems.values())
            for sid, run in list(self._running.items()):
                spec = run.spec
                rc = run.proc.poll()
                entry: Dict[str, Any] = {
                    "service_id": sid,
                    "kind": spec["kind"],
                    "pid": run.proc.pid,
                    "bind": spec["bind"],
                    "port": spec["port"],
                    "uptime_s": round(time.time() - run.started_at, 1),
                }
                if spec["kind"] == "llama_server":
                    entry["model"] = spec["model"]
                if rc is None and spec["kind"] == "llama_server" and spec.get("rpc") and run.ready_at is None:
                    run.rejected = run.rejected or self._rpc_rejection(run.log_path)
                if run.rejected:
                    entry["state"] = "failed"
                    entry["error"] = f"a planned remote device was not used: {run.rejected}"
                    entry["log_tail"] = self._tail(run.log_path)
                    self._problems[sid] = entry
                    self._stop(sid)
                    out.append(entry)
                    continue
                if rc is not None:
                    entry["state"] = "failed"
                    entry["error"] = f"exited with code {rc}"
                    entry["log_tail"] = self._tail(run.log_path)
                    # Keep it visible as a problem; the hub decides what next.
                    self._problems[sid] = entry
                    del self._running[sid]
                    out.append(entry)
                    continue
                ready = (
                    _tcp_ready(spec["bind"], spec["port"])
                    if spec["kind"] == "llama_rpc"
                    else _http_ready(spec["bind"], spec["port"])
                )
                if ready and run.ready_at is None:
                    run.ready_at = time.time()
                entry["state"] = "running" if ready else "starting"
                if run.ready_at is not None:
                    entry["ready_after_s"] = round(run.ready_at - run.started_at, 1)
                if not ready:
                    entry["log_tail"] = self._tail(run.log_path, 600)
                out.append(entry)
            return out

    @staticmethod
    def _rpc_rejection(path: Path) -> Optional[str]:
        try:
            with open(path, "rb") as fh:
                head = fh.read(256 * 1024).decode("utf-8", errors="replace")
        except OSError:
            return None
        m = _RPC_REJECTED.search(head)
        return m.group(1).strip() if m else None

    @staticmethod
    def _tail(path: Path, chars: int = LOG_TAIL_CHARS) -> str:
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - chars * 2))
                return fh.read().decode("utf-8", errors="replace")[-chars:]
        except OSError:
            return ""
