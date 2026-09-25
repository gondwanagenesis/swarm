"""The browser worker: any device with a web browser can be a node.

iPhones, tablets, smart TVs, a friend's laptop — anything that can open a URL
and cannot (or should not) run Python. The owner opens
``http://<hub>:8777/worker?token=<invite>`` on the device and taps **Start**:
that tap is the consent. Nothing runs before it.

What it does, honestly scoped:

- Registers as a node (``os: browser``) with an explicit op list —
  ``primesum``, ``hashwork``, ``matmul`` — so the hub routes it only work it
  can finish (op routing, ``queue._op_filter``). It never receives ``map``,
  ``chat`` or ``embed`` work.
- Computes in a Web Worker, so the page stays responsive, and produces results
  byte-identical to the Python ops (same algorithms; ``matmul`` uses BigInt
  for the counter-based generator), so content addressing and idempotency
  hold across tiers.
- Politeness: works only while the tab is visible (unless the owner ticks
  "keep working in the background") and pauses when a battery reports <40%
  and not charging.
- Long-polls like every other node; a closed tab is just a node whose lease
  expires — the work regrows elsewhere.
"""

from __future__ import annotations

WORKER_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Swarm browser node</title>
<style>
:root{--bg:#0f1215;--card:#171c22;--line:#2a3038;--fg:#dde2e7;--dim:#7f8d98;--acc:#3cc492;--warn:#d9a825}
body{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif;margin:0;padding:24px 16px}
main{max-width:560px;margin:0 auto}
h1{font-size:22px;margin:0 0 6px} p{color:var(--dim)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:16px 0}
button{background:var(--acc);color:#0f1215;border:0;border-radius:10px;padding:14px 22px;font-size:17px;font-weight:700;width:100%}
button.stop{background:#2a3038;color:var(--fg)}
.row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #222830}
.row span:last-child{font-family:ui-monospace,Consolas,monospace}
label{display:block;margin-top:12px;color:var(--dim);font-size:14px}
.warn{color:var(--warn)}
</style></head><body><main>
<h1>Swarm browser node</h1>
<p>Tapping Start lets this device lend spare cycles to your swarm while this page is open.
It only runs small, checkable math jobs, backs off on low battery, and stops the moment you close the tab.</p>
<div class="card">
  <button id="go">Start contributing</button>
  <label><input type="checkbox" id="bg"> keep working while this tab is in the background</label>
</div>
<div class="card" id="stats">
  <div class="row"><span>state</span><span id="state">not started</span></div>
  <div class="row"><span>node</span><span id="node">&mdash;</span></div>
  <div class="row"><span>tasks done</span><span id="done">0</span></div>
  <div class="row"><span>last op</span><span id="last">&mdash;</span></div>
</div>
<p id="err" class="warn"></p>
</main>
<script>
"use strict";
const OPS = ["primesum", "hashwork", "matmul"];
const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
};
const params = new URLSearchParams(location.search);
const token = params.get("token") || "";
let nodeId = store.get("swarm_node_id");
if (!nodeId) {
  nodeId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  store.set("swarm_node_id", nodeId);
}
let nodeKey = store.get("swarm_node_key:" + location.host) || "";
let running = false, done = 0;

// ---- the math, in a Web Worker (identical results to swarm/agent/ops.py) ----
const workerSrc = `
const M64 = (1n << 64n) - 1n, GOLDEN = 0x9E3779B97F4A7C15n, MIXA = 0xBF58476D1CE4E5B9n, MIXB = 0x94D049BB133111EBn;
function splitmix(x){ let z=(x+GOLDEN)&M64; z=((z^(z>>30n))*MIXA)&M64; z=((z^(z>>27n))*MIXB)&M64; return z^(z>>31n); }
function element(seed, stream, index){
  const key=(BigInt(seed)*0x2545F4914F6CDD1Dn + BigInt(stream)*0x9E3779B97F4A7C15n + BigInt(index)) & M64;
  return Number(splitmix(key) % 17n) - 8;
}
async function sha256hex(bytes){ const d=await crypto.subtle.digest("SHA-256", bytes); return [...new Uint8Array(d)].map(b=>b.toString(16).padStart(2,"0")).join(""); }
async function primesum(p){ const n=parseInt(p.n ?? 5000); if(n<2) return {n, count:0};
  let count=1; for(let c=3;c<n;c+=2){ const lim=Math.floor(Math.sqrt(c)); let comp=false; for(let d=3; d<=lim; d+=2){ if(c%d===0){comp=true;break;} } if(!comp) count++; }
  return {n, count}; }
async function hashwork(p){ const seed=String(p.seed ?? "swarm"), rounds=parseInt(p.rounds ?? 20000);
  let d=new TextEncoder().encode(seed); for(let i=0;i<rounds;i++){ d=new Uint8Array(await crypto.subtle.digest("SHA-256", d)); }
  return {seed, rounds, digest:[...d].map(b=>b.toString(16).padStart(2,"0")).join("")}; }
async function matmul(p){
  const seed=parseInt(p.seed ?? 0); const m=Math.max(1,parseInt(p.m ?? 8)); const k=Math.max(1,parseInt(p.k ?? p.m ?? 8)); const n=Math.max(1,parseInt(p.n ?? p.m ?? 8));
  const A=[],B=[]; for(let r=0;r<m;r++){ const row=[]; for(let c=0;c<k;c++) row.push(element(seed,1,r*k+c)); A.push(row); }
  for(let r=0;r<k;r++){ const row=[]; for(let c=0;c<n;c++) row.push(element(seed,2,r*n+c)); B.push(row); }
  const C=[]; for(let i=0;i<m;i++){ const row=new Array(n).fill(0); for(let t=0;t<k;t++){ const a=A[i][t]; for(let j=0;j<n;j++) row[j]+=a*B[t][j]; } C.push(row); }
  let total=0, trace=0; C.forEach((row,i)=>{ row.forEach(v=>total+=v); if(i<row.length) trace+=row[i]; });
  const checksum=await sha256hex(new TextEncoder().encode(C.map(r=>r.join(",")).join(";")));
  const maxReturn=parseInt(p.max_return ?? 1024);
  return {op:"matmul", m, k, n, seed, checksum, trace, sum:total, matrix: m*n<=maxReturn ? C : null,
          tier:"browser_js", device:"browser", backend: (self.navigator && navigator.userAgent || "browser").slice(0,80), degraded_from:[]};
}
const OPS={primesum, hashwork, matmul};
onmessage = async (e) => { const t=e.data; const t0=performance.now();
  try { const payload = await OPS[t.op](t.params||{}); postMessage({ok:true, payload, duration_s:(performance.now()-t0)/1000}); }
  catch(err){ postMessage({ok:false, payload:{error:String(err).slice(0,200)}, duration_s:(performance.now()-t0)/1000}); } };
`;
const worker = new Worker(URL.createObjectURL(new Blob([workerSrc], {type: "text/javascript"})));
function runInWorker(task) {
  return new Promise((resolve) => { worker.onmessage = (e) => resolve(e.data); worker.postMessage({op: task.op, params: task.params}); });
}

// ---- talking to the hub ----
async function post(path, body, timeoutMs) {
  const ctrl = new AbortController(); const timer = setTimeout(() => ctrl.abort(), timeoutMs || 15000);
  const headers = {"Content-Type": "application/json"};
  if (nodeKey) { headers["X-Swarm-Node"] = nodeId; headers["X-Swarm-Node-Key"] = nodeKey; }
  try {
    const r = await fetch(path, {method: "POST", headers, body: JSON.stringify(body), signal: ctrl.signal});
    const data = await r.json().catch(() => ({}));
    if (r.status === 409 && data.moved_to) { $("err").textContent = "the hub moved to " + data.moved_to + " - open the worker page there"; running = false; }
    return {status: r.status, data};
  } catch (e) { return {status: 0, data: {}}; } finally { clearTimeout(timer); }
}
async function register() {
  const ua = navigator.userAgent || "browser";
  const mem = navigator.deviceMemory ? Math.round(navigator.deviceMemory * 1073741824) : null;
  const body = {
    profile: {node_id: nodeId, hostname: "browser-" + (ua.match(/iPhone|iPad|Android|Macintosh|Windows|Linux|CrOS|TV/) || ["web"])[0] + "-" + nodeId.slice(0, 4),
              os: "browser", arch: (navigator.platform || "web").slice(0, 32), memory: {free_bytes: mem, total_bytes: mem}},
    capability: {}, benchmarks: [], token, inference: {runtimes: ["browser_js"], models: []},
    ops: OPS, can_hub: false, dedicated: false, role: "node",
  };
  const r = await post("/api/register", body, 20000);
  if (r.status === 200 && r.data.ok) {
    if (r.data.node_key) { nodeKey = r.data.node_key; store.set("swarm_node_key:" + location.host, nodeKey); }
    return true;
  }
  if (r.status === 401 && nodeKey) { nodeKey = ""; return register(); }
  $("err").textContent = r.status === 403 ? "this invite link is invalid or expired - ask for a fresh one" : "cannot reach the hub (" + r.status + ")";
  return false;
}
async function politeToWork() {
  if (!$("bg").checked && document.visibilityState !== "visible") return "paused: tab hidden";
  if (navigator.getBattery) {
    try { const b = await navigator.getBattery(); if (!b.charging && b.level < 0.4) return "paused: battery " + Math.round(b.level * 100) + "%"; } catch (e) {}
  }
  return null;
}
async function loop() {
  let lastBeat = 0;
  while (running) {
    const why = await politeToWork();
    if (why) { $("state").textContent = why; await new Promise(r => setTimeout(r, 3000)); continue; }
    if (Date.now() - lastBeat > 30000) { const hb = await post("/api/heartbeat", {node_id: nodeId}); lastBeat = Date.now(); if (hb.status === 401) await register(); }
    $("state").textContent = "waiting for work";
    const pulled = await post("/api/tasks/pull", {node_id: nodeId, wait_s: 20}, 40000);
    if (pulled.status === 401) { await register(); continue; }
    const tasks = (pulled.data && pulled.data.tasks) || [];
    if (!tasks.length) { if (pulled.status !== 200) await new Promise(r => setTimeout(r, 3000)); continue; }
    const results = [];
    for (const t of tasks) {
      $("state").textContent = "computing " + t.op;
      const out = await runInWorker(t);
      results.push({bag_id: t.bag_id, seq: t.seq, idem_key: t.idem_key, payload: out.payload, duration_s: out.duration_s, ok: out.ok});
      $("last").textContent = t.op + " (" + out.duration_s.toFixed(2) + " s)";
    }
    await post("/api/tasks/complete", {node_id: nodeId, results}, 20000);
    done += results.length; $("done").textContent = String(done);
  }
  $("state").textContent = "stopped";
}
$("go").addEventListener("click", async () => {
  if (running) { running = false; $("go").textContent = "Start contributing"; $("go").className = ""; return; }
  $("err").textContent = ""; $("state").textContent = "joining...";
  if (!(await register())) { $("state").textContent = "not joined"; return; }
  $("node").textContent = nodeId.slice(0, 8);
  running = true; $("go").textContent = "Stop"; $("go").className = "stop";
  loop();
});
</script></body></html>"""


def worker_page() -> str:
    return WORKER_PAGE
