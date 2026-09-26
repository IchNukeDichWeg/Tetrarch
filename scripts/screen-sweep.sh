#!/usr/bin/env bash
# Screen the trained nets. CPU ONLY -- rent cores, not a GPU.
#
# match.py spawns engine subprocesses running the C core; nothing here touches
# a GPU, and throughput is workers-bound. A wide cheap box beats a narrow
# expensive one by the ratio of their core counts.
#
# The first attempt at this ran ON the GPU box and lost every game to
# `BlockingIOError(11)` -- fork() refusing, because a worker needs five
# processes in Teams and the container's pid limit could not take 96 of them.
# match.py caps workers against RLIMIT_NPROC now, but the lesson stands: this
# script belongs on a machine with cores to spare.
#
# Teams nets screen against net-v5, FFA nets against net-ffa1 -- in both cases
# the current default, which is what a confirm runs against.
set -euo pipefail
cd ~/Tetrarch
mkdir -p runs/ab

# These two lists MUST stay equal to the RUNS tags in scripts/gpu-session.sh
# and scripts/gpu-ffa.sh. They did not: those scripts train twelve Teams nets
# and seven FFA nets, this screened three and six. Nine GPU-trained nets --
# including every seed replicate, i.e. the noise floor that makes the rest
# readable -- were paid for and never measured.
TEAMS="l05 l07 l085 l10 s1 s2 s3 e40s1 e40s2 e40s3 lr3e4 lr3e3"
FFA="f05 f07 f085 f10 fs1 fs2 f40s1"

echo "=== 0. sync and build ==="
git fetch origin && git reset --hard origin/main && ./setup.sh

# BEFORE the expensive part. Nineteen matches at 1,250 games is many hours of
# rented cores, and discovering a missing net at hour four costs all of it.
missing=""
for t in $TEAMS; do [ -f "nets/net-v9$t.nnue" ] || missing="$missing nets/net-v9$t.nnue"; done
for t in $FFA;   do [ -f "nets/net-$t.nnue"   ] || missing="$missing nets/net-$t.nnue"; done
for f in nets/net-v5.nnue nets/net-ffa1.nnue books/book-ffa20k.txt; do
  [ -f "$f" ] || missing="$missing $f"
done
if [ -n "$missing" ]; then
  echo "MISSING, nothing run:"; for f in $missing; do echo "     $f"; done; exit 1
fi

echo "=== 1. Teams sweep vs net-v5, fixed nodes ==="
for t in $TEAMS; do
  echo "--- $t ---"
  python3 match.py 1250 --log runs/ab/v9${t}_nodes.jsonl --nodes 20000 \
    --workers 0 --net-a nets/net-v9$t.nnue --net-b nets/net-v5.nnue
done

echo "=== 2. FFA sweep vs net-ffa1, fixed nodes ==="
for t in $FFA; do
  echo "--- $t ---"
  python3 match.py 1250 --mode ffa --book books/book-ffa20k.txt \
    --log runs/ab/${t}_nodes.jsonl --nodes 20000 \
    --workers 0 --net-a nets/net-$t.nnue --net-b nets/net-ffa1.nnue
done

echo "=== 3. results ==="
{
  echo "### TEAMS -- lambda vs net-v5, fixed nodes 20,000"
  for t in $TEAMS; do
    echo "== $t =="; python3 match.py --summarise runs/ab/v9${t}_nodes.jsonl; echo
  done
  echo "  l07, s1, s2 and s3 are ONE recipe (lambda 0.7, 8 epochs) at seeds 0,"
  echo "  1, 2 and 3. The spread across those four is the TEAMS NOISE FLOOR."
  echo "  Read it first: l05, l085 or l10 beating l07 by less than that spread"
  echo "  has not been shown to differ from it. e40s1/2/3 are paired against"
  echo "  s1/2/3 and differ only in epochs; lr3e4 and lr3e3 are paired"
  echo "  against s1 and differ only in learning rate."
  echo
  echo "### FFA -- vs net-ffa1, fixed nodes 20,000"
  for t in $FFA; do
    echo "== $t =="
    python3 match.py --summarise runs/ab/${t}_nodes.jsonl --book books/book-ffa20k.txt
    echo
  done
  echo "  f07, fs1 and fs2 are ONE recipe (lambda 0.7, 8 epochs) at seeds 0, 1"
  echo "  and 2. The spread across those three is the NOISE FLOOR. Nothing else"
  echo "  here means anything until you have it: f05, f085 or f10 beating f07"
  echo "  by less than that spread has not been shown to differ from it."
} | tee runs/ab/SCREENS.txt

echo "=== 4. export ==="
# runs/logs too: the per-run stdout is what diagnosed the last failure, and a
# tarball that carries only the summaries cannot answer "why did it do that".
find runs/ab runs/logs -type f -print0 2>/dev/null \
  | tar --null --transform='s,^,tetrarch-screens/,' \
        -czf ~/tetrarch-screens.tar.gz --files-from=-
ls -lh ~/tetrarch-screens.tar.gz
echo
cat runs/ab/SCREENS.txt
