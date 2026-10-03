#!/usr/bin/env python3
"""
server.py — Ferma markazi (controller).

Vazifasi:
  * inventory.json dagi barcha PC larni kuzatadi (online/offline, resurslar)
  * bitta yoki bir nechta PC ga buyruq tarqatadi (parallel)
  * natijalarni results/ papkasiga saqlaydi
  * brauzerda tez boshqarish uchun yengil dashboard beradi

Dashboard sukut bo'yicha faqat 127.0.0.1 da ochiladi (xavfsizlik).
Ishga tushirish:
    python3 server.py            # http://127.0.0.1:8790
    python3 server.py --bind 0.0.0.0   # Tailscale orqali boshqa PC dan ko'rish
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import farmlib as fl

VERSION = "0.1.0"
CFG = fl.load_config()
TOKEN = CFG["token"]                       # admin token — to'liq boshqaruv
WORKER_TOKEN = CFG.get("worker_token") or TOKEN   # ishchilar uchun cheklangan token
AGENT_PORT = int(CFG["agent_port"])

_lock = threading.Lock()
_status_cache: dict[str, dict] = {}
_cache_ts = 0.0

# --- Sessiyalar va login himoyasi ------------------------------------------
SESSION_TTL = 24 * 3600
LOGIN_MAX_FAILS = 5          # shu nechta xatodan keyin blok
LOGIN_WINDOW = 300.0         # xatolar hisoblanadigan oyna (s)
LOGIN_BLOCK = 300.0          # blok davomiyligi (s)

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()
_login_fails: dict[str, list[float]] = {}


def new_session(ip: str) -> str:
    sid = secrets.token_urlsafe(24)
    with _sessions_lock:
        _sessions[sid] = {"ts": time.time(), "ip": ip}
    return sid


def session_valid(sid: str | None) -> bool:
    if not sid:
        return False
    with _sessions_lock:
        s = _sessions.get(sid)
        if not s:
            return False
        if time.time() - s["ts"] > SESSION_TTL:
            _sessions.pop(sid, None)
            return False
        s["ts"] = time.time()
        return True


def drop_session(sid: str | None) -> None:
    if sid:
        with _sessions_lock:
            _sessions.pop(sid, None)


def login_blocked(ip: str) -> float:
    """Qolgan blok sekundlari (0 = bloklanmagan)."""
    now = time.time()
    with _sessions_lock:
        fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
        _login_fails[ip] = fails
        if len(fails) >= LOGIN_MAX_FAILS:
            return max(0.0, LOGIN_BLOCK - (now - fails[-1]))
    return 0.0


def record_login_fail(ip: str) -> None:
    with _sessions_lock:
        _login_fails.setdefault(ip, []).append(time.time())


def clear_login_fails(ip: str) -> None:
    with _sessions_lock:
        _login_fails.pop(ip, None)


def node_role(name: str) -> str:
    """Node roli: config.node_roles yoki sukut (controller/worker)."""
    roles = CFG.get("node_roles") or {}
    if name in roles:
        return roles[name]
    return "worker"


def admin_password_hash() -> str:
    """config.json dan har safar yangi hash o'qiladi (parol server qayta
    ishga tushmasdan ham yangilanadi)."""
    try:
        with open(fl.CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("admin_password") or ""
    except Exception:
        return CFG.get("admin_password") or ""


# ----------------------------------------------------------------------------
# Inventory
# ----------------------------------------------------------------------------
def load_inventory() -> dict:
    if not os.path.exists(fl.INVENTORY_PATH):
        return {"nodes": [], "updated_at": None}
    with open(fl.INVENTORY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_inventory(inv: dict) -> None:
    inv["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(fl.INVENTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(inv, f, indent=2, ensure_ascii=False)


def active_nodes() -> list[dict]:
    inv = load_inventory()
    return [n for n in inv.get("nodes", []) if n.get("ip")]


# ----------------------------------------------------------------------------
# Kuzatuv (monitoring)
# ----------------------------------------------------------------------------
def probe_node(node: dict, timeout: float = 4.0) -> dict:
    ip = node["ip"]
    port = node.get("agent_port") or AGENT_PORT
    url = f"http://{ip}:{port}/info"
    base = {
        "name": node.get("name", ip), "ip": ip, "os": node.get("os", "?"),
        "role": node_role(node.get("name", ip)),
    }
    t0 = time.time()
    try:
        res = fl.http_json(url, token=WORKER_TOKEN, timeout=timeout)
        info = res.get("info", {})
        base.update({
            "online": True,
            "latency_ms": round((time.time() - t0) * 1000),
            "host": info.get("host"), "cpu": info.get("cpu_model"),
            "cores": info.get("cpu_cores"), "ram_gb": info.get("ram_gb"),
            "gpus": info.get("gpus", []), "disk": info.get("disk", {}),
            "uptime_s": info.get("uptime_s"),
        })
    except Exception as e:
        base.update({"online": False, "error": type(e).__name__,
                     "latency_ms": round((time.time() - t0) * 1000)})
    return base


def all_status(force: bool = False, ttl: float = 5.0) -> list[dict]:
    global _status_cache, _cache_ts
    with _lock:
        if not force and (time.time() - _cache_ts) < ttl and _status_cache:
            return list(_status_cache.values())
    nodes = active_nodes()
    result: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(nodes)))) as ex:
        futs = {ex.submit(probe_node, n): n for n in nodes}
        for fut in as_completed(futs):
            try:
                st = fut.result()
            except Exception as e:
                n = futs[fut]
                st = {"name": n.get("name"), "ip": n.get("ip"), "online": False,
                      "error": f"{type(e).__name__}"}
            result[st["name"]] = st
    # pull (teskari) agentlarni ham qo'shamiz
    for p in pull_status():
        result.setdefault(p["name"], p)
    # bir xil IP li qurilmalarni birlashtiramiz (pull ustuvor)
    pull_ips = {st.get("ip") for st in result.values()
                if st.get("mode") == "pull" and st.get("online")}
    for nm, st in list(result.items()):
        if st.get("mode") != "pull" and st.get("ip") in pull_ips:
            del result[nm]
    # qulay nomlar (alias) ni qo'llaymiz
    al = fl.load_config().get("aliases", {}) or {}
    final: dict[str, dict] = {}
    for st in result.values():
        st.setdefault("raw_name", st["name"])
        st["name"] = al.get(st["name"], st["name"])
        st["role"] = node_role(st["name"])
        final[st["name"]] = st
    result = final
    with _lock:
        _status_cache = result
        _cache_ts = time.time()
    return list(result.values())


# ----------------------------------------------------------------------------
# Pull (teskari) agentlar — o'zlari markazga ulanadi (NAT/Tailscale'siz ishlaydi)
# ----------------------------------------------------------------------------
_pull_lock = threading.Lock()
_pull_agents: dict[str, dict] = {}     # nom -> {info, last_seen, queue}
_pull_results: dict[str, dict] = {}    # job_id -> {nom: natija}
_pull_events: dict[str, threading.Event] = {}
_agent_reports: dict[str, dict] = {}   # nom -> {ixtiyoriy hisobot maydonlari}


def agent_report_set(name: str, payload: dict) -> None:
    with _pull_lock:
        report = dict(payload)
        report["received_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _agent_reports[name] = report


def agent_reports() -> dict[str, dict]:
    with _pull_lock:
        return dict(_agent_reports)


def pull_hello(name: str, info: dict) -> None:
    with _pull_lock:
        a = _pull_agents.get(name)
        if a is None:
            a = {"queue": queue.Queue()}
            _pull_agents[name] = a
            print(f"  ★ pull-agent qo'shildi: {name} ({info.get('os')})", flush=True)
        a["info"] = info
        a["last_seen"] = time.time()


def pull_poll(name: str, wait: float) -> dict | None:
    with _pull_lock:
        a = _pull_agents.get(name)
        if a is None:
            # birinchi murojaat — avtomatik ro'yxatga olamiz
            a = {"queue": queue.Queue(), "info": {}, "last_seen": time.time()}
            _pull_agents[name] = a
            print(f"  ★ pull-agent qo'shildi: {name}", flush=True)
        a["last_seen"] = time.time()
        q = a["queue"]
    try:
        job = q.get(timeout=wait)
        print(f"  [{time.strftime('%H:%M:%S')}] → {name} ga vazifa berildi: {job.get('id')}", flush=True)
        return job
    except queue.Empty:
        return None


def pull_result(name: str, job_id: str, result: dict) -> None:
    with _pull_lock:
        _pull_results.setdefault(job_id, {})[name] = result
        ev = _pull_events.get(job_id)
    print(f"  [{time.strftime('%H:%M:%S')}] ← {name} dan natija keldi: {job_id} (ok={result.get('ok')})", flush=True)
    if ev:
        ev.set()


def apply_alias(name: str) -> str:
    al = fl.load_config().get("aliases", {}) or {}
    return al.get(name, name)


def pull_status() -> list[dict]:
    now = time.time()
    al = fl.load_config().get("aliases", {}) or {}
    with _pull_lock:
        out = []
        for name, a in _pull_agents.items():
            info = a.get("info") or {}
            out.append({
                "name": al.get(name, name), "raw_name": name,
                "ip": info.get("tailscale_ip") or "pull", "os": info.get("os", "?"),
                "role": node_role(name), "mode": "pull",
                "online": (now - a.get("last_seen", 0)) < 90,
                "last_seen_s": round(now - a.get("last_seen", 0)),
                "host": info.get("host"), "cpu": info.get("cpu_model"),
                "cores": info.get("cpu_cores"), "ram_gb": info.get("ram_gb"),
                "gpus": info.get("gpus", []), "disk": info.get("disk", {}),
                "uptime_s": info.get("uptime_s"),
            })
        return out


# ----------------------------------------------------------------------------
# Vazifa tarqatish
# ----------------------------------------------------------------------------
def dispatch(payload: dict) -> dict:
    targets = payload.get("nodes") or "all"
    cmd = payload.get("cmd")
    if not cmd:
        return {"ok": False, "error": "'cmd' majburiy"}

    statuses = all_status()
    if targets == "all":
        chosen = [n for n in statuses if n.get("online")]
    else:
        chosen = [n for n in statuses
                  if n.get("online") and (n.get("name") in targets
                                          or n.get("raw_name") in targets)]

    if not chosen:
        return {"ok": False, "error": "online ishchi topilmadi"}

    job = {
        "id": uuid.uuid4().hex[:10],
        "cmd": cmd,
        "timeout": payload.get("timeout") or 600,
        "files": payload.get("files") or {},
        "collect": payload.get("collect") or [],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "targets": [n["name"] for n in chosen],
        "results": {},
    }
    job_payload = {"id": job["id"], "cmd": cmd, "timeout": job["timeout"],
                   "files": job["files"], "collect": job["collect"]}

    pull_nodes = [n for n in chosen if n.get("mode") == "pull"]
    http_nodes = [n for n in chosen if n.get("mode") != "pull"]

    # 1) pull agentlarga navbatga qo'yamiz
    if pull_nodes:
        with _pull_lock:
            _pull_events[job["id"]] = threading.Event()
            for n in pull_nodes:
                a = _pull_agents.get(n.get("raw_name") or n["name"])
                if a:
                    a["queue"].put(job_payload)
                    print(f"  [{time.strftime('%H:%M:%S')}] ↳ {n['name']} navbatiga qo'yildi: {job['id']}", flush=True)
                else:
                    print(f"  ⚠ {n['name']} uchun navbat topilmadi!", flush=True)

    # 2) listen (HTTP) agentlarga to'g'ridan-to'g'ri yuboramiz
    def call(node):
        port = node.get("agent_port") or AGENT_PORT
        url = f"http://{node['ip']}:{port}/run"
        try:
            return node["name"], fl.http_json(
                url, method="POST", token=WORKER_TOKEN,
                payload={"cmd": cmd, "timeout": job["timeout"],
                         "files": job["files"], "collect": job["collect"]},
                timeout=job["timeout"] + 30,
            )
        except Exception as e:
            return node["name"], {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if http_nodes:
        with ThreadPoolExecutor(max_workers=len(http_nodes)) as ex:
            for fut in as_completed([ex.submit(call, n) for n in http_nodes]):
                name, res = fut.result()
                job["results"][name] = res

    # 3) pull natijalarini kutamiz
    # 3) pull natijalarini kutamiz — BARCHA agentlar javob berguncha
    if pull_nodes:
        expect = {(n.get("raw_name") or n["name"]) for n in pull_nodes}
        deadline = time.time() + job["timeout"] + 30
        while time.time() < deadline:
            with _pull_lock:
                have = set(_pull_results.get(job["id"], {}).keys())
            if expect.issubset(have):
                break
            time.sleep(0.4)
        with _pull_lock:
            got = dict(_pull_results.get(job["id"], {}))
        print(f"  [{time.strftime('%H:%M:%S')}] natijalar: {list(got.keys())}", flush=True)
        for n in pull_nodes:
            raw = n.get("raw_name") or n["name"]
            job["results"][n["name"]] = (got.get(raw) or got.get(n["name"])
                                         or {"ok": False, "error": "javob kelmadi (timeout)"})

    save_job(job)
    ok = sum(1 for r in job["results"].values() if r.get("ok"))
    job["summary"] = {"ok": ok, "failed": len(job["results"]) - ok}
    return {"ok": True, "job": job}


def find_node(name: str) -> dict | None:
    for n in all_status():
        if n.get("name") == name or n.get("raw_name") == name:
            return n
    return None


def quick_run(node: dict, cmd: str, collect: list[str] | None = None,
              timeout: float = 25.0) -> dict:
    """Bitta tugunga tez vazifa; natija fayllarga yozilmaydi."""
    jid = uuid.uuid4().hex[:10]
    payload = {"id": jid, "cmd": cmd, "timeout": timeout, "files": {},
               "collect": collect or []}
    if node.get("mode") == "pull":
        raw = node.get("raw_name") or node["name"]
        with _pull_lock:
            a = _pull_agents.get(raw)
            if not a:
                return {"ok": False, "error": "agent topilmadi"}
            _pull_events[jid] = threading.Event()
        a["queue"].put(payload)
        with _pull_lock:
            ev = _pull_events.get(jid)
        if ev:
            ev.wait(timeout=timeout + 15)
        with _pull_lock:
            got = dict(_pull_results.get(jid, {}))
        return (got.get(raw) or got.get(node["name"])
                or {"ok": False, "error": "javob kelmadi (timeout)"})
    port = node.get("agent_port") or AGENT_PORT
    try:
        return fl.http_json(f"http://{node['ip']}:{port}/run", method="POST",
                            token=WORKER_TOKEN, payload=payload, timeout=timeout + 15)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# Ekrandan kadr olish buyrug'i — mavjud vositaga qarab tanlanadi
SCREEN_CMD = (
    'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"; '
    'export DISPLAY="${DISPLAY:-:0}"; '
    'export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"; '
    'rm -f shot.jpg; '
    'for m in grim gnome-screenshot spectacle ffmpeg import; do '
    '  command -v $m >/dev/null 2>&1 || continue; '
    '  case $m in '
    '    grim) grim -t jpeg -q 70 shot.jpg 2>/dev/null;; '
    '    gnome-screenshot) gnome-screenshot -f shot.jpg 2>/dev/null;; '
    '    spectacle) spectacle -b -n -o shot.jpg 2>/dev/null;; '
    '    ffmpeg) ffmpeg -y -loglevel error -f x11grab -i $DISPLAY -frames:v 1 -q:v 6 shot.jpg 2>/dev/null;; '
    '    import) import -window root shot.jpg 2>/dev/null;; '
    '  esac; '
    '  [ -s shot.jpg ] && { echo "OK $(stat -c%s shot.jpg)"; exit 0; }; '
    'done; '
    'echo "SKRINSHOT VOSITASI ISHLAMADI"; exit 3'
)

SCREEN_PAGE = """<!doctype html>
<html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ferma — __NODE__ ekrani</title>
<style>
  html,body{height:100%;margin:0}
  body{background:#000;display:flex;flex-direction:column;
       font-family:-apple-system,"Segoe UI",Roboto,sans-serif}
  header{padding:8px 14px;background:#0d1117;border-bottom:1px solid #30363d;
         color:#8b949e;font-size:13px;display:flex;justify-content:space-between;
         align-items:center;gap:12px;flex-wrap:wrap}
  b{color:#e6edf3}
  .pill{padding:2px 10px;border-radius:999px;background:#21262d;font-size:12px}
  .live{color:#3fb950}
  img{flex:1;min-height:0;width:100%;object-fit:contain;background:#000}
</style></head>
<body>
  <header>
    <span>🖥️ <b>__NODE__</b> — jonli ekran</span>
    <span>
      <span class="pill" id="st">ulanmoqda…</span>
      <span class="pill">⌁ Ferma</span>
    </span>
  </header>
  <img id="vw" src="/screen/__NODE__/stream"
       onload="document.getElementById('st').className='pill live';document.getElementById('st').textContent='● JONLI'"
       onerror="document.getElementById('st').textContent='xato'">
</body></html>
"""


def save_job(job: dict) -> None:
    fl.ensure_dirs()
    jid = job["id"]
    with open(os.path.join(fl.RESULTS_DIR, f"{jid}.json"), "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2, ensure_ascii=False)
    art_dir = os.path.join(fl.RESULTS_DIR, jid)
    for node, res in job["results"].items():
        for rel, b64 in (res.get("artifacts") or {}).items():
            dest = os.path.join(art_dir, node, rel)
            os.makedirs(os.path.dirname(dest) or art_dir, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(base64.b64decode(b64))


def list_jobs(limit: int = 30) -> list[dict]:
    fl.ensure_dirs()
    files = sorted(
        (os.path.join(fl.RESULTS_DIR, f) for f in os.listdir(fl.RESULTS_DIR) if f.endswith(".json")),
        key=os.path.getmtime, reverse=True,
    )
    jobs = []
    for path in files[:limit]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                j = json.load(f)
            j.pop("files", None)
            for r in (j.get("results") or {}).values():
                r.pop("artifacts", None)
            jobs.append(j)
        except Exception:
            continue
    return jobs


# ----------------------------------------------------------------------------
# Dashboard va kirish (login)
# ----------------------------------------------------------------------------
LOGIN_HTML = """<!doctype html>
<html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ferma — kirish</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--mut:#8b949e;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
background:var(--bg);color:var(--fg);display:flex;min-height:100vh;align-items:center;justify-content:center}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px;
width:min(92vw,380px)}
h1{font-size:18px;margin:0 0 6px}p.mut{color:var(--mut);font-size:13px;margin:0 0 18px}
label{display:block;font-size:12.5px;color:var(--mut);margin-bottom:6px}
input{width:100%;font:inherit;background:#0d1117;color:var(--fg);border:1px solid var(--line);
border-radius:8px;padding:10px 12px;margin-bottom:14px}
button{width:100%;font:inherit;font-weight:600;background:var(--acc);color:#04121f;border:none;
border-radius:8px;padding:11px;cursor:pointer}
.err{color:#f85149;font-size:13px;min-height:18px;margin:8px 0 0}
.tip{color:var(--mut);font-size:11.5px;margin-top:14px}
</style></head><body>
<div class="card">
  <h1>🔒 Ferma — bosh PC kirishi</h1>
  <p class="mut">Markazni boshqarish uchun parolni kiriting.</p>
  <form id="f">
    <label for="pw">Parol</label>
    <input id="pw" type="password" autocomplete="current-password" autofocus>
    <button type="submit">Kirish</button>
    <div class="err" id="err"></div>
  </form>
  <div class="tip">Parol yo'qmi? Markazda: <code>python3 farmctl.py passwd --gen</code></div>
</div>
<script>
const f=document.getElementById('f'),pw=document.getElementById('pw'),err=document.getElementById('err');
f.onsubmit=async(e)=>{e.preventDefault();err.textContent='';
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:pw.value})});
  let d={};try{d=await r.json()}catch(_){}
  if(r.ok&&d.ok){location.href='/';return}
  err.textContent=d.error||('xato: '+r.status);
};
</script>
</body></html>
"""


DASHBOARD = """<!doctype html>
<html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ferma — boshqaruv</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--mut:#8b949e;
--ok:#3fb950;--bad:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
background:var(--bg);color:var(--fg)}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;
align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:18px;margin:0}.pill{padding:2px 10px;border-radius:20px;font-size:12px;
background:#21262d;color:var(--mut)}
main{padding:24px;max-width:1200px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h3{margin:0 0 6px;font-size:15px;display:flex;justify-content:space-between}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px}
.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}.off{background:var(--bad)}
.mut{color:var(--mut);font-size:12.5px}
.kv{display:flex;justify-content:space-between;border-top:1px solid var(--line);
padding-top:6px;margin-top:6px}
section{margin-top:28px}h2{font-size:15px;margin:0 0 12px}
textarea,input,select,button{font:inherit;background:#0d1117;color:var(--fg);
border:1px solid var(--line);border-radius:8px;padding:9px 11px}
textarea{width:100%;min-height:70px;font-family:ui-monospace,Menlo,monospace}
button{background:var(--acc);color:#04121f;font-weight:600;cursor:pointer;border:none;
padding:10px 18px}
button.ghost{background:#21262d;color:var(--fg)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:10px 0}
pre{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:12px;
overflow:auto;max-height:340px;font-size:12.5px;white-space:pre-wrap;word-break:break-word}
.job{border-left:3px solid var(--line);padding:8px 12px;margin:8px 0;background:#0d1117;
border-radius:6px}
</style></head><body>
<header><h1>🖥️ Ferma boshqaruvi</h1>
<span class="pill" id="summary">yuklanmoqda…</span>
<span class="pill">faqat Tailscale ichida</span>
<button class="ghost" onclick="refresh(true)">↻ Yangilash</button></header>
<main>
  <section><h2>Ishchilar (PC lar)</h2><div class="cards" id="nodes"></div></section>

  <section><h2>➕ Yangi PC ulash</h2>
    <p class="mut"><b>Universal buyruq</b> — Linux, macOS va Windows uchun bir xil
    (Python o'rnatilgan bo'lishi kerak):</p>
    <pre id="joinUni">__JOIN_UNI__</pre>
    <p class="mut">Windows'da <code>python3</code> ishlamasa — <code>python</code> bilan:</p>
    <pre id="joinUniPy">__JOIN_UNI_PY__</pre>
    <p class="mut"><b>To'liq o'rnatish (tavsiya)</b> — agent + OpenCode + fermaga ulash:</p>
    <pre id="setupUnix">__SETUP_SH__</pre>
    <p class="mut">Windows (PowerShell):</p>
    <pre id="setupWin">__SETUP_PS1__</pre>
    <details><summary class="mut">Muqobil: bash / PowerShell skriptlar</summary>
      <p class="mut">Linux/macOS:</p><pre id="joinLinux">__JOIN_LINUX__</pre>
      <p class="mut">Windows (PowerShell):</p><pre id="joinWin">__JOIN_WIN__</pre>
    </details>
  </section>

  <section><h2>Buyruq yuborish</h2>
    <textarea id="cmd" placeholder="masalan: echo salom && uname -a"></textarea>
    <div class="row">
      <select id="targets"><option value="all">Barcha online PC lar</option></select>
      <input id="collect" placeholder="yig'ish: *.png, *.log" style="min-width:200px">
      <input id="timeout" type="number" value="600" title="timeout (s)" style="width:110px">
      <button onclick="run()">▶ Ishga tushirish</button>
    </div>
  </section>

  <section><h2>Natija</h2><pre id="out">—</pre></section>
  <section><h2>Oxirgi vazifalar</h2><div id="jobs"></div></section>
</main>
<script>
let NODES=[];
const TOK=new URLSearchParams(location.search).get('token')||localStorage.getItem('ferma_token')||'';
if(TOK)localStorage.setItem('ferma_token',TOK);
function H(ex){return Object.assign({'X-Farm-Token':TOK},ex||{});}
async function getJSON(u){const r=await fetch(u,{headers:H()});return r.json();}
async function refresh(force){
  const d=await getJSON('/api/nodes'+(force?'?force=1':''));
  NODES=d.nodes||[];
  const on=NODES.filter(n=>n.online).length;
  document.getElementById('summary').textContent=`${on}/${NODES.length} online`;
  document.getElementById('nodes').innerHTML=NODES.map(n=>`
    <div class="card"><h3>${n.name}<span><span class="dot ${n.online?'on':'off'}"></span></span></h3>
    <div class="mut">${n.online?(n.cpu||'?')+' • x'+(n.cores||'?')+' • '+(n.ram_gb||'?')+'GB RAM':'offline'}</div>
    <div class="mut">${n.ip} • ${n.os} • ${n.latency_ms}ms</div>
    <div class="mut">GPU: ${(n.gpus&&n.gpus.length)?n.gpus.join(', '):'—'}</div>
    ${n.disk&&n.disk.total_gb?`<div class="kv mut"><span>disk</span><span>${n.disk.free_gb} / ${n.disk.total_gb} GB</span></div>`:''}
    </div>`).join('')||'<div class="mut">inventory bo\\'sh — avval discover.py ishga tushiring</div>';
  const sel=document.getElementById('targets');const cur=sel.value;
  sel.innerHTML='<option value="all">Barcha online PC lar</option>'+
    NODES.map(n=>`<option value="${n.name}">${n.name}${n.online?'':" (offline)"}</option>`).join('');
  sel.value=cur;
}
async function run(){
  const cmd=document.getElementById('cmd').value.trim();if(!cmd)return;
  document.getElementById('out').textContent='⏳ bajarilmoqda…';
  const body={cmd,nodes:document.getElementById('targets').value,
    timeout:+document.getElementById('timeout').value||600,
    collect:document.getElementById('collect').value.split(',').map(s=>s.trim()).filter(Boolean)};
  const r=await fetch('/api/run',{method:'POST',headers:H({'Content-Type':'application/json'}),
    body:JSON.stringify(body)});
  const d=await r.json();
  document.getElementById('out').textContent=JSON.stringify(d,null,2);
  refresh(true);loadJobs();
}
async function loadJobs(){
  const d=await getJSON('/api/jobs');
  document.getElementById('jobs').innerHTML=(d.jobs||[]).map(j=>`
    <div class="job"><b>${j.id}</b> <span class="mut">${j.created_at} • ${j.cmd}</span>
    <div class="mut">ok: ${j.summary?j.summary.ok:'?'} / ${j.targets.length} — ${j.targets.join(', ')}</div></div>`).join('')||'<div class="mut">hali vazifa yo\\'q</div>';
}
refresh();loadJobs();setInterval(()=>refresh(),10000);
</script></body></html>
"""

# --- Bitta buyruq bilan ulash (bootstrap) uchun shablonlar -------------------
JOIN_SH = r"""#!/usr/bin/env bash
set -e
BASE="__BASE__"
SERVER="${BASE%/j/*}"
echo "════ Ferma: bu PC ni tizimga ulaymiz ════"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "XATO: python3 topilmadi. Iltimos, avval o'rnating (masalan: sudo dnf install python3)."
  exit 1
fi
mkdir -p "$HOME/.ferma/work" "$HOME/.ferma/results"
echo "→ agent yuklab olinmoqda…"
curl -fsSL "$BASE/agent.py" -o "$HOME/.ferma/ferma-agent.py"
chmod 700 "$HOME/.ferma/ferma-agent.py"
echo "→ xizmat o'rnatilmoqda (avtomatik ishga tushadi)…"
echo "   rejim: PULL (markazga o'zi ulanadi) -> $SERVER"
"$PY" "$HOME/.ferma/ferma-agent.py" --pull --server "$SERVER" --install-service
echo ""
echo "✅ Tayyor! Bu PC fermaga ulandi."
"""

JOIN_PS1 = r"""$ErrorActionPreference = "Stop"
$Base = "__BASE__"
$Server = $Base -replace '/j/[^/]+$', ''
Write-Host "==== Ferma: bu PC ni tizimga ulaymiz ===="

$isAdmin = ([Security.Principal.WindowsPrincipal] `
  [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Write-Host "DIQQAT: PowerShell Administrator sifatida ochilmagan."
  Write-Host "         Firewall va xizmat uchun Administrator kerak."
}

$Dest = Join-Path $HOME ".ferma"
New-Item -ItemType Directory -Force -Path "$Dest\work", "$Dest\results", "$Dest\python" | Out-Null

# 1) Python topish yoki portativ yuklab olish
$py = Get-Command python  -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $py) {
  Write-Host "-> Python topilmadi: portativ Python yuklab olinmoqda..."
  $zip = Join-Path $Dest "python-embed.zip"
  try   { Invoke-WebRequest -UseBasicParsing "$Base/python-embed.zip" -OutFile $zip }
  catch { Invoke-WebRequest -UseBasicParsing "https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip" -OutFile $zip }
  Expand-Archive -Path $zip -DestinationPath (Join-Path $Dest "python") -Force
  $pyExe = Join-Path $Dest "python\python.exe"
  Write-Host "   portativ Python tayyor."
} else {
  $pyExe = $py.Source
  Write-Host "-> Python topildi: $pyExe"
}

# 2) agent
Write-Host "-> agent yuklab olinmoqda..."
Invoke-WebRequest -UseBasicParsing "$Base/agent.py" -OutFile (Join-Path $Dest "ferma-agent.py")

# 3) xizmat sifatida o'rnatish
Write-Host "-> xizmat o'rnatilmoqda (avtomatik ishga tushadi)..."
Write-Host "   rejim: PULL (markazga o'zi ulanadi) -> $Server"
& $pyExe (Join-Path $Dest "ferma-agent.py") --pull --server $Server --install-service
Write-Host ""
Write-Host "=========================================================="
Write-Host "  TAYYOR! Bu PC fermaga ulandi."
Write-Host "  Mac'da tekshirish:  python3 farmctl.py nodes"
Write-Host "=========================================================="
"""


BOOT_PY = r'''#!/usr/bin/env python3
# Universal ferma bootstrap — Linux / macOS / Windows, faqat stdlib.
# Avtomatik yaratildi. Yangilash uchun server.py dagi BOOT_PY ni tahrirlang.
import os, sys, platform, urllib.request, subprocess

BASE = "__BASE__"
SERVER = BASE.rsplit("/j/", 1)[0]   # markaz manzili (pull rejimi uchun)


def main():
    print("=" * 58)
    print("  FERMA: bu PC ni tizimga ulaymiz")
    print("=" * 58)
    print(f"  OS      : {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"  Python  : {platform.python_version()}")
    print(f"  Izoh    : {sys.executable}")

    dest = os.path.join(os.path.expanduser("~"), ".ferma")
    os.makedirs(os.path.join(dest, "work"), exist_ok=True)
    os.makedirs(os.path.join(dest, "results"), exist_ok=True)
    agent = os.path.join(dest, "ferma-agent.py")

    print("\n-> agent yuklab olinmoqda...")
    with urllib.request.urlopen(BASE + "/agent.py", timeout=30) as r:
        data = r.read()
    with open(agent, "wb") as f:
        f.write(data)
    try:
        os.chmod(agent, 0o700)
    except Exception:
        pass
    print(f"   saqlandi : {agent}  ({len(data)} bayt)")

    print("\n-> xizmat o'rnatilmoqda (avtomatik ishga tushadi)...")
    print(f"   rejim  : PULL (markazga o'zi ulanadi) -> {SERVER}")
    # eski agent jarayoni qolib ketmasin
    try:
        subprocess.run(["pkill", "-f", "ferma-agent.py"], check=False,
                       capture_output=True)
    except Exception:
        pass
    rc = subprocess.call([sys.executable, agent, "--pull", "--server", SERVER,
                          "--install-service"])
    print()
    if rc == 0:
        print("=" * 58)
        print("  TAYYOR! Bu PC fermaga ulandi.")
        print("  Mac'da tekshirish:  python3 farmctl.py nodes")
        print("=" * 58)
    else:
        print("Xizmat avtomatik o'rnatilmadi. Qo'lda ishga tushiring:")
        print(f"   {sys.executable} {agent}")


if __name__ == "__main__":
    main()
'''


def req_scheme(handler) -> str:
    """So'rov qaysi protokol bilan kelganini aniqlaydi (Funnel/Tailscale proxy)."""
    fwd = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    if fwd in ("http", "https"):
        return fwd
    host = (handler.headers.get("Host") or "").split(":")[0]
    return "https" if host.endswith(".ts.net") else "http"


def universal_command(host: str, python: str = "python3",
                      scheme: str = "http") -> str:
    """Hamma OS ga (Linux/macOS/Windows) tushadigan yagona buyruq."""
    base = f"{scheme}://{host}/j/{CFG['join_key']}"
    code = (f"import urllib.request;"
            f"exec(urllib.request.urlopen('{base}/boot.py').read())")
    return f'{python} -c "{code}"'


def standalone_path() -> str:
    """deploy/ferma-agent.py yo'lini qaytaradi; bo'lmasa yasaydi."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy", "ferma-agent.py")
    if not os.path.exists(p):
        import subprocess
        subprocess.run([sys.executable, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "deploy", "build_standalone.py")],
            check=True, capture_output=True)
    return p


def ensure_standalone() -> None:
    import subprocess
    subprocess.run([sys.executable, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "deploy", "build_standalone.py")],
        check=False, capture_output=True)


def join_commands(host: str, scheme: str = "http") -> tuple[str, str]:
    """(linux/mac buyrug'i, windows buyrug'i) qaytaradi."""
    port = CFG.get("server_port", fl.DEFAULT_SERVER_PORT)
    base = f"{scheme}://{host}/j/{CFG['join_key']}"
    linux = f'curl -fsSL "{base}/join.sh" | bash'
    win = f'powershell -c "iwr {base}/join.ps1 -UseBasicParsing | iex"'
    return linux, win


class Handler(BaseHTTPRequestHandler):
    server_version = "FermaServer/" + VERSION

    def log_message(self, fmt, *args):
        if os.environ.get("FERMA_DEBUG"):
            super().log_message(fmt, *args)

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _token(self) -> str | None:
        """So'rov tokeni: X-Farm-Token sarlavhasi yoki ?token= parametri."""
        tok = self.headers.get("X-Farm-Token")
        if not tok:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tok = (q.get("token") or [None])[0]
        return tok

    def _admin_ok(self) -> bool:
        """Admin token — to'liq boshqaruv."""
        return fl.token_ok(self._token(), TOKEN)

    def _worker_ok(self) -> bool:
        """Ishchi tokeni (yoki admin tokeni) — faqat agent endpointlari uchun."""
        tok = self._token()
        return fl.token_ok(tok, WORKER_TOKEN) or fl.token_ok(tok, TOKEN)

    def _session_id(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ferma_session" and v:
                return v
        return None

    def _session_ok(self) -> bool:
        return session_valid(self._session_id())

    def _control_ok(self) -> bool:
        """Boshqaruv amallari: admin token, sessiya cookie yoki sessiya ID
        (X-Farm-Token sifatida — CLI 'login' uchun)."""
        return (self._admin_ok() or self._session_ok()
                or session_valid(self._token()))

    # eski nom (boshqa joylarda ishlatilgan)
    def _api_ok(self) -> bool:
        return self._control_ok()

    def _frame(self, name: str, cnt: int = 1):
        node = find_node(name)
        if not node:
            return self._send({"ok": False, "error": "qurilma topilmadi"}, 404)
        t0 = time.time()
        res = quick_run(node, SCREEN_CMD, ["shot.jpg"], timeout=25)
        b64 = (res.get("artifacts") or {}).get("shot.jpg")
        if not b64:
            return self._send({"ok": False, "error": res.get("error") or "kadr olinmadi"},
                              502)
        return self._send(base64.b64decode(b64), ctype="image/jpeg")

    def _stream_screen(self, name: str):
        node = find_node(name)
        if not node:
            return self._send({"ok": False, "error": "qurilma topilmadi"}, 404)
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                t0 = time.time()
                res = quick_run(node, SCREEN_CMD, ["shot.jpg"], timeout=20)
                b64 = (res.get("artifacts") or {}).get("shot.jpg")
                if b64:
                    data = base64.b64decode(b64)
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                # haddan tashqari tez aylanmasin
                dt = time.time() - t0
                if dt < 0.5:
                    time.sleep(0.5 - dt)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        if self.path.startswith("/j/"):
            line = (f"[{time.strftime('%H:%M:%S')}] ULANISH: "
                    f"{self.client_address[0]} -> {self.path}")
            print("  ← " + line, flush=True)
            try:
                with open(os.path.join(fl.RESULTS_DIR, "deploys.log"), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
        host = self.headers.get("Host") or (
            f"{fl.primary_ip()}:{CFG.get('server_port', fl.DEFAULT_SERVER_PORT)}")
        scheme = req_scheme(self)
        linux_cmd, win_cmd = join_commands(host, scheme)
        uni = universal_command(host, "python3", scheme)
        uni_py = universal_command(host, "python", scheme)
        base_url = f"{scheme}://{host}/j/{CFG['join_key']}"
        setup_sh = f'curl -fsSL "{base_url}/setup.sh" | bash'
        setup_ps1 = f'irm {base_url}/setup.ps1 -UseBasicParsing | iex'

        if self.path in ("/", "/index.html"):
            if not self._api_ok():
                return self._send(LOGIN_HTML.encode("utf-8"),
                                  ctype="text/html; charset=utf-8")
            html = (DASHBOARD
                    .replace("__JOIN_UNI_PY__", uni_py)
                    .replace("__JOIN_UNI__", uni)
                    .replace("__JOIN_LINUX__", linux_cmd)
                    .replace("__JOIN_WIN__", win_cmd)
                    .replace("__SETUP_SH__", setup_sh)
                    .replace("__SETUP_PS1__", setup_ps1))
            return self._send(html.encode("utf-8"), ctype="text/html; charset=utf-8")

        # --- bitta buyruq bilan ulash ---
        m = re.match(r"^/j/([^/]+)/(join\.sh|join\.ps1|agent\.py|boot\.py|python-embed\.zip|ferma-all\.pyz|setup\.sh|setup\.ps1)$", self.path)
        if m:
            if not fl.token_ok(m.group(1), CFG.get("join_key", "")):
                return self._send({"ok": False, "error": "unauthorized"}, 401)
            kind = m.group(2)
            if kind == "agent.py":
                with open(standalone_path(), "rb") as f:
                    return self._send(f.read(), ctype="text/x-python; charset=utf-8")
            if kind == "ferma-all.pyz":
                p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "deploy", "ferma-all.pyz")
                if not os.path.exists(p):
                    return self._send({"ok": False, "error": "bundle yo'q — deploy/build_bundle.py ishga tushiring"}, 404)
                with open(p, "rb") as f:
                    return self._send(f.read(), ctype="application/octet-stream")
            if kind in ("setup.sh", "setup.ps1"):
                base = f"{scheme}://{host}/j/{CFG['join_key']}"
                fname = "setup-all.sh" if kind == "setup.sh" else "setup-all.ps1"
                p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "deploy", fname)
                if not os.path.exists(p):
                    return self._send({"ok": False, "error": f"{fname} topilmadi"}, 404)
                with open(p, "r", encoding="utf-8") as f:
                    script = f.read().replace("__BASE__", base)
                ctype = ("text/x-sh; charset=utf-8" if kind == "setup.sh"
                         else "text/plain; charset=utf-8")
                return self._send(script.encode("utf-8"), ctype=ctype)
            if kind == "python-embed.zip":
                p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "deploy", "vendor", "python-embed.zip")
                if not os.path.exists(p):
                    return self._send({"ok": False, "error": "vendor topilmadi"}, 404)
                with open(p, "rb") as f:
                    return self._send(f.read(), ctype="application/zip")
            base = f"{scheme}://{host}/j/{CFG['join_key']}"
            if kind == "boot.py":
                return self._send(BOOT_PY.replace("__BASE__", base).encode("utf-8"),
                                  ctype="text/x-python; charset=utf-8")
            if kind == "join.sh":
                script = JOIN_SH.replace("__BASE__", base)
                return self._send(script.encode("utf-8"),
                                  ctype="text/x-sh; charset=utf-8")
            script = JOIN_PS1.replace("__BASE__", base)
            return self._send(script.encode("utf-8"),
                              ctype="text/plain; charset=utf-8")

        if self.path.startswith("/api/join"):
            if not self._api_ok():
                return self._send({"ok": False, "error": "unauthorized"}, 401)
            return self._send({"ok": True, "universal": uni, "universal_python": uni_py,
                               "linux": linux_cmd, "windows": win_cmd, "host": host,
                               "setup_unix": setup_sh, "setup_windows": setup_ps1})
        if self.path == "/ping":
            return self._send({"ok": True, "host": fl.socket.gethostname()})
        is_agent_poll = self.path.startswith("/api/agent/jobs")
        if self.path.startswith("/api/") and not is_agent_poll and not self._api_ok():
            return self._send({"ok": False,
                               "error": "unauthorized — parol yoki ?token= kerak"}, 401)
        if self.path.startswith("/screen/") and not self._api_ok():
            return self._send({"ok": False, "error": "unauthorized — parol kerak"}, 401)
        if self.path.startswith("/screen/"):
            rest = urllib.parse.unquote(self.path[len("/screen/"):])
            if rest.endswith("/stream"):
                return self._stream_screen(rest[:-len("/stream")])
            if rest.endswith("/frame.jpg"):
                return self._frame(rest[:-len("/frame.jpg")])
            if not find_node(rest):
                return self._send({"ok": False,
                                   "error": "qurilma topilmadi: " + rest}, 404)
            return self._send(SCREEN_PAGE.replace("__NODE__", rest).encode("utf-8"),
                              ctype="text/html; charset=utf-8")
        if self.path.startswith("/api/agent/jobs"):
            if not self._worker_ok():
                return self._send({"ok": False, "error": "unauthorized"}, 401)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            try:
                wait = float((q.get("wait") or ["25"])[0])
            except ValueError:
                wait = 25.0
            if not name:
                return self._send({"ok": False, "error": "name kerak"}, 400)
            return self._send({"ok": True, "job": pull_poll(name, min(wait, 60))})
        if self.path.startswith("/api/nodes"):
            force = "force=1" in self.path
            return self._send({"ok": True, "nodes": all_status(force=force)})
        if self.path.startswith("/api/jobs"):
            return self._send({"ok": True, "jobs": list_jobs()})
        if self.path.startswith("/api/agent/status"):
            return self._send({"ok": True, "reports": agent_reports()})
        if self.path.startswith("/api/inventory"):
            return self._send({"ok": True, "inventory": load_inventory()})
        return self._send({"ok": False, "error": "not found"}, 404)

    def _handle_login(self, payload: dict):
        """Parolni tekshirib, sessiya cookie o'rnatadi (rate-limit bilan)."""
        ip = self.client_address[0]
        wait = login_blocked(ip)
        if wait > 0:
            return self._send(
                {"ok": False,
                 "error": f"juda ko'p urinish — {int(wait)}s dan keyin qayta urinib ko'ring"},
                429)
        stored = admin_password_hash()
        if not stored:
            return self._send(
                {"ok": False,
                 "error": "parol o'rnatilmagan — markazda: python3 farmctl.py passwd --gen"},
                400)
        password = str(payload.get("password") or "")
        if not password or not fl.verify_password(password, stored):
            record_login_fail(ip)
            print(f"  🔒 login xato: {ip}", flush=True)
            return self._send({"ok": False, "error": "parol xato"}, 401)
        clear_login_fails(ip)
        sid = new_session(ip)
        cookie = (f"ferma_session={sid}; Path=/; HttpOnly; SameSite=Lax; "
                  f"Max-Age={SESSION_TTL}")
        if req_scheme(self) == "https":
            cookie += "; Secure"
        body = json.dumps({"ok": True, "role": "controller", "session": sid,
                           "expires_in": SESSION_TTL}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", cookie)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        print(f"  🔑 login OK: {ip} (controller)", flush=True)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            return self._send({"ok": False, "error": f"bad json: {e}"}, 400)

        if self.path == "/api/login":
            return self._handle_login(payload)
        if self.path == "/api/logout":
            if not self._api_ok():
                return self._send({"ok": False, "error": "unauthorized"}, 401)
            drop_session(self._session_id())
            return self._send({"ok": True})

        is_agent = self.path in ("/api/agent/hello", "/api/agent/result",
                                 "/api/agent/status")
        if is_agent:
            if not self._worker_ok():
                return self._send({"ok": False, "error": "unauthorized"}, 401)
        elif self.path.startswith("/api/") and not self._api_ok():
            return self._send({"ok": False, "error": "unauthorized"}, 401)

        if self.path == "/api/agent/hello":
            name = payload.get("name")
            if not name:
                return self._send({"ok": False, "error": "name kerak"}, 400)
            pull_hello(name, payload.get("info") or {})
            return self._send({"ok": True})
        if self.path == "/api/agent/result":
            pull_result(payload.get("name"), payload.get("job_id"),
                        payload.get("result") or {})
            return self._send({"ok": True})
        if self.path == "/api/agent/status":
            name = payload.get("name")
            if not name:
                return self._send({"ok": False, "error": "name kerak"}, 400)
            agent_report_set(name, {k: v for k, v in payload.items() if k != "name"})
            return self._send({"ok": True})
        if self.path == "/api/run":
            res = dispatch(payload)
            return self._send(res, 200 if res.get("ok") else 400)
        if self.path == "/api/discover":
            import discover as disc
            inv = disc.run_discovery(probe=True)
            return self._send({"ok": True, "inventory": inv})
        return self._send({"ok": False, "error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="Ferma markazi")
    ap.add_argument("--bind", default=CFG.get("server_bind", "auto"))
    ap.add_argument("--port", type=int, default=int(CFG.get("server_port", 8790)))
    args = ap.parse_args()

    bind = args.bind
    if bind in ("auto", "", None):
        bind = fl.primary_ip() or "127.0.0.1"

    fl.ensure_dirs()
    ensure_standalone()
    print(f"Ferma markazi {VERSION}")
    print(f"  admin token: {TOKEN[:8]}…   worker token: {WORKER_TOKEN[:8]}…  ({fl.CONFIG_PATH})")
    print(f"  parol      : {'o‘rnatilgan ✔' if admin_password_hash() else 'YO‘Q — farmctl.py passwd --gen'}")
    print(f"  ishchilar  : {len(active_nodes())} ta inventarda")
    print(f"  manzil     : http://{bind}:{args.port}")
    host = f"{bind}:{args.port}"
    linux_cmd, win_cmd = join_commands(host)
    uni = universal_command(host, "python3")
    print("\n  ── Yangi PC ulash: UNIVERSAL buyruq (Linux/macOS/Windows) ──")
    print(f"  {uni}")
    print("\n  Windows'da python3 bo'lmasa: " + universal_command(host, "python"))
    print(f"  Muqobil (bash)     : {linux_cmd}")
    print(f"  Muqobil (PowerShell): {win_cmd}\n")
    if bind == "0.0.0.0":
        print("  OGOHLANTIRISH: dashboard tarmoqqa ochiq — token bilan himoyalang!")
    srv = ThreadingHTTPServer((bind, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nto'xtatildi")


if __name__ == "__main__":
    main()
