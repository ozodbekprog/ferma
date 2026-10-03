#!/usr/bin/env bash
set -u
CR="python3 /tmp/opencode/center_run.py"
FARM="--file /tmp/opencode/farm_stress.py"
GPUF="--file /tmp/opencode/gpu_burn.py"

LINUX_CMD='(python3 gpu_burn.py --seconds 360 --iters 20000000 > gpu.log 2>&1 &) ; python3 farm_stress.py --seconds 360 --disk-mb 512 > stress.log 2>&1 ; echo "== STRESS =="; tail -2 stress.log | tr -cd "[:print:]\n"; echo "== GPU =="; tr -cd "[:print:]\n" < gpu.log | tail -3'

$CR "$LINUX_CMD" gtx1650-pc 420 $FARM $GPUF > /tmp/opencode/res_gtx.txt 2>&1 &
P1=$!
$CR "$LINUX_CMD" ryzen-ai7 420 $FARM $GPUF > /tmp/opencode/res_ryzen.txt 2>&1 &
P2=$!

MAC_CMD='(~/.ferma/metal_burn 360 > metal.log 2>&1 &) ; python3 farm_stress.py --seconds 360 --disk-mb 512 > stress.log 2>&1 ; echo "== STRESS =="; tail -2 stress.log | tr -cd "[:print:]\n"; echo "== GPU =="; tr -cd "[:print:]\n" < metal.log | tail -3'
$CR "$MAC_CMD" Ozodbeks-MBP 420 $FARM > /tmp/opencode/res_mac.txt 2>&1 &
P3=$!

ASROR_CMD='(ffmpeg -hide_banner -loglevel error -vaapi_device /dev/dri/renderD128 -f lavfi -i "testsrc=size=1920x1080:rate=60" -vf "format=nv12,hwupload" -c:v h264_vaapi -b:v 20M -t 360 -f null - > vaapi.log 2>&1 &) ; python3 farm_stress.py --seconds 360 --disk-mb 512 > stress.log 2>&1 ; echo "== STRESS =="; tail -2 stress.log | tr -cd "[:print:]\n"; echo "== VAAPI =="; tr -cd "[:print:]\n" < vaapi.log | tail -3'
$CR "$ASROR_CMD" asror-fedora 420 $FARM > /tmp/opencode/res_asror.txt 2>&1 &
P4=$!

wait $P1 $P2 $P3 $P4
echo "===== HAMMASI TUGADI ====="
for f in gtx ryzen mac asror; do echo "#### $f"; cat /tmp/opencode/res_$f.txt; done
