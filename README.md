# Swarm

**A spreading cloud of compute. A superorganism, not a scheduler.**

Plug any machine in and it becomes tissue. The swarm senses it the way a
slime mold senses a new food source: it touches, it measures, it decides what
that tissue is good for — locally and globally — and it routes the work with
the best resources available *right now*. Hardware dies, appears, throttles,
wakes from sleep; the organism adjusts. Nothing special happens, because
change is the normal state.

The goal is more philosophical than "a cluster you don't have to babysit."
The easy, plug-and-play, self-maintaining compute fabric is what the organism
*does*. What it *is*: a distributed living system whose cells are your
machines, whose cell membrane is userspace, whose nervous system is
measurement, and whose metabolism is work.

## The organism's instincts

**It tells the truth about itself or says nothing.** Every capability enters
the organism through a benchmark that ran, tagged with how much trust the
instrument deserves. `None` is preferable to a plausible lie. A slime mold
that hallucinates about food gradients starves.

**It discovers before it acts.** New tissue self-reports through a capability
tower (Floor 0: bare CPython — always works; F1: system tools; F2: packages;
F3: dedicated bench suites). Novel hardware appearing on an enrolled node is
sensed by the hotplug nerve (event-driven where the OS allows, measured
polling where it doesn't), diffed against the organism's memory, and either
recognized through the proven adapter registry — never reinvented wheel — or
honestly flagged as *uncovered*, which queues it for the integrator.

**It contracts locally, grows globally.** Nodes pull work proportional to
*measured* throughput × confidence; leases expire and the swarm forgets
nothing it lost — it regrows around absence. There is no push, no failure
detector, no central brain that must stay alive for cells to keep working.

**It never cuts the host to run.** Userspace only, forever. Six laws in
[`AGENTS.md`](AGENTS.md) are load-bearing, enforced by tests, not intentions.

## Today (M1–M5 shipped, CI green)

| Organ | State |
|---|---|
| Sensing (probe + capability tower) | **Live.** Never raises, never hangs; honestly tags every measurement with a trust tier. |
| Memory (hub registry) | **Live.** sqlite, content-addressed, adapter provenance schema ready. |
| Metabolism (pull-based bag-of-tasks) | **Live.** Leased chunks sized by measured throughput × confidence; expiry sweeps regrow lost work; idempotent results. Kill-node demo proven exactly-once. |
| Nerve endings (hotplug watch) | **Live.** New device on an enrolled node is diffed, re-probed, and re-registered. |
| Immune system (adapter coverage) | **Live.** Every sensed device: proven-adapter covered or surfaced as uncovered with reasons. |
| Tail muscle (M3) | **Live.** p90-style hedging (>75% bag, >1.5× median), earned node tiers (Core/Elastic/Opportunistic), suspension rail after 3 consecutive expiries. |
| The stomach (M4) | **Live.** Tier-0 discovery first; pilot sniff; worth-it gate; hand-proven contract gate; NeuralWatt-keyed synthesis, budget-capped, promotion only after the gate passes. |
| Consent membrane (M4.5) | **Live.** Enrollment tokens + one-click invite page + per-invite bundled agent. Welfare loop — the organism exhales when you're typing or battery's low. |
| The spore (E+) | **Live.** `--seed` watches interfaces/adb for attachments; zero-click spread onto fleet-token devices; one-click for anything else; growth logged. |
| The teeth (M5) | **Scaffold.** Measured-memory pipeline planner at `/api/pipeline/plan`; behavioral model runs land with M5 proper. |
| Collective motion (M6) | **Not yet.** Multi-model packing, adaptive replication. |

## RSSI of the organism (the fuel gauge)

`GET /api/fleet-power` — proven vs fallback-tier totals, never summed into a fantasy number. On the dashboard too.

## Take part

```sh
# on the hub tissue
python -m swarm.hub.server --port 8777 --serve-agent

# on any node with bare Python 3.9+
curl -O http://hub-host:8777/agent.pyz
python agent.pyz --hub http://hub-host:8777 --work

# submit work
curl -X POST http://hub-host:8777/api/bag/submit \
  -H 'Content-Type: application/json' \
  -d '{"op":"primesum","params_list":[{"n":5000},{"n":6000}]}'
```

Dashboard: `http://hub-host:8777/` — every number shown with its trust tier,
plus the uncovered-device wall so you watch the organism learn its own body.

Demos: `python scripts/demo.py` (one node, full loop) · `python scripts/demo_m2.py`
(three workers, one killed mid-bag, exactly-once completion) ·
`python scripts/demo_hotplug.py` (plug and watch it notice).

## The brain — three lanes, one kill switch

**Default: a free, abliterated, dense 27B — no API bill, no refusals, runs on
your tissue.** The currently recommended model
(mid-2026, per r/LocalLLaMA + huihui-ai tracking):
[`huihui-ai/Huihui-Qwen3.6-27B-abliterated`](https://huggingface.co/huihui-ai/Huihui-Qwen3.6-27B-abliterated)
— dense (not MoE), strong reasoning, lowest measured refusal and KLD drift in
the current crop. Too much GPU? Drop to `huihui-ai/Huihui-Qwen3-14B-abliterated`
(~9 GB at Q4) or `dolphin3:8b` (~5 GB, any 8 GB card). Serve any of them with
Ollama or llama.cpp — the swarm only needs an OpenAI-compatible socket.

**Lane 2: frontier API** (NeuralWatt — or any OpenAI-compatible endpoint) for
the hardest problems. It's **off by default**, admin-toggleable at runtime,
kill-switchable instantly, rate-limited by budget caps, and *every routing
decision is logged* with its lane and its reason. A turn-offable dial, not a
hidden degree of freedom.

```sh
# the heart (local, free):
set SWARM_LOCAL_BRAIN_URL=http://127.0.0.1:11434/v1
set SWARM_LOCAL_BRAIN_MODEL=huihui-ai/Huihui-Qwen3.6-27B-abliterated

# frontier lane (optional):
set SWARM_NEURALWATT_API_KEY=...

# admin control plane (runtime, no restart):
POST /api/brain/admin {"action":"enable"}            # arm frontier lane
POST /api/brain/admin {"action":"kill_on"}           # instantly sever it
POST /api/brain/admin {"action":"sensitivity","value":0.85}
GET  /api/brain                                      # status + last 100 route decisions
```

Sensitivity tunes how *hard* a task must look before escalating past the
local brain. No frontier key, and it's absent from the path entirely.

## Docs

[`AGENTS.md`](AGENTS.md) — the six laws, priority order, milestone order
(sacred). [`CLAUDE.md`](CLAUDE.md) — contract for AI contributors. `tests/`
— 82 conditions including the law-tests.

*A spreading cloud of compute — measured, not declared.*
