#!/usr/bin/env bash
# Tetrarch: screen the lambda sweep. CPU ONLY -- rent cores, not a GPU.
#
# match.py spawns engine subprocesses running the C core; nothing here touches
# a GPU, and throughput is workers-bound. A wide cheap box beats an expensive
# narrow one by the ratio of their core counts.
#
# Expects the six nets from scripts/gpu-session.sh to be committed, so a
# clone has them. Screens each against net-v5 -- the current default, which is
# what a confirm runs against -- at fixed nodes only. Fixed time decides, so
# whichever lambda wins earns its confirm afterwards rather than paying for
# three.
set -euo pipefail
cd ~/Tetrarch
mkdir -p runs/ab

echo "=== 0. sync and build ==="
git fetch origin && git reset --hard origin/main && ./setup.sh

TAGS="l05 l07 l085 l10 s1 s2"

for tag in $TAGS; do
  [ -f "nets/net-v9$tag.nnue" ] || {
    echo "MISSING nets/net-v9$tag.nnue -- commit the nets the GPU run produced first."
    exit 1
  }
done

echo "=== 1. screen each lambda against net-v5 at fixed nodes ==="
for tag in $TAGS; do
  echo "--- $tag ---"
  python3 match.py 1250 --log runs/ab/v9${tag}_nodes.jsonl --nodes 20000 \
    --workers 0 --net-a nets/net-v9$tag.nnue --net-b nets/net-v5.nnue
done

echo "=== 2. results ==="
{
  for tag in $TAGS; do
    echo "== $tag vs net-v5, FIXED NODES =="
    python3 match.py --summarise runs/ab/v9${tag}_nodes.jsonl
    echo
  done
  echo "s1 and s2 are lambda 0.7 at seeds 1 and 2, the same recipe as l07."
  echo "The spread across those THREE is the noise floor. Any lambda that does"
  echo "not clear it by more than that spread has not been shown to differ."
} | tee runs/ab/SWEEP.txt

echo "=== 3. export ==="
find runs/ab -type f -print0 \
  | tar --null --transform='s,^,tetrarch-sweep/,' \
        -czf ~/tetrarch-sweep.tar.gz --files-from=-
ls -lh ~/tetrarch-sweep.tar.gz
cat runs/ab/SWEEP.txt
