# REQUIREMENTS — what "done" means for The Swarm

The goal, in the owner's words: *connect all my devices' compute together —
old phones, GPUs, anything — plug it in, click accept, and everything else
handles itself behind the scenes.*

**Priority order (owner, 2026-09-26)** — when requirements conflict, the
higher one wins:

1. **Break-resistant.** Devices die, links drop, processes crash — the swarm
   carries on and heals. It can take all the time it needs.
2. **Invisible.** The owner never notices it working on their devices.
3. **Never harms a host.** No hogging, no filling disks, no bricking, no
   breaking what was installed before.
4. **Holographic.** Any part can recreate the whole — no single machine
   whose loss ends the swarm.
5. **All connected.** Every device the owner has, reachable as one fabric.
6. **Fast** — welcome, but last.

This file turns that into checkable requirements. Status uses the same
discipline as the rest of the repo — **nothing is marked proven because it
looked right**:

| Mark | Meaning |
|---|---|
| ✅ | **Proven** — exercised on real hardware (live run named), or it is pure logic fully pinned by tests |
| 🟡 | **Built** — code + tests against fakes; not yet run on that real hardware |
| ⬜ | **Not started** |
| ⛔ | **Impossible by OS design** — the limit is documented, the nearest honest alternative is built |

Live-run evidence refers to the **2026-09-26 run**: hub on the Hostinger VPS
(Tailscale-only), nodes = the X1 Yoga laptop (Windows 11, Iris Xe via
Vulkan) and the VPS itself (Linux, 2 vCPU). Update this file in the same PR
as the work it describes.

---

## K. Low profile (owner request, 2026-09-26: "blend in; open with a secret code")

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| K1 | A secure hub is dark to strangers: plain 404, generic server header, no name / version / swarm id | ✅ | `tests/test_lowprofile.py` sweeps 11 paths; live: a stranger saw HTTP 404 on Windows and Linux test hubs |
| K2 | Swarm-internal endpoints (hub info, agent file, updates, worker page) answer only a node key, the owner key, a valid invite, or a signed peer hub | ✅ | Peer hubs sign with HMAC(owner-key hash, timestamp), 5-minute window; replayed signatures refused |
| K3 | Devices show nothing: no windows, no output, hidden folder, owner-only logs/keys, neutral service names (ComputeNode / compute-node) | ✅ Windows, Linux · 🟡 macOS, Termux | Live on Windows (hidden folder, `ComputeNode` Run entry) and Linux (`compute-node` systemd unit) |
| K4 | A device opens only with the access code (`status`, `pause`, `resume`, `leave`); wrong code → `no` | ✅ | Live on Windows and Linux: wrong code printed `no`; right code showed hub, state, tasks, successor rank, log. Devices hold only a salted PBKDF2 verifier |
| K5 | No disguise: the agent is never presented as a system process or hidden from antivirus / Task Manager | ✅ by design | Low profile, not deception: processes stay honestly `python`/`pythonw`; files stay in the user's own profile |

## H. Holographic & break-resistant (priorities 1 and 4)

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| H1 | Work survives any node dying mid-job (leases expire, work regrows elsewhere, exactly-once results) | ✅ | Pre-existing: `demo_m2.py` kill-node, 400/400 exactly once |
| H2 | Failed work is retried elsewhere, then reported — never silently "done" | ✅ | `tests/test_queue_failures.py` |
| H3 | Crashed agent comes back by itself on every platform | ✅ Windows, Linux · 🟡 others | Live: killed the Windows agent → the VBS watchdog restarted it in ~10 s; killed the Linux agent → systemd restarted it (PID 333 → 438). Found + fixed on the way: a PowerShell precedence bug had split the VBS command and the Windows agent never started |
| H4 | A restart can never double a node | ✅ | Single-instance lock (`tests/test_invisible.py`) |
| H5 | A model deployment whose participant dies is failed and re-placed on the next request | 🟡 | `tick()` logic + tests; not killed live |
| H6 | Every node carries the WHOLE swarm (hub code too — it is stdlib) | ✅ | Agent file ships hub + integrator + CLI + MCP; CI gates the whole package stdlib-only; `tests/test_agentbundle.py` runs a hub FROM the agent file |
| H7 | Hub state is replicated to several nodes continuously | ✅ | Up to 3 ranked successors pull a pruned, gzipped, hashed snapshot when it changes (`/api/replica`); `tests/test_holo.py` |
| H8 | Hub loss → a successor restarts the hub from the latest replica; nodes re-attach on their own | ✅ | `tests/test_holo.py::test_hub_failover_end_to_end`: real hub killed, successor promotes a NEW hub process at epoch 2, the other node follows, new work completes. Old hub steps aside (HTTP 409 `moved_to`) on seeing a higher epoch |
| H8b | Every surviving node re-attaches after a failover — including nodes that joined seconds before the hub died | ✅ | Simulation S9 alone: 8/8 re-attached in 13 s. Found + fixed: the advertised replica went stale (rebuilt only when fetched, fetched only when changed); now rebuilt on membership change and at least every 30 s. Nodes missing from a replica re-join with their original invite |
| H8c | A node's heartbeat (its lifeline) survives any error | ✅ | Each heartbeat iteration is guarded and logged; a single exception used to end it silently |
| H9 | Owner key survives a hub loss without being copied in plaintext to every node | ✅ | Replicas carry only its sha256; a restored hub verifies against the hash (`test_identity_is_minted_once_and_replicates`) |
| H10 | Nothing in the swarm depends on one cloud service being up | ✅ | GitHub is touched only at join time (llama.cpp download); hub, agents, failover, updates all run inside the fleet |

## J. Everything tested, emulated, simulated

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| J1 | Unit + integration suite across Windows/macOS/Linux × Python 3.9/3.12 | ✅ | GitHub Actions matrix; stdlib gate covers the whole package |
| J2 | Emulated devices: phone, old phone, Pi, GPU box, laptop, thin laptop, server — every emulated node says so | ✅ | `--emulate`; declared numbers flagged as an anomaly and `emulated` on registration |
| J3 | Whole-fleet simulation with chaos (devices die, a pooled helper dies, the hub dies) | ✅ | `docs/SIMULATION.md`: 9/9 scenarios pass |
| J4 | Real joiners on other operating systems, and a cross-OS job | ✅ Linux + Windows · 🟡 Android container | WSL-Linux hub + WSL-Linux device (real `curl \| sh`) + Windows device: a 40-item batch split Linux 29 / Windows 11, 40/40 ok. Container runs (Linux ×2, Alpine without Python, Termux) are scripted in `scripts/docker_fleet.py` for a machine with working Docker |

## I. Invisible & harmless (priorities 2 and 3)

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| I1 | Agent and everything it launches run below normal priority | ✅ | `lower_own_priority()` at start, inherited by children; test |
| I2 | Work parks while the owner is at the keyboard (unless dedicated) or the battery is under 40% | ✅ | Welfare gate |
| I3 | Model placement leaves the host a reserve: max(2 GiB, 25%) RAM, max(512 MiB, 10%) GPU | ✅ | `usable_bytes()`; tests |
| I4 | Idle nodes cost nothing: long-poll instead of busy polling; self-benchmarking at most every 10 min | ✅ | Was: a CPU benchmark every ~2 s on idle nodes |
| I5 | Logs are bounded (rotate at 5 MB) | ✅ | `--log` rotation |
| I6 | Uninstall removes only what the swarm added | ✅ | Joiners' leave line (hub files never touched) |
| I7 | Joiners never replace software the owner installed (swarm copies live in `~/.swarm`) | ✅ | llama.cpp goes to `~/.swarm/llama`; the owner's winget copy was untouched |
| I8 | Thermal awareness (back off when a phone runs hot) | ✅ | Battery ≥ 43 °C or CPU ≥ 85 °C parks work (Linux/Android sysfs; unknown elsewhere); `test_hot_device_rests` |

## A. Joining — "plug it in, click accept, done"

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| A1 | One owner action joins a device: one pasted line or one double-click | ✅ Linux, Windows · 🟡 macOS, Android, Pi | Live: VPS and WSL-Linux joined with `curl … \| sh`; Windows joined with the real `irm … \| iex` line (isolated profile). macOS/Android/Pi paths written and syntax-checked, not run on those devices |
| A2 | Joiner installs its own prerequisites, after saying what it will install | ✅ llama.cpp (Linux, Windows) · 🟡 Python auto-install | Live on Windows + Linux: plan printed first, pinned llama.cpp downloaded, verified to run, installed to `~/.swarm/llama`. A build that cannot run is removed, not left broken. Python auto-install (winget/pkg) not exercised: Python was present |
| A3 | Starts on boot, the platform's own way | ✅ Windows, Linux (systemd) · 🟡 macOS, Termux, cron | Live: Windows HKCU Run + hidden VBS watchdog; Linux systemd --user unit enabled + active |
| A4 | Every node runs the same llama.cpp build (the hub pins one) | ✅ | Found live: winget's b10615 vs release b11190 → `RPC server version mismatch`. Hub now pins a tag; joiners install exactly it into `~/.swarm/llama`; agent prefers it |
| A5 | After joining, the device never needs touching: self-update | ✅ | VPS agent swapped itself 22 s after the hub's code changed, kept its hub/node identity (code-hash compare + config carried over) |
| A6 | One-line leave that removes only agent files | ✅ | Live on Windows + Linux: after the printed leave line, no process, no Run key / unit, no agent files. Never touches a hub's `owner.key` / `hub.db` |
| A7 | Seed kit: a zip that turns a USB stick / SD card / old phone into a joiner for every OS | 🟡 | `/join/seed-kit.zip` built + tested (contents, exec bits, baked config); not yet plugged into a real stranger PC |
| A8 | A device that cannot compute can still *carry* the seed and serve it over Wi-Fi | 🟡 | `swarm-agent.pyz --seed` serves the kit + joiners on :8788; not yet run on a real phone |
| A9 | USB autorun ("plug in and it starts") | ⛔ | Every modern OS blocks it (Windows since 2011). Nearest honest version: double-click `JOIN-*` on the stick |
| A10 | Android without installing Termux first | ⛔ | Android forbids apps installing themselves. One-time Termux install, then one line |
| A11 | iPhone / iPad as a worker | ✅ via browser | Background Python is impossible on iOS; the browser worker (E8) is the honest alternative |
| A12 | Consent is never bypassed: nothing joins without its owner running something | ✅ | Tokens required off-loopback; `/invite` no longer mints tokens for strangers (bug fixed, test-pinned) |

## B. Security

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| B1 | Off-box hubs are secure by default; loopback stays open for dev | ✅ | `tests/test_auth.py`; live hub is secure |
| B2 | Owner key guards everything that runs code or reads the fleet (bags, workshop, brain admin, backups, `/v1`) | ✅ | Tests + live (CLI uses it; unauthenticated calls get 401) |
| B3 | Per-node keys for the work loop; a key never speaks for another node; revocable | ✅ | Tests + live (laptop rejoined with its key, no token) |
| B4 | Hub never on the public internet | ✅ | VPS hub bound to its Tailscale IP only; public IP refuses (checked live) |
| B5 | llama.cpp `rpc-server` (no auth by design) bound to one specific address, never 0.0.0.0 | ✅ | Validated spec; live VPS rpc-server bound to 100.81.227.46 |
| B6 | Hub never names a binary or a path on a node; agent builds every command line itself | ✅ | `tests/test_services.py` hostile-spec cases |
| B7 | Traffic encrypted in transit | ✅ via Tailscale | The hub speaks plain HTTP; Tailscale (WireGuard) is the encryption. Documented, deliberate |
| B8 | AI-written code runs only on nodes that opt in as code workers (phones in Termux are Android-sandboxed) | ✅ | `--code-worker` → `role:code-worker`; MCP tools target only that class and refuse when none is online (`test_ai_code_runs_only_on_opted_in_code_workers`) |
| B9 | Signed agent bundles | ✅ | Hub signs each update offer with HMAC(sha256(node key)); keyed nodes refuse unsigned or wrongly-signed offers (`test_updates_must_be_signed_with_the_node_key`) |

## C. Measurement & honesty (the laws)

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| C1 | Probe never raises/hangs; every number carries a trust tier | ✅ | Pre-existing law tests |
| C2 | Nothing scheduled on declared numbers; model placement uses measured free memory only, overhead labelled ESTIMATED | ✅ | Every plan carries `need_basis`; tests |
| C3 | Never claim a deployment that is not really running as planned | ✅ | Found live: a head that silently dropped its helper was reported "pooled". Now the agent reads llama.cpp's log and fails it with the reason |
| C4 | Failures are visible: failed tasks retried, then closed as failed with the error kept | ✅ | Was: errors silently stored as "done" results. `tests/test_queue_failures.py` |
| C5 | Placement learns from real runs (tokens/s per deployment feeds the next plan) | ✅ | Gateway records llama.cpp's measured tokens/s (streaming too); planner prefers the measured-fastest head that fits (`test_measured_speed_picks_the_head`) |

## D. Work — what the fleet can do

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| D1 | **Ask**: OpenAI-compatible `/v1/models`, `/v1/chat/completions`, `/v1/embeddings` | ✅ | Live: chat through the VPS hub answered from the laptop GPU; embeddings (bge-m3, 3×1024) routed to the laptop's Ollama |
| D2 | **Serve**: a GGUF model runs on the one node it fits, started on first request | ✅ | Live: Qwen3.5-4B on the laptop's Iris Xe (Vulkan), ~5.4–7 tok/s |
| D3 | **Serve pooled**: a model split across nodes by measured memory (llama.cpp RPC) | ✅ | Live: Qwen3.5-4B split laptop GPU (25 layers) + VPS CPU (7 layers) across the internet; VPS held 2 GB and computed (42 s CPU); 1.6–2.0 tok/s generation — bound by the 324 ms link, as predicted |
| D4 | Never shard what fits one node | ✅ | Planner refused on the live fleet ("fits on Anomalocaris alone") |
| D5 | Only pool nodes on the same llama.cpp build | ✅ | Planner skips mismatched builds and names them |
| D6 | Idle models unload after 30 min; memory goes back to the hosts | 🟡 | Implemented in `tick()`; not observed live |
| D7 | A failed/offline participant fails the deployment; next request re-places it | 🟡 | Unit-tested paths; not killed live |
| D8 | Ollama models on any node are routed to that node | ✅ | Live (embeddings); routing classes now actually indexed (was dead code: `NodeProfile` had no `runtimes`) |
| D9 | **Map**: the owner's Python function across every node, results in input order | ✅ | Live: 24/24 items across laptop + VPS in 4.5 s, 0 failed |
| D10 | Interactive work jumps the queue; idle nodes long-poll (no nap between polls) | ✅ | Tests (woke in < 5 s) |
| D11 | Nodes holding model layers take no batch work ("thinking nodes don't do chores") | ✅ | Test |
| D12 | Cloud fallback in `/v1`: local first, frontier API only if nothing local serves, with budget + kill switch | ✅ | Armed + enabled + not killed → forwarded, labelled `path: frontier`, daily cap (`test_cloud_lane_is_an_owner_armed_fallback`) |
| D13 | Brains hand work to small devices: `swarm_run_python` / `swarm_map` as tools (MCP) for Claude, OpenCode, Thea, local models | ✅ | `python -m swarm.mcp` (stdio JSON-RPC): status/models/chat/embed/run_python/map; tested end to end against a live hub + code worker |
| D14 | Leases renew over HTTP for long tasks | ✅ | Was a 404 (route missing); test-pinned |

## E. Devices — "everything"

| # | Device class | Status | Notes |
|---|---|---|---|
| E1 | Windows x64 laptop/desktop, Intel/AMD GPU via Vulkan | ✅ | Laptop, Iris Xe, live |
| E2 | Linux x64 (server, desktop) | ✅ | VPS (Ubuntu) and WSL (Kali, kernel 6.6, systemd) — both live |
| E3 | NVIDIA GPU box | 🟡 | Joiners install the Vulkan build (works on NVIDIA drivers); CUDA builds not wired |
| E4 | Android phone (Termux) | 🟡 | Joiner path written (pkg Python install, Termux:Boot watchdog, arm64 llama.cpp, code worker by default); emulated as `--emulate phone` in the fleet simulation. The termux-docker run is scripted (`scripts/docker_fleet.py`) but Docker Desktop on this machine cannot start (a Windows fault: every AF_UNIX socket file it creates becomes undeletable) |
| E5 | Raspberry Pi / ARM Linux | 🟡 | ubuntu-arm64 release selected by the joiner; untested |
| E6 | Mac (Metal) | 🟡 | macos-arm64/x64 release; untested |
| E7 | Carrier-only (USB stick, dead phone) | 🟡 | Seed kit (A7/A8) |
| E8 | Browser tab (iPhone, tablet, TV) | ✅ | `/worker?token=`: joins on one tap, Web Worker compute, op-routed (never gets map/chat), pauses when hidden or on low battery. Live in a real browser: joined, paused itself at 14% battery, and its primesum/hashwork/matmul results were byte-identical to the Python agents' |

## F. Operations

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| F1 | Hub always on, restarts itself | ✅ | `swarm-hub.service` on the VPS (systemd, Restart=always) |
| F2 | Dashboard: nodes, models, deployments, "+ add a device" | ✅ | `/` and `/join` (owner key, cookie after one `?key=` visit) |
| F3 | Owner CLI: status / models / chat / deploy / plan / map / join | ✅ | Used for the live run |
| F4 | Devices back off when used or low on battery; `--dedicated` for compute-only machines | ✅ logic · 🟡 phones | Welfare gate; dedicated keeps battery protection |
| F5 | Scheduled hub backups | ✅ | Successor replicas ARE continuous off-machine backups; `/api/backup` for manual copies |
| F6 | Hub shutdown is crash-free | ✅ | Found: closing sqlite under a long-poll thread was an access violation; fixed + test |
| F7 | Multi-hub federation (separate swarms sharing work) | — not a goal | One owner = one swarm. The resilience federation was meant to buy is delivered by the holographic hub (H6–H9); linking someone else's swarm is a different product |

## G. Decisions pending the owner

- **Demote AI adapter/kernel synthesis** to an experimental track; make
  "discover and drive existing engines" the primary way new hardware is
  used. (Proposed; AGENTS.md unchanged until decided.)
- **Workshop autopilot default → off.** Self-edits are now owner-only;
  whether they apply themselves by default is a law change. (Proposed.)

---

## Live run log

**2026-09-26 — hub on VPS, laptop + VPS nodes**

| Step | Result |
|---|---|
| Hub install (systemd, Tailscale-only) | up; public IP unreachable |
| VPS joins via one-liner | registered in 40 s, pinned llama.cpp installed |
| Chat, model fits laptop → planner: single | ✅ answered via hub; 5.4 tok/s (Iris Xe, Vulkan, with the test suite loading the CPU) |
| Forced split, mismatched builds | ❌→✅ caught: head silently ran alone; led to C3, A4, D5 |
| Self-update after hub code change | ✅ 22 s, identity kept |
| Forced split, matched builds (25+7 layers) | ✅ link direct but **324 ms RTT**, weights at ~1.2–1.5 MB/s → **first load ≈ 20 min** (rpc-server `--cache` makes reloads fast) |
| Pooled generation | ✅ **1.6–2.0 tok/s** (≈ two round trips per token); prompt reading ≈ 7.7 s/token over this link |
| Same model on the laptop alone | ✅ 5.4–7.0 tok/s generation, 24 tok/s prompt |
| Batch map across both nodes | ✅ 24/24 in 4.5 s |
| Embeddings via hub → laptop Ollama | ✅ 3×1024-dim in 8.6 s |
| VPS left as found | ✅ test worker stopped, 1.4 GB weight cache removed, hub kept running |

| Real Windows installer (isolated profile) | ✅ plan printed, pinned llama.cpp verified, hidden start-at-logon, registered in ~90 s (first measurement), parked while the owner typed, took a hub replica; watchdog restart ~10 s; leave line left nothing |
| Real Linux installer (WSL Kali) | ✅ systemd unit enabled + active, llama.cpp verified, registered; killed → systemd restarted it |
| Cross-OS batch (Linux hub; Linux + Windows workers) | ✅ 40/40, Linux 29 / Windows 11 |
| Browser worker in a real browser | ✅ joined; paused itself at 14% battery; results byte-identical to Python's |

**What the numbers say:** over the internet, pooling is a capacity tool — it
runs models no single device can hold, slowly. On a home LAN (~1–2 ms) the
same round trips cost ~100× less. The planner already refuses to split a
model that fits, so the owner never pays this by accident.
