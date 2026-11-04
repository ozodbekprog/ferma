# CONTRACT.md — Ferma jamoasi shartnomasi (v1)

Bu fayl butun jamoa (7+ kishi) bitta loyihada parallel ishlashi uchun **yagona qonun**.
Kod yozishdan oldin shu fayl o'qiladi. O'zgartirish — faqat PR orqali, Tony tasdiqi bilan.

## 0. Asosiy tamoyillar

1. **Har bir soha — bitta egada.** Boshqalarning fayllariga tegilinmaydi.
2. **Bitta vazifa — bitta odam.** Ikki kishi bitta ishga (bug, fayl) birga tegmaydi.
3. **Avval kelishuv, keyin kod.** API, config va umumiy fayllar o'zgarishi — avval shu faylga yoziladi.
4. **Kutubxonasiz qoida.** Faqat Python 3 stdlib. Yangi pip-paket — alohida kelishuv bilan.
5. **Maxfiy narsa repoga tushmaydi.** Token/parol faqat `config.json` da (`.gitignore` da).

## 1. Modul egaligi (har kim faqat o'ziniki)

| Soha | Mas'ul | Fayllar / papkalar |
|---|---|---|
| Markaz (yadro) | `<ism>` | `src/server.py` |
| Agent (ishchi) | `<ism>` | `src/agent.py`, `deploy/ferma-agent.py` |
| CLI | `<ism>` | `src/farmctl.py`, `src/__main__.py` |
| Umumiy yadro | `<ism>` | `src/farmlib.py`, `config.example.json` |
| Xavfsizlik / ombor | `<ism>` | `src/vault.py` |
| Tarmoq / topish | `<ism>` | `src/discover.py` |
| Deploy / vositalar | `<ism>` | `deploy/`, `build.sh`, `tools/` |
| Hujjat / QA | `<ism>` | `docs/`, reliz tekshiruvlari |
| Lider | Tony | Vazifa tayinlash, umumiy fayllar tasdiqi, birlashtirish |

- Ismlar jadvalga yoziladi. Soha vaqtincha bo'sh bo'lsa — Tony mas'ul tayinlaydi.
- Yangi fayl kerak bo'lsa — o'z sohangiz doirasida yarating va egasiga xabar bering.

## 2. Muzlatilgan fayllar (faqat kelishuv bilan)

| Fayl | Nima uchun |
|---|---|
| `src/farmlib.py` | Umumiy yadro: config, parol, tizim ma'lumotlari, HTTP klient — hamma unga tayanadi |
| `config.example.json` | Sozlamalar shartnomasi — yangi sozlama faqat shu yerga qo'shiladi |
| `src/__main__.py` | Buyruqlar dispetcheri |
| `build.sh` | Yig'ish tartibi |
| `.gitignore` | Maxfiylik kafolati |
| `CONTRACT.md` | Shu faylning o'zi |

O'zgartirish kerak bo'lsa: PR ochib, soha egasi va Tonyni belgilaysiz — tasdiqsiz o'zgartirilmaydi.
`ferma-all.pyz` — yig'iladigan artefakt, repoga commit qilinmaydi.

## 3. API shartnomasi

Javoblar yagona formatda:

```json
{ "ok": true, "...": "..." }
{ "ok": false, "error": "Inson o'qiy oladigan xabar" }
```

Status kodlari: `400` validatsiya, `401` token yo'q, `403` ruxsat yo'q, `404` topilmadi, `409` konflikt.

Agent yo'llari (`/api/agent/hello`, `/api/agent/jobs`, `/api/agent/result`) — **moslik qatlami**:
o'zgarsa, markaz va agent birga yangilanadi; eski agentlar ishlashda davom etishi kerak
(yangi maydon qo'shiladi, mavjudi o'chirilmaydi).

Yangi endpoint qo'shishdan oldin shu faylga yoziladi (yo'l, kirim, chiqim) — keyin kod.

## 4. Git tartibi

- Branch: `feat/<ism>-<mavzu>` yoki `fix/<ism>-<mavzu>` (masalan `feat/aziz-live-view`).
- `main` — himoyalangan: faqat PR orqali, kamida 1 review. To'g'ridan-to'g'ri push yo'q.
- Pushdan oldin: `git pull --rebase origin main`.
- Bitta PR — bitta vazifa. Katta o'zgarishni kichik PR'larga bo'ling.
- Review 24 soat ichida (avval soha egasi, keyin Tony). Kechiksa — Tonyga yozing.
- Konflikt: o'z fayllaringizni saqlaysiz, umumiy fayl bo'lsa egasiga xabar berasiz.

## 5. Bug qoidasi (eng muhim)

1. **Topgan — tuzatmaydi.** O'z moduli bo'lmasa, taxtaga/guruhga yozadi:
   qayerda, qanday takrorlanadi, kutilgan natija qanday.
2. Tuzatuvchi — **bitta odam**: Tony yoki soha egasi tayinlaydi.
   Bitta bug = bitta branch = bitta PR.
3. Tuzatilgach — `docs/FERMA-QA.md` ga tekshiruv qadami yoziladi (qaytarilib qo'ymasligi uchun).
4. Ikki kishi bir xil muammoni ko'rsa — birinchisi yozib qo'yadi, ikkinchisi takrorlamaydi.

## 6. Vazifani chegarasi bilan berish (shablon)

```
Vazifa:      <nima qilinadi>
Kim:         <ism>
Fayllar:     <faqat shu fayllar / papka>
Qachon:      <sana>
Tayyor:      <natija qanday bo'lsa tugagan hisoblanadi>
Tegilmaydi:  <boshqa modullar>
```

"Shu narsani qil" emas — yuqoridagidek. Har vazifa taxtada turadi:
**Navbatda → Bajarilmoqda → Review → Tayyor.**

## 7. Definition of Done (har vazifa)

1. `python3 -m py_compile src/*.py` va `bash build.sh` xatosiz o'tadi; natija qo'lda tekshirilgan.
2. Stdlib-only qoidasi buzilmagan (yangi import — stdlib).
3. Faqat o'z sohangiz fayllarida o'zgarish.
4. Maxfiy qiymatlar yo'q; yangi sozlama bo'lsa — `config.example.json` ga qo'shilgan.
5. PR izohida: nima o'zgardi, qanday tekshirildi.
6. Kamida 1 review tasdiqi.

## 8. Kun tartibi

- Har kuni 10 daqiqa (yoki Telegram'da yozma): kecha nima qildim / bugun nima qilaman / nima to'sqinlik qilyapti.
- Har hafta: nima tayyor bo'ldi, keyingi hafta rejasi.
- Savol: avval soha egasiga, javob bo'lmasa — Tonyga.

## 9. AI vositalar bilan ishlash

- AI (masalan opencode) faqat o'z sohangiz doirasida ishlaydi — chegara o'zgarmaydi.
- AI yozgan kodni odam tekshiradi; PR izohida "AI yordamida" deb yoziladi.
- AI'ga vazifa 6-bo'limdagi shaklda beriladi.

---
Versiya: v1 · 2026-10-04 · O'zgartirishlar faqat PR orqali.
