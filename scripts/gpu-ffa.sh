#!/usr/bin/env bash
# Tetrarch GPU session, FFA arm -- TRAINING ONLY.
#
# The FFA recipe has never been swept. net-ffa1 used lambda 0.7 at 8 epochs,
# selected epoch 2, and measured +106.05 +/- 5.55 -- so the mode works, and
# nothing about how it was trained has been tested against an alternative.
#
# FFA is also the harder case for a recipe. A paranoid score is in the mover's
# terms and does not convert to another seat, so there is no perspective
# augmentation: 13.5M rows from 50,000 games where a Teams generation of the
# same size yields four times that. Data-starved is exactly where lambda and
# epoch count should matter most.
#
# Nothing here plays a game. Screening belongs on a wide CPU box.
# Out: ~/tetrarch-ffa-nets.tar.gz -- the nets and their epoch tables.
set -euo pipefail
cd ~/Tetrarch
mkdir -p runs/games runs/cache runs/logs

echo "=== 0. sync and build ==="
git fetch origin && git reset --hard origin/main && ./setup.sh

echo "=== 1. dataset ==="
# -f so an HTTP error is an error rather than an error page written to disk
# under the dataset's name, --retry because a rented box's network is not the
# thing being tested, and -C - so a re-run resumes a multi-GB download instead
# of starting it again on the clock.
[ -f runs/games/games_ffa1.jsonl ] || {
  curl -fL --retry 3 --retry-delay 5 -C - -o runs/games/games_ffa1.jsonl.gz \
    https://github.com/IchNukeDichWeg/Tetrarch/releases/download/dataset-ffa-gen1/games_ffa1.jsonl.gz
  gunzip -f runs/games/games_ffa1.jsonl.gz
}
echo "  games: $(wc -l < runs/games/games_ffa1.jsonl)"

echo "=== 1b. CUDA pre-flight -- a GATE, not a formality ==="
# The Teams arm has had this since the first GPU box was rented; this arm never
# did, so seven nets could come off a card whose arithmetic nobody had compared
# against the trainer every shipped net came from. A silently wrong CUDA does
# not crash -- it produces plausible nets that lose Elo, and you find out on
# the CPU screen hours and two rentals later. Two minutes against seven runs.
python3 train.py --data runs/games/games_ffa1.jsonl --cache /tmp/pre_ffa.npz \
  --games 4000 --cache-workers 0 --out /tmp/pre_ffa_np --epochs 2 --quiet \
  | tee /tmp/pre_ffa_np.txt
python3 train.py --cache /tmp/pre_ffa.npz --out /tmp/pre_ffa_cu --epochs 2 \
  --device cuda --quiet | tee /tmp/pre_ffa_cu.txt
python3 - <<'GATE'
import re
def loss(path):
    hits = re.findall(r"^epoch 2\s+train\s+([\d.]+)", open(path).read(), re.M)
    return float(hits[0]) if hits else None
a, b = loss("/tmp/pre_ffa_np.txt"), loss("/tmp/pre_ffa_cu.txt")
if a is None or b is None:
    raise SystemExit("PRE-FLIGHT FAILED: no epoch-2 line (numpy=%r cuda=%r)" % (a, b))
gap = abs(a - b) / a
print("  numpy %.5f | cuda %.5f | %.2f%% apart" % (a, b, 100 * gap))
if gap > 0.02:
    raise SystemExit("PRE-FLIGHT FAILED: cuda disagrees with numpy by over 2%. STOP.")
print("  cuda agrees with the trainer every shipped net came from.")
GATE

echo "=== 2. cache, built once ==="
# No --augment: train.py refuses it for FFA anyway, since only the seat that
# moved has a label.
python3 train.py --data runs/games/games_ffa1.jsonl --cache runs/cache/ffa1.npz \
  --cache-workers 0 --epochs 0 \
  | tee runs/logs/ffa-cache.txt

# "tag:lambda:seed:epochs". net-ffa1 was 0.7 / 8 and picked epoch 2, so the
# question is whether that early peak is the data talking or the recipe.
#
#   f05 f07 f085 f10 -- the lambda sweep at seed 0.
#   fs1 fs2          -- lambda 0.7 at seeds 1 and 2: the noise floor, without
#                       which none of the above can be read.
#   f40s1            -- 40 epochs against fs1, paired. If it still picks an
#                       early epoch, the ceiling is the data.
RUNS="f05:0.5:0:8 f07:0.7:0:8 f085:0.85:0:8 f10:1.0:0:8 \
      fs1:0.7:1:8 fs2:0.7:2:8 f40s1:0.7:1:40"

echo "=== 3. seven FFA nets from one cache ==="
for spec in $RUNS; do
  IFS=: read -r tag L S E <<<"$spec"
  echo "--- $tag: lambda $L, seed $S, $E epochs ---"
  python3 train.py --cache runs/cache/ffa1.npz --out nets/ffa_$tag \
    --epochs "$E" --lambda "$L" --seed "$S" --device cuda | tee runs/logs/$tag.txt
  cp nets/ffa_$tag/net-best.nnue nets/net-$tag.nnue
done

echo "=== 4. the epoch each run chose ==="
{
  for spec in $RUNS; do
    IFS=: read -r tag L S E <<<"$spec"
    echo "== $tag (lambda $L, seed $S, $E epochs) =="
    grep -E "^epoch|best" runs/logs/$tag.txt | tail -8
    echo
  done
  echo "net-ffa1 picked epoch 2 of 8. If every run here does the same, the"
  echo "ceiling is the data and more epochs will never be the answer."
} | tee runs/logs/FFA_EPOCHS.txt

echo "=== 5. export ==="
tar -czf ~/tetrarch-ffa-nets.tar.gz --transform='s,^,tetrarch-ffa-nets/,' \
  $(for spec in $RUNS; do echo "nets/net-${spec%%:*}.nnue"; done) runs/logs
ls -lh ~/tetrarch-ffa-nets.tar.gz
echo
echo "NEXT: screen these against nets/net-ffa1.nnue on a CPU box -- --mode ffa"
echo "with books/book-ffa20k.txt, which is committed."
cat runs/logs/FFA_EPOCHS.txt
