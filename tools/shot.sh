#!/bin/sh
# Ferma: GNOME Shell kengaytmasi orqali skrinshot -> joriy papkada shot.jpg
out="$(pwd)/shot.jpg"
rm -f "$out"
if ! gdbus call --session --dest org.gnome.Shell --object-path /com/ferma/Screenshot \
     --method com.ferma.Screenshot.Ping 2>/dev/null | grep -q true; then
  echo "KENGAYTMA YOQ"
  exit 4
fi
tmp="/tmp/ferma-shot-$$.png"
rm -f "$tmp"
gdbus call --session --dest org.gnome.Shell --object-path /com/ferma/Screenshot \
  --method com.ferma.Screenshot.TakeShot "$tmp" >/dev/null 2>&1
n=0
while [ "$n" -lt 10 ] && [ ! -s "$tmp" ]; do sleep 0.2; n=$((n + 1)); done
if [ ! -s "$tmp" ]; then
  echo "SKRINSHOT VOSITASI ISHLAMADI"
  exit 3
fi
if command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -y -loglevel error -i "$tmp" -q:v 5 "$out" 2>/dev/null
fi
[ -s "$out" ] || cp "$tmp" "$out"
rm -f "$tmp"
if [ -s "$out" ]; then
  echo "OK $(stat -c%s "$out")"
  exit 0
fi
echo "SKRINSHOT VOSITASI ISHLAMADI"
exit 3
