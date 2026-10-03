#!/usr/bin/env python3
# ============================================================================
#  FERMA AGENT — bitta o'zi yetarli (standalone) fayl
#  Avtomatik yaratildi: build_standalone.py. Qo'lda tahrirlamang.
#
#  Ishlatish:
#      python3 ferma-agent.py                 # Tailscale IP ni o'zi topadi
#      python3 ferma-agent.py --port 8787
#
#  DIQQAT: bu fayl ichida farma TOKENi bor — hech kimga bermang (0600).
# ============================================================================
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ----------------------------------------------------------------------------
# Yo'llar va standartlar
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
# FERMA_HOME orqali sozlamalar papkasini almashtirish mumkin (masalan bitta
# faylli paket ishlaganda: FERMA_HOME=~/.ferma).
_BASE = os.path.expanduser(os.environ["FERMA_HOME"]) if os.environ.get("FERMA_HOME") else HERE
CONFIG_PATH = os.path.join(_BASE, "config.json")
INVENTORY_PATH = os.path.join(_BASE, "inventory.json")
RESULTS_DIR = os.path.join(_BASE, "results")

DEFAULT_AGENT_PORT = 8787
DEFAULT_SERVER_PORT = 8790


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def default_config() -> dict:
    return {
        "token": base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="),
        # Ishchilar uchun cheklangan token — agentlarga faqat shu beriladi.
        "worker_token": base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="),
        "join_key": base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("="),
        # Markaz paroli (pbkdf2_sha256 hash) — "bosh PC" roli uchun.
        # O'rnatish: python3 farmctl.py passwd --gen
        "admin_password": "",
        # Node rollari: {"Nom": "controller"|"worker"} (farmctl.py role ...)
        "node_roles": {},
        "agent_port": DEFAULT_AGENT_PORT,
        "server_port": DEFAULT_SERVER_PORT,
        "server_bind": "auto",   # dashboard faqat lokal ochiladi
        "agent_bind": "auto",          # "auto" = Tailscale IP, aks holda IP
        "network": "auto",             # auto | tailscale | lan
    }


def load_config(path: str = CONFIG_PATH, create: bool = True) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # Yetishmayotgan kalitlarni to'ldiramiz
        changed = False
        for k, v in default_config().items():
            if k not in cfg:
                cfg[k] = v
                changed = True
        if changed and create:
            save_config(cfg, path)
        return cfg
    cfg = default_config()
    if create:
        save_config(cfg, path)
    return cfg


def save_config(cfg: dict, path: str = CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Parol (markaz / "bosh PC" roli uchun)
# ----------------------------------------------------------------------------
PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Parolni pbkdf2_sha256 hash ko'rinishida qaytaradi (config uchun)."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Parolni saqlangan hash bilan solishtiradi (constant-time)."""
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        salt = base64.urlsafe_b64decode(pad(salt_b64))
        expected = base64.urlsafe_b64decode(pad(hash_b64))
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def gen_password(length: int = 24) -> str:
    """O'qish oson, lekin kuchli parol (A-Z a-z 0-9 - _)."""
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ----------------------------------------------------------------------------
# Tizim ma'lumotlari (cross-platform)
# ----------------------------------------------------------------------------
def _run(cmd, timeout=8):
    try:
        out = subprocess.run(
            cmd, shell=isinstance(cmd, str), capture_output=True,
            text=True, timeout=timeout,
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


def cpu_model() -> str:
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            return _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor()
        if sysname == "Linux":
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        if sysname == "Windows":
            out = _run(["wmic", "cpu", "get", "name"])
            lines = [l.strip() for l in out.splitlines() if l.strip() and l.strip().lower() != "name"]
            if lines:
                return lines[0]
    except Exception:
        pass
    return platform.processor() or "unknown"


def total_ram_gb() -> float:
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            return round(int(_run(["sysctl", "-n", "hw.memsize"])) / 1e9, 1)
        if sysname == "Linux":
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return round(int(line.split()[1]) * 1024 / 1e9, 1)
        if sysname == "Windows":
            out = _run(["wmic", "ComputerSystem", "get", "TotalPhysicalMemory"])
            nums = re.findall(r"\d+", out)
            if nums:
                return round(int(max(nums, key=len)) / 1e9, 1)
    except Exception:
        pass
    return 0.0


def gpu_info() -> list[str]:
    sysname = platform.system()
    gpus: list[str] = []
    try:
        if sysname == "Darwin":
            out = _run(["system_profiler", "SPDisplaysDataType"], timeout=20)
            for m in re.finditer(r"Chipset Model:\s*(.+)", out):
                gpus.append(m.group(1).strip())
        elif sysname == "Linux":
            out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
            if out:
                gpus += [l.strip() for l in out.splitlines() if l.strip()]
            if not gpus:
                out = _run("lspci | grep -Ei 'vga|3d|display'")
                gpus += [l.strip() for l in out.splitlines() if l.strip()]
        elif sysname == "Windows":
            out = _run(["wmic", "path", "win32_VideoController", "get", "name"])
            gpus += [l.strip() for l in out.splitlines()
                     if l.strip() and l.strip().lower() != "name"]
    except Exception:
        pass
    return gpus


def disk_info(path: str = "/") -> dict:
    try:
        target = path
        if platform.system() == "Windows":
            target = os.environ.get("SystemDrive", "C:") + "\\"
        u = shutil.disk_usage(target)
        return {"total_gb": round(u.total / 1e9, 1),
                "free_gb": round(u.free / 1e9, 1)}
    except Exception:
        return {"total_gb": 0.0, "free_gb": 0.0}


def uptime_s() -> float:
    try:
        if platform.system() == "Linux":
            with open("/proc/uptime") as f:
                return float(f.read().split()[0])
        if platform.system() == "Darwin":
            out = _run(["sysctl", "-n", "kern.boottime"])
            m = re.search(r"sec\s*=\s*(\d+)", out)
            if m:
                return time.time() - int(m.group(1))
    except Exception:
        pass
    return 0.0


def system_info() -> dict:
    return {
        "host": socket.gethostname(),
        "os": platform.system(),
        "os_version": platform.release(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpu_model": cpu_model(),
        "cpu_cores": os.cpu_count(),
        "ram_gb": total_ram_gb(),
        "gpus": gpu_info(),
        "disk": disk_info(),
        "uptime_s": round(uptime_s(), 1),
        "tailscale_ip": tailscale_ip(),
        "agent_port": load_config().get("agent_port", DEFAULT_AGENT_PORT),
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ----------------------------------------------------------------------------
# Tailscale
# ----------------------------------------------------------------------------
def _tailscale_bin() -> str | None:
    candidates = [
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        shutil.which("tailscale"),
        r"C:\Program Files\Tailscale\tailscale.exe",
        "/usr/bin/tailscale",
        "/usr/local/bin/tailscale",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def tailscale_installed() -> bool:
    return _tailscale_bin() is not None


def tailscale_status() -> dict | None:
    """tailscale status --json natijasini qaytaradi (bo'lmasa None)."""
    binp = _tailscale_bin()
    if not binp:
        return None
    try:
        out = subprocess.run([binp, "status", "--json"], capture_output=True,
                             text=True, timeout=10)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


_TS_IP_RE = re.compile(r"\b(100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b")


def _ip_from_interfaces() -> str | None:
    """Tailscale IP ni tarmoq interfeyslaridan topadi (CLI'siz, ishonchli)."""
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            out = _run(["ifconfig", "-a"])
        elif sysname == "Linux":
            out = _run(["ip", "-4", "addr"]) or _run(["ifconfig", "-a"])
            if not out:
                with open("/proc/net/fib_trie", "r", encoding="utf-8", errors="ignore") as f:
                    out = f.read()
        elif sysname == "Windows":
            out = _run(["ipconfig"])
        else:
            out = ""
    except Exception:
        out = ""
    m = _TS_IP_RE.search(out)
    return m.group(1) if m else None


def tailscale_ip() -> str | None:
    st = tailscale_status()
    if st:
        self_node = st.get("Self") or {}
        ips = self_node.get("TailscaleIPs") or []
        for ip in ips:
            if ip.startswith("100."):
                return ip
        if ips:
            return ips[0]
    # CLI ishlamasa (masalan macOS launchd kontekstida) — interfeysdan olamiz
    return _ip_from_interfaces()


def lan_ip() -> str | None:
    """Lokal tarmoqdagi asosiy IP (Tailscale bo'lmagan)."""
    import socket as _socket
    for probe in (("1.1.1.1", 80), ("8.8.8.8", 80)):
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            s.connect(probe)
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            continue
    return None


def primary_ip(mode: str | None = None) -> str | None:
    """
    Ferma uchun asosiy IP. Rejim config 'network' kalitidan olinadi:
      auto      -> Tailscale bo'lsa o'sha, aks holda LAN  (sukut)
      tailscale -> faqat Tailscale
      lan       -> faqat lokal tarmoq (Tailscale'siz mustaqil ishlash)
    """
    mode = mode or load_config().get("network", "auto")
    if mode == "tailscale":
        return tailscale_ip()
    if mode == "lan":
        return lan_ip()
    return tailscale_ip() or lan_ip()


def tailscale_peers() -> list[dict]:
    st = tailscale_status()
    peers = []
    if not st:
        return peers
    for _, p in (st.get("Peer") or {}).items():
        ips = p.get("TailscaleIPs") or []
        v4 = next((i for i in ips if i.startswith("100.")), ips[0] if ips else None)
        peers.append({
            "name": (p.get("HostName") or "").strip() or p.get("DNSName", "?").split(".")[0],
            "ip": v4,
            "os": p.get("OS", "unknown"),
            "online": bool(p.get("Online")),
            "last_seen": p.get("LastSeen", ""),
            "tags": p.get("Tags") or [],
        })
    peers.sort(key=lambda x: (not x["online"], x["name"].lower()))
    return peers


# ----------------------------------------------------------------------------
# HTTP yordamchilari (faqat Tailscale ichida ishlatiladi)
# ----------------------------------------------------------------------------
def http_json(url: str, method: str = "GET", payload: dict | None = None,
              token: str | None = None, timeout: float = 10.0, raw: bytes | None = None):
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Farm-Token"] = token
    if raw is not None:
        data = raw
        headers["Content-Type"] = "application/octet-stream"
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def token_ok(given: str | None, expected: str) -> bool:
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


def ensure_dirs() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)

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


MAX_BODY = 256 * 1024 * 1024      # 256 MB kiruvchi
MAX_OUT = 512 * 1024              # 512 KB stdout/stderr
MAX_ARTIFACT = 512 * 1024 * 1024  # 512 MB bitta natija fayli

WORK_ROOT = os.path.join(os.path.expanduser("~"), ".ferma", "work")
VERSION = "0.1.0"
START = time.time()
_POINTER_URL = "https://gist.githubusercontent.com/ozodbekprog/3319c09855eb4cae368a11383386d440/raw/ferma-server.txt"   # build_standalone tomonidan to'ldiriladi (barqaror manzil)

CFG = load_config()
# Agent faqat WORKER token bilan ishlaydi (admin token markazda qoladi).
_EMBEDDED_TOKEN = ""   # markazga ulanishda o'z tokeningizni qo'ying (FARM_TOKEN yoki config.json)
TOKEN = os.environ.get("FARM_TOKEN") or _EMBEDDED_TOKEN or CFG["token"]
AGENT_PORT = int(CFG["agent_port"])


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def resolve_bind(requested: str) -> str:
    """Agent qaysi manzilga bog'lanishini aniqlaydi (mustahkam, retry bilan)."""
    if requested and requested != "auto":
        return requested
    mode = load_config().get("network", "auto")

    if mode in ("auto", "tailscale") and tailscale_installed():
        # Tailscale hali ko'tarilmagan bo'lishi mumkin — kutamiz
        for attempt in range(8):
            ip = tailscale_ip()
            if ip:
                return ip
            if attempt == 0:
                log("Tailscale IP kutilmoqda…")
            time.sleep(1)

    if mode == "lan":
        ip = lan_ip()
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
                "host": socket.gethostname(),
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
            "host": socket.gethostname(),
            "exit": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_s": dur,
            "artifacts": artifacts,
            "workdir": workdir,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "job_id": job_id, "error": f"timeout ({timeout}s)",
                "host": socket.gethostname()}
    except Exception as e:
        return {"ok": False, "job_id": job_id, "error": f"{type(e).__name__}: {e}",
                "host": socket.gethostname()}


class Handler(BaseHTTPRequestHandler):
    server_version = "FermaAgent/" + VERSION

    def log_message(self, fmt, *args):  # sukut bo'yicha shovqinni kamaytiramiz
        if os.environ.get("FERMA_DEBUG"):
            super().log_message(fmt, *args)

    # --- yordamchilar ---
    def _auth(self) -> bool:
        return token_ok(self.headers.get("X-Farm-Token"), TOKEN)

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
            return self._send({"ok": True, "host": socket.gethostname(),
                               "version": VERSION, "uptime_s": round(time.time() - START, 1)})
        if self.path == "/info":
            return self._send({"ok": True, "info": system_info()})
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
    info = system_info()
    log(f"  OS     : {info['os']} | CPU x{info['cpu_cores']} | RAM {info['ram_gb']}GB")
    log(f"  GPU    : {', '.join(info['gpus']) or 'yo`q'}")
    backoff = 3
    while True:
        try:
            http_json(base + "/api/agent/hello", method="POST", token=TOKEN,
                         payload={"name": name, "info": info}, timeout=20)
            r = http_json(
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
                    http_json(base + "/api/agent/result", method="POST", token=TOKEN,
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

    name = args.name or socket.gethostname()
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
        save_config(CFG)
        log(f"Yangi token saqlandi -> {CONFIG_PATH}")
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
    ensure_dirs()
    srv = ThreadingHTTPServer((bind, args.port), Handler)
    info = system_info()
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
