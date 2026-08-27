#!/usr/bin/env bash
# Tetrarch: screen the lambda sweep. CPU ONLY -- rent cores, not a GPU.
#
# match.py spawns engine subprocesses running the C core; nothing here touches
# a GPU, and throughput is workers-bound. A wide cheap box beats an expensive
# narrow one by the ratio of their core counts.
#
# Expects the three nets from scripts/gpu-session.sh to be committed, so a
# clone has them. Screens each against net-v5 -- the current default, which is
# what a confirm runs against -- at fixed nodes only. Fixed time decides, so
# whichever lambda wins earns its confirm afterwards rather than paying for
# three.
set -euo pipefail
cd ~/Tetrarch
mkdir -p runs/ab

echo "=== 0. sync and build ==="
git fetch origin && git reset --hard origin/main && ./setup.sh

for tag in 05 07 085; do
  [ -f "nets/net-v9l$tag.nnue" ] || {
    echo "MISSING nets/net-v9l$tag.nnue -- commit the nets the GPU run produced first."
    exit 1
  }
done

echo "=== 1. screen each lambda against net-v5 at fixed nodes ==="
for tag in 05 07 085; do
  echo "--- lambda 0.${tag#0} ---"
  python3 match.py 1250 --log runs/ab/v9l${tag}_nodes.jsonl --nodes 20000 \
    --workers 0 --net-a nets/net-v9l$tag.nnue --net-b nets/net-v5.nnue
done

echo "=== 2. results ==="
{
  for tag in 05 07 085; do
    echo "== lambda 0.${tag#0} vs net-v5, FIXED NODES =="
    python3 match.py --summarise runs/ab/v9l${tag}_nodes.jsonl
    echo
  done
} | tee runs/ab/SWEEP.txt

echo "=== 3. export ==="
find runs/ab -type f -print0 \
  | tar --null --transform='s,^,tetrarch-sweep/,' \
        -czf ~/tetrarch-sweep.tar.gz --files-from=-
ls -lh ~/tetrarch-sweep.tar.gz
cat runs/ab/SWEEP.txt
