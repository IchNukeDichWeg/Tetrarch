#!/usr/bin/env python3
"""train.py -- NNUE trainer.

    python3 train.py --data games.jsonl --cache cache.npz --out nets/ --epochs 8

Every output path is a CLI argument with no repo default, and --out must be a
directory that this creates or extends. A smoke test that reuses a real output
path once destroyed four hours of a training run; nothing here writes anywhere
it was not explicitly told to.

TWO PHASES
    1. Cache. Games are replayed once and every position is reduced to its 64
       active feature indices, 7 extra inputs, and a target. Written to a .npz
       and reused, because replaying 3.9M positions costs more than an epoch.
    2. Train. Minibatch Adam on float weights, then quantise to the integer
       format in tetrarch/nnue.py.

THE TARGET
    A blend of the search score and the game result, both from the training
    perspective's team:

        target = L * sigmoid(cp / SCALE) + (1 - L) * result

    Pure score teaches the net to imitate a weak hand eval; pure result is too
    noisy per position. L is --lambda.

HELD-OUT LOSS IS NOT ELO
    The validation number this prints is for spotting overfitting and nothing
    else. Do not promote a net on it. The gate is a measured A/B against the
    hand-eval build, and the loss-to-Elo conversion has to be calibrated once
    on a known-good version pair before either number means anything alone.
"""

import argparse
import json
import multiprocessing
import os
import sys
import time

import numpy as np

from tetrarch.board import Board, MODE_TEAMS, MODE_FFA, move_str
from tetrarch import game as game_rules
from tetrarch import movegen as gen
from tetrarch import nnue

MAX_FEATURES = 64          # 64 pieces at most; promotion never adds one
SCALE_CP = 400.0           # centipawns per logit, the usual NNUE choice
QA = 127 * 64              # input-layer weight scale (see nnue.py)
QB = 64                    # hidden-layer weight scale
W1_CLIP = 32767.0 / QA
WQ_CLIP = 127.0 / QB


# --- phase 1: cache ---------------------------------------------------------

def _cache_chunk(job):
    """Replay a block of games into arrays. Pure function of its input lines,
    so it parallelises per block with no shared state and no ordering
    subtlety -- the pool preserves block order, so the cache is identical
    whatever --cache-workers is set to."""
    lines, augment = job
    feats, extras, cps, results, games = [], [], [], [], []
    for game_id, line in enumerate(lines):
        record = json.loads(line)
        ffa = record.get("mode") == "ffa"
        mode = MODE_FFA if ffa else MODE_TEAMS
        board = Board.from_fen4(record["fen4"], mode)
        result = record["result"]      # team 0's score, or a per-seat vector
        for token, cp in zip(record["moves"].split(), record["scores"]):
            # The score was recorded before this move was played, so the
            # position to label is the one we are standing in now.
            #
            # FFA cannot be augmented. The four-view trick rests on a Teams
            # score negating to the other team; a paranoid score is in the
            # MOVER's terms and does not convert to any other seat, so only
            # the seat that moved has a label. That costs FFA a factor of four
            # in positions per game, which is the price of the mode and not a
            # setting anyone should reach for.
            views = range(4) if (augment and not ffa) else (board.turn,)
            for persp in views:
                active = nnue.active_features(board, persp)
                row = np.full(MAX_FEATURES, -1, dtype=np.int16)
                row[:len(active)] = active[:MAX_FEATURES]
                feats.append(row)
                extras.append(nnue.extra_inputs(board, persp))
                if ffa:
                    cps.append(cp)                 # already the mover's
                    results.append(result[persp])
                else:
                    # Stored scores and results are team 0's; flip for team 1.
                    flip = (persp & 1) == 1
                    cps.append(-cp if flip else cp)
                    results.append(1.0 - result if flip else result)
                games.append(game_id)
            move = next((m for m in gen.gen_legal(board)
                         if move_str(m) == token), None)
            if move is None:
                break                          # corrupt line; drop the rest
            board.make(move)
            # The move list does not encode eliminations, so a replay that
            # only makes moves drifts off the recorded game at the first mate
            # -- same failure uci.py had, and here it would silently mislabel
            # every position after it rather than produce a visible bad move.
            if ffa:
                game_rules.resolve(board)
    return (np.asarray(feats, dtype=np.int16).reshape(-1, MAX_FEATURES),
            np.asarray(extras, dtype=np.int16).reshape(-1, nnue.NEXTRA),
            np.asarray(cps, dtype=np.float32),
            np.asarray(results, dtype=np.float32),
            np.asarray(games, dtype=np.int32),
            len(lines))


#: Games per work unit, capped so every worker gets several blocks -- a fixed
#: size gives a short run one block and therefore no parallelism at all.
CACHE_CHUNK = 250
CHUNKS_PER_WORKER = 4


def build_cache(data_path, cache_path, limit=None, augment=False, quiet=False,
                workers=1):
    """Replay every game once and write features, extras and targets.

    Replaying a game means running the Python move generator once per move to
    turn a token back into a move, which is why this is worth parallelising:
    it is pure Python, and it is the one phase BLAS cannot help with.
    """
    started = time.time()

    with open(data_path) as fh:
        lines = [line for _, line in zip(range(limit), fh)] if limit \
            else fh.readlines()
    games = len(lines)

    # Refuse a mixed file rather than train on one. The two modes label
    # differently -- a scalar team result against a per-seat vector, a
    # negatable score against a paranoid one -- so a cache built from both is
    # not merely worse, it is meaningless, and nothing downstream would say so.
    modes = set()
    for line in lines:
        try:
            modes.add(json.loads(line).get("mode") or "teams")
        except ValueError:
            continue
    if len(modes) > 1:
        raise ValueError("%s mixes %s games; the two modes cannot be trained "
                         "together" % (data_path, " and ".join(sorted(modes))))
    if not quiet and modes:
        print("  mode: %s" % modes.pop())

    if workers == 0:
        workers = multiprocessing.cpu_count()
    chunk = max(1, min(CACHE_CHUNK,
                       -(-games // max(1, workers * CHUNKS_PER_WORKER))))
    jobs = [(lines[i:i + chunk], augment) for i in range(0, games, chunk)]
    del lines
    parts, done = [], 0
    if workers > 1:
        # imap, not imap_unordered: block order is what makes the cache
        # reproducible regardless of the worker count.
        with multiprocessing.Pool(workers) as pool:
            for part in pool.imap(_cache_chunk, jobs):
                parts.append(part)
                done += chunk
                if not quiet:
                    rate = done / max(time.time() - started, 1e-9)
                    print("  cached %d/%d games, %d positions, %.0f games/s"
                          % (min(done, games), games,
                             sum(len(p[2]) for p in parts), rate), flush=True)
    else:
        for job in jobs:
            parts.append(_cache_chunk(job))
            done += chunk
            if not quiet:
                rate = done / max(time.time() - started, 1e-9)
                print("  cached %d/%d games, %d positions, %.0f games/s"
                      % (min(done, games), games,
                         sum(len(p[2]) for p in parts), rate), flush=True)

    # Game ids are numbered within a chunk, so shift each block past the last.
    game_ids, base = [], 0
    for part in parts:
        game_ids.append(part[4] + base)
        base += part[5]

    arrays = {
        "features": np.concatenate([p[0] for p in parts]),
        "extras": np.concatenate([p[1] for p in parts]),
        "cp": np.concatenate([p[2] for p in parts]),
        "result": np.concatenate([p[3] for p in parts]),
        "game": np.concatenate(game_ids),
    }
    os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
    np.savez(cache_path, **arrays)
    if not quiet:
        print("cached %d positions from %d games in %.0fs -> %s"
              % (len(arrays["cp"]), games, time.time() - started, cache_path))
    return arrays


def load_cache(path):
    with np.load(path) as data:
        out = {k: data[k] for k in ("features", "extras", "cp", "result")}
        # Caches written before the split was fixed carry no game ids. Refusing
        # is the right answer: silently falling back to a per-position split
        # would reproduce the leak the ids exist to close, and the only sign
        # would be a validation number that quietly means something else.
        if "game" not in data:
            raise SystemExit(
                "%s predates the per-game validation split and has no game "
                "ids.\nDelete it and let this rebuild -- a per-position split "
                "puts a game's own\nneighbouring plies on both sides of the "
                "line, so the held-out number is\nmeasuring memory rather "
                "than generalisation." % path)
        out["game"] = data["game"]
        return out


def extras_for_training(raw):
    """The cache stores the RAW extras; this is what the float model is fed.

    Every input reaches the quantised net at 127x its float value, because the
    accumulator half is crelu'd to [0,127] where this model clips to [0,1]. The
    points differences arrive raw in [-127,127], so on that footing they were
    127x too loud in training, while the alive flags were 127x too quiet at
    inference. Dividing the differences here puts both halves on the same
    footing as h0; tetrarch/nnue.py scales them back on the way out, and the
    two are versioned together as net format 2.

    Derived rather than cached, so a cache built before this stays usable.
    """
    out = raw.astype(np.float32).copy()
    out[:, 4:] /= float(nnue.EXTRA_CLAMP)
    return out


# --- the float model --------------------------------------------------------

class Model:
    """Float weights in the same shape as the quantised net."""

    def __init__(self, seed=0):
        rng = np.random.default_rng(seed)

        def scaled(shape, fan_in):
            return (rng.standard_normal(shape) * (1.0 / np.sqrt(fan_in))
                    ).astype(np.float32)

        # One extra row, permanently zero: an inactive feature slot indexes
        # it instead of being masked out after the fact. That deletes an
        # (n, MAX_FEATURES, L1) temporary from every forward pass.
        #
        # Drawn at the real size and then padded, so the RNG stream -- and
        # therefore every later layer's init -- is unchanged by the pad row.
        self.w1 = np.vstack([scaled((nnue.NFEATURES, nnue.L1), MAX_FEATURES),
                             np.zeros((1, nnue.L1), dtype=np.float32)])
        self.b1 = np.zeros(nnue.L1, dtype=np.float32)
        self.w2 = scaled((nnue.L2, nnue.L1 + nnue.NEXTRA), nnue.L1)
        self.b2 = np.zeros(nnue.L2, dtype=np.float32)
        self.w3 = scaled((nnue.L3, nnue.L2), nnue.L2)
        self.b3 = np.zeros(nnue.L3, dtype=np.float32)
        self.w4 = scaled((nnue.L3,), nnue.L3)
        self.b4 = np.zeros(1, dtype=np.float32)
        self.names = ("w1", "b1", "w2", "b2", "w3", "b3", "w4", "b4")

    def params(self):
        return [getattr(self, n) for n in self.names]

    def forward(self, features, extras):
        """Returns the output and everything the backward pass needs."""
        mask = features >= 0
        safe = np.where(mask, features, nnue.NFEATURES)        # -> the zero row
        acc = self.w1[safe].sum(axis=1) + self.b1             # (n, L1)
        h0 = np.clip(acc, 0.0, 1.0)
        x = np.concatenate([h0, extras], axis=1)              # (n, L1+NEXTRA)
        z1 = x @ self.w2.T + self.b2
        h1 = np.clip(z1, 0.0, 1.0)
        z2 = h1 @ self.w3.T + self.b3
        h2 = np.clip(z2, 0.0, 1.0)
        out = h2 @ self.w4 + self.b4
        return out, (mask, safe, acc, h0, x, z1, h1, z2, h2)

    def backward(self, cache, d_out):
        mask, safe, acc, h0, x, z1, h1, z2, h2 = cache
        n = d_out.shape[0]
        grads = {}

        grads["w4"] = h2.T @ d_out / n
        grads["b4"] = np.array([d_out.mean()], dtype=np.float32)
        d_h2 = np.outer(d_out, self.w4)
        d_z2 = d_h2 * ((z2 > 0) & (z2 < 1))

        grads["w3"] = d_z2.T @ h1 / n
        grads["b3"] = d_z2.mean(axis=0)
        d_h1 = d_z2 @ self.w3
        d_z1 = d_h1 * ((z1 > 0) & (z1 < 1))

        grads["w2"] = d_z1.T @ x / n
        grads["b2"] = d_z1.mean(axis=0)
        d_x = d_z1 @ self.w2
        d_h0 = d_x[:, :nnue.L1]
        d_acc = d_h0 * ((acc > 0) & (acc < 1)) / n

        grads["b1"] = d_acc.sum(axis=0)
        # Every active feature of a sample gets that sample's d_acc, so the w1
        # gradient is indicator.T @ d_acc. Written as an explicit scatter this
        # was `np.add.at` over an (active, L1) array -- 79% of a training step,
        # single-threaded, and it materialised ~55 MB a batch. As a matmul it
        # is ~65x faster, bit-identical, and BLAS spreads it over the cores.
        #
        # Plain assignment rather than accumulation is safe: a feature is
        # (colour, type, square) and a square holds one piece, so a sample
        # cannot list the same feature twice.
        sample, _ = np.nonzero(mask)
        ind = np.zeros((n, nnue.NFEATURES + 1), dtype=np.float32)
        ind[sample, safe[mask]] = 1.0
        grads["w1"] = ind.T @ d_acc.astype(np.float32)
        return grads


class Adam:
    def __init__(self, model, lr=1e-3, beta1=0.9, beta2=0.999, eps=1e-8):
        self.model, self.lr = model, lr
        self.b1, self.b2, self.eps = beta1, beta2, eps
        self.t = 0
        self.m = {n: np.zeros_like(getattr(model, n)) for n in model.names}
        self.v = {n: np.zeros_like(getattr(model, n)) for n in model.names}

    def step(self, grads):
        self.t += 1
        bc1 = 1 - self.b1 ** self.t
        bc2 = 1 - self.b2 ** self.t
        for name in self.model.names:
            param = getattr(self.model, name)
            grad = grads[name]
            self.m[name] = self.b1 * self.m[name] + (1 - self.b1) * grad
            self.v[name] = self.b2 * self.v[name] + (1 - self.b2) * grad * grad
            param -= self.lr * (self.m[name] / bc1) / \
                (np.sqrt(self.v[name] / bc2) + self.eps)
            clip = W1_CLIP if name in ("w1", "b1") else WQ_CLIP
            np.clip(param, -clip, clip, out=param)


# --- quantisation -----------------------------------------------------------

def quantise(model):
    """Float weights -> the integer net the C core and nnue.py both read."""
    return nnue.Net(
        np.clip(np.round(model.w1[:nnue.NFEATURES] * QA),
                -32768, 32767).astype(np.int16),
        np.clip(np.round(model.b1 * QA), -2 ** 31, 2 ** 31 - 1).astype(np.int32),
        np.clip(np.round(model.w2 * QB), -128, 127).astype(np.int8),
        np.round(model.b2 * QA).astype(np.int32),
        np.clip(np.round(model.w3 * QB), -128, 127).astype(np.int8),
        np.round(model.b3 * QA).astype(np.int32),
        np.clip(np.round(model.w4 * QB), -128, 127).astype(np.int8),
        np.round(model.b4 * QA).astype(np.int32),
    )


# --- self check -------------------------------------------------------------

def _hms(secs):
    secs = int(round(secs))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm%02ds" % (secs // 60, secs % 60)
    return "%dh%02dm%02ds" % (secs // 3600, secs % 3600 // 60, secs % 60)


def summarise_run(history, out_dir):
    """The end-of-run report.

    `history` is (epoch, train loss, held-out loss, seconds) per epoch. Pure so
    that --self-check can exercise the epoch picking, which is the only part
    here that can be wrong quietly: reporting the wrong epoch as best would
    send someone to A/B a net that is not the one the run selected.
    """
    best_epoch, best_val = min(((e, v) for e, _, v, _ in history),
                               key=lambda pair: pair[1])
    total = sum(s for _, _, _, s in history)
    last = history[-1][0]

    out = ["", "epoch   train     held-out      time"]
    for epoch, train_loss, val, secs in history:
        out.append("  %3d   %.5f   %.5f   %7s%s"
                   % (epoch, train_loss, val, _hms(secs),
                      "   <- best" if epoch == best_epoch else ""))

    out.append("")
    out.append("best epoch %d of %d, held-out loss %.5f"
               % (best_epoch, last, best_val))
    if len(history) > 1:
        if best_epoch == last:
            out.append("The last epoch was the best one, so this run may still "
                       "have been improving.")
            out.append("Consider more epochs before reading anything into the "
                       "number.")
        else:
            stalled = last - best_epoch
            out.append("%d later epoch%s did not improve on it."
                       % (stalled, "" if stalled == 1 else "s"))
    out.append("net-best.nnue is epoch %d: %s"
               % (best_epoch, os.path.join(out_dir, "net-best.nnue")))
    out.append("%d epochs in %s" % (len(history), _hms(total)))
    out.append("")
    out.append("Held-out loss is NOT Elo. Promote only on a measured A/B "
               "against the net")
    out.append("the engine currently plays with.")
    return "\n".join(out)


def self_check():
    """The two invariants this file's fast paths rest on.

        python3 train.py --self-check

    Neither would raise if it broke. A gradient leaking into the pad row makes
    the shipped net differ from the trained one, because `quantise` slices that
    row off; and the matmul gradient replaced an explicit scatter that nothing
    else ever compares it against.
    """
    rng = np.random.default_rng(0)
    n = 96
    model = Model(0)
    opt = Adam(model, lr=1e-3)

    features = np.full((n, MAX_FEATURES), -1, dtype=np.int16)
    for row in range(n):
        live = int(rng.integers(4, MAX_FEATURES))       # some slots inactive
        features[row, :live] = rng.choice(nnue.NFEATURES, size=live,
                                          replace=False)
    extras = rng.integers(-4, 5, size=(n, nnue.NEXTRA)).astype(np.float32)
    d_out = rng.standard_normal(n).astype(np.float32)

    _, cache = model.forward(features, extras)
    mask, safe = cache[0], cache[1]
    got = model.backward(cache, d_out)["w1"]

    # 1. the matmul gradient equals the scatter it replaced
    want = np.zeros_like(model.w1)
    contrib = np.repeat(_d_acc_of(model, cache, d_out), MAX_FEATURES,
                        axis=0)[mask.ravel()]
    np.add.at(want, safe[mask], contrib)
    worst = float(np.abs(want - got).max())
    assert worst < 1e-6, "w1 gradient diverged from the scatter: %.3e" % worst

    # 2. inactive slots contribute nothing -- the whole point of the pad row
    assert not got[nnue.NFEATURES].any(), "pad row received gradient"

    # 3. and it stays zero through real optimiser steps, including the clip
    for _ in range(5):
        _, cache = model.forward(features, extras)
        opt.step(model.backward(
            cache, rng.standard_normal(n).astype(np.float32)))
    pad = float(np.abs(model.w1[nnue.NFEATURES]).max())
    assert pad == 0.0, "pad row picked up weight: %.3e" % pad

    # 4. the report names the epoch the run actually selected. A ranking that
    #    is right by accident on a monotone run is not a check, so the best
    #    value sits in the middle and the losses are not sorted either way.
    mixed = [(1, 0.9, 0.50, 61.0), (2, 0.8, 0.30, 62.0),
             (3, 0.7, 0.41, 63.0), (4, 0.6, 0.35, 64.0)]
    report = summarise_run(mixed, "nets/x")
    assert "best epoch 2 of 4" in report, report
    assert "0.30000" in report, report
    assert "2 later epochs did not improve" in report, report
    assert "net-best.nnue is epoch 2" in report, report
    # A tie keeps the earlier epoch: it trained for less and generalised the
    # same, and net-best.nnue was written on strict improvement, so the later
    # one never overwrote it.
    tied = [(1, 0.9, 0.30, 1.0), (2, 0.8, 0.30, 1.0)]
    assert "best epoch 1 of 2" in summarise_run(tied, "n"), "tie went late"
    # Still falling at the end is the case worth saying out loud.
    falling = [(1, 0.9, 0.50, 1.0), (2, 0.8, 0.40, 1.0)]
    assert "may still" in summarise_run(falling, "n")
    assert _hms(59) == "59s" and _hms(61) == "1m01s" and _hms(3661) == "1h01m01s"

    print("train.py self-check: gradient matches the scatter (%.2e), "
          "pad row zero after 5 steps, %d features, report picks the right "
          "epoch" % (worst, nnue.NFEATURES))


def _d_acc_of(model, cache, d_out):
    """d_acc exactly as `backward` computes it, for the self-check only."""
    _, _, acc, _, _, z1, _, z2, _ = cache
    n = d_out.shape[0]
    d_z2 = np.outer(d_out, model.w4) * ((z2 > 0) & (z2 < 1))
    d_z1 = (d_z2 @ model.w3) * ((z1 > 0) & (z1 < 1))
    d_x = d_z1 @ model.w2
    return (d_x[:, :nnue.L1] * ((acc > 0) & (acc < 1)) / n).astype(np.float32)


# --- training ---------------------------------------------------------------

def targets_for(cp, result, blend):
    return blend * _sigmoid(cp / SCALE_CP) + (1.0 - blend) * result


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def evaluate_loss(model, data, index, batch, blend):
    total, seen = 0.0, 0
    for start in range(0, len(index), batch):
        sel = index[start:start + batch]
        out, _ = model.forward(data["features"][sel],
                               extras_for_training(data["extras"][sel]))
        want = targets_for(data["cp"][sel], data["result"][sel], blend)
        pred = _sigmoid(out * 127.0 / SCALE_CP)
        total += float(((pred - want) ** 2).sum())
        seen += len(sel)
    return total / max(seen, 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", metavar="PATH", help="gen_data.py JSONL")
    ap.add_argument("--cache", metavar="PATH",
                    help="feature cache .npz; built from --data if absent")
    ap.add_argument("--out", metavar="DIR",
                    help="checkpoint directory; required, no default")
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--lambda", dest="blend", type=float, default=0.7,
                    help="score/result blend, 1.0 = pure score")
    ap.add_argument("--val", type=float, default=0.02,
                    help="held-out fraction")
    ap.add_argument("--games", type=int, help="cap games when building a cache")
    ap.add_argument("--augment", action="store_true",
                    help="label every position from all four perspectives")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--self-check", action="store_true",
                    help="verify the gradient fast paths and exit")
    ap.add_argument("--cache-workers", type=int, default=0, metavar="N",
                    help="processes for the cache build; 0 means every core")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.self_check:
        self_check()
        return 0
    # Checked here rather than by argparse so --self-check needs neither. Both
    # stay mandatory otherwise: no output path in this repo has a default.
    if not args.cache or not args.out:
        ap.error("--cache and --out are required")

    if os.path.exists(args.cache):
        print("loading cache %s" % args.cache)
        data = load_cache(args.cache)
    else:
        if not args.data:
            ap.error("--cache does not exist, so --data is needed to build it")
        data = build_cache(args.data, args.cache, args.games, args.augment,
                           args.quiet, args.cache_workers)

    os.makedirs(args.out, exist_ok=True)
    n = len(data["cp"])
    print("%d positions, %d features wide, batch %d, lambda %.2f"
          % (n, MAX_FEATURES, args.batch, args.blend))

    # Split by GAME, not by position. A game's plies are near-duplicates of
    # each other -- one move apart, nearly the same board, nearly the same
    # label -- so a per-position split leaves about seventy relatives of every
    # validation position sitting in the training set. The number that came out
    # of that measured how well the net had memorised games it had largely
    # already seen, which is why it has never tracked Elo, and every "best
    # epoch" in docs/AB.md was chosen on it.
    rng = np.random.default_rng(args.seed)
    game = data["game"]
    ids = np.unique(game)
    held = rng.permutation(ids)[:max(1, int(len(ids) * args.val))]
    is_val = np.isin(game, held)
    val_index = np.nonzero(is_val)[0]
    train_index = np.nonzero(~is_val)[0]
    print("holding out %d of %d games (%d of %d positions)"
          % (len(held), len(ids), len(val_index), n))

    model = Model(args.seed)
    opt = Adam(model, lr=args.lr)
    best = float("inf")
    history = []

    for epoch in range(1, args.epochs + 1):
        started = time.time()
        rng.shuffle(train_index)
        running, batches = 0.0, 0
        for start in range(0, len(train_index), args.batch):
            sel = train_index[start:start + args.batch]
            features = data["features"][sel]
            extras = extras_for_training(data["extras"][sel])
            out, cache = model.forward(features, extras)
            want = targets_for(data["cp"][sel], data["result"][sel], args.blend)
            pred = _sigmoid(out * 127.0 / SCALE_CP)
            # d/dout of MSE through the sigmoid.
            d_out = (2.0 * (pred - want) * pred * (1 - pred)
                     * 127.0 / SCALE_CP).astype(np.float32)
            opt.step(model.backward(cache, d_out))
            running += float(((pred - want) ** 2).mean())
            batches += 1
            if not args.quiet and batches % 200 == 0:
                done = start + len(sel)
                rate = done / max(time.time() - started, 1e-9)
                print("  epoch %d  %d/%d  loss %.5f  %.0f pos/s"
                      % (epoch, done, len(train_index), running / batches, rate),
                      flush=True)

        val = evaluate_loss(model, data, val_index, args.batch, args.blend)
        secs = time.time() - started
        train_loss = running / max(batches, 1)
        history.append((epoch, train_loss, val, secs))
        print("epoch %d  train %.5f  val %.5f  %.0fs"
              % (epoch, train_loss, val, secs))

        path = os.path.join(args.out, "net-epoch%02d.nnue" % epoch)
        quantise(model).save(path)
        if val < best:
            best = val
            quantise(model).save(os.path.join(args.out, "net-best.nnue"))
            print("  new best -> %s" % os.path.join(args.out, "net-best.nnue"))

    print(summarise_run(history, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
