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

WHICH ENGINE LABELS THE DATA
    --net decides. Without it the throwaway hand eval plays, which is how net
    v0 and v1 were bootstrapped (project brief).

    With it, the named net plays and the loop compounds: the labels are search
    scores, and a depth-5 search over a net's own evaluations is better than
    that net evaluating directly. Training on them is what lets generation N+1
    exceed generation N, rather than converging on whatever taught it. Data
    labelled by the hand eval caps a net at roughly the hand eval.
"""

import argparse
import json
import multiprocessing
import os
import random
import signal
import sys
import time

from tetrarch.board import (
    Board, start_board, SETUPS, DEFAULT_SETUP, MODE_TEAMS, MODE_FFA, move_str)
from tetrarch import game as game_rules
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
    index, seed, setup, opening_plies, nodes, depth, opening, mode = job
    rng = random.Random(seed)

    if opening is not None:
        # A book position, already walked and already checked for balance.
        setup, fen4 = opening
        board = Board.from_fen4(fen4, mode)
    else:
        board = start_board(setup, mode)
        for _ in range(opening_plies):
            legal = gen.gen_legal(board)
            if not legal:
                return None
            board.make(rng.choice(legal))
    if not gen.gen_legal(board):
        return None

    start_fen4 = board.to_fen4().replace("\n", "")
    moves, scores = [], []
    # Every position the game has been through, so a self-play game cannot
    # shuffle for 200 plies believing each repeat is a fresh position (§10.2).
    history = []
    result, reason = None, ""
    resign_run = 0

    ffa = mode == MODE_FFA
    core.clear_hash()
    for _ in range(MAX_PLIES):
        ended = game_rules.resolve(board)
        if ended:
            if ffa:
                # Four seats, so the outcome is a vector and not a scalar:
                # each seat's share of its three pairwise contests, on the same
                # [0,1] scale the Teams result uses.
                result = [game_rules.ffa_score(board, s) for s in range(4)]
            elif ended["over"] == "checkmate":
                # The mated seat's team loses (§7). Result is from team 0's view.
                result = 0.0 if (board.turn & 1) == 0 else 1.0
            else:
                result = 0.5
            reason = ended["over"].replace(" ", "-")
            break
        if board.halfmove >= 200:
            if ffa:
                game_rules.award_draw(board)
                result = [game_rules.ffa_score(board, s) for s in range(4)]
            else:
                result = 0.5
            reason = "fifty-move"
            break

        # Through the root driver, not core.search directly: iterative
        # deepening is what turns a node budget into a usable move. A raw
        # fixed-depth call with a shallow budget just aborts inside the first
        # subtree.
        r = search(board, Limits(depth=depth, nodes=nodes), None, history)
        if not r.best:
            result = ([game_rules.ffa_score(board, s) for s in range(4)] if ffa
                      else 0.5)
            reason = "no move"
            break
        # Teams: from team 0's perspective, so the trainer never has to guess.
        # FFA: raw, in the MOVER's terms. A paranoid score is not convertible
        # to another seat by negation -- that is the whole difference between
        # the two modes -- so there is no common frame to store it in, and the
        # trainer labels each position from the seat that was to move.
        scores.append(r.score if ffa or (board.turn & 1) == 0 else -r.score)
        moves.append(move_str(r.best))
        history.append(board.key)
        board.make(r.best)

        # No resign adjudication in FFA. The rule counts consecutive plies over
        # a threshold, and consecutive plies are DIFFERENT seats here, so the
        # run means nothing. A seat 2500 ahead of one opponent still has two
        # more; the game is not over and cutting it there would teach the net
        # that it is. FFA games therefore run to the fifty-move rule or the
        # ply cap, which is most of why they cost 3-4x a Teams game.
        if ffa:
            continue
        if abs(scores[-1]) >= RESIGN_CP:
            resign_run += 1
            if resign_run >= RESIGN_PLIES:
                result = 1.0 if scores[-1] > 0 else 0.0
                reason = "resign"
                break
        else:
            resign_run = 0
    else:
        result = ([game_rules.ffa_score(board, s) for s in range(4)] if ffa
                  else 0.5)
        reason = "adjudicated"

    record = {"i": index, "setup": setup, "fen4": start_fen4,
              "moves": " ".join(moves), "scores": scores,
              "result": result, "reason": reason}
    if ffa:
        # The mode is on every line, so the trainer reads it off the data
        # rather than being told, and a Teams and an FFA file can never be
        # silently mixed into one cache.
        record["mode"] = "ffa"
        record["points"] = [int(p) for p in board.points]
        record["alive"] = [bool(a) for a in board.alive]
    return record


def setup_for(index, setup):
    """Which setup game `index` is played from.

    `all` round-robins the five, so a resumed or truncated run still ends up
    with an even split rather than whatever prefix it reached. The five differ
    only in where the kings and queens start, which is exactly the part of the
    board a learned evaluation memorises per square -- a net trained on one of
    them has no signal at all for the others.
    """
    return SETUPS[index % len(SETUPS)] if setup == "all" else setup


#: Loaded once in main and read by the job generator. A module global rather
#: than an argument because the generator is consumed inside a Pool, and
#: shipping the whole book with every job would copy it per game.
BOOK = None


def jobs(start, count, args):
    """A generator, so Pool's thread-backed task handler feeds workers lazily
    rather than materialising millions of items in a queue."""
    for i in range(start, start + count):
        yield (i, args.seed * 1000003 + i, setup_for(i, args.setup),
               args.opening_plies, args.nodes, args.depth,
               BOOK[i % len(BOOK)] if BOOK else None,
               MODE_FFA if args.mode == "ffa" else MODE_TEAMS)


def resume_index(path):
    """Where a resumed run should start: one past the highest index present.

    NOT the line count. A dead opening writes no line, and interrupting a pool
    leaves dispatched games unwritten, so counting lines rewinds into indices
    that are already in the file and regenerates them. That silently put 80
    duplicate games into the generation-2 dataset.

    Returns (next_index, lines_present).
    """
    highest, lines = -1, 0
    with open(path) as fh:
        for line in fh:
            try:
                i = json.loads(line).get("i")
            except ValueError:
                continue               # truncated final line: not a game
            lines += 1
            if isinstance(i, int) and i > highest:
                highest = i
    return highest + 1, lines


def _worker_init(net_path):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Once per worker, never per game: loading a net clears the transposition
    # table, so doing it inside play_one would throw away the table every game
    # and quietly change what the node budget buys.
    if net_path:
        _load_net(net_path)


def _load_net(path):
    from tetrarch import nnue
    core.load_net(nnue.Net.load(path))


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
    ap.add_argument("--setup", default=DEFAULT_SETUP,
                    choices=list(SETUPS) + ["all"],
                    help="one setup, or `all` to round-robin the five")
    ap.add_argument("--mode", default="teams", choices=("teams", "ffa"),
                    help="teams is a two-sided game with a scalar result; ffa "
                         "is four independent seats and stores a per-seat "
                         "result vector plus scores in the mover's terms. The "
                         "two cannot be trained together -- the mode is "
                         "written on every line so they cannot be mixed by "
                         "accident")
    ap.add_argument("--book", metavar="PATH",
                    help="opening positions from book.py instead of random "
                         "plies; --setup and --opening-plies are then unused")
    ap.add_argument("--opening-plies", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--net", metavar="PATH",
                    help="net that plays; omitted means the throwaway hand eval")
    ap.add_argument("--workers", type=int, default=1, help="0 means every core")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    # Create the parent directory rather than dying on it. Failing after the
    # banner has printed reads like the run started and then crashed.
    parent = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(parent, exist_ok=True)

    done = 0
    if os.path.exists(args.out):
        if not args.resume:
            ap.error("%s exists; pass --resume to continue it, or pick a fresh "
                     "path" % args.out)
        done, lines = resume_index(args.out)
        print("resuming %s: %d games present, next index %d"
              % (args.out, lines, done))
    remaining = args.games - done
    if remaining <= 0:
        print("%s already has %d games" % (args.out, done))
        return 0

    if args.net and not os.path.exists(args.net):
        ap.error("no such net: %s" % args.net)

    global BOOK
    if args.book:
        import book as book_mod
        # A book screens openings for its own mode -- balance means something
        # different in each -- so using the wrong one silently measures the
        # wrong thing rather than failing.
        book_mode = book_mod.mode_of(args.book)
        if book_mode != args.mode:
            print("%s is a %s book; this is a %s run"
                  % (args.book, book_mode, args.mode), file=sys.stderr)
            return 1
        BOOK = book_mod.load(args.book)

    nproc = (os.cpu_count() or 1) if args.workers == 0 else max(1, args.workers)
    print("%d games (%d remaining) | %s %s | nodes %d depth %d | eval %s"
          % (args.games, remaining,
             ("book:%s (%d)" % (os.path.basename(args.book), len(BOOK)))
             if BOOK else args.setup, args.mode,
             args.nodes, args.depth,
             args.net if args.net else "hand (throwaway)"))

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
            if args.net:
                _load_net(args.net)
            for job in jobs(done, remaining, args):
                absorb(play_one(job))
        else:
            with multiprocessing.Pool(nproc, initializer=_worker_init,
                                      initargs=(args.net,)) as pool:
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
