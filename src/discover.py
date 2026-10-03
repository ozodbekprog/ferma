#!/usr/bin/env python3
"""
discover.py — Ferma qurilmalarini avtomatik topib, inventory.json ni yangilaydi.

Manbalar:
  1) Tailscale tailnet  — o'z PC larimiz (ishonchli, shifrlangan)
  2) Ixtiyoriy: lokal subnet skanerlash (--lan) — faqat o'z Wi-Fi/LAN uchun

Foydalanish:
    python3 discover.py                 # tailnetni skanerlaydi
    python3 discover.py --lan           # + lokal subnet (ogohlantirish bilan)
    python3 discover.py --lan --subnet 192.168.1.0/24
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import farmlib as fl

CFG = fl.load_config()


def ping(ip: str, timeout: int = 2) -> bool:
    sysname = platform.system()
    if sysname == "Windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout * 1000), ip]
    elif sysname == "Darwin":
        cmd = ["ping", "-c", "1", "-t", str(timeout), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout), ip]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout + 2).returncode == 0
    except Exception:
        return False


def port_open(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def hostname_of(ip: str) -> str | None:
    try:
        socket.setdefaulttimeout(1.5)
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return None


def local_subnet() -> str | None:
    """Asosiy interfeys IP sidan /24 subnetni hisoblaydi."""
    ip = fl.lan_ip()
    if not ip:
        return None
    try:
        return str(ipaddress.ip_network(ip + "/24", strict=False))
    except Exception:
        return None


def scan_subnet(subnet: str) -> list[dict]:
    net = ipaddress.ip_network(subnet, strict=False)
    hosts = [str(h) for h in net.hosts()]
    found = []
    print(f"  lokal subnet skanerlanmoqda: {subnet} ({len(hosts)} host)…")
    with ThreadPoolExecutor(max_workers=64) as ex:
        futs = {ex.submit(ping, h, 1): h for h in hosts}
        alive = [futs[f] for f in as_completed(futs) if f.result()]
    for ip in sorted(alive, key=lambda x: tuple(map(int, x.split(".")))):
        found.append({
            "name": hostname_of(ip) or ip,
            "ip": ip,
            "os": "unknown",
            "source": "lan",
            "online": True,
        })
    print(f"  {len(found)} ta tirik host topildi")
    return found


def scan_tailnet() -> list[dict]:
    self_ip = fl.tailscale_ip()
    nodes = []
    if self_ip:
        nodes.append({
            "name": socket.gethostname(),
            "ip": self_ip,
            "os": {"Darwin": "macOS", "Windows": "windows"}.get(platform.system(), "linux"),
            "source": "tailscale",
            "role": "controller",
        })
    for p in fl.tailscale_peers():
        if not p.get("ip"):
            continue
        nodes.append({
            "name": p["name"], "ip": p["ip"], "os": p["os"],
            "source": "tailscale", "online": p.get("online", False),
        })
    return nodes


def enrich(node: dict) -> dict:
    ip = node["ip"]
    port = node.get("agent_port") or int(CFG["agent_port"])
    node["reachable"] = ping(ip)
    node["agent_port"] = port
    node["agent"] = port_open(ip, port)
    if node.get("agent"):
        try:
            res = fl.http_json(f"http://{ip}:{port}/info", token=CFG["token"], timeout=4)
            info = res.get("info", {})
            node.update({
                "host": info.get("host"), "os": info.get("os") or node.get("os"),
                "cpu": info.get("cpu_model"), "cores": info.get("cpu_cores"),
                "ram_gb": info.get("ram_gb"), "gpus": info.get("gpus", []),
                "disk": info.get("disk", {}),
            })
        except Exception as e:
            node["agent_error"] = type(e).__name__
    return node


def run_discovery(probe: bool = True, lan: bool = False, subnet: str | None = None) -> dict:
    print(f"Ferma qurilmalarini izlash… (rejim: {fl.load_config().get('network', 'auto')})")
    nodes = scan_tailnet()
    if lan or fl.load_config().get("network") == "lan":
        sub = subnet or local_subnet()
        if sub:
            nodes += scan_subnet(sub)

    # o'zimizni (asosiy IP) ro'yxatga qo'shamiz
    self_ip = fl.primary_ip()
    if self_ip and all(n.get("ip") != self_ip for n in nodes):
        nodes.append({
            "name": socket.gethostname(), "ip": self_ip,
            "os": {"Darwin": "macOS", "Windows": "windows"}.get(platform.system(), "linux"),
            "source": "self", "role": "controller",
        })

    # mavjud inventar bilan birlashtirib, qo'lda berilgan nom/rolni saqlaymiz
    old = {}
    if os.path.exists(fl.INVENTORY_PATH):
        try:
            with open(fl.INVENTORY_PATH, "r", encoding="utf-8") as f:
                for n in json.load(f).get("nodes", []):
                    old[n.get("ip")] = n
        except Exception:
            pass

    for n in nodes:
        prev = old.get(n["ip"], {})
        for key in ("role", "note"):
            if prev.get(key):
                n[key] = prev[key]

    if probe:
        print(f"  {len(nodes)} ta nomzod tekshirilmoqda…")
        with ThreadPoolExecutor(max_workers=16) as ex:
            nodes = list(ex.map(enrich, nodes))

    # takrorlarni IP bo'yicha yig'amiz (tailscale ustuvor)
    merged: dict[str, dict] = {}
    for n in nodes:
        if not n.get("ip"):
            continue
        if n["ip"] in merged and merged[n["ip"]].get("source") == "tailscale":
            continue
        merged[n["ip"]] = n
    final = sorted(merged.values(), key=lambda x: (x.get("source") != "tailscale", x["name"].lower()))

    inv = {"nodes": final, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(fl.INVENTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(inv, f, indent=2, ensure_ascii=False)

    print(f"\n{len(final)} ta qurilma inventory.json ga yozildi:\n")
    print(f"  {'NOM':22} {'IP':16} {'OS':9} {'AGENT':6} {'PING':5}")
    print("  " + "-" * 62)
    for n in final:
        print(f"  {n['name'][:21]:22} {n['ip']:16} {str(n.get('os'))[:8]:9} "
              f"{'✅' if n.get('agent') else '—':6} {'✅' if n.get('reachable') else '❌':5}")
    agents = sum(1 for n in final if n.get("agent"))
    print(f"\n  Agent o'rnatilgan va javob berayotgan: {agents}/{len(final)}")
    if agents < len(final):
        print("  Eslatma: agent yo'q PC larni farmaga qo'shish uchun deploy/ skriptlarini ishlating.")
    return inv


def main():
    ap = argparse.ArgumentParser(description="Ferma qurilmalarini topish")
    ap.add_argument("--lan", action="store_true", help="lokal subnetni ham skanerlash")
    ap.add_argument("--subnet", help="aniq subnet, masalan 192.168.1.0/24")
    ap.add_argument("--no-probe", action="store_true", help="ping/agent tekshiruvisiz")
    args = ap.parse_args()
    run_discovery(probe=not args.no_probe, lan=args.lan, subnet=args.subnet)


if __name__ == "__main__":
    main()
