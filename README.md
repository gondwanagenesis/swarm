# Swarm

> Next builder: **`HANDOFF.md`** — the full map, the laws, the footguns.

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

## Today — what is proven, and what is only built

Two columns, deliberately. **Proven** means a test or a demo exercises the real
behaviour on real hardware. **Built** means the code path exists and is
exercised against fakes, but no run on real silicon has earned it yet. The
organism does not get to grade itself on intentions — see `AGENTS.md`, Law 1.

Where a claim is bounded by the hardware it was measured on, the bound is
stated. Nothing here is marked proven because it looked right.


| Organ | State | Evidence / bound |
|---|---|---|
| Sensing (probe + capability tower) | **Proven** | Never raises, never hangs; every measurement carries a trust tier. Full probe runs on this machine. |
| Memory (hub registry) | **Proven** | sqlite, content-addressed. Adapter **source** is now persisted and retrievable, not just its hash. |
| Metabolism (pull-based bag-of-tasks) | **Proven** | `demo_m2.py`: 3 workers, 1 killed mid-bag, 400/400 completed exactly once. |
| Nerve endings (hotplug watch) | **Proven** | Device diff → re-probe → re-register; removal retracts the class rather than leaving a ghost. |
| Device-class routing | **Proven** | Registration indexes classes from the measured profile. GPU-classed work does not leak to CPU-only nodes; an unservable bag is visibly blocked with a reason, never silently queued. |
| Tail muscle (M3) | **Proven** | Hedging (>75% bag, >1.5× median), earned tiers, suspension after 3 consecutive expiries. |
| Contract gate (M4) | **Proven** | Contract-driven entrypoint + comparison; candidate code runs in a **subprocess**, not restricted-`exec`. Timeout, crash, and forged-stdout all fail closed. Known-bad rejection asserted in CI. |
| LAN discovery | **Proven** | Raw mDNS PTR+SRV+A; a `DiscoveryLoop` resolved a live announcing hub over real multicast. Degrades to "no peers" — never crashes, never guesses. |
| Consent membrane (M4.5) | **Proven** | Enrollment tokens, one-line joiners, seed kits. Off-loopback hubs are **secure by default**: owner key, per-node keys, tokens (`hub/auth.py`). Discovery finds candidates; it never enrolls. |
| Welfare loop | **Proven** | Backs off on typing / battery; `--dedicated` for machines that exist to compute (battery rules still apply). |
| Gene expression (workshop) | **Proven** | Self-edits only through a sandboxed gate, owner-key only; every byte content-hashed; rollback restores exact bytes. |
| Real workloads | **Proven** | `embed` and `chat` run on the node's own runtime (Ollama), routed by `model:<name>` to the node that actually holds the model. Fail closed with no runtime. |
| The front door (`/v1`) | **Proven live** | OpenAI-compatible chat/embeddings/models on the hub; any tool with a base-URL setting uses the fleet. Answered live through a VPS hub from a laptop GPU. |
| The teeth (M5) | **Proven live** | Models run on one node when they fit (never sharded then), pooled over several via llama.cpp RPC — split from measured free memory — when they do not. Live: Qwen3.5-4B split laptop GPU + VPS CPU across the internet at 1.6–2.0 tok/s (324 ms link; the same model alone on the laptop: 5.4–7). Pooling buys capacity, not speed — numbers in `REQUIREMENTS.md`. |
| General compute (`swarm map`) | **Proven (tests)** | Your Python function over a list, across every node, results in order; failures retried, then reported. |
| Accelerated compute (GPU tier) | **Proven via Vulkan** | llama.cpp's Vulkan backend on the laptop's Iris Xe (live). The `matmul` torch-CUDA tier is still unexecuted — no CUDA box yet. |
| Adapter synthesis (M4 Tier 1) | **Built, unproven here** | Loop, budget cap, and promotion-only-after-gate are wired and tested against scripted LLMs. No run against a live model has been recorded. |
| Collective motion (M6) | **Not started** | Multi-model packing, adaptive replication. |

**The honest summary:** the scheduling and measurement organism is real and
proven under failure, and it now schedules real work — live model inference,
routed to nodes that actually have the runtime. What it still cannot do is
*accelerate* that work on a GPU (the path is written; no machine here has a
bound GPU runtime to run it) or execute a model too large for one node (the
partitioner is exact; the executor does not exist). Those two gaps are the
next milestones, not footnotes.

## RSSI of the organism (the fuel gauge)

`GET /api/fleet-power` — proven vs fallback-tier totals, never summed into a fantasy number. On the dashboard too.

## Take part — the easy way

```sh
# the hub (always-on box; binds only where you tell it, secure by default off-loopback)
python -m swarm.hub.server --host <tailscale-or-lan-ip>
# -> prints where the owner key lives; open http://<hub>:8777/?key=<owner key>
```

Then open **`/join`** on the hub: one copy-paste line per platform (Linux,
macOS, Pi, Android-Termux, Windows) and a **seed kit** zip for USB sticks
and old phones. The joiner says what it will install, installs Python and
the fleet's pinned llama.cpp build where it can, starts on boot, and turns
on self-update. After that the device never needs touching.

Use it from anything that speaks OpenAI:

```sh
export OPENAI_BASE_URL=http://<hub>:8777/v1  OPENAI_API_KEY=<owner key>
python -m swarm.cli models
python -m swarm.cli chat Qwen3.5-4B-Q4_K_M "hello"
python -m swarm.cli map my_fn.py inputs.jsonl -o results.jsonl
```

Drop GGUF files into `~/.swarm/models` on any node with llama.cpp; the
fleet serves them on demand, splitting across nodes only when a model does
not fit on one. Progress against the goals lives in
[`REQUIREMENTS.md`](REQUIREMENTS.md).

## Take part — by hand

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
`python scripts/demo_hotplug.py` (plug and watch it notice) ·
`python scripts/demo_fabric.py` (probe → tower → device classes → routed
matmul → **which tier actually ran it** → an unservable bag failing loudly).

Agents can now find a hub instead of being told one:

```sh
python -m swarm.hub.server --lan          # bind all interfaces + announce over mDNS
python -m swarm.agent.daemon --work       # no --hub: discovers it on the LAN
```

Discovery yields a *candidate address only* — joining still goes through the
token/consent path. Finding a hub is not the same as being recruited by one.

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
