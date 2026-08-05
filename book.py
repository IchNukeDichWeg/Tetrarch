#!/usr/bin/env python3
"""book.py -- build a balanced opening book.

    python3 book.py 2000 --out book.txt --setup all --workers 0
    python3 book.py 200 --out small.txt --band 40 --depth 7
    python3 book.py 2000 --mode ffa --out ffa.txt --setup all --workers 0

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

BALANCE MEANS SOMETHING ELSE IN FFA (--mode ffa)
    There are no two sides to be level between, and a Teams band is not merely
    the wrong size here -- an FFA search returns a paranoid score against three
    opponents, which sits near -8600 at the start, so `abs(score) <= 60` would
    reject every position ever generated.

    What is filtered instead is the SPREAD: each of the four seats is searched
    as though it were to move, and the position is kept when the best and
    worst are within `--band` of each other. Judging every seat on the move
    normalises away the tempo, so the four numbers are comparable.

    Note that this is NOT filtering for seat fairness -- match.py's rotation
    already cancels that exactly, by putting A on each seat of one identical
    position. It filters for positions already DECIDED, where one seat wins
    whoever plays it and three of the four rotation games carry no signal.

    Four searches instead of one, and the FFA search has no transposition
    table, so a candidate costs about 274,000 nodes at depth 4 against 20,000
    for a Teams candidate at depth 6. Depth 5 is 2.7x that again and depth 3
    is 31x cheaper but visibly noisier (median spread 224 against 160), which
    is why the default is 4.
"""

import argparse
import multiprocessing
import os
import random
import signal
import sys
import time

from tetrarch.board import (
    SETUPS, DEFAULT_SETUP, MODE_TEAMS, MODE_FFA, MODE_NAMES, start_board)
from tetrarch import core
from tetrarch import game as game_rules
from tetrarch import movegen as gen

#: Default --band per mode. Teams is a two-sided score centred on zero; FFA is
#: a spread between four seats and has no relation to it. Measured over 8-ply
#: classic walks at depth 4: median spread 160, upper quartile 336, so 250
#: keeps roughly three positions in five.
DEFAULT_BAND = {"teams": 60, "ffa": 250}


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
    index, seed, setup, plies, depth, band, mode = job
    rng = random.Random(seed)
    board = start_board(setup, mode)
    for _ in range(plies):
        legal = gen.gen_legal(board)
        if not legal:
            return None
        board.make(rng.choice(legal))
        if mode == MODE_FFA:
            game_rules.resolve(board)
    if not gen.gen_legal(board):
        return None

    if mode == MODE_FFA:
        # An opening that has already eliminated somebody is not an opening.
        if not all(board.alive):
            return None
        # Every seat judged as though it were to move, so the tempo is out of
        # the comparison and the four numbers mean the same thing.
        scores = []
        for seat in range(4):
            view = board.copy()
            view.turn = seat
            view.recompute_key()
            if not gen.gen_legal(view):
                return None
            core.clear_hash()
            result = core.search(view, depth)
            if not result.best:
                return None
            scores.append(result.score)
        spread = max(scores) - min(scores)
        if spread > band:
            return None
        return (setup, board.to_fen4().replace("\n", ""), board.key, spread)

    core.clear_hash()
    result = core.search(board, depth)
    if not result.best or abs(result.score) > band:
        return None
    return (setup, board.to_fen4().replace("\n", ""), board.key, result.score)


def jobs(count, args):
    for i in range(count):
        yield (i, args.seed * 7919 + i, setup_for(i, args.setup),
               args.plies, args.depth, args.band,
               MODE_FFA if args.mode == "ffa" else MODE_TEAMS)


def mode_of(path):
    """Which mode a book was built for, from its `# mode` header.

    A book with no such line predates FFA and is Teams by construction. Kept
    separate from load() so the consumers can refuse a mismatch without either
    of them changing shape.
    """
    with open(path) as handle:
        for line in handle:
            if line.startswith("# mode "):
                name = line[len("# mode "):].strip()
                if name in MODE_NAMES:
                    return name
            elif not line.startswith("#") and line.strip():
                break
    return "teams"


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
    ap.add_argument("--mode", default="teams", choices=("teams", "ffa"),
                    help="teams keeps positions level between the two sides; "
                         "ffa keeps positions whose four seats are within "
                         "--band of each other. The two books are not "
                         "interchangeable and each records its mode")
    ap.add_argument("--depth", type=int, default=None,
                    help="search depth the balance is judged at "
                         "(default 6 teams, 4 ffa -- the FFA search has no "
                         "transposition table and costs 31x more per ply)")
    ap.add_argument("--band", type=int, default=None, metavar="CP",
                    help="keep positions within this many centipawns "
                         "(default 60 teams, 250 ffa; the two measure "
                         "different things and do not compare)")
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
    if args.band is None:
        args.band = DEFAULT_BAND[args.mode]
    if args.depth is None:
        args.depth = 4 if args.mode == "ffa" else 6

    # Like gen_data.py and train.py: a run that dies on a missing directory
    # costs a round-trip to the box for nothing.
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    nproc = (os.cpu_count() or 1) if args.workers == 0 else max(1, args.workers)
    print("%d positions | %s %s | %d plies, depth %d, band %s%d | eval %s"
          % (args.positions, args.setup, args.mode, args.plies, args.depth,
             "spread <=" if args.mode == "ffa" else "+/-", args.band,
             args.net if args.net else "hand (throwaway)"))

    seen = set()
    kept = []
    tried = 0
    interrupted = False
    started = time.time()
    # Walks are cheap and most are rejected, so ask for far more than needed
    # and stop when the target is reached rather than guessing a yield rate.
    batch = max(args.positions * 8, 256)

    with open(args.out, "w") as handle:
        # The mode is a machine-readable header line, not prose: a Teams book
        # fed to an FFA run screens the openings for the wrong thing entirely,
        # and match.py and gen_data.py both refuse it on this line.
        handle.write("# mode %s\n" % args.mode)
        handle.write("# %d plies, depth %d, band %s%d, setup %s, seed %d\n"
                     % (args.plies, args.depth,
                        "spread <=" if args.mode == "ffa" else "+/-",
                        args.band, args.setup, args.seed))
        # Workers ignore SIGINT (see _worker_init), so Ctrl-C arrives here and
        # nowhere else. Positions are written as they are found, so an
        # interrupted run leaves a shorter book rather than a broken one.
        try:
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
                        print("  %d/%d kept, %d walked"
                              % (len(kept), args.positions, tried), flush=True)
                    if len(kept) >= args.positions:
                        pool.terminate()
                        break
        except KeyboardInterrupt:
            interrupted = True
            print("\ninterrupted", flush=True)

    secs = time.time() - started
    print("%d positions from %d walks (%.1f%% kept) in %.1fs"
          % (len(kept), tried, 100.0 * len(kept) / max(tried, 1), secs))
    print("out: %s" % os.path.abspath(args.out))
    if len(kept) < args.positions:
        # Non-zero even when the stop was deliberate, so `book.py && gen_data`
        # cannot walk on into a run with a book smaller than it asked for --
        # which would silently repeat openings and duplicate games.
        print("SHORT of %d (%s): the file is complete and usable, rerun or "
              "widen --band for more"
              % (args.positions, "interrupted" if interrupted
                 else "widen --band or raise the walk budget"),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
