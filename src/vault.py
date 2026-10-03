#!/usr/bin/env python3
"""
vault.py — Ferma shifrlangan ombori (bitta fayl, faqat stdlib).

- Ma'lumotlar ChaCha20-Poly1305 (RFC 8439) bilan shifrlanadi;
  kalit paroldan scrypt orqali olinadi.
- Fayl yashirin joyda, tasodifiy nom bilan saqlanadi:
      ~/.ferma/.vault/<16-hex>.bin      (shifrlangan ombor)
      ~/.ferma/.vault/.<16-hex>.cnt     (xato urinishlar hisoblagichi)
- XAVFSIZLIK QOIDASI: parol KETMA-KET 2 marta xato kiritilsa,
  ombor avtomatik va qaytarilmas o'chiriladi (wipe).

Ishlatish:
    python3 vault.py init            # yangi ombor + parol
    python3 vault.py put <nom> --file <fayl>   # fayl qo'shish
    python3 vault.py put <nom> --text "matn"   # matn qo'shish
    python3 vault.py get <nom> [--out fayl]    # olish
    python3 vault.py ls
    python3 vault.py rm <nom>
    python3 vault.py passwd          # parolni almashtirish
    python3 vault.py status
    python3 vault.py wipe            # qo'lda o'chirish (tasdiq bilan)
    python3 vault.py selftest        # kripto test (RFC 8439 vektori)
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import struct
import sys
import time

# ----------------------------------------------------------------------------
# ChaCha20-Poly1305 (RFC 8439) — pure Python, stdlib only
# ----------------------------------------------------------------------------
_MASK32 = 0xFFFFFFFF


def _rotl32(v: int, c: int) -> int:
    return ((v << c) & _MASK32) | (v >> (32 - c))


def _chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    constants = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
    state = list(constants) + list(struct.unpack("<8I", key)) + [counter] \
        + list(struct.unpack("<3I", nonce))
    working = state[:]
    for _ in range(10):
        # column rounds
        working[0] = (working[0] + working[4]) & _MASK32
        working[12] = _rotl32(working[12] ^ working[0], 16)
        working[8] = (working[8] + working[12]) & _MASK32
        working[4] = _rotl32(working[4] ^ working[8], 12)
        working[0] = (working[0] + working[4]) & _MASK32
        working[12] = _rotl32(working[12] ^ working[0], 8)
        working[8] = (working[8] + working[12]) & _MASK32
        working[4] = _rotl32(working[4] ^ working[8], 7)
        working[1] = (working[1] + working[5]) & _MASK32
        working[13] = _rotl32(working[13] ^ working[1], 16)
        working[9] = (working[9] + working[13]) & _MASK32
        working[5] = _rotl32(working[5] ^ working[9], 12)
        working[1] = (working[1] + working[5]) & _MASK32
        working[13] = _rotl32(working[13] ^ working[1], 8)
        working[9] = (working[9] + working[13]) & _MASK32
        working[5] = _rotl32(working[5] ^ working[9], 7)
        working[2] = (working[2] + working[6]) & _MASK32
        working[14] = _rotl32(working[14] ^ working[2], 16)
        working[10] = (working[10] + working[14]) & _MASK32
        working[6] = _rotl32(working[6] ^ working[10], 12)
        working[2] = (working[2] + working[6]) & _MASK32
        working[14] = _rotl32(working[14] ^ working[2], 8)
        working[10] = (working[10] + working[14]) & _MASK32
        working[6] = _rotl32(working[6] ^ working[10], 7)
        working[3] = (working[3] + working[7]) & _MASK32
        working[15] = _rotl32(working[15] ^ working[3], 16)
        working[11] = (working[11] + working[15]) & _MASK32
        working[7] = _rotl32(working[7] ^ working[11], 12)
        working[3] = (working[3] + working[7]) & _MASK32
        working[15] = _rotl32(working[15] ^ working[3], 8)
        working[11] = (working[11] + working[15]) & _MASK32
        working[7] = _rotl32(working[7] ^ working[11], 7)
        # diagonal rounds
        working[0] = (working[0] + working[5]) & _MASK32
        working[15] = _rotl32(working[15] ^ working[0], 16)
        working[10] = (working[10] + working[15]) & _MASK32
        working[5] = _rotl32(working[5] ^ working[10], 12)
        working[0] = (working[0] + working[5]) & _MASK32
        working[15] = _rotl32(working[15] ^ working[0], 8)
        working[10] = (working[10] + working[15]) & _MASK32
        working[5] = _rotl32(working[5] ^ working[10], 7)
        working[1] = (working[1] + working[6]) & _MASK32
        working[12] = _rotl32(working[12] ^ working[1], 16)
        working[11] = (working[11] + working[12]) & _MASK32
        working[6] = _rotl32(working[6] ^ working[11], 12)
        working[1] = (working[1] + working[6]) & _MASK32
        working[12] = _rotl32(working[12] ^ working[1], 8)
        working[11] = (working[11] + working[12]) & _MASK32
        working[6] = _rotl32(working[6] ^ working[11], 7)
        working[2] = (working[2] + working[7]) & _MASK32
        working[13] = _rotl32(working[13] ^ working[2], 16)
        working[8] = (working[8] + working[13]) & _MASK32
        working[7] = _rotl32(working[7] ^ working[8], 12)
        working[2] = (working[2] + working[7]) & _MASK32
        working[13] = _rotl32(working[13] ^ working[2], 8)
        working[8] = (working[8] + working[13]) & _MASK32
        working[7] = _rotl32(working[7] ^ working[8], 7)
        working[3] = (working[3] + working[4]) & _MASK32
        working[14] = _rotl32(working[14] ^ working[3], 16)
        working[9] = (working[9] + working[14]) & _MASK32
        working[4] = _rotl32(working[4] ^ working[9], 12)
        working[3] = (working[3] + working[4]) & _MASK32
        working[14] = _rotl32(working[14] ^ working[3], 8)
        working[9] = (working[9] + working[14]) & _MASK32
        working[4] = _rotl32(working[4] ^ working[9], 7)
    out = [(working[i] + state[i]) & _MASK32 for i in range(16)]
    return struct.pack("<16I", *out)


def _chacha20_xor(key: bytes, nonce: bytes, counter: int, data: bytes) -> bytes:
    out = bytearray()
    for offset in range(0, len(data), 64):
        block = _chacha20_block(key, counter + offset // 64, nonce)
        chunk = data[offset:offset + 64]
        n = len(chunk)
        mixed = (int.from_bytes(chunk, "little")
                 ^ int.from_bytes(block[:n], "little"))
        out += mixed.to_bytes(n, "little")
    return bytes(out)


def _poly1305_mac(key: bytes, msg: bytes) -> bytes:
    assert len(key) == 32
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    p = (1 << 130) - 5
    acc = 0
    for offset in range(0, len(msg), 16):
        block = msg[offset:offset + 16]
        n = int.from_bytes(block + b"\x01", "little")
        acc = ((acc + n) * r) % p
    acc = (acc + s) & ((1 << 128) - 1)
    return acc.to_bytes(16, "little")


def _pad16(data: bytes) -> bytes:
    if len(data) % 16 == 0:
        return b""
    return b"\x00" * (16 - (len(data) % 16))


def chacha20_poly1305_encrypt(key: bytes, nonce: bytes, aad: bytes,
                              plaintext: bytes) -> bytes:
    """ciphertext + 16 baytli Poly1305 tag."""
    otk = _chacha20_block(key, 0, nonce)[:32]
    ciphertext = _chacha20_xor(key, nonce, 1, plaintext)
    mac_data = (aad + _pad16(aad) + ciphertext + _pad16(ciphertext)
                + struct.pack("<Q", len(aad)) + struct.pack("<Q", len(ciphertext)))
    return ciphertext + _poly1305_mac(otk, mac_data)


def chacha20_poly1305_decrypt(key: bytes, nonce: bytes, aad: bytes,
                              data: bytes) -> bytes | None:
    """Muvaffaqiyatda plaintext; aks holda None (teg mos emas)."""
    if len(data) < 16:
        return None
    ciphertext, tag = data[:-16], data[-16:]
    otk = _chacha20_block(key, 0, nonce)[:32]
    mac_data = (aad + _pad16(aad) + ciphertext + _pad16(ciphertext)
                + struct.pack("<Q", len(aad)) + struct.pack("<Q", len(ciphertext)))
    if not hmac.compare_digest(_poly1305_mac(otk, mac_data), tag):
        return None
    return _chacha20_xor(key, nonce, 1, ciphertext)


# ----------------------------------------------------------------------------
# Ombor (vault)
# ----------------------------------------------------------------------------
FERMA_HOME = os.path.expanduser(os.environ.get("FERMA_HOME") or "~/.ferma")
HIDDEN_DIR = os.path.join(FERMA_HOME, ".vault")
LOCATOR = os.path.join(FERMA_HOME, ".vault.loc")   # ombor fayl nomini biladi

HEADER_MAGIC = "ferma-vault"
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
MAX_TRIES = 2          # shuncha xatodan keyin — o'chirish


class VaultError(Exception):
    pass


class WrongPassword(VaultError):
    pass


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.b64decode(text + pad)


def _derive_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=n, r=r, p=p, dklen=32, maxmem=128 * 1024 * 1024)


def _locator_read() -> str:
    try:
        with open(LOCATOR, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _counter_path(vault_id: str) -> str:
    return os.path.join(HIDDEN_DIR, f".{vault_id}.cnt")


def _read_fails(vault_id: str) -> int:
    try:
        with open(_counter_path(vault_id), "r", encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _write_fails(vault_id: str, fails: int) -> None:
    os.makedirs(HIDDEN_DIR, exist_ok=True)
    path = _counter_path(vault_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(fails))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def vault_exists() -> bool:
    return bool(_locator_read()) and os.path.exists(_locator_read())


def _read_header(path: str) -> tuple[dict, bytes, bytes]:
    """(header, aad, ciphertext) qaytaradi."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise VaultError("ombor topilmadi")
    magic = HEADER_MAGIC.encode()
    if raw[:len(magic)] != magic:
        raise VaultError("ombor formati buzuq")
    body = raw[len(magic):]
    pos = body.find(b"\n\n")
    if pos < 0:
        raise VaultError("ombor sarlavhasi buzuq")
    header_bytes = body[:pos]
    data = body[pos + 2:]
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except Exception:
        raise VaultError("ombor sarlavhasini o'qib bo'lmadi")
    return header, header_bytes, data


def _seal(header: dict, key: bytes, plaintext: bytes) -> bytes:
    header_bytes = json.dumps(header, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")
    blob = chacha20_poly1305_encrypt(key, _b64d(header["nonce"]), header_bytes,
                                     plaintext)
    return HEADER_MAGIC.encode() + header_bytes + b"\n\n" + blob


def _new_vault(password: str, path: str | None = None) -> str:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    vault_id = secrets.token_hex(8)
    header = {
        "v": 1, "id": vault_id,
        "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
        "salt": _b64e(salt), "nonce": _b64e(nonce),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not path:
        os.makedirs(HIDDEN_DIR, exist_ok=True)
        path = os.path.join(HIDDEN_DIR, secrets.token_hex(8) + ".bin")
    key = _derive_key(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    empty = json.dumps({"items": {}}, ensure_ascii=False).encode("utf-8")
    raw = _seal(header, key, empty)
    with open(path, "wb") as f:
        f.write(raw)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    os.makedirs(os.path.dirname(LOCATOR), exist_ok=True)
    with open(LOCATOR, "w", encoding="utf-8") as f:
        f.write(path)
    os.chmod(LOCATOR, stat.S_IRUSR | stat.S_IWUSR)
    _write_fails(vault_id, 0)
    return path


def _wipe(path: str, vault_id: str = "") -> None:
    """Omborni qaytarilmas o'chirish: ustidan tasodifiy yozib, o'chirish."""
    try:
        if os.path.exists(path):
            size = os.path.getsize(path)
            with open(path, "r+b") as f:
                for _ in range(3):
                    f.seek(0)
                    f.write(secrets.token_bytes(max(1, size)))
                    f.flush()
                    os.fsync(f.fileno())
            os.remove(path)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
    if vault_id:
        try:
            os.remove(_counter_path(vault_id))
        except Exception:
            pass
    try:
        os.remove(LOCATOR)
    except Exception:
        pass


def unlock(password: str) -> tuple[dict, dict, str, bytes, bytes]:
    """(header, data_dict, path, key, nonce) qaytaradi.

    Parol xato bo'lsa: hisoblagichni oshiradi; MAX_TRIES ga yetganda
    ombor avtomatik o'chiriladi va WrongPassword ko'tariladi.
    """
    path = _locator_read()
    if not path or not os.path.exists(path):
        raise VaultError("ombor yo'q — avval: vault.py init")
    header, header_bytes, blob = _read_header(path)
    vault_id = header.get("id", "")
    kdf = header.get("kdf") or {}
    key = _derive_key(password, _b64d(header["salt"]), int(kdf.get("n", SCRYPT_N)),
                      int(kdf.get("r", SCRYPT_R)), int(kdf.get("p", SCRYPT_P)))
    plain = chacha20_poly1305_decrypt(key, _b64d(header["nonce"]),
                                      header_bytes, blob)
    if plain is None:
        fails = _read_fails(vault_id) + 1
        _write_fails(vault_id, fails)
        if fails >= MAX_TRIES:
            _wipe(path, vault_id)
            raise WrongPassword(
                f"parol {MAX_TRIES} marta xato — ombor AVTOMATIK O'CHIRILDI")
        raise WrongPassword(
            f"parol xato ({fails}/{MAX_TRIES}) — yana xato bo'lsa ombor o'chiriladi!")
    _write_fails(vault_id, 0)          # muvaffaqiyat — hisoblagich nolga
    try:
        data = json.loads(plain.decode("utf-8"))
    except Exception:
        data = {"items": {}}
    return header, data, path, key, _b64d(header["nonce"])


def save(header: dict, data: dict, key: bytes, nonce: bytes) -> None:
    path = _locator_read()
    plain = json.dumps(data, ensure_ascii=False).encode("utf-8")
    raw = _seal(header, key, plain)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    _write_fails(header.get("id", ""), 0)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _ask_password(confirm: bool = False) -> str:
    if not sys.stdin.isatty():
        raise SystemExit("Interaktiv emas — parolni qo'lda kiritish kerak")
    pw = getpass.getpass("Parol: ")
    if confirm:
        again = getpass.getpass("Parolni takrorlang: ")
        if pw != again:
            raise SystemExit("XATO: parollar mos emas")
    if len(pw) < 6:
        raise SystemExit("XATO: parol kamida 6 belgi")
    return pw


def cmd_init(args):
    if vault_exists():
        raise SystemExit("Ombor allaqachon mavjud (yangi yaratish: avval `wipe`)")
    pw = args.password or _ask_password(confirm=True)
    path = _new_vault(pw)
    print("✅ Shifrlangan ombor yaratildi")
    print(f"   joyi (yashirin): {path}")


def cmd_put(args):
    header, data, path, key, nonce = unlock(args.password or _ask_password())
    items = data.setdefault("items", {})
    if args.file:
        with open(args.file, "rb") as f:
            blob = f.read()
        items[args.name] = {"type": "file", "size": len(blob),
                            "sha256": hashlib.sha256(blob).hexdigest(),
                            "b64": _b64e(blob), "ts": time.time()}
    else:
        text = args.text if args.text is not None else ""
        items[args.name] = {"type": "text", "size": len(text.encode()),
                            "b64": _b64e(text.encode()), "ts": time.time()}
    save(header, data, key, nonce)
    print(f"✅ Saqlandi: {args.name} ({items[args.name]['size']} bayt)")


def cmd_get(args):
    header, data, path, key, nonce = unlock(args.password or _ask_password())
    item = (data.get("items") or {}).get(args.name)
    if not item:
        raise SystemExit(f"XATO: '{args.name}' topilmadi")
    blob = _b64d(item["b64"])
    if args.out:
        with open(args.out, "wb") as f:
            f.write(blob)
        print(f"✅ Chiqarildi: {args.out} ({len(blob)} bayt)")
    else:
        try:
            sys.stdout.write(blob.decode("utf-8"))
            if not blob.endswith(b"\n"):
                sys.stdout.write("\n")
        except UnicodeDecodeError:
            print(f"[binaire {len(blob)} bayt] --out bilan saqlang")


def cmd_ls(args):
    _h, data, _p, _k, _n = unlock(args.password or _ask_password())
    items = data.get("items") or {}
    if not items:
        print("(ombor bo'sh)")
        return
    for name, item in sorted(items.items()):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.get("ts", 0)))
        print(f"{name:32} {item.get('type','?'):5} {item.get('size',0):>9} bayt  {ts}")


def cmd_rm(args):
    header, data, _p, key, nonce = unlock(args.password or _ask_password())
    items = data.get("items") or {}
    if args.name not in items:
        raise SystemExit(f"XATO: '{args.name}' topilmadi")
    del items[args.name]
    save(header, data, key, nonce)
    print(f"🗑  O'chirildi: {args.name}")


def cmd_passwd(args):
    if not sys.stdin.isatty() and not args.password:
        raise SystemExit("Interaktiv emas")
    old = args.password or getpass.getpass("Eski parol: ")
    header, data, _p, _key, _nonce = unlock(old)
    new = None
    if args.new_password:
        new = args.new_password
    else:
        if not sys.stdin.isatty():
            raise SystemExit("Interaktiv emas")
        new = getpass.getpass("Yangi parol: ")
        again = getpass.getpass("Yangi parolni takrorlang: ")
        if new != again:
            raise SystemExit("XATO: parollar mos emas")
    if len(new) < 6:
        raise SystemExit("XATO: parol kamida 6 belgi")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    header["salt"] = _b64e(salt)
    header["nonce"] = _b64e(nonce)
    header["changed"] = time.strftime("%Y-%m-%d %H:%M:%S")
    kdf = header.get("kdf") or {}
    key = _derive_key(new, salt, int(kdf.get("n", SCRYPT_N)),
                      int(kdf.get("r", SCRYPT_R)), int(kdf.get("p", SCRYPT_P)))
    save(header, data, key, nonce)
    print("✅ Parol almashtirildi")


def cmd_status(args):
    path = _locator_read()
    if not path or not os.path.exists(path):
        print("Ombor yo'q")
        return
    header, _aad, blob = _read_header(path)
    fails = _read_fails(header.get("id", ""))
    print("Ombor:")
    print(f"  joyi    : {path}")
    print(f"  hajmi   : {os.path.getsize(path)} bayt (shifrlangan)")
    print(f"  yaratilgan: {header.get('created', '?')}")
    print(f"  kdf     : scrypt n={header.get('kdf', {}).get('n')}")
    print(f"  xato    : {fails}/{MAX_TRIES} (yana {MAX_TRIES - fails} ta xatodan keyin o'chadi)")


def cmd_wipe(args):
    path = _locator_read()
    if not path or not os.path.exists(path):
        print("Ombor yo'q")
        return
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("Tasdiqlash kerak: --yes")
        ans = input("Rostdan ham omborni QAYTARILMAS O'CHIRAMIZMI? (ha/yo'q): ")
        if ans.strip().lower() not in ("ha", "yes", "y"):
            print("Bekor qilindi")
            return
    header, _aad, _blob = _read_header(path)
    _wipe(path, header.get("id", ""))
    print("🗑  Ombor o'chirildi")


def cmd_selftest(args):
    """RFC 8439 §2.8.2 test vektori."""
    key = bytes.fromhex("808182838485868788898a8b8c8d8e8f"
                        "909192939495969798999a9b9c9d9e9f")
    nonce = bytes.fromhex("070000004041424344454647")
    aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
    plaintext = (b"Ladies and Gentlemen of the class of '99: If I could offer you "
                 b"only one tip for the future, sunscreen would be it.")
    expected = bytes.fromhex(
        "d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
        "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
        "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
        "3ff4def08e4b7a9de576d26586cec64b6116"
        "1ae10b594f09e26a7e902ecbd0600691")
    blob = chacha20_poly1305_encrypt(key, nonce, aad, plaintext)
    if blob != expected:
        print("✗ ChaCha20-Poly1305 selftest YIQILDI")
        print("  kutilgan:", expected.hex()[:64])
        print("  olingan :", blob.hex()[:64])
        raise SystemExit(1)
    back = chacha20_poly1305_decrypt(key, nonce, aad, blob)
    if back != plaintext:
        print("✗ deshifrlash selftest YIQILDI")
        raise SystemExit(1)
    # buzilgan teg
    bad = bytearray(blob)
    bad[-1] ^= 0x01
    if chacha20_poly1305_decrypt(key, nonce, aad, bytes(bad)) is not None:
        print("✗ buzilgan teg aniqlanmadi")
        raise SystemExit(1)
    print("✅ ChaCha20-Poly1305 RFC 8439 vektori o'tdi (shifrlash + deshifrlash + teg)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vault", description="Ferma shifrlangan ombori")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="yangi ombor yaratish")
    p.add_argument("--password", help="parol (test uchun; odatda so'raladi)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("put", help="element qo'shish")
    p.add_argument("name")
    p.add_argument("--file")
    p.add_argument("--text")
    p.add_argument("--password")
    p.set_defaults(func=cmd_put)

    p = sub.add_parser("get", help="element olish")
    p.add_argument("name")
    p.add_argument("--out")
    p.add_argument("--password")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("ls", help="ro'yxat")
    p.add_argument("--password")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("rm", help="element o'chirish")
    p.add_argument("name")
    p.add_argument("--password")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("passwd", help="parolni almashtirish")
    p.add_argument("--password", help="eski parol")
    p.add_argument("--new-password", dest="new_password")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("status", help="holat")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("wipe", help="qo'lda o'chirish")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_wipe)

    p = sub.add_parser("selftest", help="kripto selftest")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args(argv)
    try:
        return args.func(args) or 0
    except WrongPassword as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    except VaultError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
