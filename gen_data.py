#!/usr/bin/env python3
"""gen_data.py -- self-play position generation for NNUE training.

    python3 gen_data.py 20000 --out /path/to/games.jsonl --nodes 5000 --workers 0

The positional argument is the number of **games**. Every output path is a CLI
argument with no repo default, and the tool refuses to start if the target
already exists unless --resume is given: a smoke test that reuses a real output
path once destroyed four hours of a training run.

RESUMABLE
    One JSON object per line, one line per game, appended and flushed as it
    goes. --resume counts the lines already present and skips that many game
    indices. Game N is seeded from (--seed, N), so the same index always
    produces the same game and a resumed run continues rather than repeating.

WHAT IS STORED
    Games, not positions: the opening FEN4, the move list, and the search score
    after each move. Storing games is ~1 kB each against ~170 bytes per
    position, and positions are recovered by replaying -- which the trainer has
    to be able to do anyway to build features.

    Scores are from the side to move's team, in centipawns, exactly as the
    search reports them. The trainer decides how to blend them with the game
    result; that is a training decision and does not belong in the data.

THE ENGINE THIS USES IS THE THROWAWAY
    Net v0 is bootstrapped from the hand eval (project brief). The data is only
    as good as the engine that made it, which is the entire reason this step
    exists and the entire reason it is thrown away afterwards.
"""

import argparse
import json
import multiprocessing
import os
import random
import signal
import sys
import time

from tetrarch.board import start_board, SETUPS, DEFAULT_SETUP, MODE_TEAMS, move_str
from tetrarch import core
from tetrarch import movegen as gen
from tetrarch.search import Limits, search

MAX_PLIES = 400
#: Stop recording once a side is this far ahead: the rest of the game is noise
#: for a value net, and cheap wins would dominate the label distribution.
RESIGN_CP = 2500
RESIGN_PLIES = 8


def play_one(job):
    """Self-play one game. Returns a dict, or None if the opening was dead."""
    index, seed, setup, opening_plies, nodes, depth = job
    rng = random.Random(seed)

    board = start_board(setup, MODE_TEAMS)
    for _ in range(opening_plies):
        legal = gen.gen_legal(board)
        if not legal:
            return None
        board.make(rng.choice(legal))
    if not gen.gen_legal(board):
        return None

    start_fen4 = board.to_fen4().replace("\n", "")
    moves, scores = [], []
    result, reason = None, ""
    resign_run = 0

    core.clear_hash()
    for _ in range(MAX_PLIES):
        legal = gen.gen_legal(board)
        if not legal:
            if gen.in_check(board, board.turn):
                # The mated seat's team loses (§7). Result is from team 0's view.
                result = 0.0 if (board.turn & 1) == 0 else 1.0
                reason = "checkmate"
            else:
                result, reason = 0.5, "stalemate"
            break
        if board.halfmove >= 200:
            result, reason = 0.5, "fifty-move"
            break

        # Through the root driver, not core.search directly: iterative
        # deepening is what turns a node budget into a usable move. A raw
        # fixed-depth call with a shallow budget just aborts inside the first
        # subtree.
        r = search(board, Limits(depth=depth, nodes=nodes))
        if not r.best:
            result, reason = 0.5, "no move"
            break
        # Store from team 0's perspective so the trainer never has to guess.
        scores.append(r.score if (board.turn & 1) == 0 else -r.score)
        moves.append(move_str(r.best))
        board.make(r.best)

        if abs(scores[-1]) >= RESIGN_CP:
            resign_run += 1
            if resign_run >= RESIGN_PLIES:
                result = 1.0 if scores[-1] > 0 else 0.0
                reason = "resign"
                break
        else:
            resign_run = 0
    else:
        result, reason = 0.5, "adjudicated"

    return {"i": index, "setup": setup, "fen4": start_fen4,
            "moves": " ".join(moves), "scores": scores,
            "result": result, "reason": reason}


def jobs(start, count, args):
    """A generator, so Pool's thread-backed task handler feeds workers lazily
    rather than materialising millions of items in a queue."""
    for i in range(start, start + count):
        yield (i, args.seed * 1000003 + i, args.setup, args.opening_plies,
               args.nodes, args.depth)


def _ignore_sigint():
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("games", type=int, help="number of games to generate")
    ap.add_argument("--out", required=True, metavar="PATH",
                    help="JSONL output; required, no default")
    ap.add_argument("--resume", action="store_true",
                    help="append to an existing --out, skipping games already in it")
    ap.add_argument("--nodes", type=int, default=5000,
                    help="node budget per move (default 5000)")
    ap.add_argument("--depth", type=int, default=8,
                    help="depth ceiling per move (default 8)")
    ap.add_argument("--setup", default=DEFAULT_SETUP, choices=SETUPS)
    ap.add_argument("--opening-plies", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1, help="0 means every core")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    done = 0
    if os.path.exists(args.out):
        if not args.resume:
            ap.error("%s exists; pass --resume to continue it, or pick a fresh "
                     "path" % args.out)
        with open(args.out) as fh:
            done = sum(1 for _ in fh)
        print("resuming %s: %d games already present" % (args.out, done))
    remaining = args.games - done
    if remaining <= 0:
        print("%s already has %d games" % (args.out, done))
        return 0

    nproc = (os.cpu_count() or 1) if args.workers == 0 else max(1, args.workers)
    print("%d games (%d remaining) | %s teams | nodes %d depth %d"
          % (args.games, remaining, args.setup, args.nodes, args.depth))

    started = time.time()
    written = 0
    positions = 0
    out = open(args.out, "a")

    def absorb(game):
        nonlocal written, positions
        if game is None:
            return
        out.write(json.dumps(game) + "\n")
        out.flush()
        written += 1
        positions += len(game["scores"])
        if args.quiet or written % 20:
            return
        secs = time.time() - started
        rate = written / max(secs, 1e-9)
        eta = (remaining - written) / max(rate, 1e-9)
        sys.stderr.write("\r%d/%d games  %d positions  %.1f g/s  eta %dm%02ds "
                         % (written, remaining, positions, rate,
                            eta // 60, eta % 60))
        sys.stderr.flush()

    try:
        if nproc == 1:
            for job in jobs(done, remaining, args):
                absorb(play_one(job))
        else:
            with multiprocessing.Pool(nproc, initializer=_ignore_sigint) as pool:
                for game in pool.imap_unordered(play_one,
                                                jobs(done, remaining, args),
                                                chunksize=4):
                    absorb(game)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted -- %s is complete up to the last line\n"
                         % args.out)
    finally:
        sys.stderr.write("\n")
        out.close()

    secs = time.time() - started
    print("%d games, %d positions, %.1fs (%.1f games/s)"
          % (written, positions, secs, written / max(secs, 1e-9)))
    print("out: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
