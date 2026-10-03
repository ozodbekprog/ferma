#!/usr/bin/env bash
# audit_runner.sh <b64_prompt1> <b64_prompt2> ... — ochiqkod agentlarini parallel ishga tushiradi
OC="$HOME/.opencode/bin/opencode"
[ -x "$OC" ] || OC="$(command -v opencode)"
TMO=""
command -v timeout >/dev/null 2>&1 && TMO="timeout 260"
command -v gtimeout >/dev/null 2>&1 && TMO="gtimeout 260"
PIDS=""
i=0
for b64 in "$@"; do
  i=$((i+1))
  P=$(python3 -c 'import base64,sys;print(base64.b64decode(sys.argv[1]).decode())' "$b64")
  ( $TMO "$OC" run "$P" > "audit-$i.log" 2>&1 ) &
  PIDS="$PIDS $!"
done
for p in $PIDS; do wait "$p" 2>/dev/null; done
for f in audit-*.log; do
  echo "##### $f"
  tr -cd '[:print:]\n' < "$f" | tail -70
  echo
done
