"""swarm.integrator — M4. AI adapter synthesis + contract gate. Hub-side only.

Nothing ships here at M1. The adapter registry table already exists in
swarm.hub.registry with the full provenance chain (content-hash identity,
authored_by, probe_evidence_hash, gate_run_id, exemplar_id) so that M4 lands
on prepared ground. Do not build this before M2/M3 are solid: build order is
sacred (AGENTS.md).
"""
