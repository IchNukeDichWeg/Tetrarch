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

import collections
import os
import sys
import threading
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

#: One lock for every line written while a search runs. The input thread
#: answers `isready` during a search, and print() writes the text and its
#: newline separately, so without this a `readyok` can land inside an `info`
#: line and the host reads neither.
_OUT_LOCK = threading.Lock()


def emit(text):
    """Write one whole protocol line and flush it, safe from any thread."""
    with _OUT_LOCK:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


class _Stopped(Exception):
    """Raised out of the per-depth callback when the host has sent `stop`."""


class Inbox:
    """stdin, read on its own thread so a search can still hear the host.

    `go` used to run on the thread that reads stdin, so nothing sent during a
    search was read until it returned. `stop` was documented as ignored on the
    grounds that `go` "has already returned" -- true of a bounded search and
    false of `go infinite`, a bare `go`, and a `go wtime ...` from a standard
    UCI host (wtime names no seat here, so no clock binds): each searched to
    depth 48, which is forever, and the only way out was to kill the process.
    `isready` sent mid-search, which UCI says must be answered at once, waited
    just as long, and hosts that time it out declare the engine dead.

    So the reader thread queues lines for the main loop, except while a search
    runs: then it answers `isready` itself and turns `stop` (and `quit`, and
    end of input) into an event the search polls. The C core has no interrupt
    hook, so the poll is between iterative-deepening depths -- `stop` takes
    effect when the depth in progress completes, not mid-node. Everything else
    sent during a search waits in order for the main loop, as before.
    """

    def __init__(self, stream):
        self._lines = collections.deque()
        self._cond = threading.Condition()
        self._eof = False
        self._searching = False
        self.stop = threading.Event()
        reader = threading.Thread(target=self._read, args=(stream,),
                                  name="uci-input", daemon=True)
        reader.start()

    def _read(self, stream):
        for raw in stream:
            line = raw.strip()
            if not line:
                continue
            with self._cond:
                if not (self._searching and self._consume(line)):
                    self._lines.append(line)
                self._cond.notify_all()
        with self._cond:
            self._eof = True
            # The host is gone. A search nobody can stop must not outlive it.
            self.stop.set()
            self._cond.notify_all()

    def _consume(self, line):
        """Handle a line that arrived mid-search. True if it is used up here.

        Called with the lock held. `quit` sets the stop AND stays queued, so
        the main loop prints the bestmove and then exits.
        """
        word = line.split()[0]
        if word == "isready":
            emit("readyok")
            return True
        if word == "stop":
            self.stop.set()
            return True
        if word == "quit":
            self.stop.set()
        return False

    def get(self):
        """The next queued line, or None at end of input."""
        with self._cond:
            while not self._lines and not self._eof:
                self._cond.wait()
            return self._lines.popleft() if self._lines else None

    def begin_search(self):
        """Mark a search as running, and apply what is already queued for it.

        `go infinite` and `stop` sent back to back can both be read before the
        main loop has even started the search, which leaves the `stop` queued
        as an ordinary command behind a search that never ends. So the queue
        is scanned up to the next `go`: a `stop` there belongs to this search.
        """
        with self._cond:
            self.stop.clear()
            self._searching = True
            if self._eof:
                self.stop.set()
            kept = collections.deque()
            mine = True
            for line in self._lines:
                if line.split()[0] == "go":
                    mine = False
                if not (mine and self._consume(line)):
                    kept.append(line)
            self._lines = kept

    def end_search(self):
        with self._cond:
            self._searching = False

    def wait_for_stop(self):
        """Block until `stop`, `quit` or end of input. UCI: under `go infinite`
        the bestmove is withheld until the host asks for it, even when the
        search has nothing left to do."""
        with self._cond:
            while not self.stop.is_set():
                self._cond.wait()
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
    """Read `mode setup netfile` lines into {(mode, setup): absolute path}.

    Teams and FFA are different games, not two dials on one: the eval sees a
    points race in one and never in the other, so a net trained on one has no
    signal for the other. The dispatch key is therefore the pair, not the
    setup alone.

    A two-field line is read as Teams, so a bundle written before FFA existed
    still loads and still means what it meant.

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
            fields = line.split()
            if len(fields) == 2:
                mode, setup, name = "teams", fields[0], fields[1]
            elif len(fields) == 3:
                mode, setup, name = fields
            else:
                print("info string bundle line not understood: %r" % line)
                continue
            full = os.path.join(here, name)
            if mode not in MODE_NAMES:
                print("info string bundle names unknown mode %r" % mode)
            elif setup not in SETUPS:
                print("info string bundle names unknown setup %r" % setup)
            elif not os.path.exists(full):
                print("info string bundle names a missing net for %s %s: %s"
                      % (mode, setup, name))
            else:
                out[(MODE_NAMES.index(mode), setup)] = full
    return out
#: Integer release number. Incremented once per CONFIRMED Elo gain, never
#: for a dormant toggle or a tooling change. See docs/RELEASING.md.
VERSION = "9"
AUTHOR = "IchNukeDichWeg"


class Engine:
    def __init__(self):
        self.setup = DEFAULT_SETUP
        self.mode = MODE_TEAMS
        self.hash_mb = core.DEFAULT_TT_MB
        self.net = None
        #: Set once `setoption name Net` arrives. The bundle is a DEFAULT, and
        #: a caller that named a net meant it -- match.py's banner reports what
        #: it asked for, and a later Setup or Mode silently swapping it would
        #: make that banner a lie. Every A/B in docs/AB.md rests on it.
        self.net_explicit = False
        self.lmr_min_depth = 3
        self.lmr_min_move = 3
        self.lmp_max_depth = 3
        self.lmp_base = 4
        self.null_min_depth = 3
        self.null_r = 2
        self.rfp_max_depth = 3
        self.rfp_margin = 150
        self.multipv = 1
        self.board = start_board(self.setup, self.mode)
        self.history = []
        #: (position spec, tokens already applied) for the board as it stands.
        #: None means "rebuild from scratch next time".
        self._replayed = None
        self.bundle = {}
        #: The stdin reader, when there is one. None for an Engine driven
        #: directly from Python (selftest.py), where nothing can send `stop`.
        self.inbox = None
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
        """Load whichever net this mode and setup call for, if not already in.

        Reloading clears the transposition table, so this only acts when the
        file actually changes -- a `setoption name Setup` that does not change
        which net plays must not throw the table away.
        """
        if self.net_explicit:
            return
        want = self.bundle.get((self.mode, self.setup)) if self.bundle else None
        if want is None and self.mode == MODE_TEAMS:
            want = DEFAULT_NET if os.path.exists(DEFAULT_NET) else None
        if want == self.net:
            return
        if want is None:
            # No silent fallback to a Teams net in FFA. Every net shipped was
            # trained where the points inputs are always zero and one mate
            # ends the game; net-v5 screened at -124.50 +/- 49.31 against the
            # hand eval in FFA over 32 games. That is a kill filter reading
            # rather than a measurement, but the mechanism is understood and
            # the direction is not in doubt. Said out loud rather than
            # defaulted quietly, so it is obvious the moment a net is bundled.
            core.unload_net()
            self.net = None
            print("info string no net bundled for %s %s -- playing the hand "
                  "eval" % (MODE_NAMES[self.mode], self.setup))
            return
        try:
            from tetrarch import nnue
            core.load_net(nnue.Net.load(want))
            self.net = want
            # Said out loud on every switch. The bundle dispatch was silent,
            # so nothing in the protocol told you which evaluation was
            # playing -- and an A/B run against the wrong net is a campaign
            # thrown away, which has happened here once already.
            print("info string %s %s -> %s"
                  % (MODE_NAMES[self.mode], self.setup, os.path.basename(want)))
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
        # Built, default OFF, each pending its own A/B (docs/AB.md). None is
        # exact. NullMove and RFP are Teams-only; the FFA* three touch only the
        # paranoid search and share the LMR/LMP parameters above.
        print("option name NullMove type check default true")
        print("option name NullMoveMinDepth type spin default 3 min 2 max 16")
        print("option name NullMoveR type spin default 2 min 1 max 6")
        print("option name RFP type check default true")
        print("option name RFPMaxDepth type spin default 3 min 1 max 8")
        print("option name RFPMargin type spin default 150 min 1 max 2000")
        print("option name FFAKillers type check default false")
        print("option name FFALMR type check default true")
        print("option name FFALMP type check default true")
        # A draw the search is walking into has to score as one. Off is for
        # measuring the difference, not for playing.
        print("option name Repetitions type check default true")
        # Both dormant until an A/B says otherwise. SEE prices a capture after
        # the exchange settles; ordering by it and pruning on it are separate
        # claims and separate toggles.
        print("option name FFATT type check default true")
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
                self._replayed = None
                self._use_net_for_setup()
        elif name == "mode":
            if value.lower() in MODE_NAMES:
                self.mode = MODE_NAMES.index(value.lower())
                self.board = start_board(self.setup, self.mode)
                self.history = []
                self._replayed = None
                # The net follows the mode as well as the setup: FFA and Teams
                # are different games and a net trained on one has no signal
                # for the other.
                self._use_net_for_setup()
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
        elif name == "ffatt":
            core.set_ffa_tt(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "lazyeval":
            core.set_lazy_eval(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "lmp":
            core.set_lmp(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "nullmove":
            core.set_null_move(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "rfp":
            core.set_rfp(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "ffakillers":
            core.set_ffa_killers(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "ffalmr":
            core.set_ffa_lmr(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name == "ffalmp":
            core.set_ffa_lmp(value.strip().lower() in ("true", "1", "on", "yes"))
        elif name in ("nullmovemindepth", "nullmover"):
            if name == "nullmovemindepth":
                self.null_min_depth = int(value)
            else:
                self.null_r = int(value)
            core.set_null_params(self.null_min_depth, self.null_r)
        elif name in ("rfpmaxdepth", "rfpmargin"):
            if name == "rfpmaxdepth":
                self.rfp_max_depth = int(value)
            else:
                self.rfp_margin = int(value)
            core.set_rfp_params(self.rfp_max_depth, self.rfp_margin)
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
                self.net_explicit = True
                print("info string net unloaded; using the throwaway hand eval")
                return
            # A .txt value is a bundle -- one net per setup -- rather than a
            # single net. Which one plays then follows the setup, and switching
            # setups switches nets.
            if value.strip().endswith(".txt"):
                try:
                    self.bundle = load_bundle(value.strip())
                    self.net = None
                    # A bundle IS the dispatch, so it re-enables it.
                    self.net_explicit = False
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
                self.net_explicit = True
                print("info string net loaded: %s" % self.net)
            except Exception as exc:                        # noqa: BLE001
                print("info string could not load net %s: %s" % (value, exc))

    def cmd_ucinewgame(self, _args):
        core.clear_hash()
        self.board = start_board(self.setup, self.mode)
        self.history = []
        self._replayed = None

    def cmd_position(self, args):
        """Set the position, replaying only what is new since the last one.

        A caller playing a game sends `position ... moves` once per ply with
        one more move each time, so rebuilding from move one costs O(n^2) in
        the move generator over a game. That is invisible in Teams, where games
        run about a hundred plies, and dominant in FFA, where they run three
        hundred and there are four engines rather than two: measured per engine
        move, 0.026s at ply 0 against 0.386s at ply 300, which is ~60
        core-seconds a game of replay against ~3 of actual search.

        The cache is only ever used when the new command is the old one plus
        moves on the end. Anything else -- a different start, a shorter list, a
        list that diverges -- falls through to the full rebuild that was always
        here, so this can lose speed and cannot change an answer.
        """
        if not args:
            return
        moves = []
        if "moves" in args:
            i = args.index("moves")
            moves = args[i + 1:]
            args = args[:i]

        # Setup and mode are part of the key: the same words mean a different
        # board under each, and only `position <setup>` announces a change.
        spec = (tuple(args), self.setup, self.mode)
        prior = self._replayed
        if (prior is not None and prior[0] == spec
                and len(moves) >= len(prior[1])
                and moves[:len(prior[1])] == prior[1]):
            fresh = moves[len(prior[1]):]
            applied = list(prior[1])
        else:
            # `position startpos <setup>` and `position <setup>` are two
            # spellings of one command, so the setup name is normalised out
            # here and ONE body runs. They used to be separate arms and the
            # startpos one never assigned self.setup or reloaded the net, so
            # `position startpos modern` played modern with the previous
            # setup's evaluation -- net-v5 rather than net-v7, a net the bundle
            # records at -22.15 +/- 10.81 on that setup.
            named = None
            if not args or args[0] == "startpos":
                named = args[1] if len(args) > 1 else self.setup
            elif args[0] in SETUPS:
                named = args[0]
            if named is not None:
                if named not in SETUPS:
                    print("info string unrecognised setup %r" % named)
                    self._replayed = None
                    return
                self.setup = named
                self.board = start_board(self.setup, self.mode)
                self._use_net_for_setup()
                spec = (tuple(args), self.setup, self.mode)
            elif args[0] in ("fen4", "fen"):
                self.board = Board.from_fen4(" ".join(args[1:]), self.mode)
            else:
                print("info string unrecognised position %r" % args[0])
                self._replayed = None
                return

            # A position can arrive already holding a seat with no moves -- FFA
            # eliminations are part of the position, not an event the caller
            # replays -- so it is resolved on load as well as after every move.
            game.resolve(self.board)

            # Every position the game passed through, so the search can tell a
            # repetition from a transposition (§10.2). The board's own key is
            # not included: the root supplies that itself.
            self.history = []
            fresh = moves
            applied = []

        for token in fresh:
            self.history.append(self.board.key)
            if not self.play(token):
                self.history.pop()
                print("info string illegal move %s" % token)
                # Cache what was actually applied, not what was asked for, or
                # the next command extends a position that never existed.
                self._replayed = (spec, applied)
                return
            applied.append(token)
        self._replayed = (spec, applied)

    def play(self, token):
        """Apply a move given as from-to plus an optional promotion letter.

        game.play_token, not a copy of it: the elimination rule it applies
        (§7) is the thing that must not exist three times.
        """
        return game.play_token(self.board, token) is not None

    def cmd_go(self, args):
        """`go` always answers with a `bestmove`, even on a position the
        search refuses.

        A UCI host blocks until it sees that line. Any exception in here used
        to be caught by main's dispatch guard, which prints an `info string`
        and reads the next command -- so `go` returned with no `bestmove` at
        all and the session hung. Reachable in one command: a Teams FEN4
        carrying a dead seat, which `position fen4` accepts and `game.resolve`
        passes through, and which `search()` then refuses (§7).
        """
        limits = Limits()
        seat = self.board.turn
        infinite = False
        i = 0
        while i < len(args):
            token, value = args[i], None
            if i + 1 < len(args):
                try:
                    value = int(args[i + 1])
                except ValueError:
                    value = None
            if token in ("depth", "nodes", "movetime") and value is None:
                # Said rather than swallowed: a dropped limit leaves a search
                # with no bound at all, which looks exactly like a hang.
                emit("info string go %s needs a number; ignored" % token)
            if token == "depth" and value is not None:
                limits.depth = value
            elif token == "nodes" and value is not None:
                limits.nodes = value
            elif token == "movetime" and value is not None:
                limits.movetime = value
            elif token == "infinite":
                limits.depth = limits.max_depth
                infinite = True
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
            emit("info depth %d%s score %s nodes %d nps %d time %d pv %s"
                 % (r.depth, "" if rank is None else " multipv %d" % rank,
                    score_string(r.score), r.nodes, r.nps,
                    int(r.elapsed * 1000),
                    " ".join(move_str(m) for m in moves)))

        inbox = self.inbox
        #: The best move of the last COMPLETED depth, for a search cut short
        #: by `stop`: search() never returns in that case, so its Result is
        #: read here instead.
        last = [None]

        def stopped():
            if inbox is not None and inbox.stop.is_set():
                raise _Stopped()

        if inbox is not None:
            inbox.begin_search()
        # One guard where the two arms converge, so the print below always
        # runs. A refused position falls back to the first legal move, which
        # is at least playable; with no legal move at all it is "0000".
        try:
            if self.multipv > 1:
                def report(rs):
                    for rank, r in enumerate(rs, 1):
                        line(r, rank)
                    last[0] = rs[0].best if rs else last[0]
                    stopped()

                results = search_multi(self.board, limits, self.multipv,
                                       report, self.history)
                best = results[0].best if results else None
            else:
                def report(r):
                    line(r)
                    last[0] = r.best or last[0]
                    stopped()

                best = search(self.board, limits, report,
                              self.history).best
            if infinite and inbox is not None:
                inbox.wait_for_stop()
        except _Stopped:
            best = last[0]
        except Exception as exc:                                # noqa: BLE001
            emit("info string search failed: %s: %s"
                 % (type(exc).__name__, exc))
            legal = gen.gen_legal(self.board)
            best = legal[0] if legal else None
        finally:
            if inbox is not None:
                inbox.end_search()
        emit("bestmove %s" % (move_str(best) if best else "0000"))

    def cmd_stop(self, _args):
        # Reached only when no search is running: a `stop` sent during one is
        # taken by the Inbox and never queued. Nothing to stop, so nothing to
        # say -- UCI hosts send a defensive `stop` before every `go`.
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
    engine.inbox = Inbox(sys.stdin)
    while True:
        line = engine.inbox.get()
        if line is None:
            break
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
