# Ferma — AI-klaster boshqaruv tizimi

**Ferma** — bir nechta kompyuterni (PC / laptop / server) bitta markazdan boshqarish
tizimi. Markaz (controller) vazifalarni tarqatadi, ishchilar (worker) bajaradi va
natijani qaytaradi. Hammasi **Tailscale** xususiy tarmog'i ichida ishlaydi.

Asosiy tamoyil: **kutubxonasiz** — faqat Python 3 standart kutubxonasi (stdlib).
Hamma narsa bitta `ferma-all.pyz` faylga ham yig'iladi (zipapp).

---

## 1. Arxitektura

```
                 ┌──────────────────────────────┐
                 │  MARKAZ (controller)         │
                 │  server.py — port 8790       │
                 │  • Dashboard (brauzer)       │
                 │  • /api/nodes /api/jobs      │
                 │  • Vazifa tarqatish          │
                 │  • Natijalarni saqlash       │
                 └──────────────┬───────────────┘
                                │  Tailscale (WireGuard)
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
┌───────▼──────┐        ┌───────▼──────┐        ┌───────▼──────┐
│ Agent (pull) │        │ Agent (pull) │        │ Agent (HTTP) │
│ worker-1     │        │ worker-2     │        │ worker-3     │
└──────────────┘        └──────────────┘        └──────────────┘
```

### Komponentlar

| Fayl | Vazifasi |
|---|---|
| `src/server.py` | Markaz: HTTP API, dashboard, inventar, vazifalar navbati, login/sessiya, join sahifalari |
| `src/agent.py` | Ishchi agent: `/health`, `/info`, `/run`; **pull** rejimida markazga o'zi ulanadi |
| `src/farmctl.py` | CLI: `nodes`, `run`, `oc`, `role`, `passwd`, `token`, `join-link`, `funnel` ... |
| `src/farmlib.py` | Umumiy: config, tokenlar, HTTP klient, tizim ma'lumotlari |
| `src/discover.py` | LAN / Tailscale qurilmalarni topish |
| `src/vault.py` | Shifrlangan ombor (secrets uchun, scrypt + AES-GCM) |
| `deploy/ferma-agent.py` | Bitta faylli (standalone) agent — join orqali tarqatiladi |
| `deploy/ferma-live.py` | Ishchi PC ekranini MJPEG qilib uzatish (jonli ko'rish) |
| `tools/farmstat` | Markaz holati + statistika + oxirgi vazifalar |
| `tools/farmai` | Fermadagi online AI node'larga savol/vazifa (`opencode` orqali) |
| `tools/farmaudit` | 10 AI-agent bilan parallel arxitektura auditi |
| `tools/farmstress`, `tools/gpuburn`, `tools/metal_burn.swift` | CPU/RAM/disk/GPU stress va burn testlari |
| `ferma-all.pyz` | Yuqoridagilarning hammasi bitta faylda (`build.sh` bilan yig'iladi) |

---

## 2. Ishlash prinsipi

1. **Markaz** ishga tushadi (`serve`, sukut port `8790`). U config, inventar, vazifalar
   va natijalarni `~/.ferma/` da saqlaydi.
2. **Agentlar** `--pull` rejimida ishlaydi: markazga `/api/agent/hello` yuboradi,
   keyin `/api/agent/jobs` dan **long-poll** qilib vazifa oladi, bajaradi va
   `/api/agent/result` ga natijani qaytaradi. Shu sababli agentlar NAT/firewall
   ortida ham ishlayveradi — tashqaridan kirish shart emas.
3. **Manzil barqarorligi (pointer):** agentlarga markaz manzili GitHub Gist'dagi
   "pointer" fayldan beriladi. Markaz ko'chsa, faqat pointer yangilanadi — barcha
   agentlar keyingi so'rovda yangi manzilga o'tadi (`farmctl funnel` avtomatik
   yangilaydi).
4. **Yangi PC ulash (join):** markerda `/j/<join_key>/boot.py` havolasi bor. Bitta
   buyruq agentni yuklab olib ishga tushiradi, tokenlar avtomatik joylanadi.
5. **Token modeli:**
   * `token` — admin token (to'liq boshqaruv, faqat markazda saqlanadi);
   * `worker_token` — ishchilarga beriladigan cheklangan token (agent endpointlar);
   * parol — `config.json` da faqat **pbkdf2_sha256 hash** ko'rinishida;
   * lider sessiyasi — parol bilan kirilganda 24 soatlik sessiya (HttpOnly cookie).
6. **Rollar:** har bir node `controller` yoki `worker`. Rollar markaz config'ida
   (`node_roles`) saqlanadi. Sukut: `worker`.
7. **Dashboard:** markaz manzilini brauzerda ochib parol bilan kiriladi —
   node'lar ro'yxati, resurslar, vazifalar va natijalar ko'rinadi.

---

## 3. Tez boshlash

### Markaz (controller)

```bash
# 1) Yig'ish (ixtiyoriy — tayyor pyz bo'lsa shart emas)
./build.sh

# 2) Parol o'rnatish
python3 ferma-all.pyz passwd --gen

# 3) Markazni ishga tushirish (Tailscale IP da)
python3 ferma-all.pyz serve --bind 100.x.y.z --port 8790

# yoki systemd user service sifatida (deploy/ferma-center.service namunasi)
```

### Ishchi (worker)

```bash
# Markaz bergan join buyrug'i (universal):
python3 -c "import urllib.request;exec(urllib.request.urlopen('http://MARKAZ:8790/j/<join_key>/boot.py').read())"
```

### Asosiy CLI buyruqlari

```bash
python3 ferma-all.pyz nodes                  # node'lar holati
python3 ferma-all.pyz run --cmd "uname -a"   # barcha node'larda buyruq
python3 ferma-all.pyz oc "loyihani tekshir"  # node'lardagi opencode orqali parallel AI
python3 ferma-all.pyz role <nom> controller  # rol berish
python3 ferma-all.pyz login                  # parol bilan lider sessiyasi
python3 ferma-all.pyz join-link              # yangi PC uchun havola
python3 ferma-all.pyz jobs                   # oxirgi vazifalar
```

Qo'shimcha vositalar: `farmstat`, `farmai`, `farmaudit`, `farmstress`, `farmstress`.

---

## 4. Xavfsizlik modeli

* Markaz faqat **Tailscale IP** ga bog'lanadi (0.0.0.0 emas) — tashqi internetdan
  ko'rinmaydi; Tailscale trafigi WireGuard bilan shifrlangan.
* Barcha API so'rovlar token bilan; agentlar faqat worker token ko'radi.
* Lider kirishi: parol (pbkdf2) + 24 soatlik sessiya, login rate-limit (5 ta xato —
  300 soniya blok), `HttpOnly` + `SameSite=Lax` cookie.
* Vazifalar alohida vaqtinchalik papkalarda bajariladi; natijalar `results/` da.
* Arxitektura auditi va tavsiyalar: `docs/ferma-arxitektura-auditi-2026-09-28.md`.
* **Muhim:** repo'da hech qanday token/parol yo'q. `config.example.json` dan nusxa
  olib, birinchi ishga tushirishda qiymatlar avtomatik generatsiya qilinadi.

---

## 5. Tarix

* **2026-09-24** — Ferma g'oyasi: AI-klaster, Tailscale, Digispark onboarding.
* **2026-09-25** — Windows laptop (`403`) qo'shildi.
* **2026-09-28** — 4 node (Ozodbeks-MBP, gtx1650-pc, ryzen-ai7, asror-fedora),
  maksimal CPU+GPU stress testlar, 10-agentli arxitektura auditi.
* **2026-10-02/03** — Turniket loyihasi (ESP32) ferma bilan integratsiya qilindi.
* **2026-10-03** — Markaz Mac'dan `fedora-1` (GTX 1650) mashinasiga ko'chirildi;
  Mac fermadan chiqarildi (faqat worker sifatida qaytishi mumkin).

---

## 6. Fayl tuzilishi

```
ferma/
├── README.md
├── build.sh               # ferma-all.pyz yig'uvchi
├── config.example.json    # config namunasi (qiymatlarsiz)
├── src/                   # pyz manba kodi
├── deploy/                # standalone agent, live stream, systemd unitlar
├── tools/                 # yordamchi skriptlar (farmstat, farmai, ...)
└── docs/                  # audit hisobotlari, fleet/QA hujjatlari
```
