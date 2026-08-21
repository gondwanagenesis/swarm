# AGENTS.md — build contract for Swarm

This file is the law of this repository. It binds human and AI contributors equally.
If a change violates this file, the change is wrong regardless of how elegant it is.

## The measurement thesis

A machine's usefulness is knowable only from what it **did**, on a workload
resembling the one you intend to give it, under real thermal and link conditions.

- `NodeProfile` (what a node *is*) and `NodeCapability` (what it *proved*) are
  separate types with zero overlapping fields. Only `NodeCapability` reaches a
  scheduler.
- `NodeCapability` **cannot be constructed** without a `benchmark_run_id`.
- Never schedule on: VRAM size, vendor name, spec-sheet TFLOPS, declared bandwidth.

## The six laws

1. **Measurement.** Capability is granted by a benchmark that ran, never inferred
   from a profile.
2. **The gate.** Reachable capacity counts only after the arithmetic-intensity
   gate: `compute_ms > 3.0 * move_ms`, peak memory <= **free** memory, sustained
   ratio >= class minimum.
3. **Discovery first.** Enumerate before you generate. AI codegen is the last
   resort, never the first move. Tiers in strict order: Discover -> Write Adapter
   -> Write Kernels -> Never (raw drivers).
4. **Calibrated verification.** Every capability contract ships a known-good AND
   a known-bad. If the gate cannot fail its own known-bad, the gate does not deploy.
5. **Attribution.** An adapter must prove the device did the work: device-side
   counters, low host CPU during "device" work, and a device-only workload the
   host cannot finish in budget.
6. **Userspace.** No kernel-mode code, no PCI config space, no firmware flashing,
   no power/clock limit changes. Absolute. No override flag.

## Priority order (when goals conflict)

Safety > Correctness > Honesty > Availability > Performance.

Fast is good only after the other four are satisfied. `None` is better than a
plausible-looking lie. Anomalies are recorded, never suppressed.

## Hard constraints

- `swarm/core`, `swarm/probe`, `swarm/bench`, `swarm/agent`, `swarm/transport`:
  **Python standard library only.** Every import in these packages must come from
  the stdlib. No exceptions, no "just this one small package." A Termux phone with
  bare CPython 3.9 must be able to run the agent.
- The probe **never raises and never hangs**: every subprocess has a timeout,
  every collector failure becomes an anomaly record, missing data is `None`.
- Every measurement carries a `TrustTier` and (when derived from samples) a
  variance. A measurement without provenance is not a measurement.
- Fail closed: ambiguous gate result -> reject. Impossible benchmark (> theoretical
  peak) -> flag as anomaly, never schedule on it. Unknown power source ->
  `TrustTier.ESTIMATED` or below.
- No self-propagation: the agent is installed by the machine's owner through an
  authorized channel. The system never copies itself onto a machine it found.

## The capability tower convention

Code that needs an external capability (a tool, a package, a runtime) must:

1. Ask `swarm.probe.tower` what is available — never assume.
2. Degrade down the tower, never up: floor (stdlib) always works.
3. Tag outputs with the tier that produced them.

## Conventions

- Python >= 3.9. Type hints everywhere. Dataclasses for wire types.
- Wire format: JSON via `swarm.core.serde`. Never pickle across the network.
- Identity of adapters and results = content hash (`swarm.core.identity`).
- Tests: `python -m pytest tests/` (pytest is dev-only; nothing at runtime).
- Run checks before committing: `python -m pytest tests/` and
  `python -m compileall swarm` (stdlib-only lint is manual: grep the imports).

## Milestones (build order is sacred)

- **M1 (current):** probe + capability tower + benchmarks + hub registry + link
  measurement + dashboard. See everything, do nothing.
- M2: pull-based bag-of-tasks, leases, idempotency, content-addressed results.
- M3: tail shrinking, hedging at p90, node tiers from track record.
- M4: integrator — Tier 0 discovery, AI adapter synthesis, contract gate, trial cells.
- M4.5: enrollment tokens, fleet push, resource invisibility control loop.
- M5: pipeline-parallel inference, speculative decoding.
- M6: multi-model packing, small-model fine-tuning, adaptive replication.

Do not build M4 features while M2 is unfinished.
