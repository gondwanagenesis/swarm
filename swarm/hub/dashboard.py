"""Dashboard renderer. Plain HTML, no JS, honest about trust.

Every measured number renders with its trust tier. Missing values render as
"—" (the honest None), never as a plausible-looking default.
"""

from __future__ import annotations

import html
import json
import time
from typing import List, Optional

from .queue import WorkQueue
from .registry import Registry


def _uncovered_rows(registry: Registry) -> List[str]:
    try:
        from .coverage import coverage_report

        report = coverage_report(registry)
    except Exception:
        return ['<tr><td colspan="3" class="none">coverage unavailable</td></tr>']
    rows = []
    for dev in report["uncovered"]:
        rows.append(
            '<tr><td class="mono">' + html.escape(dev["device_class"]) + "</td>"
            "<td>" + html.escape(dev["hostname"]) + "</td>"
            '<td class="warn">no proven adapter — queued for the integrator (M4)</td></tr>'
        )
    return rows


STYLE = """
body{background:#0f1215;color:#dde2e7;font:14px/1.5 'Segoe UI',system-ui,sans-serif;margin:0;padding:32px}
h1{font-size:20px;letter-spacing:.02em;border-bottom:2px solid #3cc492;padding-bottom:8px}
h2{font-size:15px;color:#7f8d98;text-transform:uppercase;letter-spacing:.12em;margin-top:32px}
table{border-collapse:collapse;width:100%;margin-top:12px}
th{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#556069;text-align:left;padding:6px 10px;border-bottom:1px solid #2a3038}
td{padding:7px 10px;border-bottom:1px solid #222830;vertical-align:top}
.mono{font-family:Consolas,monospace;font-size:12px}
.trust{font-size:10px;padding:1px 6px;border-radius:3px;letter-spacing:.06em;text-transform:uppercase}
.t-verified{background:#3cc49222;color:#3cc492}
.t-calibrated{background:#3cc49218;color:#3cc492}
.t-standard{background:#5ba3e818;color:#5ba3e8}
.t-fallback{background:#d9a82518;color:#d9a825}
.t-theoretical{background:#e46b5c18;color:#e46b5c}
.none{color:#556069;font-style:italic}
.warn{color:#d9a825}
.err{color:#e46b5c}
.meta{color:#556069;font-size:12px;margin-top:4px}
"""


def _fmt_int(v: Optional[int]) -> str:
    if v is None:
        return '<span class="none">&mdash;</span>'
    return f'<span class="mono">{v:,}</span>'


def _fmt_bytes(v: Optional[int]) -> str:
    if v is None:
        return '<span class="none">&mdash;</span>'
    gib = v / (1024**3)
    return f'<span class="mono">{gib:.1f} GiB</span>'


def _fmt_bench(value: Optional[float], unit: str, trust: str) -> str:
    if value is None:
        return '<span class="none">&mdash;</span>'
    shown = f"{value:.3f}" if abs(value) < 10 else f"{value:.1f}"
    return f'{shown} <span class="meta">{unit}</span> <span class="trust t-{trust}">{trust}</span>'


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return '<span class="none">never</span>'
    delta = max(0, time.time() - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    return f"{delta / 3600:.1f}h ago"


def _fleet_power_rows(registry: Registry) -> str:
    try:
        from .fleet_power import fleet_power

        report = fleet_power(registry)
    except Exception:
        return '<tr><td colspan="3" class="none">fleet power unavailable</td></tr>'
    rows: List[str] = []
    labels = {
        "cpu_fp32_gflops": "CPU FP32",
        "mem_bandwidth_gbps": "Memory BW",
        "mem_latency_ns": "Memory Latency",
    }
    units = {"cpu_fp32_gflops": "GFLOPS", "mem_bandwidth_gbps": "GB/s", "mem_latency_ns": "ns"}
    for kind, buckets in report.get("totals", {}).items():
        proven = buckets.get("proven", 0.0)
        fallback = buckets.get("fallback", 0.0)
        rows.append(
            "<tr><td class='mono'>%s</td>"
            "<td class='mono'>%.3f %s</td>"
            "<td class='mono'>%.3f %s</td></tr>"
            % (
                html.escape(str(labels.get(kind, kind))),
                float(proven),
                units.get(kind, ""),
                float(fallback),
                units.get(kind, ""),
            )
        )
    return "".join(rows) or '<tr><td colspan="3" class="none">no benchmarks yet</td></tr>'


def render_dashboard(registry: Registry, queue: Optional[WorkQueue] = None) -> str:
    nodes = registry.list_nodes()
    links = registry.list_links()
    anomalies = registry.recent_anomalies(20)
    bags = queue.open_bags() if queue is not None else []

    node_rows: List[str] = []
    for node in nodes:
        detail = registry.node_detail(node["node_id"]) or {}
        try:
            profile = json.loads(detail.get("profile_json") or "{}")
        except ValueError:
            profile = {}
        try:
            capability = json.loads(detail.get("capability_json") or "{}")
        except ValueError:
            capability = {}
        benches = registry.latest_benches(node["node_id"])

        cpu = profile.get("cpu") or {}
        memory = profile.get("memory") or {}
        devices = profile.get("devices") or []
        dev_names = (
            ", ".join(html.escape(d.get("name") or "?") for d in devices)
            or '<span class="none">none found</span>'
        )
        floor = capability.get("max_floor")
        floor_txt = (
            f'<span class="mono">F{floor}</span>'
            if floor is not None
            else '<span class="none">&mdash;</span>'
        )
        bench_cells = (
            "<br>".join(
                f"{html.escape(b['name'])}: {_fmt_bench(b['value'], b['unit'], b['trust'])}" for b in benches
            )
            or '<span class="none">no benchmarks yet</span>'
        )
        n_anomalies = sum(1 for a in anomalies if a.get("node_id") == node["node_id"])

        node_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(node['hostname'])}</strong><br>"
            f'<span class="mono" style="color:#556069">{html.escape(node["node_id"][:8])}</span></td>'
            f"<td>{html.escape(node['os'])} / {html.escape(node['arch'])}</td>"
            f"<td>{_fmt_int(cpu.get('logical_cores'))} cores<br>{_fmt_bytes(memory.get('free_bytes'))} free</td>"
            f"<td>{dev_names}</td>"
            f"<td>{floor_txt}</td>"
            f"<td>{bench_cells}</td>"
            f"<td>{_ago(node['last_seen'])}"
            + (f' <span class="warn">({n_anomalies} anomalies)</span>' if n_anomalies else "")
            + "</td>"
            "</tr>"
        )

    link_rows: List[str] = []
    for link in links:
        bw = link.get("bandwidth_bps")
        bw_txt = f"{bw / 1e9:.2f} Gb/s" if bw else "&mdash;"
        rtt = link.get("rtt_p50_ms")
        rtt_txt = f"{rtt:.2f} ms (p95 {link.get('rtt_p95_ms'):.2f})" if rtt is not None else "&mdash;"
        direct = link.get("direct")
        if direct == 1:
            direct_txt = "direct"
        elif direct == 0:
            direct_txt = "relayed?"
        else:
            direct_txt = "&mdash;"
        link_rows.append(
            f'<tr><td class="mono">{html.escape(str(link["src_node"]))[:8]} &rarr; {html.escape(str(link["dst_node"]))}</td>'
            f'<td class="mono">{rtt_txt}</td>'
            f'<td class="mono">{bw_txt}</td>'
            f'<td>{direct_txt} <span class="trust t-{link["trust"]}">{link["trust"]}</span></td></tr>'
        )

    anomaly_rows = [
        f'<tr><td class="meta">{_ago(a["at"])}</td><td class="mono">{html.escape(str(a["node_id"] or ""))[:8]}</td>'
        f'<td class="{"err" if a["severity"] == "error" else "warn"}">{html.escape(a["source"])}</td>'
        f"<td>{html.escape(a['message'][:200])}</td></tr>"
        for a in anomalies
    ]

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Swarm</title>
<meta http-equiv="refresh" content="15">
<style>{STYLE}</style></head><body>
<h1>Swarm &mdash; measured, not declared</h1>
<div class="meta">values shown with their trust tier; &mdash; means "we could not measure it" &middot; <a href="/api/fleet-power" style="color:#3cc492">/api/fleet-power</a></div>
<h2>Fleet Power ({len(nodes)} nodes)</h2>
<table><tr><th>Measure</th><th>Proven total</th><th>Fallback-tier total</th></tr>
{_fleet_power_rows(registry)}
</table>
<h2>Nodes ({len(nodes)})</h2>
<table><tr><th>Node</th><th>OS / Arch</th><th>CPU / Free mem</th><th>Devices</th><th>Tower</th><th>Benchmarks</th><th>Seen</th></tr>
{
        "".join(node_rows)
        or '<tr><td colspan="7" class="none">No nodes registered. Start an agent: python -m swarm.agent.daemon --hub http://&lt;this&gt;:8777</td></tr>'
    }
</table>
<h2>Links ({len(links)})</h2>
<table><tr><th>Path</th><th>RTT p50</th><th>Bandwidth</th><th>Topology</th></tr>
{"".join(link_rows) or '<tr><td colspan="4" class="none">No link measurements yet</td></tr>'}
</table>
<h2>Bags ({len(bags)})</h2>
<table><tr><th>Bag</th><th>Op</th><th>Done</th><th>Queued</th><th>Leased</th><th>Total</th></tr>
{
        "".join(
            '<tr><td class="mono">'
            + html.escape(b["bag_id"][:12])
            + '</td><td class="mono">'
            + html.escape(b["op"])
            + "</td>"
            '<td class="mono">' + str(b["done"]) + '</td><td class="mono">' + str(b["queued"]) + "</td>"
            '<td class="mono">' + str(b["leased"]) + '</td><td class="mono">' + str(b["total"]) + "</td></tr>"
            for b in bags
        )
        or '<tr><td colspan="6" class="none">No open bags. Submit: POST /api/bag/submit</td></tr>'
    }
</table>
<h2>Uncovered devices</h2>
<table><tr><th>Class</th><th>Host</th><th>Status</th></tr>
{
        "".join(_uncovered_rows(registry))
        or '<tr><td colspan="3" class="none">all sensed devices have proven adapters (or none sensed yet)</td></tr>'
    }
</table>
<h2>Anomalies</h2>
<table><tr><th>When</th><th>Node</th><th>Source</th><th>Message</th></tr>
{"".join(anomaly_rows) or '<tr><td colspan="4" class="none">None recorded</td></tr>'}
</table>
</body></html>"""
