# The Swarm — Manual

**Turn every device you own into one computer.** Old phones, a gaming PC, a
laptop, a Raspberry Pi, a VPS, even an iPad in a browser tab: each becomes a
node. You get one address that any AI tool can use, one command to run your
code everywhere, and a swarm that keeps working when devices die — including
the coordinator itself.

This manual has three parts: **set it up** (10 minutes), **use it**, and
**how it works** (the mental model, for when you want to know why). It ends
with everyday operations, troubleshooting, the honest limits, and a
reference.

## At a glance — what you do, and what happens

| You do | The swarm does |
|---|---|
| Start a hub once on an always-on machine | Mints your owner key; serves the dashboard, the join page, and the AI address |
| Paste one line on a device (or double-click `JOIN`, or tap **Start** in a browser) | Installs the agent + Python + llama.cpp as needed, starts on boot, self-updates, measures the machine, joins |
| Point an AI app at `http://<hub>:8777/v1` | Finds the device that holds the model; starts it there (split over several only if it fits nowhere); unloads it when idle |
| `swarm map fn.py inputs.jsonl` | Runs your function on every idle device; redoes work from devices that die; returns results in order |
| Let Claude/OpenCode/Thea use the MCP tools | Runs their code only on devices you marked as code workers |
| Nothing | Keeps a copy of the hub's memory on successor devices; if the hub dies, one becomes the hub and everyone follows |
| Keep using your devices normally | Yields CPU, backs off when you type, when the battery is low, or when a device runs hot |

---

## Part 1 — Set it up

### 1.1 Pick the hub

The hub is the coordinator: it hands out work, places models, and keeps the
swarm's memory. It is light (≈20 MB of RAM, almost no CPU). Put it on
something that is **always on**: a VPS, an old PC, a Pi.

The hub only needs Python 3.9+ and nothing else.

```bash
git clone https://github.com/gondwanagenesis/swarm
cd swarm
python -m swarm.hub.server --host <address other devices can reach>
```

- Use your **Tailscale** address (`100.x.y.z`) if your devices are on
  Tailscale — the hub is then reachable from anywhere on your tailnet and
  from nowhere else. A LAN address (`192.168.x.y`) works for one home.
- On Linux, make it permanent (starts on boot, restarts if it crashes):

```bash
sudo python -m swarm.hub.server --host <address> --install-service
```

The first start prints where your **owner key** lives
(`~/.swarm/owner.key`). That key is the password to everything: it opens the
dashboard, it is the API key for AI tools, and it is what lets you run code on
your devices. Keep it private.

### 1.2 Open the dashboard

```
http://<hub>:8777/?key=<owner key>
```

The key goes into a cookie on that one visit and leaves the URL. The
dashboard shows every node, what it measured, which models the fleet can
serve, who would take over if the hub died, and a **+ add a device** button.

### 1.3 Add devices

Click **+ add a device** (or open `http://<hub>:8777/join`). You get one line
per platform, with an invite baked in:

| Device | What to do |
|---|---|
| **Linux / macOS / Raspberry Pi** | paste the `curl … \| sh` line into a terminal |
| **Windows** | paste the `powershell … irm … \| iex` line into PowerShell (or Win+R) |
| **Android phone** | install **Termux** (from F-Droid), then paste the Linux line. Optional: **Termux:Boot** so it survives reboots. Set Termux's battery use to *Unrestricted*. |
| **iPhone / iPad / tablet / smart TV** | open the `…/worker?token=…` link in the browser and tap **Start** |
| **A machine with no internet but a USB port** | download the **seed kit** from the join page, unzip it onto a USB stick, plug it in, double-click `JOIN-WINDOWS.cmd` / `JOIN-MAC.command` (or `sh join-unix.sh`) |
| **An old phone that is too slow to compute** | put the seed kit on it and run `python swarm-agent.pyz --seed`: it serves the joiners to every device on its Wi-Fi at `http://<phone-ip>:8788` |

What the one line does, in order (it prints this list before doing anything —
running it is your consent):

1. puts the agent (one ~300 KB file) in `~/.swarm/`
2. installs Python if it can without a password (winget per-user on Windows,
   `pkg` on Termux); otherwise prints the exact command
3. installs **llama.cpp** — the same build on every device, pinned by the
   hub — so the device can hold part of an AI model
4. makes it start on boot, with a watchdog that restarts it within 10 s if it
   ever crashes
5. turns on self-update: when the hub gets new code, every device updates
   itself (signed with that device's own key)
6. prints a one-line **uninstall** that removes only what it added

Useful options (set before the line, e.g. `DEDICATED=1 curl … | sh`):

| Option | Meaning |
|---|---|
| `CODE_WORKER=0/1` | override the code-worker default (see 2.4) |
| `DEDICATED=1` | this device exists to compute (closet PC, phone on a charger): keep working while someone uses it. Battery and heat rules still apply. Phones are dedicated by default. |
| `CODE_WORKER=1` | accept code written by your AI tools (see 2.4). On by default for Android phones, off elsewhere. |
| `AUTOSTART=0` | don't start on boot |
| `SWARM_LLAMA=0` | don't install llama.cpp |

On Windows the same options are `$env:SWARM_DEDICATED=1`, etc.

A device appears on the dashboard within a minute. After that you never
need to touch it again.

### 1.5 Devices stay quiet — and open with your code

Nothing on a device announces the swarm: no windows, no terminal output, no
open ports, a hidden folder, owner-only log and key files, and a neutral
service name (**ComputeNode** at Windows logon, **compute-node** in
systemd / Termux / macOS). Someone else using the device sees nothing
worth asking about.

To look at a device, open a terminal on it and run its status command. It
asks for your **access code** (typed input is hidden, like a password):

```bash
python3 ~/.swarm/swarm-agent.pyz status          # Linux / macOS / Android
python %USERPROFILE%\.swarm\swarm-agent.pyz status   # Windows
```

- Right code: which hub, what it is doing (or why it is resting), tasks done,
  whether it is a successor, and the recent log.
- Wrong code: the single word `no`. Nothing else.
- Same code for `pause` (take no new work), `resume`, and `leave` (shows the
  exact line that removes this device).

Your access code is shown on the dashboard's **+ add a device** page. It is
derived from your owner key, so you can always see it again there; devices
store only a salted hash of it, never the code itself.

The hub is just as quiet: to anyone without your key, a device key, or a
valid invite, every address answers a plain `404 Not Found` — no name, no
version, nothing to fingerprint.

### 1.4 Give the fleet a model

- **Ollama models** on any device are found automatically.
- **GGUF models** (llama.cpp): put the file in `~/.swarm/models/` on any
  device that has llama.cpp. That device becomes the model's home. If the
  model does not fit there, the swarm borrows memory from other devices.

A good small model today: *Qwen3.5-4B* (Q4_K_M, 2.7 GB) —
`huggingface.co/unsloth/Qwen3.5-4B-GGUF`.

---

## Part 2 — Use it

### 2.1 From any AI app (the OpenAI-compatible address)

Anything with a "base URL" / "OpenAI-compatible endpoint" setting works:

| Setting | Value |
|---|---|
| Base URL | `http://<hub>:8777/v1` |
| API key | your owner key |
| Model | a name from the dashboard's **Models** table |

```bash
export OPENAI_BASE_URL=http://<hub>:8777/v1
export OPENAI_API_KEY=<owner key>
```

The first request for a GGUF model starts it (seconds to minutes, depending
on size); it unloads itself after 30 idle minutes so the memory goes back to
its owners.

### 2.2 From the command line

```bash
export SWARM_HUB=http://<hub>:8777
export SWARM_OWNER_KEY=<owner key>          # or keep it in ~/.swarm/owner.key

python -m swarm.cli status                  # who is online, what is served
python -m swarm.cli models                  # every servable model
python -m swarm.cli chat Qwen3.5-4B-Q4_K_M "hello"
python -m swarm.cli plan Qwen3.5-4B-Q4_K_M  # where it WOULD go, and why
python -m swarm.cli deploy Qwen3.5-4B-Q4_K_M
python -m swarm.cli undeploy Qwen3.5-4B-Q4_K_M
python -m swarm.cli join                    # the add-a-device page
```

### 2.3 Run your own code on every device (`map`)

Write a function:

```python
# fn.py
def run(params):
    return {"square": params["n"] ** 2}
```

List the inputs, one JSON value per line:

```text
{"n": 1}
{"n": 2}
{"n": 3}
```

Run it across the fleet:

```bash
python -m swarm.cli map fn.py inputs.jsonl -o results.jsonl
```

Results come back in input order. A device that dies mid-job loses nothing:
its work is re-run elsewhere, and each result is recorded exactly once. The
function runs in a subprocess on each device, so it can use whatever
packages that device's Python has.

### 2.4 Let your AI tools use the swarm (MCP)

The swarm is an MCP server, so Claude Code, OpenCode, Thea — any
MCP-capable brain — can hand work to your devices:

```bash
claude mcp add swarm -- python -m swarm.mcp --hub http://<hub>:8777
```

Tools it gets: `swarm_status`, `swarm_models`, `swarm_chat`, `swarm_embed`,
`swarm_run_python`, `swarm_map`.

**Safety:** code an AI wrote (`swarm_run_python`, `swarm_map`) runs *only*
on devices that opted in as **code workers**. Old Android phones are ideal:
Android sandboxes every app, so the code cannot reach anything outside
Termux. If no code worker is online, the tool says so and runs nothing.

### 2.5 The cloud lane (optional)

If a request names a model no device can serve, the hub can forward it to a
cloud provider — only if you set it up and switch it on:

```bash
export SWARM_LLM_API_KEY=...     SWARM_LLM_BASE_URL=https://.../v1   SWARM_LLM_MODEL=...
# then, at runtime:
curl -X POST http://<hub>:8777/api/brain/admin -H "Authorization: Bearer <owner key>" \
     -H "Content-Type: application/json" -d '{"action":"enable"}'
```

`{"action":"kill_on"}` severs it instantly. There is a daily request cap
(`SWARM_FRONTIER_DAILY_REQUESTS`, default 200). Answers from the cloud are
labelled `"path": "frontier"` so you always know.

---

## Part 3 — How it works

```text
                          ┌──────────── the hub ────────────┐
   your AI apps ──/v1──▶  │ queue · registry · model placement│ ──replica──▶ successor nodes
   swarm CLI / MCP ─────▶ │ owner key · node keys · epochs    │
                          └───────────────▲──────────────────┘
                                          │  every node PULLS (works behind NAT)
            ┌─────────────┬───────────────┼───────────────┬──────────────┐
         laptop        GPU box        old phone         Pi           iPad (browser)
      (model home)   (rpc-server)  (code worker)    (batch work)   (small math jobs)
```

### Nodes pull; the hub never pushes

Every device asks the hub for work ("long-polling": the request waits at the
hub until work exists, so there is no delay and no busy-looping). Because
devices always dial out, phones behind home routers just work. Work is
**leased**: if a device vanishes, its lease expires and the work goes to
someone else. Results are content-addressed, so a task finished twice is
still recorded once.

### Measured, never declared

A device earns work by what it *did*, not by what its spec sheet says. Every
number carries a trust tier. Model placement uses the free memory llama.cpp
itself measured on each device, minus a reserve so the owner never feels it
(max(2 GB, 25%) of RAM on your personal machines; less on dedicated ones).
When a model has run before, its measured tokens/second steers where it runs
next time.

### Three kinds of work

| Kind | What | How |
|---|---|---|
| **Ask** | chat, embeddings | `/v1`: routed to the device holding the model |
| **Serve** | keep a model running | one device if it fits; otherwise split across the **fewest** devices with llama.cpp RPC |
| **Map** | your function over a list | fanned out as leased tasks, results in order |

### Splitting a model (and why it is not about speed)

When a model does not fit on its home device, the planner adds helpers —
largest free memory first, fewest devices possible, all on the **same
llama.cpp build** (the RPC protocol changes between builds). Each helper
runs `rpc-server`; the home device runs `llama-server --rpc …
--tensor-split …` with the split computed from measured memory.

Every generated word must pass through every device, so splitting adds
**capacity** (run models no single device can hold), not speed. Measured on
this project's own fleet: the same 4B model ran 5–7 words/s on one laptop,
and 1.6–2 words/s split between that laptop and a VPS over the internet
(324 ms round trip). On a home LAN the cost per hop is ~100× smaller. The
planner never splits a model that fits on one device.

### Holographic: any part can recreate the whole

The hub is not a single point of failure:

- Every agent file carries the **whole** swarm, hub included (it is all
  plain Python, so even a phone can run a hub).
- The hub picks up to three **successors** (always-on devices first) and
  tells every device who they are on every heartbeat.
- Successors keep a compressed **replica** of the hub's memory, refreshed
  when it changes. The replica holds your owner key's *hash*, never the key.
- If the hub goes silent (3 minutes by default), devices look for a
  successor that is already serving; if none is and it is their turn, the
  best-ranked surviving successor **becomes the hub** at a higher *epoch*.
  Everyone re-attaches with the keys they already had; your owner key keeps
  working.
- If the old hub comes back, it sees the higher epoch, steps aside, and
  redirects anyone who still calls it.

Honest limit: a replica is a snapshot. Finished results survive; work queued
after the last snapshot must be submitted again.

### Never noticed, never harmful

| Rule | How |
|---|---|
| Yields to you | the agent and everything it starts run below normal CPU priority |
| Backs off | when you are typing (personal devices), when a battery is under 40% and unplugged, or a device runs hot (battery ≥ 43 °C, CPU ≥ 85 °C) |
| Leaves room | model placement keeps a memory reserve on every device |
| Idle is free | long-polling instead of polling; self-benchmarks at most every 10 min |
| Bounded | logs rotate at 5 MB |
| Crash-proof | watchdogs restart a crashed agent within 10 s; a lock prevents duplicates |
| Clean exit | uninstall removes only what the joiner added |
| Userspace only | no drivers, no kernel code, no admin rights needed |

### Security, in one table

| Who | Proves it with | Can do |
|---|---|---|
| **You** | owner key (`Authorization: Bearer`, cookie, or `?key=` once) | everything |
| **A device** | its own node key (issued at join, stored hashed on the hub) | pull work, report results — only as itself |
| **A new device** | an invite token (expires in 7 days; seed kits 30) | join once, then its node key takes over |
| **Anyone else** | nothing | liveness ping only |

A hub on `127.0.0.1` is open (for development); any other address is
secure automatically. Services a hub asks a device to run (`rpc-server`,
`llama-server`) are built by the device itself from a validated request —
the hub can never name a program or a file path. Traffic is encrypted by
Tailscale; the hub itself speaks plain HTTP.

---

## Everyday operations

| Task | How |
|---|---|
| See everything | dashboard `http://<hub>:8777/` |
| Remove a device | on the device: run its uninstall line. From the hub: `POST /api/nodes/revoke {"node_id": …}` |
| Update the swarm | update the hub's code and restart it; every device updates itself within a minute |
| Back up the hub | `GET /api/backup` (owner key) — and successors already hold replicas |
| Move the hub | start a new hub on the new machine from a backup, or just stop the old one and let a successor take over |
| Simulate the whole thing | `python scripts/simulate_fleet.py` — 8 emulated devices, real processes, nine scenarios including killing devices and the hub; writes `docs/SIMULATION.md` |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Device never appears | it cannot reach the hub address | open `http://<hub>:8777/api/ping` from that device; check Tailscale is on |
| "invalid or expired invite token" | invite older than 7 days | get a fresh line from `/join` |
| Model stays "loading" a long time | first load over a slow link (weights travel to helpers) | wait; later loads are cached. Prefer helpers on the same LAN |
| Model answers slowly when split | every word crosses every helper | expected; see *Splitting a model*. Put models on a device that holds them alone when you can |
| "a planned remote device was not used" | a helper runs a different llama.cpp build | re-run the joiner on it (installs the pinned build) |
| `swarm_run_python`: "no code-worker node is online" | no device opted in | join a phone, or re-run a joiner with `CODE_WORKER=1` |
| Device online but gets no work | it is resting: you are using it, battery low, or hot | the dashboard's anomalies say why; `DEDICATED=1` for compute-only devices |
| HTTP 409 "moved_to" | this hub stepped aside after a failover | use the `moved_to` address; devices follow automatically |
| Empty answers from Qwen3.5 | it is a thinking model and spent the budget thinking | the CLI turns thinking off; in other apps send `"chat_template_kwargs": {"enable_thinking": false}` |

## What it cannot do (by operating-system design)

- **Start by itself when a USB stick is plugged in.** No modern OS allows
  that (Windows disabled it in 2011). The stick's `JOIN` file is one double-click.
- **Install itself on Android.** Termux must be installed once.
- **Run in the background on iPhone/iPad.** They join as browser workers
  while the page is open.

## Reference

**Files on a device:** `~/.swarm/swarm-agent.pyz` (the agent), `node_keys.json`
(its key), `agent.log`, `llama/` (the pinned llama.cpp), `models/` (GGUF files),
`holo-*.json` + `replica/` (hub succession).

**Files on the hub:** `~/.swarm/hub.db` (memory), `~/.swarm/owner.key`.

**Environment variables:** `SWARM_HUB`, `SWARM_OWNER_KEY` (CLI/MCP);
`SWARM_MODELS_DIR`, `SWARM_LLAMA_DIR`, `SWARM_OLLAMA_URL` (devices);
`SWARM_LLM_*`, `SWARM_FRONTIER_DAILY_REQUESTS` (cloud lane);
`SWARM_LLAMA_TAG` (override the pinned llama.cpp build);
`SWARM_FAILOVER_S` (hub silence before failover, default 180);
`SWARM_MODEL_IDLE_S` (unload after idle, default 1800).

**More:** `REQUIREMENTS.md` (every goal and its evidence), `HANDOFF.md`
(internals for builders), `AGENTS.md` (the rules the code must obey).
