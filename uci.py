#!/usr/bin/env python3
"""uci.py -- the engine protocol.

    python3 uci.py
    printf "bench\\nquit\\n" | python3 uci.py

UCI is a two-player protocol and does not fit a four-seat game. The extensions
and divergences are minimal and every one of them is documented in
docs/PROTOCOL.md -- read that, not this file, for the contract.

Where Athena already made a choice, this matches it: a `Setup` option, and
`position <setup>` / `position fen <FEN4>`.
"""

import os
import sys
import time

from tetrarch.board import (
    Board, start_board, SETUPS, DEFAULT_SETUP, MODE_FFA, MODE_TEAMS,
    MODE_NAMES, SEAT_NAMES, move_str, name_of, mv_from, mv_to, mv_promo,
    file_of, rank_of, VALID, PC_COLOR, PC_TYPE, TYPE_CHARS, DEAD_UNKNOWN,
    sq_of,
)
from tetrarch import core
from tetrarch import movegen as gen
from tetrarch import game
from tetrarch.search import Limits, search, search_multi, MATE_SCORE

NAME = "Tetrarch"
#: The evaluation the engine plays with unless told otherwise. Net v5 beats
#: net v4 by +7.78 +/- 4.61 at fixed time and +10.50 +/- 4.59 at fixed nodes,
#: both over 20,000 games (docs/AB.md). Derived at runtime, never a hardcoded
#: path.
DEFAULT_NET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "nets", "net-v5.nnue")
#: A bundle names one net per setup. The nets on hand are trained on different
#: setups and the difference is large where it exists -- net-v5 is 23.68 Elo
#: better on classic and 22.15 worse on modern (docs/AB.md) -- so which net
#: should play is a function of the setup, until one net wins everywhere.
DEFAULT_BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "nets", "bundle.txt")


def load_bundle(path):
    """Read `setup netfile` lines into {setup: absolute path}.

    A named net that is not on disk is dropped with a warning rather than
    failing the load: a clone missing one net should still play the setups it
    does have, and the alternative is an engine that will not start.
    """
    out = {}
    here = os.path.dirname(os.path.abspath(path))
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            setup, name = line.split(None, 1)
            full = os.path.join(here, name.strip())
            if setup not in SETUPS:
                print("info string bundle names unknown setup %r" % setup)
            elif not os.path.exists(full):
                print("info string bundle names a missing net for %s: %s"
                      % (setup, name.strip()))
            else:
                out[setup] = full
    return out
#: Integer release number. Incremented once per CONFIRMED Elo gain, never
#: for a dormant toggle or a tooling change. See docs/RELEASING.md.
VERSION = "7"
AUTHOR = "IchNukeDichWeg"


class Engine:
    def __init__(self):
        self.setup = DEFAULT_SETUP
        self.mode = MODE_TEAMS
        self.hash_mb = core.DEFAULT_TT_MB
        self.net = None
        self.lmr_min_depth = 3
        self.lmr_min_move = 3
        self.lmp_max_depth = 3
        self.lmp_base = 4
        self.multipv = 1
        self.board = start_board(self.setup, self.mode)
        self.history = []
        self.bundle = {}
        self._load_default_net()

    def _load_default_net(self):
        """Play with the net unless the caller says otherwise.

        A missing file is not fatal: the hand eval still plays, and a clone
        without the net should start rather than refuse to.
        """
        if os.path.exists(DEFAULT_BUNDLE):
            try:
                self.bundle = load_bundle(DEFAULT_BUNDLE)
            except Exception as exc:                            # noqa: BLE001
                print("info string could not read the net bundle: %s" % exc)
        self._use_net_for_setup()

    def _use_net_for_setup(self):
        """Load whichever net this setup calls for, if it is not already in.

        Reloading clears the transposition table, so this only acts when the
        file actually changes -- a `setoption name Setup` that does not change
        which net plays must not throw the table away.
        """
        want = self.bundle.get(self.setup) if self.bundle else None
        if want is None:
            want = DEFAULT_NET if os.path.exists(DEFAULT_NET) else None
        if want is None or want == self.net:
            return
        try:
            from tetrarch import nnue
            core.load_net(nnue.Net.load(want))
            self.net = want
        except Exception as exc:                                # noqa: BLE001
            print("info string could not load %s: %s" % (want, exc))

    # -- protocol ---------------------------------------------------------

    def cmd_uci(self, _args):
        print("id name %s %s" % (NAME, VERSION))
        print("id author %s" % AUTHOR)
        print("option name Setup type combo default %s var %s"
              % (DEFAULT_SETUP, " var ".join(SETUPS)))
        print("option name Mode type combo default teams var teams var ffa")
        print("option name Hash type spin default %d min 1 max 4096"
              % core.DEFAULT_TT_MB)
        # Two evals in one binary, so the NNUE-vs-hand A/B is a single
        # setoption apart. "none" selects the hand eval.
        print("option name Net type string default %s"
              % (os.path.relpath(DEFAULT_NET, os.path.dirname(
                  os.path.abspath(__file__)))
                 if os.path.exists(DEFAULT_NET) else "<none>"))
        print("option name Killers type check default true")
        # Dormant: correct, but structurally dead at the depths
        # this engine reaches. See docs/AB.md.
        print("option name History type check default false")
        print("option name PVS type check default false")
        print("option name LMR type check default true")
        print("option name LMRMinDepth type spin default 3 min 1 max 16")
        print("option name LMRMinMove type spin default 3 min 1 max 32")
        print("option name LazyEval type check default true")
        print("option name LMP type check default true")
        print("option name LMPMaxDepth type spin default 3 min 0 max 8")
        print("option name LMPBase type spin default 4 min 1 max 32")
        print("option name QSEvasions type check default true")
        # A draw the search is walking into has to score as one. Off is for
        # measuring the difference, not for playing.
        print("option name Repetitions type check default true")
        # Both dormant until an A/B says otherwise. SEE prices a capture after
        # the exchange settles; ordering by it and pruning on it are separate
        # claims and separate toggles.
        print("option name SEEOrder type check default false")
        print("option name SEEPrune type check default true")
        # Hand eval only; a loaded net ignores it. Default off until it has
        # won an A/B, and it must pay for widening the lazy-eval margin
        # (docs/AB.md) as well as for its own cost.
        print("option name Mobility type check default false")
        # Analysis only. Above 1 the root loses its cutoffs so every line has
        # an exact score instead of a bound, which costs several times the
        # nodes -- see search_multi. 1 is the path every A/B was measured on.
        print("option name MultiPV type spin default 1 min 1 max 64")
        print("uciok")

    def cmd_isready(self, _args):
        core.load()
        print("readyok")

    def cmd_setoption(self, args):
        if len(args) < 4 or args[0] != "name":
            return
        try:
            vi = args.index("value")
        except ValueError:
            return
        name = " ".join(args[1:vi]).lower()
        value = " ".join(args[vi + 1:])
        if name == "setup":
            if value.lower() in SETUPS:
                self.setup = value.lower()
                self.board = start_board(self.setup, self.mode)
                self.history = []
                self._use_net_for_setup()
        elif name == "mode":
            if value.lower() in MODE_NAMES:
                self.mode = MODE_NAMES.index(value.lower())
                self.board = start_board(self.setup, self.mode)
                self.history = []
        elif name == "hash":
            # Clamped to the range `uci` declares. Without the upper bound a
            # value past what can be allocated reached core.set_hash and raised,
            # which the dispatch guard now catches but which should not happen:
            # a declared max that is never enforced is not a max.
            self.hash_mb = max(1, min(int(value), 4096))
            core.set_hash(self.hash_mb)
        elif name == "killers":
            core.set_killers(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "history":
            core.set_history(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "pvs":
            core.set_pvs(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "lmr":
            core.set_lmr(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "lazyeval":
            core.set_lazy_eval(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "lmp":
            core.set_lmp(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "multipv":
            self.multipv = max(1, min(int(value), 64))
        elif name == "mobility":
            core.set_mobility(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "seeorder":
            core.set_see_order(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "seeprune":
            core.set_see_prune(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "repetitions":
            core.set_rep_detect(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "qsevasions":
            core.set_qs_evasions(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name in ("lmpmaxdepth", "lmpbase"):
            self.lmp_max_depth = (int(value) if name == "lmpmaxdepth"
                                  else self.lmp_max_depth)
            self.lmp_base = (int(value) if name == "lmpbase" else self.lmp_base)
            core.set_lmp_params(self.lmp_max_depth, self.lmp_base)
        elif name in ("lmrmindepth", "lmrminmove"):
            self.lmr_min_depth = (int(value) if name == "lmrmindepth"
                                  else self.lmr_min_depth)
            self.lmr_min_move = (int(value) if name == "lmrminmove"
                                 else self.lmr_min_move)
            core.set_lmr_params(self.lmr_min_depth, self.lmr_min_move)
        elif name == "net":
            if value.strip().lower() in ("", "<none>", "none"):
                core.unload_net()
                self.net = None
                self.bundle = {}
                print("info string net unloaded; using the throwaway hand eval")
                return
            # A .txt value is a bundle -- one net per setup -- rather than a
            # single net. Which one plays then follows the setup, and switching
            # setups switches nets.
            if value.strip().endswith(".txt"):
                try:
                    self.bundle = load_bundle(value.strip())
                    self.net = None
                    self._use_net_for_setup()
                    print("info string net bundle loaded: %d setups"
                          % len(self.bundle))
                except Exception as exc:                    # noqa: BLE001
                    print("info string could not read bundle %s: %s"
                          % (value, exc))
                return
            try:
                self.bundle = {}
                from tetrarch import nnue
                core.load_net(nnue.Net.load(value.strip()))
                self.net = value.strip()
                print("info string net loaded: %s" % self.net)
            except Exception as exc:                        # noqa: BLE001
                print("info string could not load net %s: %s" % (value, exc))

    def cmd_ucinewgame(self, _args):
        core.clear_hash()
        self.board = start_board(self.setup, self.mode)
        self.history = []

    def cmd_position(self, args):
        if not args:
            return
        moves = []
        if "moves" in args:
            i = args.index("moves")
            moves = args[i + 1:]
            args = args[:i]

        if not args or args[0] == "startpos":
            setup = args[1] if len(args) > 1 else self.setup
            self.board = start_board(setup, self.mode)
        elif args[0] in ("fen4", "fen"):
            self.board = Board.from_fen4(" ".join(args[1:]), self.mode)
        elif args[0] in SETUPS:
            self.setup = args[0]
            self.board = start_board(self.setup, self.mode)
            self._use_net_for_setup()
        else:
            print("info string unrecognised position %r" % args[0])
            return

        # A position can arrive already holding a seat with no moves -- FFA
        # eliminations are part of the position, not an event the caller
        # replays -- so it is resolved on load as well as after every move.
        game.resolve(self.board)

        # Every position the game passed through, so the search can tell a
        # repetition from a transposition (§10.2). The board's own key is not
        # included: the root supplies that itself.
        self.history = []
        for token in moves:
            self.history.append(self.board.key)
            if not self.play(token):
                self.history.pop()
                print("info string illegal move %s" % token)
                break

    def play(self, token):
        """Apply a move given as from-to plus an optional promotion letter.

        The move list does not encode eliminations, so the receiver has to
        apply the same rule the sender did: in FFA a seat with no legal moves
        leaves the game and the turn passes over it (§7). Without this the
        engine's idea of whose turn it is diverges from the caller's at the
        first mate, and every later search answers for the wrong seat.
        """
        for m in gen.gen_legal(self.board):
            if move_str(m) == token:
                self.board.make(m)
                game.resolve(self.board)
                return True
        return False

    def cmd_go(self, args):
        limits = Limits()
        seat = self.board.turn
        i = 0
        while i < len(args):
            token, value = args[i], None
            if i + 1 < len(args):
                try:
                    value = int(args[i + 1])
                except ValueError:
                    value = None
            if token == "depth" and value is not None:
                limits.depth = value
            elif token == "nodes" and value is not None:
                limits.nodes = value
            elif token == "movetime" and value is not None:
                limits.movetime = value
            elif token == "infinite":
                limits.depth = limits.max_depth
            elif len(token) > 3 and token[0].upper() in SEAT_NAMES:
                # rtime/btime/ytime/gtime and rinc/binc/yinc/ginc: one clock
                # per seat, because there is no white and black (PROTOCOL.md).
                # The bound is 3, not 4: "rinc" is exactly four characters, so
                # a > 4 test let only the *time tokens through and the "inc"
                # arm below was dead -- budget_ms lost its whole increment term
                # while PROTOCOL.md documented the tokens as accepted.
                which = SEAT_NAMES.index(token[0].upper())
                if which == seat and value is not None:
                    if token[1:] == "time":
                        limits.clock = value
                    elif token[1:] == "inc":
                        limits.inc = value
            i += 2 if value is not None else 1

        def line(r, rank=None):
            # The whole variation, not just the move: walked out of the
            # transposition table, and short rather than wrong if an entry has
            # been overwritten (core.pv).
            moves = r.pv or core.pv(self.board, r.best) \
                or ([r.best] if r.best else [])
            print("info depth %d%s score %s nodes %d nps %d time %d pv %s"
                  % (r.depth, "" if rank is None else " multipv %d" % rank,
                     score_string(r.score), r.nodes, r.nps,
                     int(r.elapsed * 1000),
                     " ".join(move_str(m) for m in moves)))

        if self.multipv > 1:
            def report(rs):
                for rank, r in enumerate(rs, 1):
                    line(r, rank)
                sys.stdout.flush()

            results = search_multi(self.board, limits, self.multipv,
                                   report, self.history)
            best = results[0].best if results else None
        else:
            def report(r):
                line(r)
                sys.stdout.flush()

            best = search(self.board, limits, report,
                          self.history).best
        print("bestmove %s" % (move_str(best) if best else "0000"))

    def cmd_stop(self, _args):
        # Single-threaded: `go` has already returned by the time this is read.
        # Documented as a divergence in PROTOCOL.md.
        pass

    def cmd_bench(self, _args):
        import bench
        # search_round, not one_round: the latter is perft, which cannot see
        # the evaluation at all and returns the same number whether or not a
        # net is loaded. Printing it in the search bench's exact shape made the
        # two look comparable when they measure different things.
        nodes, secs, _ = bench.search_round(7)
        print("net %s" % ("loaded" if core.net_loaded() else "none (hand eval)"))
        print("%d nodes %d nps" % (nodes, int(nodes / secs)))

    def cmd_perft(self, args):
        depth = int(args[0]) if args else 4
        started = time.perf_counter()
        n = core.perft(self.board, depth)
        secs = time.perf_counter() - started
        print("perft(%d) = %d  %.3fs  %d nps" % (depth, n, secs, int(n / max(secs, 1e-9))))

    def cmd_print(self, _args):
        b = self.board
        for rank in range(13, -1, -1):
            row = []
            for file in range(14):
                sq = sq_of(file, rank)
                if not VALID[sq]:
                    row.append("   ")
                    continue
                p = b.sq[sq]
                if not p:
                    row.append(" . ")
                else:
                    c = PC_COLOR[p]
                    tag = "d" if c == DEAD_UNKNOWN else SEAT_NAMES[c].lower()
                    row.append("%s%s " % (tag, TYPE_CHARS[PC_TYPE[p]]))
            print("%2d %s" % (rank + 1, "".join(row)))
        print("   " + "".join(" %s " % f for f in "abcdefghijklmn"))
        print("turn: %s  mode: %s  alive: %s  points: %s  halfmove: %d"
              % (SEAT_NAMES[b.turn], MODE_NAMES[b.mode],
                 "".join(SEAT_NAMES[i] if a else "-" for i, a in enumerate(b.alive)),
                 ",".join(str(p) for p in b.points), b.halfmove))
        print("key: %016x" % b.key)
        print("fen4: %s" % b.to_fen4().replace("\n", ""))


def score_string(score):
    """`mate N` in team moves, or `cp N`.

    N counts the mating team's own moves, not plies: the seat rotation gives
    each team two of every four plies, so a ply count would read as double.
    """
    if abs(score) >= MATE_SCORE - 100:
        plies = MATE_SCORE - abs(score)
        moves = (plies + 1) // 2
        return "mate %d" % (moves if score > 0 else -moves)
    return "cp %d" % score


def main():
    engine = Engine()
    table = {
        "uci": engine.cmd_uci,
        "isready": engine.cmd_isready,
        "setoption": engine.cmd_setoption,
        "ucinewgame": engine.cmd_ucinewgame,
        "position": engine.cmd_position,
        "go": engine.cmd_go,
        "stop": engine.cmd_stop,
        "bench": engine.cmd_bench,
        "perft": engine.cmd_perft,
        "print": engine.cmd_print,
        "d": engine.cmd_print,
    }
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        if cmd == "quit":
            return 0
        handler = table.get(cmd)
        if handler is None:
            print("info string unknown command %r" % cmd)
            continue
        # A malformed line must not end the game. `setoption name Hash value
        # abc` raised ValueError straight out of the dispatch and the process
        # exited; a bad FEN4, an unknown setup name and `perft x` all shared
        # that fate. A GUI sending one stray token would lose the engine
        # mid-game, and the operator would see a dead process rather than a
        # complaint about the line.
        try:
            handler(args)
        except Exception as exc:                                # noqa: BLE001
            print("info string error handling %r: %s: %s"
                  % (cmd, type(exc).__name__, exc))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
