"""swarm.bench — calibration microbenchmarks. Stdlib only, trust-tagged."""

from .fallback import (
    fp32_gflops,
    memory_bandwidth,
    memory_latency,
    run_floor_benchmarks,
)

__all__ = ["fp32_gflops", "memory_bandwidth", "memory_latency", "run_floor_benchmarks"]
