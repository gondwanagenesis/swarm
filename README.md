# Swarm

A heterogeneous compute pool over hardware you already own. Plug in any machine — a
gaming PC, a Pi, an old phone, a cloud VM — and it gets probed, benchmarked, and
scheduled on **measured capability, never declared**.

The system's only currency is honest measurement. No spec sheets, no vendor claims,
no "it should be fast because it has N cores." Only benchmarks that ran, tagged with
how much you should trust them.

## Status

**M1 — discovery and registry + M2 — pull-based bag-of-tasks.** The probe runs
on bare Python 3.9+ with zero dependencies, discovers its own tooling on a
capability tower, benchmarks the node with the best instrument it can find
(down to pure-stdlib fallbacks), and registers everything — including what it
*couldn't* measure — with a hub. Workers pull leased chunks sized to their
**measured throughput × confidence**; expired leases requeue automatically;
results are content-addressed and deduplicated; a node dying mid-batch is a
non-event (proven in `scripts/demo_m2.py`).

Not yet: hedging and reliability tiers (M3), AI adapter synthesis (M4).

## The capability tower

The agent discovers itself the same way it discovers hardware:

| Tier | What it means | Measurement it can produce |
|------|---------------|---------------------------|
| 0 — Floor | Pure CPython stdlib. Always works. | Real but rough numbers, `trust=STDLIB_FALLBACK` |
| 1 — Tools | `nvidia-smi`, `clinfo`, `vulkaninfo` present | Vendor-reported inventory + device benchmarks |
| 2 — Packages | `numpy`, `psutil` importable | BLAS-backed GEMM, precise resource monitoring |
| 3 — Bench suite | `mixbench`, `clpeak`, BabelStream installed | Gold-standard microbenchmarks |

The agent climbs as high as the machine allows and **reports which tier every
measurement came from**. A rough number with an honest trust tag beats `None`,
and `None` beats a plausible-looking lie.

## Quick start

```sh
# on the hub machine
python -m swarm.hub.server --port 8777

# on every node (including the hub machine, if it should compute)
python -m swarm.agent.daemon --hub http://127.0.0.1:8777

# dashboard
python -m swarm.hub.server --port 8777 --dashboard
# then open http://127.0.0.1:8777/
```

One-command demo (hub + agent on localhost, full probe + benchmarks):

```sh
python scripts/demo.py
```

M2 kill-node demo (3 workers drain a primesum bag; one is killed mid-run;
its leases expire, the sweep requeues, the bag still completes exactly once):

```sh
python scripts/demo_m2.py
```

Submit your own bag against a running hub:

```sh
curl -X POST http://127.0.0.1:8777/api/bag/submit \
  -H 'Content-Type: application/json' \
  -d '{"op": "hashwork", "params_list": [{"seed": "a", "rounds": 20000}, {"seed": "b", "rounds": 20000}]}'
```

Available ops: `primesum` (`{"n": int}`) and `hashwork` (`{"seed", "rounds"}`).

## Enrolling a node (M4.5-lite)

The agent is stdlib-only, so it ships as **one file**:

```sh
python scripts/build_agent_pyz.py       # produces swarm-agent.pyz (~25 KB)
# or have the hub serve it: python -m swarm.hub.server --serve-agent
# then on the node:
curl -O http://hub-host:8777/agent.pyz
python agent.pyz --hub http://hub-host:8777          # probe + bench + register
python agent.pyz --hub http://hub-host:8777 --work   # also pull work
```

Runs on bare CPython 3.9+ — a Pi, a Termux phone, an old distro. Fleet tokens,
signed bundles, and MDM/Ansible push are the full M4.5 milestone; this is the
manual path that works today. Deploy over your mesh (Tailscale) or verify the
bundle hash out-of-band.

## Optional: arming the AI (M4 groundwork)

The hub talks to any OpenAI-compatible provider **or NeuralWatt** for adapter
synthesis — but only if you set a key. No key, no network calls, full
functionality without it.

```sh
set SWARM_NEURALWATT_API_KEY=your-key        # NeuralWatt
set SWARM_LLM_API_KEY=sk-...                 # any OpenAI-compatible provider
set SWARM_LLM_BASE_URL=https://api.x.ai/v1   # optional override
set SWARM_LLM_MODEL=model-id                 # optional override
```

Check what the hub is armed with (never reveals the key):
`GET http://hub:8777/api/config`

## Layout

| Path | Runs where | Dependencies | Purpose |
|------|-----------|--------------|---------|
| `swarm/core` | everywhere | **stdlib only** | Types, trust tiers, serde, identity. The laws live here. |
| `swarm/probe` | every node | **stdlib only** | Capability tower + hardware introspection. Never raises, never hangs. |
| `swarm/bench` | every node | **stdlib only** | Calibration microbenchmarks, trust-tiered. |
| `swarm/agent` | every node | **stdlib only** | The daemon. Probes, benchmarks, registers, heartbeats. |
| `swarm/transport` | every node | **stdlib only** | Link quality measurement (RTT, bandwidth). |
| `swarm/hub` | one box | stdlib (sqlite3, http.server) | Registry + dashboard. |

Zero third-party dependencies anywhere in the tree at M1. The hub may adopt
FastAPI/Postgres later; the agent never will.

## Design contract

The six laws and the priority order (Safety > Correctness > Honesty > Availability
> Performance) are enforced by construction; see `AGENTS.md` for the full contract.
The short version:

1. Capability is granted by a benchmark that ran, never inferred from a profile.
2. Reachable capacity counts only after the arithmetic-intensity gate.
3. Enumerate before you generate.
4. Every contract ships a known-good and a known-bad.
5. An adapter must prove the device did the work.
6. Userspace only, always.
