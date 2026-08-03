#!/usr/bin/env python3
"""match.py -- headless engine-vs-engine matches with a full seat rotation.

    python3 match.py 250 --log run.jsonl --nodes 20000
    python3 match.py 250 --log run.jsonl --engine-b random --workers 0

The positional argument is the number of **opening positions**, not games.
Total games = positions x 4, because a paired game here is a full seat
rotation, not two games.

THE ROTATION
    Seat identity is worth more Elo than most engine changes, so a result that
    has not cancelled it is not a result. Each opening is played four times:

        rotation 0  board as generated   A gets the ORIGINAL R+Y armies
        rotation 1  board turned 90 deg   A gets the ORIGINAL R+Y armies
        rotation 2  board turned 180 deg  A gets the ORIGINAL B+G armies
        rotation 3  board turned 270 deg  A gets the ORIGINAL B+G armies

    What matters is which ORIGINAL armies an engine receives, not which seat
    index it is handed. Rotating the board shifts every seat's colour, so
    rotating the team assignment along with it cancels nothing -- see play_game.
    A also moves first in exactly two of the four.

    Score sum per opening runs 0..4 in half-point steps, which is the
    nine-bucket distribution reported below -- NOT a pentanomial, which would
    assume two games per opening.

THREE IMPLEMENTATION NOTES, each of which cost real money to learn elsewhere
    * Workers are fed from a generator through Pool.imap_unordered, whose task
      handler is a thread. A plain multiprocessing.Queue silently deadlocks on
      macOS past 32767 items, and a run bigger than that hangs with no error.
    * Each engine command gets its own subprocess. Two engine versions must
      never share a process: dlopen returns the stale same-name image and you
      get a silent fallback to the wrong engine.
    * The progress bar is NOT gated on isatty(). It is written to stderr
      unconditionally, so piping through `tee` keeps it. Use --quiet to silence.

Ctrl-C prints the running summary and the distribution. The per-game log is
written as it goes, so a hard kill is still recoverable from --log.
"""

import argparse
import json
import math
import multiprocessing
import os
import random
import signal
import subprocess
import sys
import time

from tetrarch.board import (
    Board, start_board, rotate, SETUPS, DEFAULT_SETUP, MODE_TEAMS,
    move_str, SEAT_NAMES,
)
from tetrarch import movegen as gen
from tetrarch import pgn4

ROTATIONS = 4
#: Adjudicate a draw rather than play forever. 50-move already covers most of
#: it; this is the backstop.
MAX_PLIES = 400
RANDOM_ENGINE = "random"


# --- engine driver ----------------------------------------------------------

class UciEngine:
    """One engine subprocess speaking the protocol in docs/PROTOCOL.md."""

    def __init__(self, command, setup, mode, hash_mb=16, net=None, opts=()):
        self.command = command
        self.proc = subprocess.Popen(
            command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._send("uci")
        self._wait_for("uciok")
        self._send("setoption name Setup value %s" % setup)
        self._send("setoption name Mode value %s" % ("teams" if mode else "ffa"))
        self._send("setoption name Hash value %d" % hash_mb)
        # No net means the throwaway hand eval. One binary, two evals, so the
        # NNUE-vs-hand A/B differs by exactly this line.
        self._send("setoption name Net value %s" % (net if net else "none"))
        # Arbitrary per-side options, so a new search toggle is A/B-able
        # without editing this file again.
        for name, value in opts:
            self._send("setoption name %s value %s" % (name, value))
        self._send("isready")
        self._wait_for("readyok")

    def _send(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _wait_for(self, token):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("engine %r died" % self.command)
            if line.strip().startswith(token):
                return line.strip()

    def newgame(self):
        self._send("ucinewgame")
        self._send("isready")
        self._wait_for("readyok")

    def bestmove(self, start_fen4, moves, go):
        position = "position fen4 %s" % start_fen4
        if moves:
            position += " moves " + " ".join(moves)
        self._send(position)
        self._send(go)
        line = self._wait_for("bestmove")
        parts = line.split()
        return parts[1] if len(parts) > 1 else None

    def close(self):
        try:
            self._send("quit")
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            self.proc.kill()


_ENGINE_CACHE = {}


def get_engine(side, command, setup, mode, hash_mb=16, net=None, opts=()):
    """One subprocess per side per worker, reused across games.

    Keyed on `side` as well as configuration. Without it a null self-test --
    identical command, net and options on both sides -- collapses into a single
    process, so all four seats share one transposition table and the run
    validates a setup no real A/B ever uses. Every A/B that differs in a net or
    an option got two processes and was measuring something else.
    """
    if command == RANDOM_ENGINE:
        return RANDOM_ENGINE
    key = (side, command, setup, mode, hash_mb, net, opts)
    if key not in _ENGINE_CACHE:
        _ENGINE_CACHE[key] = UciEngine(command, setup, mode, hash_mb, net, opts)
    engine = _ENGINE_CACHE[key]
    engine.newgame()
    return engine


# --- openings ---------------------------------------------------------------

#: Set once in main. A module global so the job generator can read it without
#: shipping a copy of the book to every worker.
BOOK = None


def per_setup(games, book):
    """Split a book run into one result per setup.

    A book spanning the five setups makes the headline Elo an average over
    them, which hides the thing worth knowing: a net trained on one setup and
    a net trained on all five do not differ evenly. The mapping back is exact
    -- match.py records each game's opening seed and the book records each
    position's setup -- so this needs no extra games, only the log.
    """
    # Grouped by OPENING, not by game, and then summed over the rotation --
    # exactly what summarise() does for the headline. The four seat-rotation
    # games of one opening share a start position and cancel seat identity, so
    # they are strongly correlated; treating them as four independent samples
    # understates the interval by up to a factor of two. Incomplete rotations
    # are dropped for the same reason the headline drops them: a partial
    # rotation is seat bias, not a result.
    buckets = {}
    for g in games:
        if g.get("score") is None:
            continue
        setup = book[g["opening"] % len(book)][0]
        buckets.setdefault(setup, {}).setdefault(g["opening"], []).append(
            g["score"])

    out = ["", "  setup     openings    rate       Elo"]
    for setup in SETUPS:
        by_opening = buckets.get(setup)
        if not by_opening:
            continue
        sums = [sum(v) for v in by_opening.values() if len(v) == ROTATIONS]
        if not sums:
            continue
        rate = sum(sums) / (ROTATIONS * len(sums))
        mean = sum(sums) / len(sums)
        var = sum((s - mean) ** 2 for s in sums) / max(len(sums) - 1, 1)
        err = 1.96 * math.sqrt(var / len(sums)) / ROTATIONS
        out.append("  %-9s %6d   %.4f   %+7.2f +/- %.2f"
                   % (setup, len(sums), rate, elo(rate),
                      (elo(min(rate + err, 0.9999))
                       - elo(max(rate - err, 0.0001))) / 2))
    return "\n".join(out)


def make_opening(setup, mode, plies, seed):
    """A start position reached by `plies` random legal moves. Deterministic."""
    rng = random.Random(seed)
    b = start_board(setup, mode)
    for _ in range(plies):
        legal = gen.gen_legal(b)
        if not legal:
            return None
        b.make(rng.choice(legal))
    if not gen.gen_legal(b):
        return None
    return b


# --- one game ---------------------------------------------------------------

def play_game(job):
    """Play one game. Returns a dict; never raises past a forfeit."""
    (index, opening_seed, rotation, setup, mode, plies, cmd_a, cmd_b,
     go_string, seed, want_pgn4, net_a, net_b, hash_mb, opts_a, opts_b) = job

    if BOOK is not None:
        _setup, fen4 = BOOK[opening_seed % len(BOOK)]
        base = Board.from_fen4(fen4, mode)
    else:
        base = make_opening(setup, mode, plies, opening_seed)
    if base is None:
        return None
    board = rotate(base, rotation)
    start_fen4 = board.to_fen4().replace("\n", "")

    # Which ORIGINAL armies engine A gets -- not which seats.
    #
    # rotate(b, k) shifts every seat's colour by +k, so rotated-frame team t
    # holds the armies that were originally team `t ^ (k & 1)`. Setting
    # a_team = k & 1 therefore hands A the original team 0 in ALL FOUR
    # rotations and cancels nothing: it turns the board and the team
    # assignment together. That measured as +36 Elo in a null self-test.
    #
    # Pick the original team first, then work back to the rotated seat index.
    original_team = (rotation >> 1) & 1
    a_team = original_team ^ (rotation & 1)
    engines = {}
    try:
        engines[a_team] = get_engine("A", cmd_a, setup, mode, hash_mb,
                                     net_a, opts_a)
        engines[1 - a_team] = get_engine("B", cmd_b, setup, mode, hash_mb,
                                         net_b, opts_b)
    except Exception as exc:                                   # noqa: BLE001
        return {"index": index, "error": "engine start: %r" % (exc,)}

    rng = random.Random(seed)
    moves = []
    played = []
    result = None
    reason = ""

    for ply in range(MAX_PLIES):
        legal = gen.gen_legal(board)
        if not legal:
            if gen.in_check(board, board.turn):
                # Checkmating one opponent wins for the whole team (§7).
                loser_team = board.turn & 1
                result = 0.0 if loser_team == a_team else 1.0
                reason = "checkmate %s" % SEAT_NAMES[board.turn]
            else:
                result = 0.5
                reason = "stalemate %s" % SEAT_NAMES[board.turn]
            break
        if board.halfmove >= 200:
            result, reason = 0.5, "fifty-move"
            break

        engine = engines[board.turn & 1]
        if engine is RANDOM_ENGINE:
            chosen = rng.choice(legal)
        else:
            try:
                token = engine.bestmove(start_fen4, moves, go_string)
            except Exception as exc:                           # noqa: BLE001
                result = 0.0 if (board.turn & 1) == a_team else 1.0
                reason = "engine failure: %r" % (exc,)
                break
            match = [m for m in legal if move_str(m) == token]
            if not match:
                result = 0.0 if (board.turn & 1) == a_team else 1.0
                reason = "illegal move %s by %s" % (token, SEAT_NAMES[board.turn])
                break
            chosen = match[0]
        moves.append(move_str(chosen))
        played.append(chosen)
        board.make(chosen)
    else:
        result, reason = 0.5, "adjudicated at %d plies" % MAX_PLIES

    record = {"index": index, "opening": opening_seed, "rotation": rotation,
              "score": result, "reason": reason, "plies": len(moves),
              "a_team": a_team, "moves": " ".join(moves)}
    if want_pgn4:
        record["pgn4"] = pgn4.write(rotate(base, rotation), played, {
            "Event": "Tetrarch match",
            "Round": "%d.%d" % (opening_seed, rotation),
            "Red": cmd_a if a_team == 0 else cmd_b,
            "Blue": cmd_b if a_team == 0 else cmd_a,
            "Yellow": cmd_a if a_team == 0 else cmd_b,
            "Green": cmd_b if a_team == 0 else cmd_a,
            "Result": {1.0: "1-0", 0.0: "0-1", 0.5: "1/2-1/2"}.get(result, "*"),
            "Termination": reason,
        })
    return record


# --- statistics -------------------------------------------------------------

def elo(score_rate):
    if score_rate <= 0.0:
        return -800.0
    if score_rate >= 1.0:
        return 800.0
    return -400.0 * math.log10(1.0 / score_rate - 1.0)


def summarise(by_opening, games, elapsed):
    """Report as the doctrine requires: Elo with its margin, games, and the
    nine-bucket distribution. Never a bare Elo, never a pentanomial."""
    complete = [v for v in by_opening.values() if len(v) == ROTATIONS]
    partial = len(by_opening) - len(complete)
    total = sum(g["score"] for g in games if g.get("score") is not None)
    played = sum(1 for g in games if g.get("score") is not None)

    lines = []
    lines.append("games %d  score %.2f/%d  rate %.4f"
                 % (played, total, played, total / played if played else 0.0))

    if complete:
        sums = [sum(v) for v in complete]
        rate = sum(sums) / (ROTATIONS * len(sums))
        mean = sum(sums) / len(sums)
        var = sum((s - mean) ** 2 for s in sums) / max(1, len(sums) - 1)
        se_rate = math.sqrt(var / len(sums)) / ROTATIONS
        e = elo(rate)
        if 0.0 < rate < 1.0:
            margin = 1.96 * se_rate * 400.0 / (math.log(10) * rate * (1 - rate))
        else:
            margin = float("inf")
        lines.append("Elo %+.2f +/- %.2f   (%d complete rotations)"
                     % (e, margin, len(complete)))
        buckets = [0] * 9
        for s in sums:
            buckets[int(round(s * 2))] += 1
        lines.append("Dist: %s" % ", ".join(str(b) for b in buckets))
        lines.append("      (score sums 0, 0.5, 1 ... 4 over the 4-game rotation)")
    if partial:
        lines.append("%d openings have an INCOMPLETE rotation and are excluded "
                     "from the Elo -- an incomplete rotation is seat bias, not "
                     "a result." % partial)

    # What the games looked like, not just how they scored. A short mean game
    # or a collapsed decisive rate says the instrument moved, and that is worth
    # seeing next to the Elo rather than only in the raw log.
    plies = [g["plies"] for g in games if g.get("plies")]
    if plies:
        reasons = {}
        for g in games:
            # An empty reason must not crash the summary: a leg that loses its
            # one reported line to a formatting slip has to be run again.
            words = (g.get("reason") or "unknown").split()
            key = words[0] if words else "unknown"
            reasons[key] = reasons.get(key, 0) + 1
        decisive = sum(v for k, v in reasons.items()
                       if k in ("checkmate", "resign"))
        lines.append("Games %.2f plies mean, %.2f median, %d..%d"
                     % (sum(plies) / len(plies), sorted(plies)[len(plies) // 2],
                        min(plies), max(plies)))
        lines.append("Ended %s"
                     % ", ".join("%s %d (%.2f%%)"
                                 % (k, v, 100.0 * v / len(games))
                                 for k, v in sorted(reasons.items(),
                                                    key=lambda kv: -kv[1])))
        lines.append("Decisive %.2f%%" % (100.0 * decisive / len(games)))

    if elapsed > 0:
        hrs, rem = divmod(int(elapsed), 3600)
        mins, secs = divmod(rem, 60)
        lines.append("Time %.2fs (%dh%02dm%02ds)  %.2f games/min  %.2f games/s"
                     % (elapsed, hrs, mins, secs,
                        played * 60.0 / elapsed, played / elapsed))
    return "\n".join(lines)


# --- main -------------------------------------------------------------------

def jobs(count, args):
    """A generator, so Pool's thread-backed task handler feeds the workers
    lazily. A plain Queue deadlocks past 32767 items on macOS."""
    index = 0
    for i in range(count):
        for rotation in range(ROTATIONS):
            yield (index, args.seed * 7919 + i, rotation, args.setup,
                   MODE_TEAMS, args.opening_plies, args.engine_a,
                   args.engine_b, args.go_string, args.seed * 104729 + index,
                   bool(args.pgn4), args.net_a, args.net_b, args.hash,
                   args.opts_a, args.opts_b)
            index += 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The progress bar is written to stderr unconditionally and is "
               "not gated on isatty(), so piping through tee keeps it. "
               "Use --quiet to silence it.")
    ap.add_argument("positions", type=int, nargs="?",
                    help="number of OPENING POSITIONS; games = positions x 4")
    ap.add_argument("--log", metavar="PATH",
                    help="per-game JSONL log; required, no default, so a smoke "
                         "test can never overwrite a real run")
    ap.add_argument("--engine-a", default="python3 uci.py")
    ap.add_argument("--engine-b", default="python3 uci.py",
                    help="engine command, or the literal word 'random'")
    ap.add_argument("--nodes", type=int)
    ap.add_argument("--summarise", metavar="LOG",
                    help="re-report a finished --log and exit; the summary is "
                         "printed, not stored, and an overnight run scrolls "
                         "away")
    ap.add_argument("--movetime", type=int, metavar="MS")
    ap.add_argument("--depth", type=int)
    ap.add_argument("--setup", default=DEFAULT_SETUP, choices=SETUPS)
    ap.add_argument("--opening-plies", type=int, default=8)
    ap.add_argument("--book", metavar="PATH",
                    help="opening positions from book.py rather than random "
                         "plies. Both engines get the same position and the "
                         "same rotation, so this only removes openings that "
                         "were decided before either side moved.")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1,
                    help="0 means every core")
    ap.add_argument("--opt-a", action="append", default=[], metavar="NAME=VALUE",
                    help="extra setoption for engine A; repeatable")
    ap.add_argument("--opt-b", action="append", default=[], metavar="NAME=VALUE",
                    help="extra setoption for engine B; repeatable")
    ap.add_argument("--net-a", metavar="PATH",
                    help="net for engine A; omitted means the hand eval")
    ap.add_argument("--net-b", metavar="PATH",
                    help="net for engine B; omitted means the hand eval")
    ap.add_argument("--hash", type=int, default=16, metavar="MB",
                    help="per-engine hash (default 16). Two subprocesses per "
                         "worker, so 111 workers at 16 MB is ~3.5 GB of table")
    ap.add_argument("--pgn4", metavar="PATH",
                    help="also write every game as PGN4, for the viewer")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.summarise:
        # Everything summarise() needs is in the log: it groups by opening and
        # sums the rotation, and both fields are written per game.
        games, by_opening = [], {}
        with open(args.summarise) as fh:
            for line in fh:
                try:
                    g = json.loads(line)
                except ValueError:
                    continue        # a run killed mid-write leaves one short
                games.append(g)
                if g.get("score") is not None:
                    by_opening.setdefault(g["opening"], []).append(g["score"])
        print(summarise(by_opening, games, 0.0))
        if args.book:
            import book as book_mod
            print(per_setup(games, book_mod.load(args.book)))
        return 0
    # Required for a real run, checked here so --summarise needs neither.
    if args.positions is None or not args.log:
        ap.error("positions and --log are required")

    instruments = [x for x in (args.nodes, args.movetime, args.depth)
                   if x is not None]
    if len(instruments) != 1:
        ap.error("choose exactly one instrument: --nodes, --movetime or "
                 "--depth. Mixing them inside one campaign invalidates it.")
    if args.nodes:
        args.go_string = "go nodes %d" % args.nodes
    elif args.movetime:
        args.go_string = "go movetime %d" % args.movetime
    else:
        args.go_string = "go depth %d" % args.depth

    for path in (args.log, args.pgn4):
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    def parse_opts(raw):
        out = []
        for item in raw:
            if "=" not in item:
                ap.error("--opt expects NAME=VALUE, got %r" % item)
            name, value = item.split("=", 1)
            out.append((name.strip(), value.strip()))
        return tuple(out)

    args.opts_a = parse_opts(args.opt_a)
    args.opts_b = parse_opts(args.opt_b)

    # Never overwrite a previous run, and never refuse to start over a name
    # collision either -- just take the next free one and say so.
    args.log = _free_path(args.log)
    if args.pgn4:
        args.pgn4 = _free_path(args.pgn4)

    nproc = (os.cpu_count() or 1) if args.workers == 0 else max(1, args.workers)
    total = args.positions * ROTATIONS
    global BOOK
    if args.book:
        import book as book_mod
        BOOK = book_mod.load(args.book)

    print("setup %s teams | %s | %d openings x %d = %d games | %d workers"
          % (("book:%s (%d)" % (os.path.basename(args.book), len(BOOK)))
             if BOOK else args.setup,
             args.go_string, args.positions, ROTATIONS, total, nproc))
    def describe(cmd, net, opts):
        bits = ["net=%s" % (net or "none (hand eval)")]
        bits += ["%s=%s" % kv for kv in opts]
        return "%s  %s" % (cmd, "  ".join(bits))

    print("  A: %s" % describe(args.engine_a, args.net_a, args.opts_a))
    print("  B: %s" % describe(args.engine_b, args.net_b, args.opts_b))

    games = []
    by_opening = {}
    started = time.time()
    log = open(args.log, "w")
    pgn_out = open(args.pgn4, "w") if args.pgn4 else None

    def absorb(game):
        if game is None:
            return
        games.append(game)
        if pgn_out and game.get("pgn4"):
            pgn_out.write(game.pop("pgn4") + "\n")
            pgn_out.flush()
        log.write(json.dumps(game) + "\n")
        log.flush()
        if game.get("score") is not None:
            by_opening.setdefault(game["opening"], []).append(game["score"])

    def progress():
        if args.quiet:
            return
        done = len(games)
        secs = time.time() - started
        rate = done / max(secs, 1e-9)
        eta = (total - done) / max(rate, 1e-9)
        width = 28
        filled = int(width * done / max(total, 1))
        sys.stderr.write("\r[%s%s] %d/%d  %.1f g/s  eta %dm%02ds "
                         % ("#" * filled, "." * (width - filled), done, total,
                            rate, eta // 60, eta % 60))
        sys.stderr.flush()

    try:
        if nproc == 1:
            for job in jobs(args.positions, args):
                absorb(play_game(job))
                progress()
        else:
            with multiprocessing.Pool(nproc, initializer=_ignore_sigint) as pool:
                for game in pool.imap_unordered(play_game,
                                                jobs(args.positions, args),
                                                chunksize=1):
                    absorb(game)
                    progress()
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted -- summary of what completed:\n")
    finally:
        if not args.quiet:
            sys.stderr.write("\n")
        log.close()
        if pgn_out:
            pgn_out.close()
        for engine in _ENGINE_CACHE.values():
            if engine is not RANDOM_ENGINE:
                engine.close()

    print(summarise(by_opening, games, time.time() - started))
    errors = [g for g in games if g.get("error") or "illegal" in g.get("reason", "")
              or "failure" in g.get("reason", "")]
    if errors:
        print("%d games ended on an engine error or illegal move:" % len(errors))
        for g in errors[:5]:
            print("  %s" % (g.get("error") or g.get("reason")))
    print("log: %s" % args.log)
    if args.pgn4:
        print("pgn4: %s" % args.pgn4)
    return 0


def _free_path(path):
    """`run.jsonl` -> `run.jsonl` if free, else `run-2.jsonl`, `run-3.jsonl`...

    A campaign log is the only record of a run, so overwriting one silently is
    unacceptable; so is refusing to start because a name was reused.
    """
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists("%s-%d%s" % (stem, n, ext)):
        n += 1
    chosen = "%s-%d%s" % (stem, n, ext)
    print("%s exists; writing %s" % (path, chosen))
    return chosen


def _ignore_sigint():
    """Workers ignore Ctrl-C so the parent can print its summary instead of
    every child dumping a traceback."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


if __name__ == "__main__":
    sys.exit(main())
