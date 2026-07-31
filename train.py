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
import os
import sys
import time

import numpy as np

from tetrarch.board import Board, MODE_TEAMS, move_str
from tetrarch import movegen as gen
from tetrarch import nnue

MAX_FEATURES = 64          # 64 pieces at most; promotion never adds one
SCALE_CP = 400.0           # centipawns per logit, the usual NNUE choice
QA = 127 * 64              # input-layer weight scale (see nnue.py)
QB = 64                    # hidden-layer weight scale
W1_CLIP = 32767.0 / QA
WQ_CLIP = 127.0 / QB


# --- phase 1: cache ---------------------------------------------------------

def build_cache(data_path, cache_path, limit=None, augment=False, quiet=False):
    """Replay every game once and write features, extras and targets."""
    feats, extras, cps, results = [], [], [], []
    started = time.time()
    games = 0

    with open(data_path) as fh:
        for line in fh:
            if limit and games >= limit:
                break
            record = json.loads(line)
            games += 1
            board = Board.from_fen4(record["fen4"], MODE_TEAMS)
            result = record["result"]           # team 0's score
            for token, cp in zip(record["moves"].split(), record["scores"]):
                # The score was recorded before this move was played, so the
                # position to label is the one we are standing in now.
                views = range(4) if augment else (board.turn,)
                for persp in views:
                    active = nnue.active_features(board, persp)
                    row = np.full(MAX_FEATURES, -1, dtype=np.int16)
                    row[:len(active)] = active[:MAX_FEATURES]
                    feats.append(row)
                    extras.append(nnue.extra_inputs(board, persp))
                    # Stored scores and results are team 0's; flip for team 1.
                    flip = (persp & 1) == 1
                    cps.append(-cp if flip else cp)
                    results.append(1.0 - result if flip else result)
                move = next((m for m in gen.gen_legal(board)
                             if move_str(m) == token), None)
                if move is None:
                    break                      # corrupt line; drop the rest
                board.make(move)
            if not quiet and games % 2000 == 0:
                rate = games / max(time.time() - started, 1e-9)
                print("  cached %d games, %d positions, %.0f games/s"
                      % (games, len(feats), rate), flush=True)

    arrays = {
        "features": np.asarray(feats, dtype=np.int16),
        "extras": np.asarray(extras, dtype=np.int16),
        "cp": np.asarray(cps, dtype=np.float32),
        "result": np.asarray(results, dtype=np.float32),
    }
    os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
    np.savez(cache_path, **arrays)
    print("cached %d positions from %d games in %.0fs -> %s"
          % (len(feats), games, time.time() - started, cache_path))
    return arrays


def load_cache(path):
    with np.load(path) as data:
        return {k: data[k] for k in ("features", "extras", "cp", "result")}


# --- the float model --------------------------------------------------------

class Model:
    """Float weights in the same shape as the quantised net."""

    def __init__(self, seed=0):
        rng = np.random.default_rng(seed)

        def scaled(shape, fan_in):
            return (rng.standard_normal(shape) * (1.0 / np.sqrt(fan_in))
                    ).astype(np.float32)

        self.w1 = scaled((nnue.NFEATURES, nnue.L1), MAX_FEATURES)
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
        safe = np.where(mask, features, 0)
        acc = self.w1[safe] * mask[:, :, None]
        acc = acc.sum(axis=1) + self.b1                       # (n, L1)
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
        # Scatter the accumulator gradient back onto the active features. Every
        # row of a sample shares the same d_acc, so this is one add per active
        # feature rather than a dense (NFEATURES, L1) matmul.
        flat = safe[mask]
        contrib = np.repeat(d_acc, MAX_FEATURES, axis=0)[mask.ravel()]
        grads["w1"] = (flat, contrib)
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
            if name == "w1":
                # Sparse: only the rows that were active moved.
                rows, contrib = grad
                if rows.size == 0:
                    continue
                dense = np.zeros_like(param)
                np.add.at(dense, rows, contrib)
                grad = dense
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
        np.clip(np.round(model.w1 * QA), -32768, 32767).astype(np.int16),
        np.clip(np.round(model.b1 * QA), -2 ** 31, 2 ** 31 - 1).astype(np.int32),
        np.clip(np.round(model.w2 * QB), -128, 127).astype(np.int8),
        np.round(model.b2 * QA).astype(np.int32),
        np.clip(np.round(model.w3 * QB), -128, 127).astype(np.int8),
        np.round(model.b3 * QA).astype(np.int32),
        np.clip(np.round(model.w4 * QB), -128, 127).astype(np.int8),
        np.round(model.b4 * QA).astype(np.int32),
    )


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
                               data["extras"][sel].astype(np.float32))
        want = targets_for(data["cp"][sel], data["result"][sel], blend)
        pred = _sigmoid(out * 127.0 / SCALE_CP)
        total += float(((pred - want) ** 2).sum())
        seen += len(sel)
    return total / max(seen, 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", metavar="PATH", help="gen_data.py JSONL")
    ap.add_argument("--cache", required=True, metavar="PATH",
                    help="feature cache .npz; built from --data if absent")
    ap.add_argument("--out", required=True, metavar="DIR",
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
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if os.path.exists(args.cache):
        print("loading cache %s" % args.cache)
        data = load_cache(args.cache)
    else:
        if not args.data:
            ap.error("--cache does not exist, so --data is needed to build it")
        data = build_cache(args.data, args.cache, args.games, args.augment,
                           args.quiet)

    os.makedirs(args.out, exist_ok=True)
    n = len(data["cp"])
    print("%d positions, %d features wide, batch %d, lambda %.2f"
          % (n, MAX_FEATURES, args.batch, args.blend))

    rng = np.random.default_rng(args.seed)
    order = rng.permutation(n)
    cut = int(n * args.val)
    val_index, train_index = order[:cut], order[cut:]

    model = Model(args.seed)
    opt = Adam(model, lr=args.lr)
    best = float("inf")

    for epoch in range(1, args.epochs + 1):
        started = time.time()
        rng.shuffle(train_index)
        running, batches = 0.0, 0
        for start in range(0, len(train_index), args.batch):
            sel = train_index[start:start + args.batch]
            features = data["features"][sel]
            extras = data["extras"][sel].astype(np.float32)
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
        print("epoch %d  train %.5f  val %.5f  %.0fs"
              % (epoch, running / max(batches, 1), val, secs))

        path = os.path.join(args.out, "net-epoch%02d.nnue" % epoch)
        quantise(model).save(path)
        if val < best:
            best = val
            quantise(model).save(os.path.join(args.out, "net-best.nnue"))
            print("  new best -> %s" % os.path.join(args.out, "net-best.nnue"))

    print("\nbest held-out loss %.5f" % best)
    print("Held-out loss is NOT Elo. Promote only on a measured A/B against "
          "the hand-eval build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
