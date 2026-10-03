# Ferma arxitektura auditi — 2026-09-28

**Usul:** 10 ta AI agent 4 ta online node'ning opencode'larida parallel ishga tushirildi (~30 soniya).
**Qamrov:** Ozodbeks-MBP (2 agent), gtx1650-pc (3), ryzen-ai7 (3), asror-fedora (2).
**Offline (qamrovsiz):** 403_ (Windows), Fedora.

---

## 1. Umumiy holat

| Node | Rol | Holat | GPU | AI (opencode) |
|---|---|---|---|---|
| Ozodbeks-MBP | controller | online (601–633ms) | Intel UHD + AMD 5500M | 1.18.32, deepseek auth bor |
| gtx1650-pc | worker | online | GTX 1650 4GB | 1.18.33, deepseek-flash ✓ |
| ryzen-ai7 | worker | online | RTX 5060 8GB | 1.18.33, deepseek-flash ✓ |
| asror-fedora | worker | online | Intel iGPU | v2.0.10, free model |

Jami: RAM 66GB, bo'sh disk ~1.6TB. Agentlar: uzilishga chidamli, `NRestarts=0`.

---

## 2. Kritik topilmalar (ustuvor tuzatish kerak)

### 2.1 asror-fedora: RDP/VNC ochiq turibdi — [JIDDIY]
- `gnome-remote-desktop` **enabled** va tinglayapti: `*:3389` (RDP), `*:5900` (VNC).
- Firewall'da vnc-server ruxsati yo'q, lekin servis barcha interfeyslarda ochiq — firewall o'chsa yoki port ruxsat berilsa, tashqi kirish xavfi.
- **Tavsiya:** `systemctl --user disable --now gnome-remote-desktop` (kerak bo'lmasa) yoki faqat Tailscale/LAN'ga cheklash.

### 2.2 Markaz server (Mac): `0.0.0.0:8790`, TLS yo'q, token URL'da — [JIDDIY]
- `config.json → server_bind = "0.0.0.0"`; `lsof: TCP *:8790 (LISTEN)` — LAN'dagi har qanday qurilma ko'radi, HTTP (TLS yo'q).
- Admin token `?token=` query orqali qabul qilinadi va `localStorage`'da saqlanadi (log/history orqali oqishi mumkin).
- Sessiya IP'ga bog'lanmagan; `sid` cookie + JSON tanada qaytadi; `Secure` flag http sabab qo'yilmaydi.
- **Yaxshi:** login rate-limit (5/300s), HttpOnly+SameSite=Lax, nosniff.
- **Tavsiya:** bind'ni `127.0.0.1` yoki Tailscale IP qilish; query-token'ni o'chirish; sessiyani IP'ga bog'lash; HTTPS.

### 2.3 Mac: agent restart loop + RAM bosimi — [JIDDIY → hozir yaxshilangan]
- 13:20–14:00 orasida agent 4 marta qayta ishga tushgan (hostname o'zgarishi bilan).
- Maksimal test paytida bo'sh RAM ~46MB gacha tushgan; hozir ~1.6GB (Chrome 3.8%, opencode-cli 2.8%).
- **Tavsiya:** restart sababini `log show` bilan aniqlash; RAM limiti/monitoring; stress testlarni rejalashtirilgan vaqtda o'tkazish.

---

## 3. O'rta daraja

| # | Node | Topilma | Tavsiya |
|---|---|---|---|
| 3.1 | gtx1650 | Tailscale map yangilanmayapti, barcha peerlar relay orqali; uplink iPhone hotspotda (172.20.10.6) | `tailscale up` yangilash; direct uchun UDP 41641; barqaror Wi-Fi |
| 3.2 | gtx1650 | 502 xatolar — proxy/backend tomonida; `/ping` esa ishlaydi (~0.65s, TLS connect 219ms) | Markazdagi proxy port/backend health-check |
| 3.3 | gtx1650 | OpenCode ruxsatlari juda ochiq (bash/edit/external_directory allow) + API kalit config faylida (600) | Read-only audit profili; kalitni env/secrets'ga; fayl 600 ✓ |
| 3.4 | ryzen | GPU: RTX 5060 8GB, driver 615.71.09, VRAM 2.4GB ni ollama band qilgan; ~5.5GB bo'sh | 7B Q4 limit; drayver/CUDA mosligi; PowerMizer balans |
| 3.5 | asror | opencode v2: auth integratsiyalar yo'q, modellar ro'yxati bo'sh (free model ishlaydi) | Auth ulash yoki modelni explicit sozlash |
| 3.6 | Mac | sessiya/token mexanimi (2.2 ga qarang) | X-Farm-Token header + cookie only |

---

## 4. Past / OK

- **Saqlash (ryzen):** `~/.ferma/work` atigi 3.6MB, 250 yozuv, 64 eski job (>2 kun). Tozalash mumkin: `find ~/.ferma/work -maxdepth 1 -type d -mtime +2 -exec rm -rf {} +`
- **Ishonchlilik (ryzen):** agent qulashsiz ishlayapti, xatolar tarmoq/SSL tomonida (upstream 502), backoff bor — lekin eksponensial qilish tavsiya.
- **GUI (gtx1650):** `GDK_BACKEND=x11 + wmctrl` qisman ishonchli; `xdotool` yo'q; muqobil — `gnome-remote-desktop` (RDP) yoki Wayland skrinshot (`grim`/DBus).
- **Disk (Mac):** 838GB bo'sh — OK. **Login himoyasi:** yaxshi.

---

## 5. Ustuvor reja (3 qadam)

1. **Xavfsizlik:** asror'da RDP/VNC o'chirish; Mac server bind'ni localhost/Tailscale'ga o'tkazish; query-token'ni olib tashlash.
2. **Barqarorlik:** Tailscale direct ulanish + 502 manbasini tuzatish; Mac agent restart sababini aniqlash; RAM monitoring.
3. **Standartlashtirish:** AI layer (model/auth) bir xil; eski joblarni tozalash; audit'ni har hafta `farmaudit` bilan.

---

## 6. Auditni qayta o'tkazish

```bash
farmaudit          # 10 agentni 4 node'ga taqsimlab parallel ishga tushiradi
```

Natijalar: `~/.ferma/audit/audit_<node>.txt`
