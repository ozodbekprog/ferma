#!/usr/bin/env python3
"""
farmctl.py — Fermani tez boshqarish uchun buyruq qatori (CLI).

Misollar:
    python3 farmctl.py discover                 # PC larni topish
    python3 farmctl.py nodes                     # holatni ko'rish
    python3 farmctl.py run --cmd "uname -a"      # barcha online PC da bajarish
    python3 farmctl.py run --cmd "nvidia-smi" --nodes 403
    python3 farmctl.py run --cmd "blender -b s.blend -f 1" \\
        --file s.blend --collect "*.png" --timeout 3600
    python3 farmctl.py serve                     # dashboardni ishga tushirish
    python3 farmctl.py agent                     # shu PC ni ishchi qilish
    python3 farmctl.py token                     # tokenni ko'rsatish
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import farmlib as fl

# Barqaror manzil (pointer) — agentlar server manzilini shu yerdan oladi
GIST_ID = "3319c09855eb4cae368a11383386d440"
POINTER_URL = ("https://gist.githubusercontent.com/ozodbekprog/"
               f"{GIST_ID}/raw/ferma-server.txt")

# Tailscale Funnel — doimiy ochiq manzil (uzilmaydi)
TS_BIN_CANDIDATES = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale", "tailscale")


def _ts_bin() -> str:
    import shutil
    for c in TS_BIN_CANDIDATES:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
        else:
            w = shutil.which(c)
            if w:
                return w
    return "tailscale"


def _update_pointer(url: str) -> bool:
    """Gist'dagi pointer'ni yangilaydi — agentlar manzilni shu yerdan oladi."""
    import subprocess
    try:
        r = subprocess.run(["gh", "api", "-X", "PATCH", f"/gists/{GIST_ID}",
                            "-f", f"files[ferma-server.txt][content]={url}"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def cmd_discover(args):
    import discover
    discover.run_discovery(probe=not args.no_probe, lan=args.lan, subnet=args.subnet)


def _center_base(cfg) -> str:
    ip = fl.primary_ip() or "127.0.0.1"
    port = cfg.get("server_port", fl.DEFAULT_SERVER_PORT)
    return f"http://{ip}:{port}"


def _resolve_center(explicit: str | None = None) -> str:
    """Markaz manzilini topadi: --server → config.center_url → pointer (gist)
    → shu PC dagi lokal manzil. Bitta faylli paketda 'login' uchun."""
    if explicit:
        return explicit.rstrip("/")
    cfg = fl.load_config()
    if cfg.get("center_url"):
        return str(cfg["center_url"]).rstrip("/")
    try:
        import urllib.request as _u
        sep = "&" if "?" in POINTER_URL else "?"
        with _u.urlopen(f"{POINTER_URL}{sep}ts={int(time.time())}", timeout=15) as r:
            txt = r.read().decode("utf-8", "replace")
        for h in re.findall(r"https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%\-]+", txt):
            h = h.rstrip("/\\\",:")
            if "gist" in h or "github" in h:
                continue
            return h
    except Exception:
        pass
    return _server_api(cfg)


def _center_base(cfg) -> str:
    """Farmctl buyruqlari uchun markaz manzili (sessiya → center_url → lokal)."""
    s = _load_session()
    if s.get("server"):
        return s["server"].rstrip("/")
    if cfg.get("center_url"):
        return str(cfg["center_url"]).rstrip("/")
    return _server_api(cfg)


SESSION_FILE = os.path.join(os.path.dirname(fl.CONFIG_PATH), "session.json")


def _load_session() -> dict:
    """Saqlangan lider-sessiya (parol bilan login qilingan bo'lsa)."""
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        if s.get("session") and s.get("expires_at", 0) > time.time() + 60:
            return s
    except Exception:
        pass
    return {}


def _cli_token(cfg) -> str:
    """Avval login sessiyasi, bo'lmasa admin token (config.json)."""
    return _load_session().get("session") or cfg["token"]


def cmd_nodes(args):
    cfg = fl.load_config()
    nodes = None
    try:
        r = fl.http_json(_center_base(cfg) + "/api/nodes?force=1",
                         token=_cli_token(cfg), timeout=15)
        nodes = r.get("nodes")
    except Exception:
        nodes = None
    if not nodes:
        import server
        nodes = server.all_status(force=True)
    if not nodes:
        print("inventory bo'sh. Avval: python3 farmctl.py discover")
        return
    print(f"{'NOM':22} {'ROL':11} {'HOLAT':10} {'IP':16} {'CPU':26} {'CORES':6} RAM   GPU")
    print("-" * 121)
    for n in sorted(nodes, key=lambda x: (not x.get("online"), x.get("name", ""))):
        if n.get("online"):
            state = "online"
        elif n.get("mode") == "pull" and n.get("last_seen_s") is not None:
            sec = int(n["last_seen_s"])
            if sec < 60:
                state = f"off {sec}s"
            elif sec < 3600:
                state = f"off {sec // 60}m"
            else:
                state = f"off {sec // 3600}h"
        else:
            state = "offline"
        print(f"{str(n.get('name'))[:21]:22} {str(n.get('role') or 'worker'):11} "
              f"{state:10} {str(n.get('ip')):16} "
              f"{str(n.get('cpu') or '—')[:25]:26} {str(n.get('cores') or '—'):6} "
              f"{str(n.get('ram_gb') or '—'):5} {', '.join(n.get('gpus') or []) or '—'}")
    on = sum(1 for n in nodes if n.get("online"))
    print(f"\n{on}/{len(nodes)} online")


def cmd_run(args):
    cfg = fl.load_config()
    files = {}
    for path in (args.file or []):
        with open(path, "rb") as f:
            files[os.path.basename(path)] = base64.b64encode(f.read()).decode()
    nodes = args.nodes
    if nodes and nodes != "all":
        nodes = [s.strip() for s in nodes.split(",") if s.strip()]
    payload = {
        "cmd": args.cmd, "nodes": nodes, "timeout": args.timeout,
        "collect": args.collect or [], "files": files,
    }
    # Avval ishlab turgan markazga API orqali yuboramiz (pull agentlar uchun shart)
    try:
        res = fl.http_json(_center_base(cfg) + "/api/run", method="POST",
                           token=_cli_token(cfg), payload=payload,
                           timeout=args.timeout + 90)
    except Exception:
        import server
        res = server.dispatch(payload)
    if not res.get("ok"):
        print("XATO:", res.get("error"))
        sys.exit(1)
    job = res["job"]
    print(f"Vazifa {job['id']} — {job['summary']['ok']}/{len(job['results'])} muvaffaqiyatli\n")
    for name, r in job["results"].items():
        print(f"=== {name} ===")
        if r.get("error"):
            print("  xato:", r["error"])
            continue
        print(f"  exit={r.get('exit')}  {r.get('duration_s')}s")
        if r.get("stdout"):
            print("  " + "\n  ".join(r["stdout"].strip().splitlines()[:40]))
        if r.get("stderr"):
            print("  [stderr] " + "\n  ".join(r["stderr"].strip().splitlines()[:15]))
        if r.get("artifacts"):
            print(f"  natija fayllar: {', '.join(r['artifacts'].keys())}")
        print()
    print(f"Natijalar: {fl.RESULTS_DIR}/{job['id']}.json")


def cmd_oc(args):
    """PC lardagi opencode orqali vazifani parallel bajarish (headless rejim)."""
    cfg = fl.load_config()
    nodes = args.nodes
    if not nodes or nodes == "auto":
        try:
            r = fl.http_json(_center_base(cfg) + "/api/nodes?force=1",
                             token=_cli_token(cfg), timeout=15)
            lst = r.get("nodes") or []
            nodes = ",".join(n["name"] for n in lst
                             if n.get("online") and n.get("role") != "controller")
        except Exception:
            nodes = "all"
    if not nodes:
        print("Online PC topilmadi. Avval: python3 farmctl.py nodes")
        sys.exit(1)
    prompt_b64 = base64.b64encode(" ".join(args.prompt).encode()).decode()
    model = f" -m {args.model}" if args.model else ""
    agent = f" --agent {args.agent}" if args.agent else ""
    inner = max(10, int(args.timeout) - 15)
    cwd = getattr(args, "cwd", None)
    prefix = f"cd {cwd} && " if cwd else ""
    remote = (f'{prefix}P=$(printf %s {prompt_b64} | base64 -d) && '
              f'timeout {inner} $HOME/.opencode/bin/opencode run'
              f'{model}{agent} "$P" 2>&1')
    node_list = nodes if nodes == "all" else [s.strip() for s in nodes.split(",") if s.strip()]
    print(f"Opencode vazifasi → {nodes}\n")
    payload = {"cmd": remote, "nodes": node_list, "timeout": args.timeout,
               "collect": [], "files": {}}
    try:
        res = fl.http_json(_center_base(cfg) + "/api/run", method="POST",
                           token=_cli_token(cfg), payload=payload,
                           timeout=args.timeout + 90)
    except Exception:
        import server
        res = server.dispatch(payload)
    if not res.get("ok"):
        print("XATO:", res.get("error"))
        sys.exit(1)
    job = res["job"]
    print(f"Vazifa {job['id']} — {job['summary']['ok']}/{len(job['results'])} muvaffaqiyatli\n")
    for name, r in job["results"].items():
        print(f"=== {name} ===  exit={r.get('exit')}  {r.get('duration_s')}s")
        if r.get("error"):
            print("  xato:", r["error"])
        else:
            print((r.get("stdout") or "").strip() or "  (bo'sh javob)")
        print()
    print(f"Natijalar: {fl.RESULTS_DIR}/{job['id']}.json")


def cmd_jobs(args):
    import server
    for j in server.list_jobs(limit=args.limit):
        s = j.get("summary") or {}
        print(f"{j['id']}  {j['created_at']}  ok={s.get('ok','?')}/{len(j.get('targets',[]))}  {j['cmd'][:70]}")


def cmd_serve(args):
    import server
    cfg = fl.load_config()
    sys.argv = ["server.py", "--bind", args.bind or cfg.get("server_bind", "127.0.0.1"),
                "--port", str(args.port or cfg.get("server_port", 8790))]
    server.main()


def cmd_agent(args):
    import agent
    cfg = fl.load_config()
    sys.argv = ["agent.py", "--bind", args.bind or "auto",
                "--port", str(args.port or cfg.get("agent_port", 8787))]
    agent.main()


def cmd_token(args):
    cfg = fl.load_config()
    print(cfg["token"])
    s = _load_session()
    if s:
        left = int(s.get("expires_at", 0) - time.time())
        print(f"# sessiya: {s.get('server')} (yana {max(0, left)//3600} soat) — {s.get('session','')[:10]}…")
    else:
        print("# sessiya yo'q (lider kirish: farmctl login)")


def cmd_login(args):
    """Parol bilan markazga kirish — 24 soatlik lider sessiyasi.

    Admin token uzoq muddatli PCda saqlanmaydi; sessiya muddati tugagach
    yana shu buyruq bilan kiriladi.
    """
    import getpass
    cfg = fl.load_config()
    base = _resolve_center(args.server).rstrip("/")
    pw = args.password
    if not pw:
        if not sys.stdin.isatty():
            print("Interaktiv emas — --password bering (yoki dashboard orqali kiring)")
            sys.exit(1)
        pw = getpass.getpass("Markaz paroli: ")
    try:
        res = fl.http_json(base + "/api/login", method="POST",
                           payload={"password": pw}, timeout=20)
    except Exception as e:
        print(f"XATO: serverga ulanib bo'lmadi: {type(e).__name__}: {e}")
        sys.exit(1)
    if not res.get("ok"):
        print("XATO:", res.get("error", "parol xato"))
        sys.exit(1)
    sid = res.get("session")
    if not sid:
        print("XATO: server sessiya qaytarmadi (server eski versiyada — yangilang)")
        sys.exit(1)
    data = {
        "session": sid, "role": res.get("role", "controller"), "server": base,
        "expires_at": time.time() + float(res.get("expires_in", 86400)),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    os.makedirs(os.path.dirname(SESSION_FILE) or ".", exist_ok=True)
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(SESSION_FILE, 0o600)
    # keyingi buyruqlar uchun markaz manzilini eslab qolamiz
    if cfg.get("center_url") != base:
        cfg["center_url"] = base
        fl.save_config(cfg)
    print(f"✅ Kirdingiz: {base}")
    print(f"   rol: {data['role']} • sessiya 24 soat amal qiladi")
    print("   chiqish: python3 farmctl.py logout")


def cmd_logout(args):
    cfg = fl.load_config()
    s = _load_session()
    base = s.get("server") or _center_base(cfg)
    if s.get("session"):
        try:
            fl.http_json(base + "/api/logout", method="POST",
                         token=s["session"], payload={}, timeout=15)
        except Exception:
            pass
    try:
        os.remove(SESSION_FILE)
    except Exception:
        pass
    print("✅ Chiqdingiz (sessiya o'chirildi)")


def cmd_vault(args):
    """Shifrlangan ombor (vault.py) buyruqlarini bajaradi."""
    import vault
    extra = list(getattr(args, "vault_args", []) or [])
    if extra and extra[0] == "--":
        extra = extra[1:]
    sys.exit(vault.main(extra) or 0)


def cmd_passwd(args):
    """Markaz parolini o'rnatish/yangilash (bosh PC roli uchun)."""
    import getpass
    cfg = fl.load_config()
    if args.gen:
        pw = fl.gen_password()
    else:
        if not sys.stdin.isatty():
            print("Interaktiv emas — `--gen` ishlating (yoki parolni qo'lda kiriting).")
            sys.exit(1)
        pw1 = getpass.getpass("Yangi parol: ")
        pw2 = getpass.getpass("Parolni takrorlang: ")
        if pw1 != pw2:
            print("XATO: parollar mos emas")
            sys.exit(1)
        pw = pw1
    if len(pw) < 8:
        print("XATO: parol kamida 8 belgidan iborat bo'lsin")
        sys.exit(1)
    cfg["admin_password"] = fl.hash_password(pw)
    fl.save_config(cfg)
    print("✅ Markaz paroli o'rnatildi (config.json — faqat hash, 0600).")
    if args.gen:
        print(f"   PAROL: {pw}")
        print("   (bir marta ko'rsatiladi — yozib oling; o'zgartirish: farmctl.py passwd)")
    print("   Kirish: dashboard (/ — parol so'raydi)")


def cmd_role(args):
    """Node rolini ko'rish/belgilash: controller (bosh) yoki worker (ishchi)."""
    cfg = fl.load_config()
    roles = cfg.get("node_roles") or {}

    if args.all_workers:
        names = set(roles)
        # 1) Ishlab turgan markazdan to'liq ro'yxat (pull agentlar ham kiradi)
        try:
            res = fl.http_json(_center_base(cfg) + "/api/nodes?force=1",
                               token=_cli_token(cfg), timeout=25)
            names |= {n.get("name") for n in res.get("nodes", []) if n.get("name")}
        except Exception:
            pass
        # 2) Inventar faylidan
        try:
            with open(fl.INVENTORY_PATH, "r", encoding="utf-8") as f:
                names |= {n.get("name") for n in json.load(f).get("nodes", []) if n.get("name")}
        except Exception:
            pass
        try:
            import socket as _sock
            local = _sock.gethostname()
        except Exception:
            local = ""
        changed = 0
        for name in names:
            if not name:
                continue
            if name in (local,):
                roles.setdefault(name, "controller")
                continue
            roles[name] = "worker"
            changed += 1
        cfg["node_roles"] = roles
        fl.save_config(cfg)
        print(f"✅ {changed} ta node ishchi (worker) qilib belgilandi; markaz — controller")
        return

    if not args.name:
        if not roles:
            print("Rollar belgilanmagan (sukut: qolganlar=worker)")
        for name, role in sorted(roles.items()):
            print(f"{name:24} {role}")
        return
    if not args.role:
        print(f"{args.name}: {roles.get(args.name, 'sukut (worker)')}")
        return
    roles[args.name] = args.role
    cfg["node_roles"] = roles
    fl.save_config(cfg)
    print(f"✅ {args.name} → {args.role}")


def cmd_share(args):
    """Standalone agent faylini Tailscale orqali tarqatadi (bir necha daqiqaga)."""
    import http.server
    import secrets as _secrets
    import socket as _socket

    src = args.file or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "deploy", "ferma-agent.py")
    if not os.path.exists(src):
        print("Standalone fayl yo'q — yasalmoqda…")
        import subprocess as _sp
        _sp.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                              "deploy", "build_standalone.py")], check=True)

    ip = args.bind or fl.tailscale_ip()
    if not ip:
        print("Tailscale IP topilmadi — Tailscale ishlayaptimi?")
        sys.exit(1)
    port = args.port
    secret = _secrets.token_urlsafe(9)
    filename = os.path.basename(src)
    data = open(src, "rb").read()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            print(f"  ← so'rov: {self.client_address[0]}")

        def do_GET(self):
            if self.path != f"/{secret}/{filename}":
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", "text/x-python; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            print(f"\n✅ {self.client_address[0]} faylni oldi — endi o'sha PC da ishga tushiring!\n")

    url = f"http://{ip}:{port}/{secret}/{filename}"
    print("═" * 68)
    print("  FERMA AGENTNI ULASHISH — Tailscale ichida (maxfiy havola)")
    print("═" * 68)
    print(f"\n  Fayl    : {src}")
    print(f"  Havola  : {url}\n")
    print("  ── Fedora/Linux PC da SHU buyruqni ishga tushiring ──\n")
    print(f'  curl -fsSL "{url}" -o ~/ferma-agent.py && python3 ~/ferma-agent.py\n')
    print("  (to'xtatish uchun bu yerda Ctrl+C)")
    print("═" * 68)
    sys.stdout.flush()
    try:
        http.server.ThreadingHTTPServer((ip, port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nulashish to'xtatildi")


def cmd_net(args):
    """Tarmoq rejimini ko'rsatadi yoki almashtiradi: auto | tailscale | lan."""
    cfg = fl.load_config()
    if args.mode:
        if args.mode not in ("auto", "tailscale", "lan"):
            print("rejim: auto | tailscale | lan")
            sys.exit(1)
        cfg["network"] = args.mode
        fl.save_config(cfg)
        print(f"✅ Tarmoq rejimi o'zgartirildi: {args.mode}")
    print(f"  rejim        : {cfg.get('network', 'auto')}")
    print(f"  Tailscale IP : {fl.tailscale_ip() or '— (yo`q)'}")
    print(f"  LAN IP       : {fl.lan_ip() or '—'}")
    print(f"  ➜ asosiy IP  : {fl.primary_ip() or '—'}")
    print(f"  Tailscale    : {'o`rnatilgan' if fl.tailscale_status() else 'yo`q (mustaqil rejim)'}")


def cmd_screen(args):
    """PC ekranini jonli ko'rish (brauzerda MJPEG translyatsiya)."""
    import webbrowser
    cfg = fl.load_config()
    url = f"{_center_base(cfg)}/screen/{args.node}"
    print("═" * 66)
    print(f"  🖥️  JONLI EKRAN — {args.node}")
    print("═" * 66)
    print(f"\n  Havola : {url}")
    print(f"  Kadr   : {url}/frame.jpg   (bitta rasm)")
    print("  To'xtatish: brauzer oynasini yopish\n")
    if not args.no_open:
        try:
            webbrowser.open(url)
            print("  ✅ Brauzerda ochildi")
        except Exception as e:
            print(f"  (brauzer ochilmadi: {e})")
    print("═" * 66)


def cmd_rename(args):
    """PC ga qulay nom (alias) beradi."""
    cfg = fl.load_config()
    al = cfg.setdefault("aliases", {})
    al[args.old] = args.new
    fl.save_config(cfg)
    print(f"✅ '{args.old}' → '{args.new}'")
    print("   Endi buyruqlarda shu nomni ishlatasiz.")


def cmd_tunnel(args):
    """Tailscale'siz ulanish uchun Mac'ni ochiq manzil (localhost.run) orqali ko'rsatadi."""
    import subprocess
    cfg = fl.load_config()
    port = args.port or cfg.get("server_port", fl.DEFAULT_SERVER_PORT)
    ip = args.bind or fl.primary_ip() or "127.0.0.1"
    target = f"{ip}:{port}"
    print("═" * 66)
    print("  FERMA TUNNEL — Tailscale'siz ulanish (localhost.run)")
    print("═" * 66)
    print(f"  Yo'naltirish: {target}  →  localhost.run")
    print(f"  Barqaror pointer: {POINTER_URL}")
    print("\n  ⏳ ochilmoqda (5-15 sekund)…")

    def watchdog(url: str, proc) -> None:
        """Tunnel sog'lig'ini tekshiradi; buzuq bo'lsa ssh'ni to'xtatadi (qayta ulanadi)."""
        import urllib.request as _u
        fails = 0
        while proc.poll() is None:
            time.sleep(30)
            if proc.poll() is not None:
                return
            try:
                with _u.urlopen(url + "/ping", timeout=20) as r:
                    if r.status == 200:
                        fails = 0
                        continue
                fails += 1
            except Exception:
                fails += 1
            if fails >= 2:
                print("  ⚠ tunnel javob bermayapti — qayta ulanadi…", flush=True)
                try:
                    proc.terminate()
                except Exception:
                    pass
                return

    try:
        while True:
            proc = subprocess.Popen(
                ["ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
                 "-o", "ExitOnForwardFailure=yes",
                 "-R", f"80:{target}", "nokey@localhost.run"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            url = None
            for line in proc.stdout:
                line = line.rstrip()
                m = re.search(r"https://[A-Za-z0-9._-]+", line)
                if m and "lhr.life" in m.group(0) and not url:
                    url = m.group(0)
                    cfg["public_url"] = url
                    fl.save_config(cfg)
                    ok_ptr = _update_pointer(url)
                    print("\n" + "═" * 66)
                    print(f"  ✅ OCHIQ MANZIL: {url}")
                    print(f"  {'✅' if ok_ptr else '⚠'} Barqaror pointer "
                          f"{'yangilandi' if ok_ptr else 'yangilanmadi'}")
                    print("═" * 66)
                    print("\n  ▸ ISTALGAN PC da (Tailscale KERAK EMAS) — bitta buyruq:\n")
                    print(f'    python3 -c "import urllib.request;exec(urllib.request.urlopen(\'{url}/j/{cfg["join_key"]}/boot.py\').read())"')
                    print("\n  ▸ Windows (PowerShell, Administrator):\n")
                    print(f'    powershell -c "iwr {url}/j/{cfg["join_key"]}/join.ps1 -UseBasicParsing | iex"')
                    print("\n  ⚠ Bu oynani yopmang — tunnel shu yerda ishlab turadi.")
                    print("    To'xtatish: Ctrl+C\n")
                    import threading as _th
                    _th.Thread(target=watchdog, args=(url, proc), daemon=True).start()
                elif line and not m:
                    print("  " + line)
            proc.terminate()
            print(f"  ⚠ tunnel uzildi — 5s dan keyin qayta ulanadi…")
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        print("\ntunnel to'xtatildi")


def cmd_funnel(args):
    """Tailscale Funnel — doimiy ochiq manzil (uzilmaydi, manzil o'zgarmaydi)."""
    import subprocess
    cfg = fl.load_config()
    port = args.port or cfg.get("server_port", fl.DEFAULT_SERVER_PORT)
    action = getattr(args, "action", "status") or "status"
    ts = _ts_bin()
    if action == "up":
        cmd = [ts, "funnel", "--bg", f"http://127.0.0.1:{port}"]
    elif action == "down":
        cmd = [ts, "funnel", "reset"]
    else:
        cmd = [ts, "funnel", "status"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        print(f"⚠ tailscale topilmadi: {ts}")
        return
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if out:
        print(out)
    if action == "up" and r.returncode == 0:
        m = re.search(r"https://[\w.-]+\.ts\.net", out)
        if m:
            url = m.group(0)
            cfg["public_url"] = url
            fl.save_config(cfg)
            ok = _update_pointer(url)
            print(f"\n  ✅ Doimiy manzil: {url}")
            print(f"  {'✅' if ok else '⚠'} Barqaror pointer "
                  f"{'yangilandi' if ok else 'yangilanmadi'}")
            print("  ℹ 'farmctl.py tunnel' endi shart emas.")
    if action == "up" and r.returncode != 0 and "not enabled" in out.lower():
        print("\n  ⚠ Funnel tailnetda yoqilmagan — chiqqan havolani ochib yoqing.")


def cmd_join_link(args):
    """Yangi PC ni ulash uchun bitta buyruqni ko'rsatadi."""
    cfg = fl.load_config()
    ip = args.host or fl.primary_ip() or "127.0.0.1"
    port = args.port or cfg.get("server_port", fl.DEFAULT_SERVER_PORT)
    host = f"{ip}:{port}"
    pub = cfg.get("public_url")
    import server
    linux, win = server.join_commands(host)
    uni = server.universal_command(host, "python3")
    uni_py = server.universal_command(host, "python")

    if pub:
        pbase = f"{pub}/j/{cfg['join_key']}"
        pcode = f"import urllib.request;exec(urllib.request.urlopen('{pbase}/boot.py').read())"
        print("═" * 68)
        print("  TAILSCALE'SIZ — OCHIQ MANZIL orqali ulash")
        print("═" * 68)
        print(f"\n  manzil: {pub}\n")
        print(f'    python3 -c "{pcode}"\n')
        print(f'    Windows: powershell -c "iwr {pbase}/join.ps1 -UseBasicParsing | iex"\n')

    print("═" * 68)
    print("  YANGI PC NI ULASH — UNIVERSAL buyruq (Tailscale ichida)")
    print("═" * 68)
    print("\n  ▸ HAR QANDAY PC da (Linux / macOS / Windows), bitta buyruq:\n")
    print(f"    {uni}\n")
    print("  ▸ Windows'da 'python3' ishlamasa:\n")
    print(f"    {uni_py}\n")
    print("  ▸ Muqobil (Python'siz):")
    print(f"    Linux/macOS : {linux}")
    print(f"    Windows     : {win}\n")
    print("  Bajarilgach: python3 farmctl.py nodes")
    print("═" * 68)


def main():
    ap = argparse.ArgumentParser(description="Ferma boshqaruv CLI", prog="farmctl")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="qurilmalarni topish")
    p.add_argument("--lan", action="store_true")
    p.add_argument("--subnet")
    p.add_argument("--no-probe", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("nodes", help="holatni ko'rish")
    p.set_defaults(func=cmd_nodes)

    p = sub.add_parser("run", help="buyruq tarqatish")
    p.add_argument("--cmd", required=True)
    p.add_argument("--nodes", default="all", help="'all' yoki 'a,b'")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--file", action="append", help="yuboriladigan fayl (takrorlash mumkin)")
    p.add_argument("--collect", action="append", help="yig'iladigan natija globi (masalan '*.png')")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("jobs", help="oxirgi vazifalar")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("oc", help="PC lardagi opencode orqali parallel vazifa bajarish")
    p.add_argument("prompt", nargs="+", help="opencode'ga yuboriladigan so'rov/vazifa")
    p.add_argument("--nodes", default="auto", help="'auto', 'all' yoki 'a,b'")
    p.add_argument("--model", help="model (masalan opencode/big-pickle)")
    p.add_argument("--agent", help="agent nomi")
    p.add_argument("--cwd", help="ish papkasi (masalan ~/ttpu-lms)")
    p.add_argument("--timeout", type=float, default=300)
    p.set_defaults(func=cmd_oc)

    p = sub.add_parser("serve", help="markazni ishga tushirish")
    p.add_argument("--bind")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("agent", help="shu PC ni ishchiga aylantirish")
    p.add_argument("--bind")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("token", help="tokenni ko'rsatish")
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("login", help="parol bilan markazga kirish (lider sessiyasi)")
    p.add_argument("--password", help="parol (odatda so'raladi)")
    p.add_argument("--server", help="markaz manzili (masalan https://...ts.net)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", help="lider sessiyasidan chiqish")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("vault", help="shifrlangan ombor (init/put/get/ls/passwd/status/wipe)")
    p.add_argument("vault_args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_vault)

    p = sub.add_parser("passwd", help="markaz parolini o'rnatish (bosh PC / controller roli)")
    p.add_argument("--gen", action="store_true", help="kuchli parol yaratib o'rnatish")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("role", help="node rolini ko'rish/belgilash (controller|worker)")
    p.add_argument("name", nargs="?", help="node nomi (bo'sh bo'lsa — ro'yxat)")
    p.add_argument("role", nargs="?", choices=["controller", "worker"])
    p.add_argument("--all-workers", action="store_true",
                   help="markazdan tashqari barcha node'larga 'worker' rolini berish")
    p.set_defaults(func=cmd_role)

    p = sub.add_parser("share-agent", help="agentni boshqa PC ga Tailscale orqali tarqatish")
    p.add_argument("--file")
    p.add_argument("--bind")
    p.add_argument("--port", type=int, default=8899)
    p.set_defaults(func=cmd_share)

    p = sub.add_parser("join-link", help="yangi PC ulash uchun bitta buyruqni ko'rsatish")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_join_link)

    p = sub.add_parser("net", help="tarmoq rejimi: auto | tailscale | lan")
    p.add_argument("mode", nargs="?", choices=["auto", "tailscale", "lan"])
    p.set_defaults(func=cmd_net)

    p = sub.add_parser("tunnel", help="Tailscale'siz ulanish uchun ochiq manzil ochish")
    p.add_argument("--port", type=int)
    p.add_argument("--bind")
    p.set_defaults(func=cmd_tunnel)

    p = sub.add_parser("funnel", help="Tailscale Funnel — doimiy ochiq manzil")
    p.add_argument("action", nargs="?", choices=["status", "up", "down"], default="status")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_funnel)

    p = sub.add_parser("rename", help="PC ga qulay nom berish")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("screen", help="PC ekranini jonli ko'rish")
    p.add_argument("node")
    p.add_argument("--no-open", action="store_true", help="brauzerda ochmaslik")
    p.set_defaults(func=cmd_screen)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
