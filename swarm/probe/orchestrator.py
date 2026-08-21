"""Probe orchestrator. Runs every collector with a hard per-collector timeout,
records failures as anomalies, and always returns the best partial picture.

Contract: never raise, never hang, always report what it could not find.
Collectors run sequentially (subprocess storms risk deadlock on constrained
hardware); timeouts are enforced with daemon threads, never multiprocessing
(breaks on Termux, heavyweight on Windows).
"""

from __future__ import annotations

import platform
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

from ..core.identity import get_node_id, machine_hostname
from ..core.models import AgentCapability, Anomaly, NodeProfile
from . import cpu, gpu, power, self_probe

COLLECTOR_TIMEOUT = 20.0


@dataclass
class ProbeContext:
    anomalies: List[Anomaly] = field(default_factory=list)
    collector_durations: Dict[str, float] = field(default_factory=dict)
    collectors_ran: List[str] = field(default_factory=list)
    collectors_missed: List[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0


def _run_timed(
    name: str, fn: Callable[[], Any], timeout: float, ctx: ProbeContext
) -> Any:
    result: Dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = fn()
        except Exception as exc:  # collector violated its contract; contain it
            ctx.anomalies.append(Anomaly(f"probe.{name}", f"raised: {exc}", "error"))
            result["value"] = None

    t0 = time.perf_counter()
    thread = threading.Thread(target=target, name=f"probe-{name}", daemon=True)
    thread.start()
    thread.join(timeout)
    ctx.collector_durations[name] = round(time.perf_counter() - t0, 3)
    if thread.is_alive():
        ctx.anomalies.append(
            Anomaly(
                f"probe.{name}", f"collector hung past {timeout}s; skipped", "error"
            )
        )
        ctx.collectors_missed.append(name)
        return None
    ctx.collectors_ran.append(name)
    return result.get("value")


def full_probe(
    timeout: float = 60.0,
) -> Tuple[NodeProfile, AgentCapability, ProbeContext]:
    ctx = ProbeContext(started_at=time.time())
    total_deadline = ctx.started_at + timeout

    def remaining() -> float:
        return max(1.0, min(COLLECTOR_TIMEOUT, total_deadline - time.time()))

    profile = NodeProfile(
        node_id=get_node_id(),
        hostname=machine_hostname(),
        os=platform.system().lower(),
        arch=platform.machine().lower(),
        probed_at=ctx.started_at,
    )

    tower = _run_timed("self_probe", lambda: self_probe.climb_tower(), remaining(), ctx)
    capability: AgentCapability
    if tower:
        capability, tower_anomalies = tower
        ctx.anomalies.extend(tower_anomalies)
    else:
        capability = AgentCapability()

    cpu_res = _run_timed("cpu", cpu.collect_cpu, remaining(), ctx)
    if cpu_res:
        profile.cpu, cpu_anomalies = cpu_res
        ctx.anomalies.extend(cpu_anomalies)

    mem_res = _run_timed("memory", cpu.collect_memory, remaining(), ctx)
    if mem_res:
        profile.memory, mem_anomalies = mem_res
        ctx.anomalies.extend(mem_anomalies)

    gpu_res = _run_timed("gpu", gpu.collect_devices, remaining(), ctx)
    if gpu_res:
        profile.devices, gpu_anomalies = gpu_res
        ctx.anomalies.extend(gpu_anomalies)

    power_res = _run_timed("power", power.collect_power, remaining(), ctx)
    if power_res:
        profile.power, power_anomalies = power_res
        ctx.anomalies.extend(power_anomalies)

    profile.anomalies = list(ctx.anomalies)
    ctx.finished_at = time.time()
    return profile, capability, ctx
