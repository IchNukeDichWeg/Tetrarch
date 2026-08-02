#!/usr/bin/env python3
"""book.py -- build a balanced opening book.

    python3 book.py 2000 --out book.txt --setup all --workers 0
    python3 book.py 200 --out small.txt --band 40 --depth 7

Openings have always been `n` random legal moves. That is diverse, which the
trainer wants, but a good share of what it produces is already decided before
either engine has moved. Those games measure the opening rather than the thing
under test: in an A/B they widen the interval, and in training data they teach
the net about positions no game reaches.

This keeps the random walk and then filters it. A position is kept when the
engine scores it inside `--band` centipawns of level at `--depth`, so both
teams still have a game. Duplicates are dropped by Zobrist key, which also
throws out the transpositions a short random walk produces in quantity.

The output is one `setup fen4` per line, and `gen_data.py --book` and
`match.py --book` both read it. Being a plain file matters: the book that
produced a dataset can be kept beside it, and a result stays reproducible
without re-deriving which positions it used.

Balance is measured with whatever evaluation is loaded, so a book built with
`--net` is balanced in that net's opinion. That is a real bias and the reason
`--net` is not the default: a book built by the net being trained would filter
out exactly the positions that net misjudges.
"""

import argparse
import multiprocessing
import os
import random
import signal
import sys
import time

from tetrarch.board import SETUPS, DEFAULT_SETUP, MODE_TEAMS, start_board
from tetrarch import core
from tetrarch import movegen as gen


def setup_for(index, setup):
    """Round-robin when `all`, so the book is evenly spread over the five."""
    return SETUPS[index % len(SETUPS)] if setup == "all" else setup


def _worker_init(net_path):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if net_path:
        from tetrarch import nnue
        core.load_net(nnue.Net.load(net_path))


def candidate(job):
    """One random walk. Returns (setup, fen4, key, score) or None.

    None means the walk died or the position is too one-sided to be worth
    playing from. Every rejection is silent by design -- the caller counts
    them, because a band that rejects almost everything is a broken setting
    rather than a slow one.
    """
    index, seed, setup, plies, depth, band = job
    rng = random.Random(seed)
    board = start_board(setup, MODE_TEAMS)
    for _ in range(plies):
        legal = gen.gen_legal(board)
        if not legal:
            return None
        board.make(rng.choice(legal))
    if not gen.gen_legal(board):
        return None

    core.clear_hash()
    result = core.search(board, depth)
    if not result.best or abs(result.score) > band:
        return None
    return (setup, board.to_fen4().replace("\n", ""), board.key, result.score)


def jobs(count, args):
    for i in range(count):
        yield (i, args.seed * 7919 + i, setup_for(i, args.setup),
               args.plies, args.depth, args.band)


def load(path):
    """Read a book as a list of (setup, fen4). Blank lines and # are ignored."""
    out = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            setup, fen4 = line.split(None, 1)
            out.append((setup, fen4))
    if not out:
        raise ValueError("%s has no positions" % path)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("positions", type=int, help="how many to keep")
    ap.add_argument("--out", required=True, metavar="PATH")
    ap.add_argument("--setup", default=DEFAULT_SETUP,
                    choices=list(SETUPS) + ["all"])
    ap.add_argument("--plies", type=int, default=8,
                    help="random plies before the balance test")
    ap.add_argument("--depth", type=int, default=6,
                    help="search depth the balance is judged at")
    ap.add_argument("--band", type=int, default=60, metavar="CP",
                    help="keep positions within this many centipawns of level")
    ap.add_argument("--net", metavar="PATH",
                    help="judge balance with this net rather than the hand "
                         "eval; biases the book towards what it understands")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1, help="0 means every core")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not core.available():
        print("book.py needs the C core: run ./setup.sh", file=sys.stderr)
        return 1
    if args.net and not os.path.exists(args.net):
        ap.error("no such net: %s" % args.net)

    nproc = (os.cpu_count() or 1) if args.workers == 0 else max(1, args.workers)
    print("%d positions | %s | %d plies, depth %d, band +/-%d | eval %s"
          % (args.positions, args.setup, args.plies, args.depth, args.band,
             args.net if args.net else "hand (throwaway)"))

    seen = set()
    kept = []
    tried = 0
    started = time.time()
    # Walks are cheap and most are rejected, so ask for far more than needed
    # and stop when the target is reached rather than guessing a yield rate.
    batch = max(args.positions * 8, 256)

    with open(args.out, "w") as handle:
        handle.write("# %d plies, depth %d, band +/-%d, setup %s, seed %d\n"
                     % (args.plies, args.depth, args.band, args.setup, args.seed))
        with multiprocessing.Pool(nproc, initializer=_worker_init,
                                  initargs=(args.net,)) as pool:
            for found in pool.imap_unordered(candidate, jobs(batch, args), 16):
                tried += 1
                if found is not None:
                    setup, fen4, key, _score = found
                    if key not in seen:
                        seen.add(key)
                        kept.append(fen4)
                        handle.write("%s %s\n" % (setup, fen4))
                if not args.quiet and tried % 500 == 0:
                    print("  %d/%d kept, %d walked" % (len(kept), args.positions, tried),
                          flush=True)
                if len(kept) >= args.positions:
                    pool.terminate()
                    break

    secs = time.time() - started
    print("%d positions from %d walks (%.1f%% kept) in %.1fs"
          % (len(kept), tried, 100.0 * len(kept) / max(tried, 1), secs))
    print("out: %s" % os.path.abspath(args.out))
    if len(kept) < args.positions:
        print("SHORT of %d: widen --band or raise the walk budget"
              % args.positions, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
