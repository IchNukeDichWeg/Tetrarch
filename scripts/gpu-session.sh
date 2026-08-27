#!/usr/bin/env bash
# Tetrarch GPU session -- TRAINING ONLY.
#
# Verify CUDA, build the generation-9 cache once, and train three nets from it
# at lambda 0.5 / 0.7 / 0.85. Nothing here plays a game: match.py spawns engine
# subprocesses that run the C core on the CPU and never touch the GPU, so
# screening on a rented GPU box is paying GPU rates for CPU work on a machine
# that usually has few cores. The screens live in scripts/screen-sweep.sh and
# belong on something cheap and wide.
#
# One cache serves all three nets: lambda blends cp and result at batch time
# and never touches the features.
#
# Out: ~/tetrarch-nets.tar.gz -- three nets and their epoch tables, ~6 MB, so
# it comes off an expensive box in seconds.
set -euo pipefail
cd ~/Tetrarch
mkdir -p runs/games runs/cache runs/logs

echo "=== 0. sync and build ==="
git fetch origin && git reset --hard origin/main && ./setup.sh

echo "=== 1. dataset ==="
[ -f runs/games/games_v9.jsonl ] || {
  curl -L -o runs/games/games_v9.jsonl.gz \
    https://github.com/IchNukeDichWeg/Tetrarch/releases/download/dataset-teams-gen9/games_v9.jsonl.gz
  gunzip -f runs/games/games_v9.jsonl.gz
}
echo "  games: $(wc -l < runs/games/games_v9.jsonl)"

echo "=== 2. CUDA pre-flight -- a GATE, not a formality ==="
python3 train.py --data runs/games/games_v9.jsonl --cache /tmp/pre.npz --games 4000 \
  --augment --cache-workers 0 --out /tmp/pre_np --epochs 2 --quiet | tee /tmp/pre_np.txt
python3 train.py --cache /tmp/pre.npz --out /tmp/pre_cu --epochs 2 --device cuda \
  --quiet | tee /tmp/pre_cu.txt
python3 - <<'GATE'
import re
def loss(path):
    hits = re.findall(r"^epoch 2\s+train\s+([\d.]+)", open(path).read(), re.M)
    return float(hits[0]) if hits else None
a, b = loss("/tmp/pre_np.txt"), loss("/tmp/pre_cu.txt")
if a is None or b is None:
    raise SystemExit("PRE-FLIGHT FAILED: no epoch-2 line (numpy=%r cuda=%r)" % (a, b))
gap = abs(a - b) / a
print("  numpy %.5f | cuda %.5f | %.2f%% apart" % (a, b, 100 * gap))
if gap > 0.02:
    raise SystemExit("PRE-FLIGHT FAILED: cuda disagrees with numpy by over 2%. STOP.")
print("  cuda agrees with the trainer every shipped net came from.")
GATE

echo "=== 3. the full cache, built once and reused by all three nets ==="
python3 train.py --data runs/games/games_v9.jsonl --cache runs/cache/v9.npz \
  --augment --cache-workers 0 --out /tmp/cacheonly --epochs 1 --device cuda \
  | tee runs/logs/cache.txt

echo "=== 4. lambda sweep: three nets from one cache ==="
for L in 0.5 0.7 0.85; do
  tag=$(echo "$L" | tr -d .)
  echo "--- lambda $L ---"
  python3 train.py --cache runs/cache/v9.npz --out nets/v9_l$tag \
    --epochs 8 --lambda "$L" --device cuda | tee runs/logs/lambda$tag.txt
  cp nets/v9_l$tag/net-best.nnue nets/net-v9l$tag.nnue
done

echo "=== 5. the epoch each lambda chose ==="
{
  for tag in 05 07 085; do
    echo "== lambda 0.${tag#0} =="
    grep -E "^epoch|best" runs/logs/lambda$tag.txt | tail -12
    echo
  done
} | tee runs/logs/EPOCHS.txt

echo "=== 6. export just the nets -- tiny, so it leaves the box fast ==="
tar -czf ~/tetrarch-nets.tar.gz \
  --transform='s,^,tetrarch-nets/,' \
  nets/net-v9l05.nnue nets/net-v9l07.nnue nets/net-v9l085.nnue runs/logs
ls -lh ~/tetrarch-nets.tar.gz
echo
echo "NEXT: pull that tarball, commit the three nets, then screen them on a"
echo "CPU box with scripts/screen-sweep.sh. Nothing left here needs a GPU."
cat runs/logs/EPOCHS.txt
