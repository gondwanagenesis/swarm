# Fleet simulation report

Generated 2026-09-26 06:59 by `scripts/simulate_fleet.py` on Anomalocaris.
**9 passed, 0 failed, 0 skipped.**

Each node is a real agent process with its own home directory, declaring an
emulated device profile (`--emulate`; the numbers are declared and every such
node says so). The hub is a real hub process in secure mode. Pooled inference
uses the real llama.cpp binaries and a real GGUF model when present.

| Node | Emulated as | Role |
|---|---|---|
| thin-laptop | thin-laptop | holds_models, dedicated |
| gpu-box | gpu-box | worker |
| phone-1 | phone | code_worker |
| phone-2 | phone | code_worker |
| phone-3 | phone | code_worker |
| old-phone | old-phone | code_worker, hot |
| pi | pi | worker |
| server | server | worker |

| # | Scenario | Result | Time | Evidence |
|---|---|---|---|---|
| S1 | fleet assembles | **PASS** | 66.4s | 8/8 nodes online: Anomalocaris-emu-gpu-box, Anomalocaris-emu-old-phone, Anomalocaris-emu-phone, Anomalocaris-emu-phone, Anomalocaris-emu-phone, Anomalocaris-emu-pi, Anomalocaris-emu-server, Anomalocaris-emu-thin-laptop |
| S2 | batch map spreads across the fleet | **PASS** | 33.7s | 60/60 done on 7 nodes [10, 10, 8, 8, 8, 8, 8] |
| S4 | a hot phone rests while the others work | **PASS** | 0.0s | old-phone (50 C) did 0 tasks; others did 60 |
| S3 | two nodes die mid-batch; batch completes exactly once | **PASS** | 28.0s | killed ['pi', 'server']; 80/80 results, 0 failed |
| S5 | AI code (MCP swarm_run_python) runs only on code workers | **PASS** | 1.1s | result=332833500 on sim-phon* (code workers: phones) |
| S6 | embeddings via /v1 reach the holder | **PASS** | 50.0s | bge-m3:latest: dims [1024, 1024] computed on ['sim-thin-laptop'] |
| S7 | a model too big for its holder is pooled over the fewest helpers | **PASS** | 241.2s | Qwen3.5-4B-Q4_K_M: pooled over 2 nodes (fewest nodes that fit, largest first; accelerators hold layers first (a prior until measured)): 3820338044 B needed vs 14280766260 B usable; layer split [0, 32] from measured free memory -> answered 'Hello, there. How are you?' via ['sim-thin-laptop', 'sim-gpu-box'], 2.7 tok/s |
| S8 | a pooled helper dies; the next request re-places the model | **PASS** | 114.1s | killed gpu-box; re-placed on ['sim-thin-laptop', 'sim-phone-2']; answered 'Earth' |
| S9 | hub dies; a successor becomes the hub; the fleet follows | **PASS** | 21.8s | new hub http://127.0.0.1:51754 after 14s (epoch 2, same swarm); 5/5 nodes re-attached; owner key accepted; new batch 30/30 done |

**Note on S7.** With the accelerator-first prior, all 32 layers went to the
emulated GPU box and generation ran at 2.7 tok/s; an earlier run that kept 31
layers on the thin laptop's CPU ran at 5.4 tok/s. On this laptop the
"GPU box" is the same integrated GPU reached through an RPC hop, sharing one
memory bus with the CPU, so the prior did not pay. That is why the split
strategy is now learned: the prior runs first, the home-first alternative gets
one measured trial, then the faster measured strategy wins
(`test_the_split_strategy_is_learned_from_measured_speed`).

## Real installers on other machines (2026-09-26)

| # | Scenario | Result | Evidence |
|---|---|---|---|
| R1 | Real Windows installer (`irm … \| iex`, isolated profile) | **PASS** | plan printed; pinned llama.cpp b11191 downloaded + verified; hidden start-at-logon (HKCU Run + VBS watchdog); registered after ~90 s of first measurement; parked while the owner typed; received a hub replica |
| R2 | Windows crash recovery | **PASS** | agent killed → watchdog restarted it in ~10 s |
| R3 | Windows leave line | **PASS** | no process, no Run key, no agent files afterwards |
| R4 | Real Linux installer (WSL Kali, kernel 6.6, systemd) | **PASS** | `curl … \| sh`: llama.cpp verified, systemd --user unit enabled + active, registered |
| R5 | Linux crash recovery | **PASS** | agent killed → systemd restarted it (PID 333 → 438) |
| R6 | Cross-OS batch (hub on Linux; Linux + Windows workers) | **PASS** | 40/40 results, Linux 29 / Windows 11, 0 failed |
| R7 | Browser worker in a real browser | **PASS** | joined with one tap; paused itself at 14% battery; primesum / hashwork / matmul results byte-identical to the Python agents' |
| R8 | Containers (Linux ×2, Alpine without Python, Termux) | **NOT RUN** | scripted in `scripts/docker_fleet.py`; Docker Desktop cannot start on this machine (a Windows fault: every AF_UNIX socket file it creates becomes undeletable; usually cleared by a reboot) |

Bugs these real runs found, all fixed with regression tests: a PowerShell
precedence trap that split the Windows launcher command (the agent never
started); uninstall lines that missed agent-created files; unsafe tar
extraction (now `filter="data"`).
