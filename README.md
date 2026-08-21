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

## Today (M1–M2 shipped, CI green)

| Organ | State |
|---|---|
| Sensing (probe + capability tower) | **Live.** Never raises, never hangs; honestly tags every measurement with a trust tier. |
| Memory (hub registry) | **Live.** sqlite, content-addressed, adapter provenance schema ready. |
| Metabolism (pull-based bag-of-tasks) | **Live.** Leased chunks sized by measured throughput × confidence; expiry sweeps regrow lost work; idempotent results. Kill-node demo proven exactly-once. |
| Nerve endings (hotplug watch) | **Live.** New device on an enrolled node is diffed, re-probed, and re-registered. |
| Adaptive immunity (adapter coverage map) | **Live.** `/api/coverage` and the dashboard report every sensed device as *covered* (proven adapter exists) or *uncovered* (queued for the integrator). |
| Synthesis (AI-written adapters) | **M4. Not yet.** The LLM hook accepts any OpenAI-compatible or NeuralWatt key; the contract gate (known-good + known-bad) comes with the milestone. |
| Collective motion (scheduling real models) | **M5–M6. Not yet.** |

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

## Optional AI arming (M4 groundwork)

```sh
set SWARM_NEURALWATT_API_KEY=...   # NeuralWatt
# or any OpenAI-compatible endpoint:
set SWARM_LLM_API_KEY=... ; set SWARM_LLM_BASE_URL=... ; set SWARM_LLM_MODEL=...
```

No key → no network calls, organism fully functional. Status (key masked):
`GET /api/config`.

## Docs

[`AGENTS.md`](AGENTS.md) — the six laws, priority order, milestone order
(sacred). [`CLAUDE.md`](CLAUDE.md) — contract for AI contributors. `tests/`
— 82 conditions including the law-tests.

*A spreading cloud of compute — measured, not declared.*
