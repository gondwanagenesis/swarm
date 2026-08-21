"""Wire types for the swarm. Every field is a fact or None; never a fabrication.

NodeProfile  = what a node IS.   Descriptive. Never reaches a scheduler.
NodeCapability = what a node PROVED. Requires a benchmark run behind it.
The two types share no field names on purpose (Law 1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class MeasurementTrust(str, Enum):
    """How much a benchmark number may be leaned on. Weights are explicit;
    never derive ordering from the name."""

    VERIFIED = "verified"
    CALIBRATED = "calibrated"
    STANDARD = "standard"
    FALLBACK = "fallback"
    THEORETICAL = "theoretical"


TRUST_WEIGHT = {
    MeasurementTrust.VERIFIED: 1.0,
    MeasurementTrust.CALIBRATED: 0.9,
    MeasurementTrust.STANDARD: 0.7,
    MeasurementTrust.FALLBACK: 0.4,
    MeasurementTrust.THEORETICAL: 0.1,
}


class PowerTrust(str, Enum):
    """Where a watts reading came from. The ladder is the point."""

    WALL = "wall_plug"
    SHUNT = "shunt_sensor"
    RAPL_PSYS = "rapl_psys"
    RAPL_PACKAGE = "rapl_package"
    VENDOR_TOOL = "vendor_tool"
    BATTERY = "battery"
    ESTIMATED = "estimated"
    NONE = "none"


POWER_TRUST_WEIGHT = {
    PowerTrust.WALL: 1.0,
    PowerTrust.SHUNT: 0.95,
    PowerTrust.RAPL_PSYS: 0.9,
    PowerTrust.RAPL_PACKAGE: 0.85,
    PowerTrust.VENDOR_TOOL: 0.7,
    PowerTrust.BATTERY: 0.8,
    PowerTrust.ESTIMATED: 0.2,
    PowerTrust.NONE: 0.0,
}


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AdapterOrigin(str, Enum):
    HUMAN = "human"
    AI = "ai"


@dataclass
class Anomaly:
    source: str
    message: str
    severity: str = Severity.WARNING.value


@dataclass
class CpuInfo:
    model: Optional[str] = None
    logical_cores: Optional[int] = None
    physical_cores: Optional[int] = None
    max_freq_hz: Optional[float] = None
    freq_spread_ratio: Optional[float] = None
    heterogeneous: Optional[bool] = None
    cgroup_quota_cores: Optional[float] = None


@dataclass
class MemoryInfo:
    total_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    cgroup_limit_bytes: Optional[int] = None


@dataclass
class DeviceInfo:
    kind: str
    name: Optional[str] = None
    vendor: Optional[str] = None
    pci_address: Optional[str] = None
    uuid: Optional[str] = None
    vram_bytes: Optional[int] = None
    driver_version: Optional[str] = None
    unified_memory: Optional[bool] = None
    runtimes: List[str] = field(default_factory=list)
    evidence: Dict[str, str] = field(default_factory=dict)

    @property
    def merge_key(self) -> Optional[str]:
        if self.pci_address:
            return "pci:" + self.pci_address.lower()
        if self.uuid:
            return "uuid:" + self.uuid.lower()
        return None


@dataclass
class PowerReading:
    watts: Optional[float] = None
    trust: PowerTrust = PowerTrust.NONE
    integrated_joules: Optional[float] = None


@dataclass
class AgentCapability:
    """What the agent process itself can do on this machine (the capability
    tower). max_floor: 0=bare stdlib, 1=system tools, 2=optional packages,
    3=dedicated benchmark suites."""

    max_floor: int = 0
    python_version: str = ""
    tools: Dict[str, Optional[str]] = field(default_factory=dict)
    packages: Dict[str, Optional[str]] = field(default_factory=dict)
    runtimes: List[str] = field(default_factory=list)
    instrument: Dict[str, Optional[float]] = field(default_factory=dict)


@dataclass
class NodeProfile:
    node_id: str = ""
    hostname: str = ""
    os: str = ""
    arch: str = ""
    cpu: Optional[CpuInfo] = None
    memory: Optional[MemoryInfo] = None
    devices: List[DeviceInfo] = field(default_factory=list)
    power: Optional[PowerReading] = None
    anomalies: List[Anomaly] = field(default_factory=list)
    probed_at: float = 0.0


@dataclass
class BenchResult:
    name: str = ""
    value: Optional[float] = None
    unit: str = ""
    trust: MeasurementTrust = MeasurementTrust.THEORETICAL
    samples: List[float] = field(default_factory=list)
    variance: Optional[float] = None
    benchmark_run_id: str = ""
    duration_s: float = 0.0
    burst: Optional[float] = None
    sustained: Optional[float] = None
    anomalies: List[Anomaly] = field(default_factory=list)

    @property
    def stddev(self) -> Optional[float]:
        if self.variance is None:
            return None
        return math.sqrt(self.variance)

    @property
    def sustained_ratio(self) -> Optional[float]:
        if self.burst and self.sustained and self.burst > 0:
            return self.sustained / self.burst
        return None

    @property
    def confidence(self) -> float:
        base = TRUST_WEIGHT[self.trust]
        if self.value is None:
            return 0.0
        if self.variance is not None and self.variance >= 0 and self.value:
            cv = math.sqrt(self.variance) / abs(self.value)
            base *= max(0.5, 1.0 - min(cv, 0.5))
        return round(base, 4)


@dataclass
class NodeCapability:
    node_id: str = ""
    kind: str = ""
    value: Optional[float] = None
    unit: str = ""
    trust: MeasurementTrust = MeasurementTrust.THEORETICAL
    benchmark_run_id: str = ""
    sustained_ratio: Optional[float] = None

    def __post_init__(self) -> None:
        if self.trust is MeasurementTrust.VERIFIED and not self.benchmark_run_id:
            raise ValueError("VERIFIED capability requires a benchmark_run_id")


@dataclass
class LinkMeasurement:
    src_node: str = ""
    dst_node: str = ""
    rtt_p50_ms: Optional[float] = None
    rtt_p95_ms: Optional[float] = None
    bandwidth_bps: Optional[float] = None
    direct: Optional[bool] = None
    trust: MeasurementTrust = MeasurementTrust.THEORETICAL
    measured_at: float = 0.0


@dataclass
class TaskSpec:
    task_id: str = ""
    op: str = ""
    input_refs: List[str] = field(default_factory=list)
    params: Dict[str, str] = field(default_factory=dict)
    flops: Optional[float] = None
    bytes_in: Optional[int] = None
    bytes_out: Optional[int] = None
    round_trips: Optional[int] = None
    peak_mem: Optional[int] = None

    @property
    def idem_key(self) -> str:
        from .identity import canonical_hash

        return canonical_hash(
            {"op": self.op, "input_refs": self.input_refs, "params": self.params}
        )


@dataclass
class TaskResult:
    task_id: str = ""
    idem_key: str = ""
    result_key: str = ""
    node_id: str = ""
    payload_ref: str = ""
    ok: bool = False
    duration_s: float = 0.0


@dataclass
class AdapterRecord:
    adapter_id: str = ""
    device_class: str = ""
    source_ref: str = ""
    authored_by: AdapterOrigin = AdapterOrigin.AI
    probe_evidence_hash: str = ""
    gate_run_id: str = ""
    exemplar_id: str = ""
    registered_at: float = 0.0
