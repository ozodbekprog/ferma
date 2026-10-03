#!/usr/bin/env python3
"""Ferma arxitektura auditi — 10 agentni 4 node'ga taqsimlab parallel ishga tushiradi."""
import base64
import os
import subprocess
import sys

CR = os.path.expanduser("~/.local/bin/centerrun")
HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "audit_runner.sh")

TASKS = {
    "Ozodbeks-MBP": [
        # 1/10 Markaz server
        "Sen ferma arxitektura auditi agentisan (1/10: Markaz server qatlami). Faqat O'QI, hech narsani o'zgartirma, maxfiy qiymatlarni (token/parol) chiqarma. Shell bilan tekshir: "
        "1) ls ~/Documents/Default\\ Project/ferma | head -30; "
        "2) grep -o 'self.path == \"[^\"]*\"' ~/Documents/Default\\ Project/ferma/server.py | head -20; "
        "3) tail -40 ~/Library/Logs/ferma-server.log | grep -iE 'error|traceback|xato' | head; "
        "4) lsof -nP -iTCP:8790 -sTCP:LISTEN; "
        "5) tailscale serve status 2>/dev/null | head -6; "
        "6) grep -nE 'def _handle_login|login_blocked|session_valid|rate' ~/Documents/Default\\ Project/ferma/server.py | head -12. "
        "Chiqish: TOPILMALAR — har biri [JIDDIY]/[O'RTA]/[OK] + qisqa izoh; oxirida 3 ta aniq tavsiya. O'zbek tilida yoz.",
        # 2/10 Mac xizmatlar
        "Sen ferma auditi agentisan (2/10: Mac tizim va launchd xizmatlari). Faqat o'qi. Tekshir: "
        "launchctl list | grep -i ferma; "
        "ps ax | grep -iE 'ferma|opencode' | grep -v grep | head -6; "
        "uptime; df -h / | tail -1; vm_stat | head -4; "
        "tail -20 ~/.ferma/agent.log 2>/dev/null; tailscale status | head -8. "
        "Chiqish: TOPILMALAR [JIDDIY]/[O'RTA]/[OK] + izoh; 3 ta tavsiya. O'zbekcha yoz.",
    ],
    "gtx1650-pc": [
        # 3/10 AI qatlami
        "Sen ferma auditi agentisan (3/10: AI qatlami). Faqat o'qi. Tekshir: "
        "$HOME/.opencode/bin/opencode --version; "
        "grep -o '\"model\"[^,]*' ~/.config/opencode/opencode.json | head -2; "
        "python3 -c \"import json;c=json.load(open('$HOME/.config/opencode/opencode.json'));print('deepseek kaliti:', 'bor' if c.get('provider',{}).get('deepseek',{}).get('options',{}).get('apiKey') else 'yoq')\"; "
        "timeout 60 $HOME/.opencode/bin/opencode run 'Faqat raqam: 3+4' 2>&1 | tail -2; "
        "python3 -c \"import json;c=json.load(open('$HOME/.config/opencode/opencode.json'));print('ruxsatlar:', c.get('permission'))\". "
        "Chiqish: TOPILMALAR [JIDDIY]/[O'RTA]/[OK] + 3 tavsiya. O'zbekcha yoz.",
        # 4/10 Tarmoq
        "Sen ferma auditi agentisan (4/10: Tarmoq qatlami). Faqat o'qi. Tekshir: "
        "tailscale status | head -10; "
        "for i in 1 2 3; do curl -s -o /dev/null -w '%{time_total}s ' http://100.87.253.88:8790/ping; done; echo; "
        "tailscale status 2>&1 | grep -A3 -i 'health'; "
        "ip -brief addr | head -6; "
        "curl -s -o /dev/null -w 'dns: %{time_namelookup}s connect: %{time_connect}s total: %{time_total}s\\n' http://100.87.253.88:8790/ping. "
        "Savol: 502 xatolar sababi nima bo'lishi mumkin? Chiqish: TOPILMALAR + 3 tavsiya. O'zbekcha yoz.",
        # 5/10 GUI/monitor
        "Sen ferma auditi agentisan (5/10: GUI va monitor qatlami). Faqat o'qi. Tekshir: "
        "loginctl list-sessions --no-legend | head -4; pgrep -c gnome-shell; "
        "command -v gnome-system-monitor wmctrl xdotool; ps -C gnome-system-monitor -o pid=; "
        "ls /tmp/.X11-unix/ 2>/dev/null; systemctl --user show-environment | grep -E 'DISPLAY|WAYLAND|XAUTH'; "
        "echo GDK_BACKEND=$GDK_BACKEND. "
        "Savol: masofadan GUI ochish (GDK_BACKEND=x11 + wmctrl) ishonchli ishlayaptimi, muqobil yo'llar (skrinshot/remote) bormi? "
        "Chiqish: TOPILMALAR + 3 tavsiya. O'zbekcha yoz.",
    ],
    "ryzen-ai7": [
        # 6/10 GPU
        "Sen ferma auditi agentisan (6/10: GPU qatlami). Faqat o'qi. Tekshir: "
        "nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used,temperature.gpu,power.draw,clocks.sm --format=csv,noheader; "
        "nvidia-smi | tail -6; "
        "ls /usr/lib64/libcuda.so* 2>/dev/null | head -3; "
        "python3 -c 'import ctypes; ctypes.CDLL(\"libcuda.so.1\"); print(\"libcuda OK\")' 2>&1; "
        "echo \"CUDA burn: mavjud emas (o'rnatilmagan)\". "
        "Chiqish: TOPILMALAR (GPU holati, drayver, cheklovlar) + 3 tavsiya. O'zbekcha yoz.",
        # 7/10 Reliability
        "Sen ferma auditi agentisan (7/10: Ishonchlilik). Faqat o'qi. Tekshir: "
        "systemctl --user status ferma-agent --no-pager | head -6; "
        "systemctl --user show ferma-agent -p NRestarts; "
        "journalctl --user -u ferma-agent --no-pager -n 200 -o cat 2>/dev/null | grep -icE 'xato|error|timeout|502'; "
        "journalctl --user -u ferma-agent --no-pager -n 20 -o cat 2>/dev/null | tail -10; "
        "ls -la ~/.ferma/config.json. "
        "Savol: agent uzilishga chidamlimi, restart bo'lganmi? Chiqish: TOPILMALAR + 3 tavsiya. O'zbekcha yoz.",
        # 8/10 Storage
        "Sen ferma auditi agentisan (8/10: Saqlash/disk). Faqat o'qi. Tekshir: "
        "du -sh ~/.ferma 2>/dev/null; du -sh ~/.ferma/work 2>/dev/null; "
        "ls ~/.ferma/work 2>/dev/null | wc -l; find ~/.ferma/work -maxdepth 1 -type d -mtime +2 2>/dev/null | wc -l; "
        "du -sh ~/.ferma/results 2>/dev/null; df -h / | tail -1. "
        "Chiqish: TOPILMALAR (hajm, eski joblar, xavf) + tozalash uchun aniq buyruq tavsiyasi (bajarmasdan). O'zbekcha yoz.",
    ],
    "asror-fedora": [
        # 9/10 AI v2
        "Sen ferma auditi agentisan (9/10: AI qatlami — opencode v2). Faqat o'qi. Tekshir: "
        "$HOME/.opencode/bin/opencode --version; "
        "$HOME/.opencode/bin/opencode auth list 2>&1 | head -4; "
        "$HOME/.opencode/bin/opencode models 2>&1 | head -5; "
        "python3 -c \"import json;d=json.load(open('$HOME/.config/opencode/opencode.json'));print('keys:',list(d.keys()));print('permissions:',len(d.get('permissions',[])))\"; "
        "ls ~/.local/share/opencode 2>/dev/null | head -4. "
        "Savol: nega 'models' ro'yxati bo'sh, qanday modellar ishlaydi, v1 bilan farqi nima? "
        "Chiqish: TOPILMALAR + 3 tavsiya. O'zbekcha yoz.",
        # 10/10 Security
        "Sen ferma auditi agentisan (10/10: Xavfsizlik va maxfiylik). Faqat o'qi, token/parol qiymatlarini CHIQARMA. Tekshir: "
        "ss -tlnp 2>/dev/null | head -14; "
        "ls -la ~/.ferma/config.json ~/.config/opencode/opencode.json 2>/dev/null; "
        "ss -tlnp 2>/dev/null | grep -E '8787|8790' || echo 'agent/server portlari tashqariga ochiq emas'; "
        "grep -q '\"token\"' ~/.ferma/config.json && echo 'token configda mavjud (qiymat yashirildi)'; "
        "id; ls -la ~/.ferma | head -6. "
        "Chiqish: TOPILMALAR [JIDDIY]/[O'RTA]/[OK] + 3 tavsiya. O'zbekcha yoz.",
    ],
}

procs = {}
for node, prompts in TASKS.items():
    b64s = [base64.b64encode(p.encode()).decode() for p in prompts]
    cmd = "bash audit_runner.sh " + " ".join(b64s)
    short = node.split("-")[0].split(".")[0].lower()
    out = open(os.path.join(HERE, f"audit_{short}.txt"), "w")
    p = subprocess.Popen(
        ["python3", CR, cmd, node, "300", "--file", RUNNER],
        stdout=out, stderr=subprocess.STDOUT)
    procs[node] = (p, out)
    print(f"started: {node} ({len(prompts)} agent)")

for node, (p, out) in procs.items():
    p.wait()
    out.close()
    print(f"===== {node} tugadi (rc={p.returncode})")

for node in TASKS:
    short = node.split("-")[0].split(".")[0].lower()
    fname = os.path.join(HERE, f"audit_{short}.txt")
    print()
    print("=" * 70)
    print("####", node)
    print(open(fname).read()[:9000])
