# CLAUDE.md — pointers for AI builders in this repo

Read `AGENTS.md` first. It is the law; this file is the orientation.

## The one-line contract

Measurements only. Nothing enters the schedulable pool by declaration.
`None` is better than a plausible-looking lie.

## Non-negotiables while editing

- `swarm/core`, `swarm/probe`, `swarm/bench`, `swarm/agent`, `swarm/transport`
  import **Python standard library only**. `scripts/check_stdlib_imports.py`
  enforces this in CI; it will fail your PR.
- `swarm/probe` code **never raises and never hangs**. Every collector returns
  partial data + `Anomaly` records. Every subprocess goes through
  `probe/_proc.run_bounded`.
- `NodeProfile` and `NodeCapability` have zero overlapping field names. Keep
  it that way — there is a test (`test_models.py`) that asserts it.
- A `NodeCapability` with `trust=VERIFIED` cannot be constructed without a
  `benchmark_run_id`.
- Every benchmark result carries a `MeasurementTrust` tier. The pure-Python
  fallbacks in `swarm/bench/fallback.py` are always `FALLBACK` — a number
  whose instrument you distrust slightly is honest; an untagged number is not.
- Windows: do not use `wmic` (absent on Windows 11 ≥ ~22H2; verified gone on
  build 26100). Use PowerShell CIM (`Get-CimInstance`) and `ctypes` fallbacks.
- Windows: `GlobalMemoryStatusEx` exists in ctypes; `Win32_VideoController.AdapterRAM`
  is a signed 32-bit clamp — values `>= 0xFFFFF000` mean ">4 GB" and must be
  reported as `None`.
- RAPL energy counters wrap at `max_energy_range_uj` (~262 J measured, not
  2^32). Take the modulo on deltas; never report a negative delta.

## Layout

| Path | Role | Deps |
|---|---|---|
| `swarm/core` | models, serde, identity | stdlib |
| `swarm/probe` | capability tower + collectors + orchestrator | stdlib |
| `swarm/bench` | fallback calibration benchmarks | stdlib |
| `swarm/agent` | node daemon | stdlib |
| `swarm/transport` | link measurement | stdlib |
| `swarm/hub` | registry (sqlite3) + server (http.server) + dashboard | stdlib today; FastAPI extras reserved in pyproject |
| `swarm/integrator` | M4 placeholder — do not build before M2/M3 | — |
| `contracts/` | home of capability contracts + known-good/known-bad (M4) | — |
| `tests/` | pytest, dev-only dependency | pytest |

## Verify before pushing

```sh
python -m pytest tests/ -x --tb=short
python scripts/check_stdlib_imports.py swarm/core swarm/probe swarm/bench swarm/agent swarm/transport
python -m compileall -q swarm
python scripts/demo.py          # full loop: hub + agent + probe + bench + dashboard
```
