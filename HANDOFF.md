# HANDOFF — building The Swarm

> For whoever builds next: human, AI agent, or hybrid. This doc is the whole
> map. Read [`AGENTS.md`](AGENTS.md) first — that's the law. This file is the
> lay of the land.

## What this is

A heterogeneous compute **organism**: any machine (GPU rig, laptop, Pi, phone
via Termux, VM) runs one stdlib-only file (`agent.pyz`), gets *measured* (never
declared), and pulls work proportional to its *proven* throughput × confidence.
Philosophy: the slime mold of compute — senses, decides locally, contracts and
regrows, never lies about itself. Everything on disk is the nervous system of
that idea.

## Headline invariants (do not violate)

| Rule | Where enforced |
|---|---|
| Agent-side code: **stdlib only**, Python ≥ 3.9 | `scripts/check_stdlib_imports.py` (CI gate) |
| Probe never raises, never hangs | contract tests + threading timeouts |
| Nothing schedules on declared/spec-sheet numbers | `NodeCapability` ↔ `NodeProfile` disjointness test |
| VERIFIED capabilities require benchmark_run_id | `__post_init__` on the model |
| Fail closed everywhere — calibration, gate, enrollment, consent | the contract gate + enrollment tests |
| Userspace only, always | law; there's exactly zero kernel-code here |
| Self-edit only through the workshop (sandbox-gated, ledgered) | `swarm/hub/workshop.py` + law 7 in AGENTS.md |
| AI is a last resort behind the worth-it gate, never the first move | `hub/verdicts.py` + `integrator/policy.py` |

## Architecture map (by organ)

| Organ | Module(s) | What it must never do |
|---|---|---|
| Senses | `swarm/probe/` (`orchestrator`, `cpu`, `gpu`, `power`, `self_probe`, `discovery`, `hotplug`) | raise, hang, fabricate, assume a tool exists |
| Instrument self-report | `probe/self_probe.py` (capability tower F0–F3 + instrument bench) | claim F2+ without import success |
| Metabolism (scheduler) | `swarm/hub/queue.py` (lease/sweep/hedge), `hub/scheduler.py` (chunk = throughput × confidence, tail shrink), `hub/registry.py` (sqlite) | schedule on declared numbers; leak tasks (leases always expire) |
| Link honesty | `swarm/transport/link.py` | report unmeasured latency/bandwidth as fact |
| Node agent | `swarm/agent/` (`daemon`, `ops`, `welfare`, `spore`, `idle`, `updater`, `hotplug wiring`) | work while the welfare gate says no; install anything |
| Hub | `swarm/hub/` (`server` http.server, `dashboard`, `registry`, `enrollment`, `coverage`, `verdicts`, `fleet_power`, `backup`, `workshop`, `pipeline`) | require third-party packages (stdlib today; FastAPI allowed later if it earns it) |
| Integrator (M4) | `swarm/integrator/` (`llm`, `policy`, `gate`, `synthesize`) | ship an adapter that hasn't passed the contract gate |
| Contracts | `contracts/*.json` — each = reference cases + known-good + known-bad | promote an adapter without a gate pass |

## Dataflow (one paragraph)

Agent boots → climbs the capability tower (what CAN this agent do here) →
full probe (NodeProfile: CPU/mem/devices/power, anomalies, instrument speed)
→ floor benchmarks (trust=FALLBACK) → tier-0 discovery (does an existing
runtime already bind each device?) → pilot sniff → POST `/api/register`.
Hub stores it, computes **verdicts per device class** (`adopt_now` /
`synthesize` / `park` — with reasons), links, trust. Worker loop: pull leased
chunks → execute ops (pure functions) → time each → renew at ⅓ lease → post
results (content-addressed, duplicates are no-ops). Idle? The hone ladder
sharpens measurement. Hardware arrives? Hotplug diff → re-register → fresh
verdicts. That loop *is* the organism breathing.

## Endpoint index

| Route | Dir | Purpose |
|---|---|---|
| `/api/ping`, `/api/echo` | GET/POST | RTT + bandwidth measurement targets |
| `/api/register`, `/api/heartbeat`, `/api/link` | POST | identity, liveness, link matrix |
| `/api/nodes`, `/api/nodes/<id>` | GET | registry views (tier+suspended patched in) |
| `/api/fleet-power` | GET | trust-tiered totals — no fantasy sums |
| `/api/coverage`, `/api/verdicts`, `/api/bindings` | GET | device coverage + worth-it verdicts + runtime bindings |
| `/api/bag/submit`, `/api/tasks/pull`, `/api/tasks/complete`, `/api/tasks/renew` | POST | the work loop |
| `/api/bags`, `/api/bag/<id>` | GET | bag status |
| `/api/workshop`, `/api/workshop/propose`, `/apply`, `/rollback` | GET/POST | lawful self-editing (autopilot on by default) |
| `/api/brain`, `/api/brain/admin` | GET/POST | brain lane status + enable/kill/sensitivity/autopilot |
| `/api/tokens`, `/invite`, `/bundle.pyz?token=` | GET/POST | enrollment, one-click join, per-invite bundle |
| `/api/spore/event`, `/api/spore/events` | POST/GET | attachment watch ledger |
| `/api/self-update`, `/api/bundle/latest` | POST/GET | node self-update channel (hash-verified) |
| `/api/backup` | GET | consistent sqlite snapshot w/ SHA-256 header |
| `/api/pipeline/plan` | POST | M5 measured-memory stage mapping (scaffold) |
| `/api/anomalies`, `/api/sharpen` | GET/POST | honest gaps + idle-hone ingestion |
| `/` | GET | dashboard (renders trust tiers, never plausibilities) |

## Brain lanes (M4 propulsion)

```
Lane 0  abliterated-local   default (huihui-ai Qwen3.6 27B — edit as models move)
Lane 1  constitutional-local fallback when no local brain armed
Lane 2  frontier API        OFF by default; admin arms; kill switch sever;
                            sensitivity dial; every call logged with reason
```

Arm via env on the hub box: `SWARM_LOCAL_BRAIN_URL`, `SWARM_LOCAL_BRAIN_MODEL`,
`SWARM_NEURALWATT_API_KEY`. A lane never runs unarmed; the router picks the
lowest honest lane and *says so*.

## Test topology (~140 conditions)

- **law tests**: field disjointness, VERIFIED-needs-run-id, trust ordering,
  gate calibration (good passes / bad rejected / sneaky rejected)
- **unit**: serde round-trips (incl. forward-compat), identity persistence,
  probe collectors mocked per-OS, AdapterRAM clamp, RAPL wrap, hedging
  semantics, tier earning, suspension rail, token lifecycle, spore debounce
  (ghost dies), mDNS packet round-trip, updater hash paths, idle ladder
- **integration**: full probe on the real machine, hub+agent register, kill-
  node requeue completes bag exactly once, hedged straggler first-finisher,
  workshop sandbox gate (apply→42→rollback), backup restore
- **demos that prove it live**: `scripts/demo.py`, `demo_m2.py` (kill-node),
  `demo_hotplug.py`

Gates before every push: `pytest tests/`, `ruff check`, stdlib import gate,
`compileall`.

## The compute-fabric pass (what changed and why)

An audit found the scheduling organism real and the *compute* it schedules
largely absent. Six gaps, all now closed except where hardware prevented it:

1. **Adapter source was discarded.** The `adapters` table had no source
   column and `record_adapter` never received the code — a "proven adapter"
   was a hash pointing at nothing, and nothing could ever load one. Now:
   `hub/adapter_store.py` (content-addressed, atomic, sharded), a `source_hash`
   column added by migration, and `get_adapter_source()`. Pinned end to end by
   `tests/test_adapter_lifecycle.py`: retrieved source still executes *and*
   still re-passes the gate.
2. **The gate checked the wrong thing.** `entrypoint` was the string literal
   `"primes"` and comparison was `len(out) == expected`, so an adapter
   returning `list(range(4))` for n=10 passed. Now entrypoint/comparison/
   timeout come from the contract (`length`/`exact`/`allclose`), and
   `prime_contract.json` v2 compares actual prime *lists*.
3. **The gate was not a boundary.** Restricted-`__builtins__` `exec` in the
   hub process is a well-known non-boundary. Now a `subprocess` with a hard
   timeout, JSON over stdin/stdout, and a per-run uuid4 stdout marker so an
   `atexit` hook cannot forge a pass.
4. **No hardware routing.** Bags had no `device_class`. Now schema v3 plus
   `node_device_classes`, populated at registration from the node's *measured*
   profile at two granularities (`nvidia:geforce_rtx_4090` and `gpu:generic`).
5. **mDNS announced into a void.** `parse_response`/`send_query` had no
   production caller. Now `agent/discovery_loop.py` browses, resolves
   PTR+SRV+A, and `--hub` is optional. Also fixed a latent infinite loop in
   `_decode_name` on self-referential compression pointers — harmless while
   nothing listened, a thread-killer once real LAN packets arrive.
6. **M5 summed stage times.** Pipeline throughput is the *bottleneck*, not the
   sum, and the greedy loop piled every stage on the biggest node. Now an
   exact DP contiguous-chain min-max partition reporting `bottleneck_ms` and
   `latency_ms` separately.

Two bugs the demo caught that unit tests could not, both now pinned:
`handle_submit` dropped `device_class` (so every classed bag submitted over
HTTP — the only path real agents use — silently became unrestricted), and
`start_lan_announce` read `self.port` before bind, advertising port 0.

**Still unproven, and honestly labelled:** the `torch_cuda` matmul tier has
never executed — this machine has an Intel Iris Xe with `runtimes=[]` and
`torch 2.13.0+cpu`. Adapter synthesis has never run against a live LLM. M5
still does not execute a model. Do not mark any of these done without a run.

## Hard-hat areas (rough drafts — honest labels)

- `swarm/hub/pipeline.py` — M5 *scaffold*. Stage→node mapping on measured free
  memory only. Does not run models. Behavioral runs land with real adapters.
- Hedge thresholds (75% / 1.5×) — research-informed defaults (Dean & Barroso /
  Spark); expect to re-tune from field data once real workloads flow.
- Workshop autopilot — on by default; sandbox gate is the membrane. The farther
  this spreads, the more we owe admin-mode audit views.
- Seed `--seed` mode — validated end-to-end; USB-gadget and Bluetooth channels
  are welcome additions behind the same debounce contract.

## Roadmap (sacred order, kept current in AGENTS.md)

- M1, M2, M3, M4, M4.5, M5-scaffold, plus workshop + brain router: **done**
- M5-real: pipeline-parallel behavioral runs on gated adapters
- M6: multi-model packing, adaptive replication, fine-tuning small models on
  fleet observations
- Later-honors: exemplar similarity index (nearest-neighbor adapter lookup),
  sub-lease splits, multi-hub federation, trial cells on real NPU/edge silicon

## Footguns (things that bit us tonight — don't get bit)

1. **wmic is dead** on Win11 ≥22H2 (build 26100 confirmed absence). PowerShell
   CIM (`Get-CimInstance`) + ctypes is the way.
2. **AdapterRAM is a signed 32-bit clamp** — values in the top ~32 MiB of 2^32
   mean ">4GB"; report `None`, never the clamped value.
3. **RAPL wraps at `max_energy_range_uj`** (~262 J), not 2^32 — modulo deltas.
4. **`sys.stdlib_module_names` doesn't exist on py3.9** — the gate unions an
   explicit baseline.
5. **sqlite across threads**: one connection + one shared `RLock` everywhere;
   WAL + busy_timeout. Don't add another connection path casually.
6. **Content-addressed storage vs per-task visibility**: `results_for_bag`
   maps *tasks* to results; storage dedupes by `result_key`. Idempotency demos
   with repeated identical params will look "wrong" while being exactly right.
7. **Workshop sandbox recursion**: tests under `SWARM_WORKSHOP_SANDBOX=1` skip
   workshop tests; the gate runs pytest with `-k "not workshop"`.
8. **GetLastInputInfo wraparound** at 49.7 days; clamp negative to 0.
9. **The welfare gate will stop a demo** if you're typing. Demos use
   `ignore_welfare=True` explicitly and loudly.
10. **Agent zipapps on Windows** replace fine via `os.replace` + `os.execv`;
    dev checkouts never self-update (`not-bundled`).

## Contribution rules (short)

- Agent-side (`core/probe/bench/agent/transport/integrator`): stdlib only,
  Python ≥3.9, `from __future__ import annotations`, dataclasses with defaults.
- Hub may adopt FastAPI/Postgres later — when it earns it; today it doesn't.
- New organ? Write the test that proves its failure mode first, then the organ.
  Known-good and known-bad for anything gated.
- Docs with it: README organ table, this handoff, AGENTS.md milestone ledger.

## Quick start for the next builder

```sh
git clone https://github.com/gondwanagenesis/swarm
cd swarm && python -m pytest tests/ -q
python scripts/demo.py                # full local loop
python -m swarm.hub.server --port 8777 --serve-agent
python -m swarm.agent.daemon --hub http://127.0.0.1:8777 --work
```

Then open `http://127.0.0.1:8777/` and read `AGENTS.md`. Build order is
sacred; laws above are load-bearing; the organism tells the truth.

*Single-file agent: run `python scripts/build_agent_pyz.py`; node side needs
bare Python 3.9+ and nothing else.*
