#!/usr/bin/env python3
"""ferma-all — bitta faylli Ferma paketi (avto-yaratilgan)."""
import os
import sys

os.environ.setdefault("FERMA_HOME", os.path.expanduser("~/.ferma"))

HELP = """ferma-all — Ferma (bitta fayl)

  vault ...            shifrlangan ombor (init/put/get/ls/passwd/status/wipe)
  local "<buyruq>"     SERversiz: shu PC da bajarish va natijani chiqarish
  agent [--pull ...]   shu PC ni ishchiga aylantirish
  serve [--port N]     markazni ishga tushirish (ixtiyoriy)
  nodes / run / oc / role / passwd / token / join-link / funnel ...
                       farmctl buyruqlari (markaz bilan ishlash)

Misollar:
  python3 ferma-all.pyz vault init
  python3 ferma-all.pyz local "df -h /"
  python3 ferma-all.pyz agent --pull --server https://misol.ts.net --install-service
"""


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    cmd = argv[0]
    if cmd == "vault":
        import vault
        return vault.main(argv[1:]) or 0
    if cmd == "local":
        import agent
        if not argv[1:]:
            print("XATO: buyruq kerak, masalan: ferma-all.pyz local \"uname -a\"")
            return 1
        timeout = float(os.environ.get("FERMA_LOCAL_TIMEOUT", "600"))
        res = agent.run_job({"cmd": " ".join(argv[1:]), "timeout": timeout})
        out = res.get("stdout") or ""
        err = res.get("stderr") or ""
        if out:
            sys.stdout.write(out)
        if err:
            sys.stderr.write(err)
        if not res.get("ok"):
            print(f"[xato] {res.get('error') or 'exit=' + str(res.get('exit'))}",
                  file=sys.stderr)
            return 1
        return 0
    if cmd in ("serve", "agent"):
        modname = "server" if cmd == "serve" else "agent"
        mod = __import__(modname)
        sys.argv = ["ferma"] + argv[1:]
        return mod.main() or 0
    import farmctl
    sys.argv = ["ferma"] + argv
    return farmctl.main() or 0


if __name__ == "__main__":
    raise SystemExit(main())
