#!/usr/bin/env bash
# Tetrarch GPU session: verify CUDA, retrain generation 9 at three lambdas,
# screen all three, export. One cache serves every net -- lambda blends cp and
# result at batch time and never touches the features.
set -euo pipefail
cd ~/Tetrarch
mkdir -p runs/games runs/ab runs/cache

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
  --augment --cache-workers 0 --out /tmp/cacheonly --epochs 1 --device cuda

echo "=== 4. lambda sweep: three nets from one cache ==="
for L in 0.5 0.7 0.85; do
  tag=$(echo "$L" | tr -d .)
  echo "--- lambda $L ---"
  python3 train.py --cache runs/cache/v9.npz --out nets/v9_l$tag \
    --epochs 8 --lambda "$L" --device cuda
  cp nets/v9_l$tag/net-best.nnue nets/net-v9l$tag.nnue
done

echo "=== 5. screen all three at fixed nodes against the current default ==="
for tag in 05 07 085; do
  python3 match.py 1250 --log runs/ab/v9l${tag}_nodes.jsonl --nodes 20000 \
    --workers 0 --net-a nets/net-v9l$tag.nnue --net-b nets/net-v5.nnue
done

echo "=== 6. results ==="
{
  for tag in 05 07 085; do
    echo "== lambda 0.${tag#0} vs net-v5, FIXED NODES =="
    python3 match.py --summarise runs/ab/v9l${tag}_nodes.jsonl
    echo
  done
} | tee runs/ab/SWEEP.txt

echo "=== 7. export ==="
find runs nets -type f ! -name '*.npz' ! -name '*.jsonl.gz' \
  ! -name 'games_v9.jsonl' -print0 \
  | tar --null --transform='s,^,tetrarch-export/,' \
        -czf ~/tetrarch-sweep.tar.gz --files-from=-
ls -lh ~/tetrarch-sweep.tar.gz
cat runs/ab/SWEEP.txt
