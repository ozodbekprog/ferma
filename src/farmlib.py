#!/usr/bin/env python3
"""
farmlib.py — Ferma tizimining umumiy qatlami.

Vazifasi:
  * config.json (token, portlar) ni o'qish/yaratish  -> 0600 ruxsat
  * har bir PC haqida tizim ma'lumotini yig'ish (OS, CPU, RAM, disk, GPU)
  * Tailscale IP ni aniqlash
  * controller va agent o'rtasidagi yengil HTTP mijozi

Tashqi kutubxona yo'q — faqat Python standart kutubxonasi.
"""
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
