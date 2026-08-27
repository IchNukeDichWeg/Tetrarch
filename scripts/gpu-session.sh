#!/usr/bin/env bash
# Tetrarch GPU session -- TRAINING ONLY.
#
# Verify CUDA, build the generation-9 cache once, and train ten nets from it:
# a lambda sweep, three seed replicates for the noise floor, and the same
# three seeds again at 40 epochs to test 8-vs-40 as a paired comparison.
#
# Nothing here plays a game: match.py spawns engine
# subprocesses that run the C core on the CPU and never touch the GPU, so
# screening on a rented GPU box is paying GPU rates for CPU work on a machine
# that usually has few cores. The screens live in scripts/screen-sweep.sh and
# belong on something cheap and wide.
#
# One cache serves all twelve nets: lambda blends cp and result at batch time
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

echo "=== 3. the full cache, built once and reused by all twelve nets ==="
python3 train.py --data runs/games/games_v9.jsonl --cache runs/cache/v9.npz \
  --augment --cache-workers 0 --out /tmp/cacheonly --epochs 1 --device cuda \
  | tee runs/logs/cache.txt

# Ten nets from the one cache. "tag:lambda:seed:epochs".
#
# l05 l07 l085 l10 -- the lambda sweep at seed 0, 8 epochs. 1.0 is pure score,
#     the control that asks whether blending the game result in does anything.
#
# s1 s2 s3 -- lambda 0.7 at seeds 1, 2, 3, 8 epochs. THE NOISE FLOOR, and the
#     reason the rest is readable: if nets differing only by initialisation
#     land 20 Elo apart, a lambda separated by 15 Elo has said nothing. Every
#     net-vs-net number in docs/AB.md so far was read without knowing that
#     spread.
#
# e40s1 e40s2 e40s3 -- the same three seeds at 40 epochs. Pygin trains 40 where
#     this trains 8, and nobody has checked which is right here. Matched seeds
#     make it PAIRED: e40s1 against s1 differs only in epochs, which is a much
#     sharper test than comparing two pooled averages.
#
#     Half the answer is free. The runs select net-best on held-out loss, so if
#     a 40-epoch run picks an epoch <= 8 then the extra 32 bought nothing and
#     the epoch table says so before a single game is played. The FFA run
#     peaked at epoch 2 of 8 and generation 9 was still improving at 2, so
#     both outcomes are live.
#
# lr3e4 lr3e3 -- the learning rate at seed 1, paired against s1. 1e-3 is the
#     default and has never been measured against anything; it is the last
#     knob in train.py with no number behind it.
RUNS="l05:0.5:0:8:1e-3 l07:0.7:0:8:1e-3 l085:0.85:0:8:1e-3 l10:1.0:0:8:1e-3 \
      s1:0.7:1:8:1e-3 s2:0.7:2:8:1e-3 s3:0.7:3:8:1e-3 \
      e40s1:0.7:1:40:1e-3 e40s2:0.7:2:40:1e-3 e40s3:0.7:3:40:1e-3 \
      lr3e4:0.7:1:8:3e-4 lr3e3:0.7:1:8:3e-3"

echo "=== 4. twelve nets from one cache ==="
for spec in $RUNS; do
  IFS=: read -r tag L S E LR <<<"$spec"
  echo "--- $tag: lambda $L, seed $S, $E epochs, lr $LR ---"
  python3 train.py --cache runs/cache/v9.npz --out nets/v9_$tag \
    --epochs "$E" --lambda "$L" --seed "$S" --lr "$LR" --device cuda \
    | tee runs/logs/$tag.txt
  cp nets/v9_$tag/net-best.nnue nets/net-v9$tag.nnue
done

echo "=== 5. the epoch each run chose ==="
{
  for spec in $RUNS; do
    IFS=: read -r tag L S E LR <<<"$spec"
    echo "== $tag (lambda $L, seed $S, $E epochs, lr $LR) =="
    grep -E "^epoch|best" runs/logs/$tag.txt | tail -8
    echo
  done
  echo "READ THIS FIRST: if e40s* picked an epoch <= 8, forty epochs bought"
  echo "nothing on this data and the A/B only confirms what is already here."
} | tee runs/logs/EPOCHS.txt

echo "=== 6. export just the nets -- tiny, so it leaves the box fast ==="
tar -czf ~/tetrarch-nets.tar.gz --transform='s,^,tetrarch-nets/,' \
  $(for spec in $RUNS; do echo "nets/net-v9${spec%%:*}.nnue"; done) runs/logs
ls -lh ~/tetrarch-nets.tar.gz
echo
echo "NEXT: pull that tarball, commit the twelve nets, then screen them on a"
echo "CPU box with scripts/screen-sweep.sh. Nothing left here needs a GPU."
cat runs/logs/EPOCHS.txt
