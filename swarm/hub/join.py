"""The "add a device" surface: one page for the owner, one line per device.

``GET /join`` (owner) mints an invite token and shows copy-paste joiners for
every platform plus the seed kit. The joiners and the kit are token-gated:
``/join.sh``, ``/join.ps1``, ``/join/seed-kit.zip`` all refuse a missing or
expired token. What the scripts do is documented in
``swarm/agent/join_scripts.py``; nothing here runs on a device until its
owner runs it.
"""

from __future__ import annotations

import html
import time
from typing import Any

from ..agent.join_scripts import build_seed_kit, render_posix, render_powershell
from .agentbundle import build_agent_pyz

JOIN_TOKEN_TTL_S = 7 * 86400.0
KIT_TOKEN_TTL_S = 30 * 86400.0
_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"


def fleet_llama_tag(hub: Any) -> str:
    """The ONE llama.cpp build every joiner installs.

    Pooling needs matching builds (the RPC protocol changes between them),
    so the hub pins a tag the first time it is asked and keeps it — every
    device joined today, next month, or off a seed kit gets the same one.
    Override with SWARM_LLAMA_TAG. Empty string = could not pin (joiners
    then take the latest release and the planner's build check still holds).
    """
    import json
    import os
    import urllib.request

    env = os.environ.get("SWARM_LLAMA_TAG")
    if env:
        return env
    conn = hub.registry._conn
    with hub.registry._lock:
        conn.execute("CREATE TABLE IF NOT EXISTS hub_settings (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM hub_settings WHERE key='llama_tag'").fetchone()
    if row and row["value"]:
        return str(row["value"])
    try:
        req = urllib.request.Request(_RELEASES, headers={"User-Agent": "swarm-hub"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
        tag = next(
            r["tag_name"] for r in releases if any(a["name"].endswith("bin-ubuntu-x64.tar.gz") for a in r.get("assets", []))
        )
    except Exception:
        return ""
    with hub.registry._lock:
        conn.execute("INSERT OR REPLACE INTO hub_settings (key, value) VALUES ('llama_tag', ?)", (tag,))
        conn.commit()
    return str(tag)


def _send_text(handler: Any, text: str, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_join(hub: Any, handler: Any, path: str) -> None:
    q = handler._query()
    base = handler._public_base()
    if path == "/join":
        _send_text(handler, join_page(hub, base, bool(q.get("dedicated") == "1")), "text/html; charset=utf-8")
        return

    token = str(q.get("token") or "")
    record = hub.enrollment.validate(token)
    if record is None:
        _send_text(handler, "# invalid or expired invite token - ask the hub owner for a fresh /join link\n", status=403)
        return
    dedicated = q.get("dedicated") == "1"
    tag = fleet_llama_tag(hub)
    if path == "/join.sh":
        _send_text(handler, render_posix(base, token, dedicated, llama_tag=tag), "text/x-shellscript; charset=utf-8")
    elif path == "/join.ps1":
        _send_text(handler, render_powershell(base, token, dedicated, llama_tag=tag))
    elif path == "/join/seed-kit.zip":
        bundle = build_agent_pyz(config={"hub": base, "token": token, "dedicated": dedicated})
        expires = time.strftime("%Y-%m-%d", time.localtime(float(record["expires_at"])))
        data = build_seed_kit(base, token, bundle, expires, dedicated, llama_tag=tag)
        handler.send_response(200)
        handler.send_header("Content-Type", "application/zip")
        handler.send_header("Content-Disposition", 'attachment; filename="swarm-seed-kit.zip"')
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)


def join_page(hub: Any, base: str, dedicated_default: bool = False) -> str:
    tok = hub.enrollment.create(role="node", label="join-page", ttl_s=JOIN_TOKEN_TTL_S)["token"]
    kit = hub.enrollment.create(role="node", label="seed-kit", ttl_s=KIT_TOKEN_TTL_S)["token"]
    e = html.escape
    sh = f'curl -fsSL "{base}/join.sh?token={tok}" | sh'
    sh_ded = f'curl -fsSL "{base}/join.sh?token={tok}&dedicated=1" | sh'
    ps = (
        'powershell -NoProfile -ExecutionPolicy Bypass -Command '
        f'"irm \'{base}/join.ps1?token={tok}\' | iex"'
    )
    ps_ded = (
        'powershell -NoProfile -ExecutionPolicy Bypass -Command '
        f'"irm \'{base}/join.ps1?token={tok}&dedicated=1\' | iex"'
    )
    nodes = hub.registry.list_nodes()
    now = time.time()
    online = sum(1 for n in nodes if n.get("last_seen") and now - n["last_seen"] < 90)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Add a device</title>
<style>
:root{{--bg:#0f1215;--card:#171c22;--line:#2a3038;--fg:#dde2e7;--dim:#7f8d98;--acc:#3cc492;--warn:#d9a825}}
body{{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,'Segoe UI',sans-serif;margin:0;padding:24px 16px}}
main{{max-width:860px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:var(--dim);margin:0 0 24px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 18px 12px;margin-bottom:16px}}
h2{{font-size:15px;margin:0 0 8px;color:var(--acc)}}
pre{{background:#0b0e11;border:1px solid var(--line);border-radius:6px;padding:10px 12px;white-space:pre-wrap;word-break:break-all;font:12.5px/1.45 Consolas,monospace;margin:6px 0 10px;cursor:pointer}}
pre:hover{{border-color:var(--acc)}}
.dim{{color:var(--dim);font-size:13px}} ol{{padding-left:20px;margin:6px 0}} li{{margin:3px 0}}
a.btn{{display:inline-block;background:var(--acc);color:#0f1215;padding:9px 16px;border-radius:7px;text-decoration:none;font-weight:600}}
.tag{{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--warn)}}
</style></head><body><main>
<h1>Add a device to the swarm</h1>
<p class="sub">{online} of {len(nodes)} known nodes online &middot; <a href="/" style="color:var(--acc)">dashboard</a> &middot;
invite links expire in 7 days (seed kit: 30). Click a command to copy it.</p>

<section><h2>Linux, macOS, Raspberry Pi</h2>
<pre>{e(sh)}</pre>
<p class="dim">Machine that exists only to compute (closet box, GPU rig)? Use the dedicated line - it keeps working while someone is logged in:</p>
<pre>{e(sh_ded)}</pre></section>

<section><h2>Windows</h2>
<p class="dim">Paste into PowerShell or the Run box (Win+R):</p>
<pre>{e(ps)}</pre>
<p class="dim">Dedicated GPU box:</p>
<pre>{e(ps_ded)}</pre></section>

<section><h2>Android phones (old ones are great nodes)</h2>
<ol>
<li>Install <b>Termux</b> from F-Droid (the Play Store build is outdated). Optional: <b>Termux:Boot</b> so it restarts after a reboot.</li>
<li>In Termux: <code>pkg install -y python</code></li>
<li>Paste the Linux line above. Phones are joined as <i>dedicated</i> automatically - plug them into a charger; below 40% battery they rest.</li>
<li>Android settings &rarr; Apps &rarr; Termux &rarr; Battery &rarr; <i>Unrestricted</i>, so Android does not kill it.</li>
</ol>
<p class="dim">GPU/pooled-model work on a phone: <code>pkg install llama-cpp</code> gives it an RPC worker the swarm will use automatically.</p></section>

<section><h2>Seed kit - USB sticks, SD cards, and devices that cannot compute</h2>
<p class="dim">A zip with the agent and a double-click joiner for every OS, invite baked in. Copy it onto a USB stick and plug it into any PC.
Put it on an old phone (even one too slow to compute) and run <code>python swarm-agent.pyz --seed</code>: it serves the kit to every device on its Wi-Fi.
Nothing in it runs until a device's owner runs it.</p>
<p><a class="btn" href="/join/seed-kit.zip?token={e(kit)}">Download seed kit</a> <span class="tag">&nbsp;contains a 30-day invite</span></p></section>

<section><h2>Make every device's GPU count</h2>
<p class="dim">Any node with <b>llama.cpp</b> installed (winget install ggml.llamacpp &middot; brew install llama.cpp &middot; pkg install llama-cpp)
can hold part of a model too big for one machine. Put GGUF models in <code>~/.swarm/models</code> on one node; the swarm splits them across
the others by measured free memory, only when a model does not fit on one.</p></section>
</main>
<script>
document.querySelectorAll('pre').forEach(p=>p.addEventListener('click',()=>{{
  navigator.clipboard&&navigator.clipboard.writeText(p.textContent).then(()=>{{p.style.borderColor='#3cc492';setTimeout(()=>p.style.borderColor='',600)}});
}}));
</script></body></html>"""
