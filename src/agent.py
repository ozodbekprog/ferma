#!/usr/bin/env python3
"""
agent.py — Har bir ishchi (worker) PC da ishlaydigan yengil agent.

Nima qiladi:
  * /health        -> tirikligini bildiradi
  * /info          -> tizim ma'lumotlari (CPU, RAM, disk, GPU, Tailscale IP)
  * /run           -> berilgan buyruqni xavfsiz ish papkasida bajaradi,
                      kerakli fayllarni qabul qiladi va natija fayllarini qaytaradi

Xavfsizlik:
  * Faqat X-Farm-Token to'g'ri bo'lsa ishlaydi.
  * Standart holatda faqat Tailscale IP ga bog'lanadi (0.0.0.0 emas).
  * Har bir vazifa alohida vaqtinchalik papkada bajariladi.

Ishga tushirish:
    python3 agent.py                 # Tailscale IP ni o'zi topadi
    python3 agent.py --bind 0.0.0.0  # majburan (tavsiya etilmaydi)
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import glob
import json
import os
import platform
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import farmlib as fl

MAX_BODY = 256 * 1024 * 1024      # 256 MB kiruvchi
MAX_OUT = 512 * 1024              # 512 KB stdout/stderr
MAX_ARTIFACT = 512 * 1024 * 1024  # 512 MB bitta natija fayli

WORK_ROOT = os.path.join(os.path.expanduser("~"), ".ferma", "work")
VERSION = "0.1.0"
START = time.time()
_POINTER_URL = ""   # build_standalone tomonidan to'ldiriladi (barqaror manzil)

CFG = fl.load_config()
# Agent faqat WORKER token bilan ishlaydi (admin token markazda qoladi).
_EMBEDDED_TOKEN = ""   # build_standalone paytida to'ldiriladi (repo'da bo'sh)
TOKEN = os.environ.get("FARM_TOKEN") or _EMBEDDED_TOKEN or CFG["token"]
AGENT_PORT = int(CFG["agent_port"])


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def resolve_bind(requested: str) -> str:
    """Agent qaysi manzilga bog'lanishini aniqlaydi (mustahkam, retry bilan)."""
    if requested and requested != "auto":
        return requested
    mode = fl.load_config().get("network", "auto")

    if mode in ("auto", "tailscale") and fl.tailscale_installed():
        # Tailscale hali ko'tarilmagan bo'lishi mumkin — kutamiz
        for attempt in range(8):
            ip = fl.tailscale_ip()
            if ip:
                return ip
            if attempt == 0:
                log("Tailscale IP kutilmoqda…")
            time.sleep(1)

    if mode == "lan":
        ip = fl.lan_ip()
        log(f"LAN rejimi: {ip}")
        return ip or "0.0.0.0"

    # auto/tailscale, lekin Tailscale IP olinmadi — 0.0.0.0 (token bilan himoyalangan)
    log("OGOHLANTIRISH: Tailscale IP topilmadi — 0.0.0.0 ga bog'lanadi")
    log("  (faqat Tailscale + token orqali foydalanish tavsiya etiladi)")
    return "0.0.0.0"


def build_service_unit(script_path: str, extra_args: str = "") -> tuple[str, str, str]:
    """(yo'l, mazmun, turi) qaytaradi. turi: systemd | launchd | windows."""
    py = sys.executable or "python3"
    sysname = platform.system()
    args = f" {extra_args}".rstrip()
    if sysname == "Linux":
        path = os.path.join(os.path.expanduser("~"), ".config/systemd/user/ferma-agent.service")
        content = f"""[Unit]
Description=Ferma Agent (render/hisoblash ishchisi)
After=network-online.target

[Service]
ExecStart={py} {script_path}{args}
WorkingDirectory={os.path.dirname(script_path)}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
        return path, content, "systemd"
    if sysname == "Darwin":
        path = os.path.join(os.path.expanduser("~"), "Library/LaunchAgents/uz.ferma.agent.plist")
        plist_args = "".join(
            f"<string>{a}</string>" for a in
            ([script_path] + ([a for a in extra_args.split() if a] if extra_args else []))
        )
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>uz.ferma.agent</string>
  <key>ProgramArguments</key><array><string>{py}</string>{plist_args}</array>
  <key>WorkingDirectory</key><string>{os.path.dirname(script_path)}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{os.path.expanduser('~')}/.ferma/agent.log</string>
  <key>StandardErrorPath</key><string>{os.path.expanduser('~')}/.ferma/agent.err.log</string>
</dict></plist>
"""
        return path, content, "launchd"
    return "", "", "windows"


def install_service(script_path: str, dry_run: bool = False, extra_args: str = "") -> None:
    path, content, kind = build_service_unit(script_path, extra_args)
    if kind == "windows":
        py = sys.executable or "python"
        task = "FermaAgent"
        tr = f'"{py}" "{script_path}"{(" " + extra_args) if extra_args else ""}'
        if dry_run:
            print("[dry-run] schtasks /create /tn FermaAgent /tr", tr, "/sc onlogon /f")
            return
        subprocess.run(["schtasks", "/create", "/tn", task, "/tr", tr,
                        "/sc", "onlogon", "/rl", "HIGHEST", "/f"],
                       check=False, capture_output=True)
        # Firewall: faqat Tailscale diapazoni uchun portni ochamiz
        subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule",
                        "name=Ferma Agent", "dir=in", "action=allow",
                        f"protocol=TCP", f"localport={AGENT_PORT}",
                        "remoteip=100.64.0.0/10"],
                       check=False, capture_output=True)
        subprocess.Popen(
            ["cmd", "/c",
             f"timeout /t 2 >nul & schtasks /end /tn {task} >nul 2>&1 & "
             f"timeout /t 2 >nul & schtasks /run /tn {task} >nul 2>&1"],
            creationflags=0x00000008,  # DETACHED_PROCESS
        )
        print("✅ Windows xizmati o'rnatildi: FermaAgent (onlogon)")
        print("   holat: schtasks /query /tn FermaAgent")
        return
    if dry_run:
        print(f"[dry-run] {path}\n{'-' * 60}\n{content}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        if kind == "systemd":
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            # MUHIM: o'rnatish xizmat ICHIDAN chaqirilishi mumkin — shuning uchun
            # restart --no-block ishlatamiz (buyruq darhol qaytadi, systemd esa
            # bizning cgroupdan tashqarida xizmatni qayta ko'taradi). Bloklovchi
            # stop/restart o'zini o'ldirib, xizmat o'chiq qolib ketardi.
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           check=False, capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", "ferma-agent.service"],
                           check=False, capture_output=True)
            subprocess.run(["systemctl", "--user", "restart", "--no-block",
                            "ferma-agent.service"],
                           check=False, capture_output=True)
            print("✅ systemd xizmati o'rnatildi va QAYTA ISHGA TUSHIRILDI: ferma-agent")
            print("   holat : systemctl --user status ferma-agent")
            print("   log   : journalctl --user -u ferma-agent -f")
        else:
            # launchd: unload bizning jarayonni ham o'ldiradi — shuning uchun
            # qayta yuklashni orqaga (detached) topshiramiz.
            subprocess.Popen(
                ["/bin/sh", "-c",
                 f"(sleep 1; launchctl unload '{path}' 2>/dev/null; "
                 f"launchctl load '{path}') >/dev/null 2>&1 &"],
                start_new_session=True,
            )
            print("✅ launchd xizmati o'rnatildi: uz.ferma.agent")
            print("   log   : tail -f ~/.ferma/agent.log")
    except FileNotFoundError:
        print(f"Xizmat fayli yozildi: {path}\n(avtomatik yoqilmadi — qo'lda yoqing)")


def run_job(payload: dict) -> dict:
    """Bitta vazifani bajaradi va natijani qaytaradi."""
    cmd = payload.get("cmd")
    if not cmd or not isinstance(cmd, str):
        return {"ok": False, "error": "'cmd' majburiy"}
    timeout = float(payload.get("timeout") or 600)
    files = payload.get("files") or {}
    collect = payload.get("collect") or []
    env_extra = payload.get("env") or {}

    job_id = uuid.uuid4().hex[:12]
    os.makedirs(WORK_ROOT, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix=f"job-{job_id}-", dir=WORK_ROOT)

    try:
        # 1) Kiruvchi fayllarni joylash
        for rel, b64 in files.items():
            rel = os.path.normpath(rel).lstrip("/\\")
            if rel.startswith(".."):
                return {"ok": False, "error": f"xavfli yo'l: {rel}"}
            dest = os.path.join(workdir, rel)
            os.makedirs(os.path.dirname(dest) or workdir, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(base64.b64decode(b64))

        # 2) Buyruqni bajarish — butun jarayon guruhini boshqaramiz; timeout
        #    bo'lsa guruh to'liq o'ldiriladi (voris jarayonlar osilib qolmasligi uchun).
        env = dict(os.environ)
        env.update({k: str(v) for k, v in env_extra.items()})
        env["FERMA_JOB_ID"] = job_id
        t0 = time.time()
        if os.name == "nt":
            proc = subprocess.Popen(
                cmd, shell=True, cwd=workdir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
            timeout_hit = False
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timeout_hit = True
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, check=False)
                try:
                    out, err = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = b"", b""
        else:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=workdir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            timeout_hit = False
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timeout_hit = True
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                try:
                    out, err = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    out, err = b"", b""
        dur = round(time.time() - t0, 3)
        if timeout_hit:
            return {
                "ok": False, "job_id": job_id,
                "host": fl.socket.gethostname(),
                "error": f"timeout ({timeout}s)",
                "stdout": (out or b"").decode("utf-8", "replace")[:MAX_OUT],
                "stderr": (err or b"").decode("utf-8", "replace")[:MAX_OUT],
                "duration_s": dur, "artifacts": {}, "workdir": workdir,
            }
        stdout = (out or b"").decode("utf-8", "replace")[:MAX_OUT]
        stderr = (err or b"").decode("utf-8", "replace")[:MAX_OUT]

        # 3) Natija fayllarini yig'ish
        artifacts = {}
        for pattern in collect:
            for path in glob.glob(os.path.join(workdir, "**", pattern), recursive=True):
                if not os.path.isfile(path):
                    continue
                if os.path.getsize(path) > MAX_ARTIFACT:
                    continue
                rel = os.path.relpath(path, workdir)
                with open(path, "rb") as f:
                    artifacts[rel] = base64.b64encode(f.read()).decode()
                if len(artifacts) >= 200:
                    break

        return {
            "ok": proc.returncode == 0,
            "job_id": job_id,
            "host": fl.socket.gethostname(),
            "exit": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_s": dur,
            "artifacts": artifacts,
            "workdir": workdir,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "job_id": job_id, "error": f"timeout ({timeout}s)",
                "host": fl.socket.gethostname()}
    except Exception as e:
        return {"ok": False, "job_id": job_id, "error": f"{type(e).__name__}: {e}",
                "host": fl.socket.gethostname()}


class Handler(BaseHTTPRequestHandler):
    server_version = "FermaAgent/" + VERSION

    def log_message(self, fmt, *args):  # sukut bo'yicha shovqinni kamaytiramiz
        if os.environ.get("FERMA_DEBUG"):
            super().log_message(fmt, *args)

    # --- yordamchilar ---
    def _auth(self) -> bool:
        return fl.token_ok(self.headers.get("X-Farm-Token"), TOKEN)

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- marshrutlar ---
    def do_GET(self):
        if not self._auth():
            return self._send({"ok": False, "error": "unauthorized"}, 401)
        if self.path == "/health":
            return self._send({"ok": True, "host": fl.socket.gethostname(),
                               "version": VERSION, "uptime_s": round(time.time() - START, 1)})
        if self.path == "/info":
            return self._send({"ok": True, "info": fl.system_info()})
        return self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._auth():
            return self._send({"ok": False, "error": "unauthorized"}, 401)
        if self.path != "/run":
            return self._send({"ok": False, "error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send({"ok": False, "error": "juda katta so'rov"}, 413)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            return self._send({"ok": False, "error": f"bad json: {e}"}, 400)
        result = run_job(payload)
        return self._send(result, 200 if result.get("ok") else 500)


def resolve_server(current: str | None, pointer: str | None = None) -> str | None:
    """Barqaror 'pointer' (gist) dan eng so'nggi server manzilini oladi."""
    p = pointer or _POINTER_URL
    if not p:
        return current
    try:
        import urllib.request as _u
        import time as _t
        sep = "&" if "?" in p else "?"
        with _u.urlopen(f"{p}{sep}ts={int(_t.time())}", timeout=15) as r:
            txt = r.read().decode("utf-8", "replace")
        import re as _re
        # matn yoki JSON ichidan http manzilni topamiz (gist/github o'zini tashlab)
        hits = _re.findall(r"https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%\-]+", txt)
        for h in hits:
            h = h.rstrip("/\\\",:")
            if "gist" in h or "github" in h:
                continue
            return h
    except Exception:
        pass
    return current


def pull_loop(server_url: str, name: str, pointer: str | None = None) -> None:
    """Teskari rejim: agent o'zi markazga ulanadi (NAT/firewall ortida ham ishlaydi)."""
    base = (resolve_server(server_url, pointer) or server_url).rstrip("/")
    log(f"Ferma agent {VERSION} — PULL rejimi")
    log(f"  nom    : {name}")
    log(f"  markaz : {base}")
    info = fl.system_info()
    log(f"  OS     : {info['os']} | CPU x{info['cpu_cores']} | RAM {info['ram_gb']}GB")
    log(f"  GPU    : {', '.join(info['gpus']) or 'yo`q'}")
    backoff = 3
    while True:
        try:
            fl.http_json(base + "/api/agent/hello", method="POST", token=TOKEN,
                         payload={"name": name, "info": info}, timeout=20)
            r = fl.http_json(
                base + "/api/agent/jobs?name=" + urllib.parse.quote(name) + "&wait=25",
                token=TOKEN, timeout=45) or {}
            backoff = 3
            job = r.get("job")
            if not job:
                continue
            log(f"Vazifa: {job.get('id')} — {str(job.get('cmd'))[:60]}")
            res = run_job(job)
            # natijani yuborishda qayta urinamiz (tunnel uzilishi mumkin)
            sent = False
            for attempt in range(5):
                try:
                    fl.http_json(base + "/api/agent/result", method="POST", token=TOKEN,
                                 payload={"name": name, "job_id": job.get("id"),
                                          "result": res},
                                 timeout=120)
                    sent = True
                    break
                except Exception as e:
                    log(f"  natija yuborilmadi ({attempt+1}/5): {type(e).__name__}")
                    time.sleep(3)
            log(f"  bajarildi: exit={res.get('exit')} ({res.get('duration_s')}s)"
                + ("" if sent else "  ⚠ natija yuborilmadi"))
        except KeyboardInterrupt:
            log("to'xtatildi")
            return
        except Exception as e:
            log(f"Ulanish xatosi: {type(e).__name__}: {e} — {backoff}s dan keyin qayta")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            yangi = resolve_server(base, pointer)
            if yangi and yangi != base:
                log(f"  ↻ barqaror manzildan yangisi olindi: {yangi}")
                base = yangi
                backoff = 3


def main():
    ap = argparse.ArgumentParser(description="Ferma ishchi agenti")
    ap.add_argument("--bind", default="auto", help="'auto' yoki aniq IP")
    ap.add_argument("--port", type=int, default=AGENT_PORT)
    ap.add_argument("--token", help="Token (config.json bo'lmasa yoki uni almashtirish uchun)")
    ap.add_argument("--new-token", action="store_true", help="Yangi token yaratib saqlaydi")
    ap.add_argument("--install-service", action="store_true",
                    help="Shu PC da doimiy xizmat (systemd/launchd) sifatida o'rnatadi")
    ap.add_argument("--dry-run", action="store_true", help="Xizmat faylini faqat ko'rsatadi")
    ap.add_argument("--pull", action="store_true",
                    help="Teskari rejim: markazga o'zi ulanadi (NAT ichida ishlaydi)")
    ap.add_argument("--server", help="Markaz manzili (pull uchun), masalan https://xxx.lhr.life")
    ap.add_argument("--pointer", help="Barqaror manzil (gist) — server manzili o'zgarsa shu yerdan olinadi")
    ap.add_argument("--name", help="Bu PC nomi (sukut: hostname)")
    args = ap.parse_args()

    name = args.name or fl.socket.gethostname()
    pointer = args.pointer or _POINTER_URL or None

    global TOKEN
    if args.install_service:
        parts = []
        if args.pull and args.server:
            parts += ["--pull", "--server", args.server, "--name", name]
        if pointer:
            parts += ["--pointer", pointer]
        install_service(os.path.abspath(__file__), dry_run=args.dry_run,
                        extra_args=" ".join(parts))
        return
    if args.token:
        TOKEN = args.token
    if args.new_token:
        import base64 as b64
        TOKEN = b64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        CFG["token"] = TOKEN
        fl.save_config(CFG)
        log(f"Yangi token saqlandi -> {fl.CONFIG_PATH}")
        return

    if args.pull or args.server:
        if not args.server:
            print("XATO: --pull uchun --server <URL> kerak")
            sys.exit(1)
        try:
            pull_loop(args.server, name, pointer)
        except KeyboardInterrupt:
            log("to'xtatildi")
        return

    bind = resolve_bind(args.bind)
    fl.ensure_dirs()
    srv = ThreadingHTTPServer((bind, args.port), Handler)
    info = fl.system_info()
    log(f"Ferma agent {VERSION} ishga tushdi")
    log(f"  host : {info['host']} ({info['os']} {info['arch']})")
    log(f"  CPU  : {info['cpu_model']} x{info['cpu_cores']} | RAM {info['ram_gb']}GB")
    log(f"  GPU  : {', '.join(info['gpus']) or 'yo`q'}")
    log(f"  manzil: http://{bind}:{args.port}")
    if bind == "0.0.0.0":
        log("  OGOHLANTIRISH: 0.0.0.0 — tashqi tarmoqqa ham ochiq!")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("to'xtatildi")


if __name__ == "__main__":
    main()
