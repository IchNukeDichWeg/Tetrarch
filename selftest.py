#!/usr/bin/env python3
"""selftest.py -- the ladder. Runs before every commit, including pure-Python ones.

    python3 selftest.py                  # the pre-commit run, a few seconds
    python3 selftest.py --crosscheck N   # N random positions through both movegens
    python3 selftest.py --perft-deep     # perft 5 for all five setups (minutes)

Section references (§n) are to docs/RULES.md.

Two things this file is deliberately built around:

* **Perft only tests the generator perft calls.** Every check below that matters
  is run through *both* movegens, and the cross-check compares their output
  move-for-move. When the search grows its own generator in Phase 3 it gets its
  own differential gate here too -- otherwise perft, the bench signature and the
  node pins are all blind to it breaking.
* **Node pins are machine-specific.** Perft is exact integer arithmetic and is
  not, so these numbers are pinned unconditionally. The float-sensitive pins
  arrive with the search; those get a named machine in docs/PERFT.md.
"""

import argparse
import os
import random
import sys
import tempfile
import time

from tetrarch.board import (
    Board, start_board, rotate, SETUPS, SETUP_SWAPS, MODE_FFA, MODE_TEAMS,
    MODE_NAMES,
    VALID, COMPACT, SQUARES, NPLAYABLE, NSQ, KNIGHT_DELTAS, QUEEN_DIRS,
    PAWN_PUSH, PAWN_TAKES, PROMO_COORD, pawn_coord, CASTLE_GEO, ROOK_HOME,
    RED, BLUE, YELLOW, GREEN, DEAD_UNKNOWN, SEAT_NAMES,
    PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING, PQUEEN,
    make_piece, PC_COLOR, PC_TYPE, sq_of, sq_from_name, name_of, file_of,
    rank_of, move_str, mv_flag, mv_to, mv_from, mv_promo,
    F_EP, F_DOUBLE, F_CASTLE_SHORT, F_CASTLE_LONG,
)
from tetrarch import movegen as fast
from tetrarch import movegen_slow as slow
from tetrarch import core
from tetrarch import eval_hand
from tetrarch import search

HAVE_C = core.available()

SCRATCH = tempfile.gettempdir()

FAILURES = []
CHECKS = [0]
SECTION = [0, 0.0]


def check(name, ok, detail=""):
    CHECKS[0] += 1
    if not ok:
        FAILURES.append(name)
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", name,
                          ("  (%s)" % detail) if detail else ""))
    return ok


def check_all(name, results, detail=""):
    """One line for a group of sub-assertions.

    `results` is a list of (label, ok). A wall of near-identical PASS lines
    hides a failure rather than revealing it, so a loop reports once and names
    only what broke.
    """
    bad = [label for label, ok in results if not ok]
    CHECKS[0] += max(len(results) - 1, 0)
    extra = "%d checks" % len(results)
    if detail:
        extra += ", " + detail
    if bad:
        extra += "; failed: " + ", ".join(bad[:4])
        if len(bad) > 4:
            extra += " and %d more" % (len(bad) - 4)
    return check(name, not bad, extra)


def section(title):
    now = time.time()
    if SECTION[0] and now - SECTION[1] >= 0.05:
        print("        %.2fs" % (now - SECTION[1]))
    SECTION[0] += 1
    SECTION[1] = now
    print("\n--- %d. %s ---" % (SECTION[0], title))


def end_sections():
    if SECTION[0] and time.time() - SECTION[1] >= 0.05:
        print("        %.2fs" % (time.time() - SECTION[1]))
    SECTION[0] = 0


def pool_map(fn, items, workers):
    """Map over a process pool, or inline when there is nothing to gain."""
    if workers <= 1 or len(items) < 2:
        return [fn(item) for item in items]
    import multiprocessing
    # chunksize=1: the cost per item varies by orders of magnitude (an unpruned
    # minimax over a sparse position can be 100x another), so any chunking
    # hands one worker the whole tail and the pool finishes no sooner than it.
    with multiprocessing.Pool(min(workers, len(items))) as pool:
        return pool.map(fn, items, chunksize=1)


# --- position construction helpers ------------------------------------------

def position(pieces, turn=RED, mode=MODE_TEAMS, alive=(1, 1, 1, 1),
             ep=None, ck=(0, 0, 0, 0), cq=(0, 0, 0, 0)):
    """Build a board from ``{"e4": (RED, PAWN), ...}``.

    Hand-written FEN4 for test positions is its own bug source; this builds
    them from squares and then round-trips through FEN4 as part of the test.
    """
    b = Board(mode)
    for square, (color, ptype) in pieces.items():
        sq = sq_from_name(square)
        assert VALID[sq], "test position uses unplayable square %s" % square
        b.sq[sq] = make_piece(color, ptype)
    b.turn = turn
    b.alive = [bool(a) for a in alive]
    b.ck = [bool(c) for c in ck]
    b.cq = [bool(c) for c in cq]
    if ep:
        for color, (target, victim) in ep.items():
            b.ep[color] = (sq_from_name(target), sq_from_name(victim))
    b.find_kings()
    b.recompute_key()
    return b


def both_legal(b):
    """Legal moves from both generators, asserting they agree. Returns the set."""
    a = sorted(fast.gen_legal(b))
    c = sorted(slow.gen_legal(b))
    if a != c:
        only_fast = [move_str(m) for m in a if m not in c]
        only_slow = [move_str(m) for m in c if m not in a]
        raise AssertionError("generators disagree: fast-only=%s slow-only=%s\n%s"
                             % (only_fast, only_slow, b.to_fen4()))
    return a


def moves_to(b, square):
    """Move strings landing on `square`, from the agreed generator output."""
    target = sq_from_name(square)
    return sorted(move_str(m) for m in both_legal(b) if mv_to(m) == target)


# --- 1. geometry ------------------------------------------------------------

def test_geometry():
    section("geometry (§1)")
    check("160 playable squares", sum(VALID) == NPLAYABLE, str(sum(VALID)))
    compact = [COMPACT[sq] for sq in SQUARES]
    check("COMPACT is a bijection onto 0..159",
          sorted(compact) == list(range(NPLAYABLE)))
    check("COMPACT marks unplayable squares",
          all(COMPACT[sq] == 255 for sq in range(NSQ) if not VALID[sq]))

    corners = ["a1", "c3", "a14", "c12", "n1", "l3", "n14", "l12"]
    check("corners are unplayable",
          all(not VALID[sq_from_name(s)] for s in corners))
    check("corner-adjacent squares are playable",
          all(VALID[sq_from_name(s)] for s in ("d1", "a4", "d14", "n11")))

    # The padding property the whole mailbox scheme rests on: no delta from a
    # real square may wrap onto the far side of another rank.
    wrapped = []
    for sq in SQUARES:
        f, r = file_of(sq), rank_of(sq)
        for d in KNIGHT_DELTAS + QUEEN_DIRS:
            t = (sq + d) & 255
            if not VALID[t]:
                continue
            df, dr = file_of(t) - f, rank_of(t) - r
            if (df, dr) != (file_of((sq + d) % 256) - f, rank_of(sq + d) - r):
                wrapped.append((name_of(sq), d))
            if abs(df) > 2 or abs(dr) > 2:
                wrapped.append((name_of(sq), d))
    check("no delta wraps between ranks", not wrapped, str(wrapped[:4]))


# --- 2. starting positions --------------------------------------------------

#: The §3.3 table, transcribed from the chess.com lobby.
EXPECTED_KINGS = {
    "classic": ("h1", "a8", "g14", "n7"),
    "modern": ("h1", "a7", "g14", "n8"),
    "by": ("h1", "a7", "h14", "n7"),
    "byg": ("h1", "a7", "h14", "n8"),
    "rg": ("g1", "a8", "g14", "n8"),
}
EXPECTED_QUEENS = {
    "classic": ("g1", "a7", "h14", "n8"),
    "modern": ("g1", "a8", "h14", "n7"),
    "by": ("g1", "a8", "g14", "n8"),
    "byg": ("g1", "a8", "g14", "n7"),
    "rg": ("h1", "a7", "h14", "n7"),
}


def test_setups():
    section("starting positions (§3)")
    check("five setups", set(SETUPS) == set(EXPECTED_KINGS), str(SETUPS))
    kings, queens, counts, opens = [], [], [], []
    for setup in SETUPS:
        b = start_board(setup)
        kings.append((setup, tuple(name_of(k) for k in b.kings)
                      == EXPECTED_KINGS[setup]))
        found = []
        for c in range(4):
            hit = [name_of(sq) for sq in SQUARES
                   if b.sq[sq] == make_piece(c, QUEEN)]
            found.append(hit[0] if len(hit) == 1 else str(hit))
        queens.append((setup, tuple(found) == EXPECTED_QUEENS[setup]))
        counts.append((setup, sum(1 for sq in SQUARES if b.sq[sq]) == 64))
        opens.append((setup, len(both_legal(b)) == 20))
    check_all("king squares match the chess.com lobby", kings)
    check_all("queen squares match the chess.com lobby", queens)
    check_all("64 pieces on 160 squares", counts)
    check_all("every setup opens with 20 moves", opens)

    # modern is the only 90-degree rotationally symmetric one (§3.3).
    mod = start_board("modern")
    rot_ok = True
    for sq in SQUARES:
        p = mod.sq[sq]
        if not p:
            continue
        f, r = file_of(sq), rank_of(sq)
        q = mod.sq[sq_of(r, 13 - f)]
        if not q or PC_TYPE[q] != PC_TYPE[p] or \
                PC_COLOR[q] != (PC_COLOR[p] + 1) % 4:
            rot_ok = False
            break
    check("modern is 90-degree rotationally symmetric", rot_ok)
    classic = start_board("classic")
    check("classic is not 90-degree symmetric",
          any(classic.sq[sq_of(rank_of(sq), 13 - file_of(sq))] !=
              make_piece((PC_COLOR[classic.sq[sq]] + 1) % 4,
                         PC_TYPE[classic.sq[sq]])
              for sq in SQUARES if classic.sq[sq]))


# --- 3. FEN4 (§11) ----------------------------------------------------------

CLASSIC_FEN4 = (
    "R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-0-\n"
    "3,yR,yN,yB,yK,yQ,yB,yN,yR,3/\n"
    "3,yP,yP,yP,yP,yP,yP,yP,yP,3/\n"
    "14/\n"
    "bR,bP,10,gP,gR/\n"
    "bN,bP,10,gP,gN/\n"
    "bB,bP,10,gP,gB/\n"
    "bK,bP,10,gP,gQ/\n"
    "bQ,bP,10,gP,gK/\n"
    "bB,bP,10,gP,gB/\n"
    "bN,bP,10,gP,gN/\n"
    "bR,bP,10,gP,gR/\n"
    "14/\n"
    "3,rP,rP,rP,rP,rP,rP,rP,rP,3/\n"
    "3,rR,rN,rB,rQ,rK,rB,rN,rR,3"
)

#: Athena and 4pchess spell the removed corners as lowercase 'x' (§11.3).
ATHENA_MODERN_FEN4 = (
    "R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-0-"
    "x,x,x,yR,yN,yB,yK,yQ,yB,yN,yR,x,x,x/"
    "x,x,x,yP,yP,yP,yP,yP,yP,yP,yP,x,x,x/"
    "x,x,x,8,x,x,x/"
    "bR,bP,10,gP,gR/bN,bP,10,gP,gN/bB,bP,10,gP,gB/bQ,bP,10,gP,gK/"
    "bK,bP,10,gP,gQ/bB,bP,10,gP,gB/bN,bP,10,gP,gN/bR,bP,10,gP,gR/"
    "x,x,x,8,x,x,x/"
    "x,x,x,rP,rP,rP,rP,rP,rP,rP,rP,x,x,x/"
    "x,x,x,rR,rN,rB,rQ,rK,rB,rN,rR,x,x,x"
)


def test_fen4():
    section("FEN4 (§11)")
    b = start_board("classic")
    check("classic writes fen4's canonical default", b.to_fen4() == CLASSIC_FEN4)

    trips = []
    for setup in SETUPS:
        for mode in (MODE_FFA, MODE_TEAMS):
            src = start_board(setup, mode)
            rt = Board.from_fen4(src.to_fen4(), mode)
            label = "%s/%s" % (setup, MODE_NAMES[mode])
            trips.append((label, rt == src and rt.key == src.key
                          and rt.to_fen4() == src.to_fen4()))
    check_all("FEN4 round trips for every setup and mode", trips)

    # Athena's lowercase-x dialect reads in and normalises out (§11.3 quirk 2).
    ath = Board.from_fen4(ATHENA_MODERN_FEN4)
    check("reads Athena's lowercase-x corners", ath == start_board("modern"))
    check("writes canonical empty runs, not x", "x" not in ath.to_fen4())

    # Dead pieces, both spellings (§9.1).
    dead = Board.from_fen4(
        "B-1,0,0,0-0,0,0,0-0,0,0,0-7,0,0,0-3-"
        "3,yR,yN,yB,yK,yQ,yB,yN,yR,3/14/14/14/14/14/"
        "bK,4,drP,dP,7/14/14/14/14/3,rK,10/14/14")
    check("dead seat parsed", dead.alive == [False, True, True, True])
    check("dead piece with origin keeps its seat",
          dead.sq[sq_from_name("f8")] == make_piece(RED, PAWN))
    check("dead piece without origin gets the unknown seat",
          PC_COLOR[dead.sq[sq_from_name("g8")]] == DEAD_UNKNOWN)
    check("points survive", dead.points == [7, 0, 0, 0])
    check("halfmove survives", dead.halfmove == 3)
    out = dead.to_fen4()
    check("dead pieces round trip", Board.from_fen4(out) == dead)
    check("dead-with-origin re-emits as drP", ",drP," in out, out.split("\n")[7])
    check("dead-without-origin re-emits as dP", ",dP," in out, out.split("\n")[7])

    # en-passant and pawnsBaseRank in the extra block (§11.4)
    fen = ("R-0,0,0,0-0,0,0,0-0,0,0,0-0,0,0,0-0-"
           "{'enPassant':('i3:i4','c6:d6','f12:f11','l9:k9'),'pawnsBaseRank':8}-"
           "3,8,3/3,8,3/14/14/14/14/14/14/14/14/14/14/3,8,3/3,8,3")
    e = Board.from_fen4(fen)
    check("enPassant parses per seat",
          [(name_of(t), name_of(v)) for t, v in e.ep] ==
          [("i3", "i4"), ("c6", "d6"), ("f12", "f11"), ("l9", "k9")])
    check("pawnsBaseRank parses", e.pawn_base_rank == 8)
    check("extra block round trips", Board.from_fen4(e.to_fen4()) == e)
    check("extra block is omitted when default",
          "{" not in start_board("classic").to_fen4())

    for bad, why in [
        ("R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-0-14/14/14", "too few ranks"),
        ("R-0,0,0,0-1,1,1,1-0,0,0,0-0-" + "14/" * 13 + "14", "too few fields"),
        ("R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-0-{'nope':1}-" + "14/" * 13 + "14",
         "unknown tag"),
        ("R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-0-" + "13/" * 13 + "14",
         "short rank"),
        ("R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-0-rK,13/" + "14/" * 12 + "14",
         "piece on a corner"),
    ]:
        try:
            Board.from_fen4(bad)
            check("rejects %s" % why, False)
        except ValueError:
            check("rejects %s" % why, True)


# --- 4. make / unmake and Zobrist -------------------------------------------

def test_make_unmake():
    section("make/unmake and Zobrist (§10.2)")
    rng = random.Random(11)
    bad_restore = bad_key = 0
    for setup in SETUPS:
        for mode in (MODE_FFA, MODE_TEAMS):
            b = start_board(setup, mode)
            for _ in range(120):
                moves = fast.gen_legal(b)
                if not moves:
                    break
                for m in moves:
                    before = b.copy()
                    key_before = b.key
                    undo = b.make(m)
                    if b.key != b.copy().recompute_key():
                        bad_key += 1
                    b.unmake(m, undo)
                    if b != before or b.key != key_before:
                        bad_restore += 1
                b.make(rng.choice(moves))
    check("unmake restores the position exactly", bad_restore == 0,
          "%d failures" % bad_restore)
    check("incremental Zobrist matches a full recompute", bad_key == 0,
          "%d failures" % bad_key)

    # The alive mask is part of the key (§10.2): two positions identical but
    # for a dead seat must not collide.
    live = position({"h1": (RED, KING), "a8": (BLUE, KING),
                     "g14": (YELLOW, KING), "n7": (GREEN, KING)})
    gone = position({"h1": (RED, KING), "a8": (BLUE, KING),
                     "g14": (YELLOW, KING), "n7": (GREEN, KING)},
                    alive=(1, 1, 1, 0))
    check("alive mask changes the Zobrist key", live.key != gone.key)


# --- 5. the hard rules ------------------------------------------------------

def test_pawn_directions():
    section("pawn direction and promotion (§4.1, §4.2)")
    base = {"h1": (RED, KING), "a8": (BLUE, KING),
            "g14": (YELLOW, KING), "n7": (GREEN, KING)}
    expect = {RED: ("f3", "f4"), BLUE: ("c6", "d6"),
              YELLOW: ("f12", "f11"), GREEN: ("l9", "k9")}
    starts = {RED: "f2", BLUE: "b6", YELLOW: "f13", GREEN: "m9"}
    for color, home in starts.items():
        b = position(dict(base, **{home: (color, PAWN)}), turn=color)
        got = sorted(move_str(m) for m in both_legal(b)
                     if mv_from(m) == sq_from_name(home))
        one, two = expect[color]
        check("%s pawn pushes one and two" % "RBYG"[color],
              got == sorted([home + one, home + two]), str(got))
        doubles = [m for m in both_legal(b) if mv_flag(m) == F_DOUBLE]
        check("%s double push is flagged" % "RBYG"[color], len(doubles) == 1)

    # Promotion happens on the seat's own 8th/11th rank, never at a board edge.
    for mode, coord_name in ((MODE_FFA, "8th"), (MODE_TEAMS, "11th")):
        for color, square, dest in ((RED, "f7", "f8"), (BLUE, "g6", "h6"),
                                    (YELLOW, "f8", "f7"), (GREEN, "h9", "g9")):
            if mode == MODE_TEAMS:
                square, dest = {
                    RED: ("f10", "f11"), BLUE: ("j6", "k6"),
                    YELLOW: ("f5", "f4"), GREEN: ("e9", "d9")}[color]
            b = position(dict(base, **{square: (color, PAWN)}),
                         turn=color, mode=mode)
            promos = [m for m in both_legal(b) if mv_promo(m)]
            want = 1 if mode == MODE_FFA else 4
            check("%s promotes on its %s rank (%s)" % ("RBYG"[color], coord_name,
                                                       MODE_NAMES[mode]),
                  len(promos) == want and all(name_of(mv_to(m)) == dest
                                              for m in promos),
                  "%d promo moves to %s" % (len(promos),
                                            {name_of(mv_to(m)) for m in promos}))
            if mode == MODE_FFA:
                check("FFA promotes to a 1-point queen only (§8.1)",
                      all(mv_promo(m) == PQUEEN for m in promos))
            else:
                check("Teams allows underpromotion",
                      {mv_promo(m) for m in promos} ==
                      {QUEEN, ROOK, BISHOP, KNIGHT})

    # A pawn on rank 8 that is NOT the promoting seat just moves normally.
    b = position(dict(base, **{"f8": (RED, PAWN)}), turn=RED, mode=MODE_TEAMS)
    check("rank 8 is not a promotion rank in Teams",
          not any(mv_promo(m) for m in both_legal(b)))


def test_en_passant():
    section("en passant (§5)")
    base = {"h1": (RED, KING), "a8": (BLUE, KING),
            "g14": (YELLOW, KING), "n7": (GREEN, KING)}

    # Lifetime: Blue double-pushes b6-d6, then Yellow and Green move, and Red
    # can still capture on c6 three plies later (§5.1).
    b = position(dict(base, **{"b6": (BLUE, PAWN), "d5": (RED, PAWN)}),
                 turn=BLUE)
    b.make(next(m for m in both_legal(b) if move_str(m) == "b6d6"))
    check("double push records the skipped square",
          b.ep[BLUE] == (sq_from_name("c6"), sq_from_name("d6")),
          str(b.ep[BLUE]))
    for ply, seat in enumerate(("Y", "G")):
        check("offer alive after %d intervening ply" % (ply + 1),
              b.ep[BLUE] is not None)
        b.make(both_legal(b)[0])
    check("Red to move three plies later", b.turn == RED)
    eps = [m for m in both_legal(b) if mv_flag(m) == F_EP]
    check("Red can still capture en passant on c6",
          [move_str(m) for m in eps] == ["d5c6"], str([move_str(m) for m in eps]))
    undo = b.make(eps[0])
    check("the captured pawn is removed from d6, not c6",
          b.sq[sq_from_name("d6")] == 0
          and b.sq[sq_from_name("c6")] == make_piece(RED, PAWN))
    b.unmake(eps[0], undo)

    # The offer dies when its owner moves again (§5.1).
    b2 = position(dict(base, **{"b6": (BLUE, PAWN), "d5": (RED, PAWN),
                                "a4": (BLUE, PAWN)}), turn=BLUE)
    b2.make(next(m for m in fast.gen_legal(b2) if move_str(m) == "b6d6"))
    for _ in range(3):
        b2.make(fast.gen_legal(b2)[0])          # Y, G, R all move
    check("Blue to move again", b2.turn == BLUE)
    b2.make(fast.gen_legal(b2)[0])
    check("Blue's own move clears its offer", b2.ep[BLUE] is None)

    # Head-on en passant is unreachable, so it must never be generated (§5.3).
    for mode in (MODE_FFA, MODE_TEAMS):
        head_on = position(dict(base, **{"f13": (YELLOW, PAWN),
                                         "e11": (RED, PAWN),
                                         "g11": (RED, PAWN)}),
                           turn=YELLOW, mode=mode)
        head_on.make(next(m for m in fast.gen_legal(head_on)
                          if move_str(m) == "f13f11"))
        check("no head-on en passant (%s)" % MODE_NAMES[mode],
              not any(mv_flag(m) == F_EP for m in both_legal(head_on)))

    # Both flanking pawns may capture -- Athena removes the wrong pawn for one
    # of them and 4pchess omits it entirely (§5.4).
    two_flank = position(dict(base, **{"b6": (BLUE, PAWN), "b5": (RED, PAWN),
                                       "d5": (RED, PAWN)}), turn=BLUE)
    two_flank.make(next(m for m in fast.gen_legal(two_flank)
                        if move_str(m) == "b6d6"))
    for _ in range(2):
        two_flank.make(fast.gen_legal(two_flank)[0])
    eps = sorted(move_str(m) for m in both_legal(two_flank) if mv_flag(m) == F_EP)
    check("both flanking pawns can capture en passant",
          eps == ["b5c6", "d5c6"], str(eps))

    # The skipped square may be occupied by a third player, and then one move
    # captures two pieces (§5.5). FFA, because in Teams the Yellow rook below
    # is Red's partner and the square would not be capturable at all.
    both = position(dict(base, **{"b6": (BLUE, PAWN), "d5": (RED, PAWN),
                                  "c9": (YELLOW, ROOK)}), turn=BLUE,
                    mode=MODE_FFA)
    both.make(next(m for m in fast.gen_legal(both) if move_str(m) == "b6d6"))
    both.make(next(m for m in fast.gen_legal(both) if move_str(m) == "c9c6"))
    both.make(fast.gen_legal(both)[0])          # Green
    landing = moves_to(both, "c6")
    check("one move onto an occupied skipped square", landing == ["d5c6"],
          str(landing))
    m = next(m for m in both_legal(both) if move_str(m) == "d5c6")
    check("and it is the en-passant move", mv_flag(m) == F_EP)
    both.make(m)
    check("both the rook and the pawn are captured",
          both.sq[sq_from_name("d6")] == 0
          and both.sq[sq_from_name("c6")] == make_piece(RED, PAWN))

    # An en-passant capture can also promote: Blue's skipped square c8 sits on
    # Red's FFA promotion rank (§4.2, §5.2).
    promo = position(dict(base, **{"b7": (RED, PAWN), "d8": (BLUE, PAWN)}),
                     turn=RED, mode=MODE_FFA,
                     ep={BLUE: ("c8", "d8")})
    ep_moves = [m for m in both_legal(promo) if mv_flag(m) == F_EP]
    check("en passant can promote at the same time",
          len(ep_moves) == 1 and mv_promo(ep_moves[0]) == PQUEEN,
          str([(move_str(m), mv_promo(m)) for m in ep_moves]))


def test_multi_check():
    section("check from several players at once (§7)")
    b = position({"h4": (RED, KING), "a8": (BLUE, KING), "g14": (YELLOW, KING),
                  "n7": (GREEN, KING), "h10": (BLUE, ROOK),
                  "d4": (YELLOW, ROOK), "k7": (GREEN, BISHOP)},
                 turn=RED, mode=MODE_FFA)
    attackers = sum(1 for c in (BLUE, YELLOW, GREEN)
                    if any(slow.attacks_square(b, file_of(sq), rank_of(sq),
                                               file_of(b.kings[RED]),
                                               rank_of(b.kings[RED]))
                           for sq in SQUARES
                           if b.sq[sq] and PC_COLOR[b.sq[sq]] == c))
    check("king is attacked by three different seats", attackers == 3,
          str(attackers))
    check("both generators see the check",
          fast.in_check(b, RED) and slow.in_check(b, RED))
    legal = both_legal(b)
    check("every legal reply escapes check", legal and all(
        _leaves_safe(b, m, RED) for m in legal), "%d moves" % len(legal))

    # In Teams the same three attackers include a partner, who cannot check.
    t = position({"h4": (RED, KING), "a8": (BLUE, KING), "g14": (YELLOW, KING),
                  "n7": (GREEN, KING), "d4": (YELLOW, ROOK)},
                 turn=RED, mode=MODE_TEAMS)
    check("a partner's rook does not give check",
          not fast.in_check(t, RED) and not slow.in_check(t, RED))
    check("and in FFA the same rook does", fast.in_check(
        position({"h4": (RED, KING), "a8": (BLUE, KING), "g14": (YELLOW, KING),
                  "n7": (GREEN, KING), "d4": (YELLOW, ROOK)},
                 turn=RED, mode=MODE_FFA), RED))


def _leaves_safe(b, m, color):
    undo = b.make(m)
    ok = not fast.in_check(b, color)
    b.unmake(m, undo)
    return ok


def test_dead_seats():
    section("dead seats (§9)")
    b = position({"h1": (RED, KING), "a8": (BLUE, KING), "g14": (YELLOW, KING),
                  "n7": (GREEN, KING), "f4": (BLUE, ROOK), "f8": (RED, ROOK)},
                 turn=RED, mode=MODE_FFA, alive=(1, 0, 1, 1))
    check("a dead seat's rook is capturable",
          "f8f4" in [move_str(m) for m in both_legal(b)])
    check("a dead seat's rook gives no check",
          not fast.in_check(b, RED) and not slow.in_check(b, RED))
    check("capturing it is legal for both generators",
          len(both_legal(b)) > 0)

    # It still blocks: Yellow's rook cannot see through it to Red's king.
    blocked = position({"f1": (RED, KING), "a8": (BLUE, KING),
                        "g14": (YELLOW, KING), "n7": (GREEN, KING),
                        "f4": (BLUE, ROOK), "f8": (YELLOW, ROOK)},
                       turn=RED, mode=MODE_FFA, alive=(1, 0, 1, 1))
    check("a dead piece still blocks a slider",
          not fast.in_check(blocked, RED) and not slow.in_check(blocked, RED))
    blocked.sq[sq_from_name("f4")] = 0
    blocked.recompute_key()
    check("and removing it exposes the check",
          fast.in_check(blocked, RED) and slow.in_check(blocked, RED))

    # Turn order skips the dead (§9).
    b.turn = RED
    check("turn order skips a dead seat", b.next_turn() == YELLOW)


def test_castling():
    section("castling (§6)")
    geometry = []
    for setup in SETUPS:
        b = start_board(setup)
        for color in range(4):
            geo = CASTLE_GEO[(color, b.kings[color])]
            for side, name in ((0, "short"), (1, "long")):
                rook_from, king_to, rook_to, between, safe = geo[side]
                geometry.append(
                    ("%s %s %s" % (setup, "RBYG"[color], name),
                     len(between) == (2 if side == 0 else 3)
                     and b.sq[rook_from] == make_piece(color, ROOK)
                     and abs(king_to - b.kings[color]) in (2, 32)))
    check_all("castling geometry is derived correctly", geometry)

    # A full castle, both sides, for every seat in every setup.
    plays = []
    for setup in SETUPS:
        for color in range(4):
            for side, flag in (("short", F_CASTLE_SHORT), ("long", F_CASTLE_LONG)):
                b = start_board(setup)
                geo = CASTLE_GEO[(color, b.kings[color])]
                rook_from, king_to, rook_to, between, _safe = \
                    geo[0 if side == "short" else 1]
                for sq in between:
                    b.sq[sq] = 0
                b.turn = color
                b.find_kings()
                b.recompute_key()
                got = [m for m in both_legal(b) if mv_flag(m) == flag]
                label = "%s %s %s" % (setup, "RBYG"[color], side)
                if len(got) != 1:
                    plays.append((label, False))
                    continue
                king_from = b.kings[color]
                b.make(got[0])
                plays.append((label,
                              b.sq[king_to] == make_piece(color, KING)
                              and b.sq[rook_to] == make_piece(color, ROOK)
                              and b.sq[king_from] == 0
                              and b.sq[rook_from] == 0))

    check_all("every seat can castle both ways in every setup", plays)

    # Rights are lost when the rook moves, and the right lost is the right one.
    b = start_board("classic")
    b.sq[sq_from_name("j1")] = 0
    b.recompute_key()
    b.make(next(m for m in fast.gen_legal(b) if move_str(m) == "k1j1"))
    check("moving the short rook clears only the short right",
          b.ck[RED] is False and b.cq[RED] is True)

    # Cannot castle out of, through or into check (§6.1).
    for blocker, why in (("j5", "into"), ("i5", "through"), ("h5", "out of")):
        b = start_board("classic")
        for sq in ("i1", "j1"):
            b.sq[sq_from_name(sq)] = 0
        # Clear the pawn in front of the attacker, or the rook never sees rank 1.
        b.sq[sq_from_name(blocker[0] + "2")] = 0
        b.sq[sq_from_name(blocker)] = make_piece(BLUE, ROOK)
        b.find_kings()
        b.recompute_key()
        castles = [m for m in both_legal(b) if mv_flag(m) == F_CASTLE_SHORT]
        check("cannot castle %s check" % why, not castles,
              str([move_str(m) for m in castles]))
    # ...but the same rook one file further away leaves castling legal.
    b = start_board("classic")
    for sq in ("i1", "j1"):
        b.sq[sq_from_name(sq)] = 0
    b.sq[sq_from_name("k2")] = 0
    b.sq[sq_from_name("k5")] = make_piece(BLUE, ROOK)
    b.find_kings()
    b.recompute_key()
    check("a rook on an unrelated file does not stop castling",
          any(mv_flag(m) == F_CASTLE_SHORT for m in both_legal(b)))


# --- 6. perft ---------------------------------------------------------------

#: Exact integer node counts. Not machine-dependent, so pinned unconditionally.
#: `modern` is cross-checked against Athena's tests/data/perft.txt (§12).
#: Setups differ from depth 2 onward: king/queen placement changes what the
#: queen sees past the cut corners, and can create pins that do not exist in
#: another setup. `classic` and `rg` share Blue's placement yet differ by 4 at
#: depth 2, because rg's Red queen on h1 pins Blue's b7 pawn against its king
#: on a8 once g2 clears -- two lost replies after each of Red's two g2 moves.
#: See docs/PERFT.md.
PERFT_PINS = {
    "classic": [1, 20, 399, 7960, 158402, 3730168],
    "modern": [1, 20, 395, 7800, 152050, 3452310],
    "by": [1, 20, 395, 7880, 155226, 3593432],
    "byg": [1, 20, 395, 7880, 155210, 3525566],
    "rg": [1, 20, 395, 7880, 155226, 3587766],
}
ATHENA_MODERN = [1, 20, 395, 7800, 152050, 3452310, 77430383, 1735784286]


def _perft_job(job):
    setup, mode, depth, which = job
    b = start_board(setup, mode)
    return (job, (slow if which == "slow" else fast).perft(b, depth))


def test_perft(depth=4, workers=1):
    section("perft to depth %d (§12)" % depth)
    jobs = []
    for setup in SETUPS:
        pins = PERFT_PINS[setup]
        for d in range(1, min(depth, len(pins) - 1) + 1):
            jobs.append((setup, MODE_TEAMS, d, "fast"))
        jobs.append((setup, MODE_FFA, 3, "fast"))
        jobs.append((setup, MODE_TEAMS, 3, "slow"))
    got = dict(pool_map(_perft_job, jobs, workers))

    check_all("pinned perft node counts",
              [("%s d%d" % (setup, d),
                got[(setup, MODE_TEAMS, d, "fast")] == PERFT_PINS[setup][d])
               for setup in SETUPS
               for d in range(1, min(depth, len(PERFT_PINS[setup]) - 1) + 1)])
    check("modern matches Athena to the pinned depth",
          PERFT_PINS["modern"][:depth + 1] == ATHENA_MODERN[:depth + 1])

    # FFA and Teams give identical counts this shallow: promotion needs a pawn
    # to travel six of its own moves (21+ plies), and no cross-team capture is
    # reachable either, so the two modes' rule differences cannot bite (§12).
    check_all("FFA and Teams agree at depth 3",
              [(setup, got[(setup, MODE_FFA, 3, "fast")]
                       == got[(setup, MODE_TEAMS, 3, "fast")])
               for setup in SETUPS])

    # Both generators must agree on the tree, not just the leaf count.
    check_all("both generators agree on the perft(3) tree",
              [(setup, got[(setup, MODE_TEAMS, 3, "slow")]
                       == got[(setup, MODE_TEAMS, 3, "fast")])
               for setup in SETUPS])


def test_c_core(deep=False):
    section("C core agreement (Phase 2 gate)")
    if not HAVE_C:
        check("C core is built", False, "run ./setup.sh")
        return
    import ctypes
    lib = core.load()
    check("TtParams layout agrees",
          lib.tt_params_size() == ctypes.sizeof(core.TtParams))
    check("TtBoard layout agrees",
          lib.tt_board_size() == ctypes.sizeof(core.TtBoard))

    # Node-for-node: identical counts at every depth, every setup, both modes.
    grid = []
    for setup in SETUPS:
        pins = PERFT_PINS[setup]
        for mode in (MODE_TEAMS, MODE_FFA):
            for d in range(1, len(pins)):
                grid.append(("%s/%s d%d" % (setup, MODE_NAMES[mode], d),
                             core.perft(start_board(setup, mode), d) == pins[d]))
    check_all("C matches the Python reference node-for-node", grid)

    check("C matches Athena at depth 5 on modern",
          core.perft(start_board("modern"), 5) == ATHENA_MODERN[5])
    if deep:
        for d in (6, 7):
            got = core.perft(start_board("modern"), d)
            check("C matches Athena at depth %d on modern" % d,
                  got == ATHENA_MODERN[d],
                  "%d != %d" % (got, ATHENA_MODERN[d]))

    # Perft cannot see a bad incremental key or a bad unmake -- it counts the
    # right number of nodes either way. This walks the tree checking both.
    check_all("C incremental key and unmake integrity",
              [("%s/%s" % (setup, MODE_NAMES[mode]),
                core.key_check(start_board(setup, mode), 3) == 0)
               for setup in SETUPS for mode in (MODE_TEAMS, MODE_FFA)])

    # The Zobrist tables really crossed the boundary intact.
    rng = random.Random(7)
    key_bad = attack_bad = move_bad = 0
    for _ in range(150):
        b = random_position(rng)
        if core.recompute_key(b) != b.recompute_key():
            key_bad += 1
        if sorted(core.gen_legal(b)) != sorted(fast.gen_legal(b)):
            move_bad += 1
        for sq in random.Random(_).sample(list(SQUARES), 12):
            if core.is_attacked(b, sq, b.turn) != fast.is_attacked(b, sq, b.turn):
                attack_bad += 1
    check("C and Python Zobrist keys agree bit-for-bit", key_bad == 0,
          "%d differ" % key_bad)
    check("C and Python agree on is_attacked", attack_bad == 0,
          "%d differ" % attack_bad)
    check("C and Python agree on legal moves", move_bad == 0,
          "%d positions differ" % move_bad)


def test_rotation():
    section("board rotation (match.py seat rotation)")
    ident, invariant, shape = [], [], []
    for setup in SETUPS:
        b = start_board(setup)
        ident.append((setup, rotate(b, 4) == b))
        counts = [fast.perft(rotate(b, k), 3) for k in range(4)]
        invariant.append((setup, len(set(counts)) == 1))
        for k in range(1, 4):
            r = rotate(b, k)
            shape.append(("%s r%d" % (setup, k),
                          sum(1 for sq in SQUARES if r.sq[sq]) == 64
                          and r.turn == (b.turn + k) & 3))
    check_all("four quarter turns is the identity", ident)
    check_all("perft is invariant under rotation", invariant)
    check_all("rotation preserves piece count and shifts the turn", shape)
    # modern is the 90-degree symmetric setup, so rotating it changes nothing.
    check("modern is its own quarter turn",
          rotate(start_board("modern"), 1).sq == start_board("modern").sq)
    check("classic is not", rotate(start_board("classic"), 1).sq
          != start_board("classic").sq)


def test_eval():
    section("throwaway eval (deleted at Phase 4)")
    if not HAVE_C:
        check("C core is built", False, "run ./setup.sh")
        return
    rng = random.Random(19)
    bad = 0
    for _ in range(400):
        b = random_position(rng)
        if eval_hand.evaluate(b) != core.evaluate(b):
            bad += 1
    check("Python and C eval agree bit for bit", bad == 0, "%d differ" % bad)
    check("eval is integer only",
          isinstance(eval_hand.evaluate(start_board("classic")), int))

    # Symmetric position, symmetric score.
    check("the start position evaluates to 0 for every setup",
          all(eval_hand.evaluate(start_board(s)) == 0 for s in SETUPS))

    # A queen and a promoted queen on the SAME square attack identically, so
    # the king-danger term cancels exactly and the difference is pure material.
    # (Comparing against an empty square would not: adding a queen anywhere
    # also changes how many squares near the enemy kings are attacked.)
    kings = {"h1": (RED, KING), "a8": (BLUE, KING),
             "g14": (YELLOW, KING), "n7": (GREEN, KING)}
    real = position(dict(kings, **{"h7": (RED, QUEEN)}))
    promoted = position(dict(kings, **{"h7": (RED, PQUEEN)}))
    check("a promoted queen is worth 1 point, not 9 (§8.1)",
          eval_hand.evaluate(real) - eval_hand.evaluate(promoted)
          == eval_hand.PIECE_VALUE[QUEEN] - eval_hand.PIECE_VALUE[PQUEEN],
          "%d vs %d" % (eval_hand.evaluate(real), eval_hand.evaluate(promoted)))
    check("bishop and rook are both worth 5 (§8.1)",
          eval_hand.PIECE_VALUE[BISHOP] == eval_hand.PIECE_VALUE[ROOK] == 500)

    # The score is from the side to move's TEAM, and the team flips every ply,
    # so advancing the turn by one seat must negate it exactly.
    rng = random.Random(23)
    asym = 0
    for _ in range(200):
        b = random_position(rng)
        flipped = b.copy()
        flipped.turn = (b.turn + 1) & 3
        flipped.recompute_key()
        if eval_hand.evaluate(b) != -eval_hand.evaluate(flipped):
            asym += 1
    check("eval negates when the turn advances one seat", asym == 0,
          "%d positions" % asym)


#: Search node counts at fixed depth with a 16 MB table, cleared before each.
#: The eval and the search are integer-only, so unlike a float eval these do
#: not drift across microarchitectures -- but they DO move whenever ordering,
#: pruning or the table changes, which is the point of pinning them.
#: Re-pinned when killers were confirmed into the default (docs/AB.md). A
#: confirmed feature changes the tree, so the pins move with it -- that is the
#: point of re-measuring rather than relaxing them.
SEARCH_PINS = {
    "classic": [40, 194, 1129, 10851, 83805],
    "modern": [40, 148, 1158, 8467, 59969],
    "by": [40, 148, 1211, 8553, 54615],
    "byg": [40, 148, 1211, 10940, 60390],
    "rg": [40, 190, 1237, 12791, 70722],
}


#: Positions compared against the unpruned oracle. The cost per position spans
#: two orders of magnitude -- an unpruned quiescence over an open board with
#: queens is enormous -- so this is deliberately modest; the check is a
#: theorem, and 90 comparisons establish it as well as 400 would.
MINIMAX_POSITIONS = 30


def _minimax_job(index):
    """One seeded sparse position, compared at depths 1-3. Seeded by index so
    a worker can rebuild it without shipping a Board across the pipe."""
    rng = random.Random(3000 + index)
    b = _sparse_teams(rng)
    legal = fast.gen_legal(b)
    if not legal:
        return (0, 0)
    # The oracle is unpruned, including its quiescence, so cost spans two
    # orders of magnitude with the branching factor. Depth 3 only on the
    # quieter positions keeps the tail from dominating the whole run.
    depths = (1, 2, 3) if len(legal) <= 24 else (1, 2)
    tested = bad = 0
    for depth in depths:
        core.clear_hash()
        if core.search(b, depth).score != search.reference_score(b.copy(), depth):
            bad += 1
        tested += 1
    return (tested, bad)


def test_search(workers=1):
    section("search (Phase 3 gate)")
    if not HAVE_C:
        check("C core is built", False, "run ./setup.sh")
        return

    # Killers are CONFIRMED into the default (+50.42 +/- 6.41, docs/AB.md).
    # The pins below are measured with them on; a toggle that silently flipped
    # would make every later A/B a comparison against something nobody
    # measured, so the default itself is pinned.
    check("killers default on (confirmed)", core.killers_enabled())
    check("history default off (dormant)", not core.history_enabled())
    check("pvs default off (dormant)", not core.pvs_enabled())

    core.set_hash(16)
    pins = []
    for setup in SETUPS:
        for i, expect in enumerate(SEARCH_PINS[setup]):
            core.clear_hash()
            pins.append(("%s d%d" % (setup, i + 1),
                         core.search(start_board(setup), i + 1).nodes == expect))
    check_all("pinned search node counts", pins)

    # The correctness theorem for the whole search: alpha-beta with a
    # transposition table must return exactly the plain minimax value. Pinned
    # node counts cannot see a wrong score -- a wrong score still has a count.
    results = pool_map(_minimax_job, list(range(MINIMAX_POSITIONS)), workers)
    tested = sum(n for n, _ in results)
    bad = sum(b for _, b in results)
    check("alpha-beta equals unpruned minimax", bad == 0,
          "%d comparisons, %d mismatches" % (tested, bad))

    # Mate is found and scored from the mating team's point of view.
    mate = position({"a5": (RED, KING), "n9": (YELLOW, KING),
                     "b7": (BLUE, KING), "m7": (GREEN, KING),
                     "d7": (RED, QUEEN), "d8": (RED, ROOK)}, turn=RED)
    core.clear_hash()
    r = core.search(mate, 3)
    check("a forced mate scores as a mate", r.score > 29000 - 100
          or r.score < -(29000 - 100), "score %d" % r.score)

    # The toggle must still reach the search, so it can be turned off for a
    # future A/B. A toggle wired to nothing passes every other check here.
    core.clear_hash()
    on = core.search(start_board("classic"), 5).nodes
    core.set_killers(False)
    core.clear_hash()
    off = core.search(start_board("classic"), 5).nodes
    core.set_killers(True)
    check("the killers toggle still reaches the search", on != off,
          "%d on, %d off" % (on, off))
    check("killers shrink the tree", on < off,
          "%.1f%% of the unordered tree" % (100.0 * on / off))

    # History must also reach the search, and must leave the pinned tree alone
    # while it is off.
    core.clear_hash()
    base = core.search(start_board("classic"), 7).nodes
    core.set_history(True)
    core.clear_hash()
    with_hist = core.search(start_board("classic"), 7).nodes
    core.set_history(False)
    core.clear_hash()
    check("the history toggle reaches the search", with_hist != base,
          "%d on, %d off at depth 7" % (with_hist, base))
    check("history off leaves the pinned tree alone",
          core.search(start_board("classic"), 5).nodes == SEARCH_PINS["classic"][4])

    # PVS must return the SAME score as plain alpha-beta -- it is a node
    # reduction, not a different search. If it ever disagrees, the window
    # handling is wrong and the null-window re-search condition is the suspect.
    core.set_pvs(True)
    same = True
    for setup in SETUPS:
        core.clear_hash()
        a = core.search(start_board(setup), 5).score
        core.set_pvs(False)
        core.clear_hash()
        b_ = core.search(start_board(setup), 5).score
        core.set_pvs(True)
        if a != b_:
            same = False
    core.set_pvs(False)
    check("pvs returns the same score as plain alpha-beta", same)

    core.set_hash(core.DEFAULT_TT_MB)


def _sparse_teams(rng):
    """A few pieces per seat: dense positions make the unpruned reference
    intractable, and it is the reference that has to stay simple."""
    b = Board(MODE_TEAMS)
    free = list(SQUARES)
    rng.shuffle(free)
    for color in range(4):
        b.sq[free.pop()] = make_piece(color, KING)
        for _ in range(rng.randrange(0, 3)):
            ptype = rng.choice((PAWN, KNIGHT, BISHOP, ROOK, QUEEN))
            if ptype == PAWN:
                candidates = [sq for sq in free
                              if 0 < pawn_coord(color, sq) < PROMO_COORD[MODE_TEAMS]]
                if not candidates:
                    continue
                sq = rng.choice(candidates)
                free.remove(sq)
            else:
                sq = free.pop()
            b.sq[sq] = make_piece(color, ptype)
    b.turn = rng.randrange(4)
    b.find_kings()
    b.recompute_key()
    return b


def test_nnue():
    section("NNUE features and net format (Phase 4)")
    from tetrarch import nnue
    import numpy as np

    check("3840 features", nnue.NFEATURES == 3840, str(nnue.NFEATURES))
    indices = set()
    for persp in range(4):
        for color in range(4):
            for ptype in range(7):
                for sq in SQUARES:
                    f = nnue.feature_index(ptype, color, sq, persp)
                    if not 0 <= f < nnue.NFEATURES:
                        check("feature index in range", False, str(f))
                        return
                    indices.add(f)
    check("every feature index is reachable", len(indices) == nnue.NFEATURES,
          str(len(indices)))

    # A promoted queen indexes as a queen: same movement, and its 1-point value
    # is a points matter that reaches the net through the extra inputs (§8.1).
    check("a promoted queen indexes as a queen",
          nnue.feature_index(PQUEEN, RED, sq_from_name("h7"), 0)
          == nnue.feature_index(QUEEN, RED, sq_from_name("h7"), 0))

    # The whole point of one weight set for four seats: rotating the board by k
    # and looking from perspective k must give the identical feature set. This
    # is also where the 4x training augmentation comes from.
    rot = []
    for setup in SETUPS:
        b = start_board(setup)
        sets = [sorted(nnue.active_features(rotate(b, k), k)) for k in range(4)]
        rot.append((setup, all(s == sets[0] for s in sets) and len(sets[0]) == 64))
    check_all("one weight set serves all four seats", rot)

    rng = random.Random(31)
    bad = 0
    for _ in range(80):
        b = random_position(rng)
        sets = [sorted(nnue.active_features(rotate(b, k), k)) for k in range(4)]
        if any(s != sets[0] for s in sets):
            bad += 1
    check("rotation invariance holds on random positions", bad == 0,
          "%d positions" % bad)

    # Extra inputs: alive mask and points differentials, rotated to perspective.
    b = position({"h1": (RED, KING), "a8": (BLUE, KING), "g14": (YELLOW, KING),
                  "n7": (GREEN, KING)}, alive=(1, 1, 0, 1))
    b.points = [10, 4, 0, 1]
    check("extras carry the alive mask rotated",
          nnue.extra_inputs(b, 0)[:4] == [1, 1, 0, 1])
    check("extras rotate with the perspective",
          nnue.extra_inputs(b, 1)[:4] == [1, 0, 1, 1])
    check("extras carry points differentials",
          nnue.extra_inputs(b, 0)[4:] == [10 - 4, 10 - 0, 10 - 1])

    if HAVE_C:
        # The whole point of the C inference: it must agree with the reference
        # forward pass exactly, not approximately. Integer arithmetic
        # throughout, so "close" would mean a real bug.
        probe = nnue.Net.random(seed=4)
        core.load_net(probe)
        check("loading a net switches the C eval", core.net_loaded())
        rng = random.Random(41)
        bad = 0
        for _ in range(250):
            b = random_position(rng)
            if probe.evaluate(b) != core.evaluate(b):
                bad += 1
        check("C and Python NNUE eval agree bit for bit", bad == 0,
              "%d of 250 differ" % bad)

        # Swapping the eval must invalidate the table. Every stored score came
        # from the other evaluation function, and a stale probe would serve it
        # straight back -- which is silent, and would corrupt an A/B.
        core.set_hash(1)
        core.clear_hash()
        start = start_board("classic")
        core.search(start, 5)
        core.unload_net()
        hand_nodes = core.search(start, 5).nodes
        core.load_net(probe)
        core.unload_net()
        again = core.search(start, 5).nodes
        check("loading a net clears the transposition table",
              hand_nodes == again and hand_nodes > 1000,
              "%d then %d" % (hand_nodes, again))
        core.set_hash(core.DEFAULT_TT_MB)

        core.unload_net()
        check("unloading restores the hand eval",
              not core.net_loaded()
              and core.evaluate(start) == eval_hand.evaluate(start))

    net = nnue.Net.random(seed=1)
    path = os.path.join(SCRATCH, "selftest-net.nnue")
    net.save(path)
    back = nnue.Net.load(path)
    check("net round trips through the file format",
          all(np.array_equal(getattr(net, a), getattr(back, a))
              for a in ("w1", "b1", "w2", "b2", "w3", "b3", "w4", "b4")))
    check("net evaluation is an int",
          isinstance(net.evaluate(start_board("classic")), int))
    # Same position, four perspectives, one weight set: identical score.
    start = start_board("modern")
    scores = [net.evaluate(rotate(start, k), k) for k in range(4)]
    check("one weight set gives one score from all four seats",
          len(set(scores)) == 1, str(scores))
    os.remove(path)


#: The example from the Wikibooks notation page -- real chess.com output, and
#: the era when `classic` was the default. `Qa7-b8` and `Qn8-m8` only resolve
#: on classic, which is an independent check of §3.
WIKIBOOK_PGN4 = """[Variant "Teams"]
[Result "0-1"]
[Site "www.chess.com/4-player-chess"]

1. d2-d4 .. b8-c8 .. k13-k11 .. m8-l8
2. d4-d5 .. b4-d4 .. k11-k10 .. Qn8-m8
3. e2-e4 .. Qa7-b8 .. g13-g12 .. Nn5-l6
"""


def test_match_rotation():
    section("match.py seat rotation (A/B validity)")
    import match

    # The seat rotation only cancels bias if engine A ends up with each
    # ORIGINAL team twice. Rotating the board and the team assignment together
    # cancels nothing -- that shipped, and measured +36 Elo in a null self-test
    # with the same engine on both sides.
    seen = {0: 0, 1: 0}
    for rotation in range(match.ROTATIONS):
        original_team = (rotation >> 1) & 1
        a_team = original_team ^ (rotation & 1)
        seats = [s for s in range(4) if (s & 1) == a_team]
        armies = sorted(((s - rotation) & 3) for s in seats)
        check("rotation %d gives A a whole team" % rotation,
              (armies[0] & 1) == (armies[1] & 1),
              "armies %s" % [SEAT_NAMES[c] for c in armies])
        seen[armies[0] & 1] += 1
    check("A plays each original team exactly twice",
          seen[0] == seen[1] == match.ROTATIONS // 2,
          "team0 %d, team1 %d" % (seen[0], seen[1]))

    # And the four rotations must be four distinct games, or the sample is
    # half the size it claims.
    # A 180-degree rotation swaps R with Y and B with G. Both members of a
    # team are played by the same engine, but the turn order is R,B,Y,G, so
    # the army that moved first now moves third -- a different game, not a
    # relabelling.
    base = start_board("classic")
    fens = {rotate(base, k).to_fen4() for k in range(match.ROTATIONS)}
    check("even the symmetric start gives four distinct rotations",
          len(fens) == match.ROTATIONS, "%d distinct" % len(fens))
    played = base.copy()
    rng = random.Random(9)
    for _ in range(8):
        played.make(rng.choice(fast.gen_legal(played)))
    fens = {rotate(played, k).to_fen4() for k in range(match.ROTATIONS)}
    check("a real opening gives four distinct rotations",
          len(fens) == match.ROTATIONS, "%d distinct" % len(fens))


def test_js_replay():
    section("standalone viewer replayer (gui/viewer.html)")
    import json
    import shutil
    import subprocess
    from tetrarch import pgn4

    node = shutil.which("node")
    if not node:
        check("node available for the JS differential", True,
              "skipped: node is not installed and is not a dependency")
        return

    # The viewer replays PGN4 in the browser with no server, so a second
    # implementation of "apply this move" exists. It generates no moves and
    # tests no legality, but castling, en passant, promotion and seat
    # elimination are real rules -- and a second implementation of a rule
    # drifts unless something compares them.
    rng = random.Random(21)
    cases = []
    for setup in SETUPS:
        b = start_board(setup)
        moves = []
        for _ in range(60):
            legal = fast.gen_legal(b)
            if not legal:
                break
            m = rng.choice(legal)
            moves.append(m)
            b.make(m)
        text = pgn4.write(start_board(setup), moves, {"Result": "*"})
        frames, _ = pgn4.replay(pgn4.parse(text))

        def key(frame):
            fen = frame["fen4"]
            cut = fen.rfind("-")
            meta = fen[:cut].split("-")
            # Board, turn and alive only: the viewer tracks no castling rights
            # and no halfmove clock, and does not need them to replay.
            return "%s|%s|%s" % (meta[0], meta[1], fen[cut + 1:])

        cases.append({"setup": setup, "pgn4": text,
                      "frames": [key(f) for f in frames]})

    path = os.path.join(SCRATCH, "tetrarch-js-cases.json")
    with open(path, "w") as fh:
        json.dump(cases, fh)
    try:
        proc = subprocess.run([node, "tests/js_replay_check.js", path],
                              capture_output=True, text=True, timeout=120)
        report = json.loads(proc.stdout or "{}")
    except Exception as exc:                                 # noqa: BLE001
        check("the JS replayer matches the Python one", False, repr(exc))
        return
    finally:
        os.remove(path)

    check("the JS replayer matches the Python one",
          proc.returncode == 0 and not report.get("failures"),
          "%d frames compared%s" % (report.get("compared", 0),
                                    "; " + report["failures"][0]
                                    if report.get("failures") else ""))


def test_pgn4():
    section("PGN4 (§11.5)")
    from tetrarch import pgn4

    game = pgn4.parse(WIKIBOOK_PGN4)
    check("tags parse", game.tags.get("Result") == "0-1")
    check("Variant Teams maps to the Teams mode", game.mode == MODE_TEAMS)
    check("12 move tokens", len(game.tokens) == 12, str(len(game.tokens)))
    frames, terminations = pgn4.replay(game)
    check("real chess.com movetext replays", len(frames) == 13,
          str(len(frames)))
    check("and it only resolves on classic", game.setup == "classic")
    check("no terminations in an unfinished game", terminations == [])

    # Write then read: every move must survive as the same move.
    rng = random.Random(5)
    trips = []
    for setup in SETUPS:
        b = start_board(setup)
        moves = []
        for _ in range(30):
            legal = fast.gen_legal(b)
            if not legal:
                break
            m = rng.choice(legal)
            moves.append(m)
            b.make(m)
        text = pgn4.write(start_board(setup), moves, {"Result": "*"})
        back = pgn4.parse(text)
        replayed, _ = pgn4.replay(back)
        trips.append((setup, len(back.tokens) == len(moves)
                      and replayed[-1]["fen4"] == b.to_fen4().replace("\n", "")))

    check_all("PGN4 round trips on every setup", trips)

    # Terminators are seat eliminations, not moves (§9).
    ended = pgn4.parse(WIKIBOOK_PGN4 + "4. R .. T\n")
    check("resign and timeout tokens are read", ended.tokens[-2:] == ["R", "T"])
    frames, terminations = pgn4.replay(ended)
    check("a terminator eliminates its seat",
          [t["reason"] for t in terminations] == ["resign", "timeout"],
          str(terminations))
    check("and the eliminated seats are marked dead",
          frames[-1]["alive"].count(False) == 2, str(frames[-1]["alive"]))

    # A move that is not legal names the ply it failed at rather than vanishing.
    try:
        pgn4.replay(pgn4.parse('[Variant "Teams"]\n\n1. d2-d9\n'))
        check("an illegal move is reported", False)
    except pgn4.Pgn4Error as exc:
        check("an illegal move is reported", "not legal" in str(exc))

    # Long algebraic always carries both squares, so a two-digit rank must not
    # be mis-split.
    b = start_board("classic")
    b.turn = 2
    b.recompute_key()
    token = pgn4.move_token(b, next(m for m in fast.gen_legal(b)
                                    if move_str(m) == "k13k11"))
    check("two-digit ranks survive tokenising", token == "k13-k11", token)


def perft_deep():
    section("deep perft -- all setups, both modes, to depth 5")
    print("  %-9s %-6s %12s %12s %8s" % ("setup", "mode", "depth 4", "depth 5",
                                         "secs"))
    rows = []
    for setup in SETUPS:
        for mode in (MODE_TEAMS, MODE_FFA):
            b = start_board(setup, mode)
            t = time.time()
            d4 = fast.perft(b.copy(), 4)
            d5 = fast.perft(b.copy(), 5)
            secs = time.time() - t
            print("  %-9s %-6s %12d %12d %8.1f"
                  % (setup, MODE_NAMES[mode], d4, d5, secs))
            rows.append((setup, MODE_NAMES[mode], d4, d5))
    check("modern/teams depth 5 matches Athena",
          ("modern", "teams", 152050, 3452310) in rows)
    return rows


# --- 7. movegen cross-check -------------------------------------------------

#: the two home central squares each seat's king can hold rights on
KING_HOMES = {}
for _color, _sq in CASTLE_GEO:
    KING_HOMES.setdefault(_color, []).append(_sq)
ROOKS_OF = {}
for _sq, _color in ROOK_HOME.items():
    ROOKS_OF.setdefault(_color, []).append(_sq)
del _color, _sq


def random_position(rng):
    """A random position, from one of two sources.

    Playouts reach castling and en passant that scattering never would;
    scattering reaches promotion ranks, dead seats and multi-check that a
    40-ply playout never would. Neither alone covers the rule surface, so the
    cross-check draws from both and asserts afterwards that it saw each.
    """
    mode = rng.choice((MODE_FFA, MODE_TEAMS))
    if rng.random() < 0.35:
        b = start_board(rng.choice(SETUPS), mode)
        for _ in range(rng.randrange(0, 45)):
            moves = fast.gen_legal(b)
            if not moves:
                break
            b.make(rng.choice(moves))
        return b

    b = Board(mode)
    alive = [rng.random() > 0.25 for _ in range(4)]
    if sum(alive) < 2:
        alive = [True] * 4
    b.alive = alive
    b.turn = rng.choice([c for c in range(4) if alive[c]])

    # Put some seats fully at home so castling rights can be real.
    at_home = {c for c in range(4) if rng.random() < 0.55}
    used = set()
    for color in at_home:
        king = rng.choice(KING_HOMES[color])
        b.sq[king] = make_piece(color, KING)
        used.add(king)
        for rook in ROOKS_OF[color]:
            if rng.random() < 0.75:
                b.sq[rook] = make_piece(color, ROOK)
                used.add(rook)

    free = [sq for sq in SQUARES if sq not in used]
    rng.shuffle(free)
    for color in range(4):
        if color not in at_home:
            b.sq[free.pop()] = make_piece(color, KING)
        for _ in range(rng.randrange(0, 8)):
            if not free:
                break
            ptype = rng.choice((PAWN, PAWN, KNIGHT, BISHOP, ROOK, QUEEN, PQUEEN))
            if ptype == PAWN:
                # A pawn cannot stand on its own back rank or past promotion.
                candidates = [sq for sq in free
                              if 0 < pawn_coord(color, sq) < PROMO_COORD[mode]]
                if not candidates:
                    continue
                sq = rng.choice(candidates)
                free.remove(sq)
            else:
                sq = free.pop()
            b.sq[sq] = make_piece(color, ptype)
    if rng.random() < 0.15 and free:
        b.sq[free.pop()] = make_piece(DEAD_UNKNOWN, rng.choice(
            (PAWN, KNIGHT, BISHOP, ROOK, QUEEN)))

    b.find_kings()
    for color in range(4):
        sides = CASTLE_GEO.get((color, b.kings[color]))
        if sides is None:
            continue
        for side, arr in ((0, b.ck), (1, b.cq)):
            if b.sq[sides[side][0]] == make_piece(color, ROOK):
                arr[color] = rng.random() < 0.75

    # An en-passant offer, planted along with a pawn placed to accept it --
    # random scatter essentially never produces the pair by itself.
    if rng.random() < 0.5:
        pusher = rng.randrange(4)
        push = PAWN_PUSH[pusher]
        landings = [sq for sq in free
                    if pawn_coord(pusher, sq) == b.pawn_base_rank + 1
                    and VALID[(sq - push) & 255]
                    and not b.sq[(sq - push) & 255]]
        if landings:
            landed = rng.choice(landings)
            target = (landed - push) & 255
            b.sq[landed] = make_piece(pusher, PAWN)
            b.ep[pusher] = (target, landed)
            takers = [c for c in range(4) if c != pusher and alive[c]]
            rng.shuffle(takers)
            for taker in takers:
                spots = [(target - d) & 255 for d in PAWN_TAKES[taker]]
                spots = [s for s in spots if VALID[s] and not b.sq[s]
                         and 0 < pawn_coord(taker, s) < PROMO_COORD[mode]]
                if spots:
                    b.sq[rng.choice(spots)] = make_piece(taker, PAWN)
                    b.turn = taker
                    break

    b.find_kings()
    b.recompute_key()
    return b


STAT_KINDS = ("ep", "promo", "castle", "check", "dead")


def crosscheck_chunk(args):
    """Run one independent slice of the cross-check. Top level, so it pickles.

    Chunks are seeded independently and share nothing, so there is no queue to
    deadlock and no ordering to preserve.
    """
    count, seed = args
    rng = random.Random(seed)
    disagreements = []
    stats = dict.fromkeys(STAT_KINDS, 0)
    empty = 0
    i = 0
    while i < count:
        b = random_position(rng)
        # Walk a short playout from each seed position too: playouts reach
        # castling and en-passant sequences that scattering rarely produces.
        for _ in range(rng.randrange(1, 5)):
            if i >= count:
                break
            i += 1
            try:
                a = sorted(fast.gen_legal(b))
                c = sorted(slow.gen_legal(b))
                d = sorted(core.gen_legal(b)) if HAVE_C else a
            except Exception as exc:                     # noqa: BLE001
                disagreements.append((b.to_fen4(), "exception: %r" % (exc,)))
                break
            if a != c or a != d:
                who = "slow" if a != c else "C"
                other = c if a != c else d
                only_a = [move_str(m) for m in a if m not in other]
                only_o = [move_str(m) for m in other if m not in a]
                disagreements.append(
                    (b.to_fen4(), "fast-only=%s %s-only=%s"
                     % (only_a, who, only_o)))
                if len(disagreements) > 4:
                    break
            if not a:
                empty += 1
                break
            for m in a:
                f = mv_flag(m)
                if f == F_EP:
                    stats["ep"] += 1
                elif f in (F_CASTLE_SHORT, F_CASTLE_LONG):
                    stats["castle"] += 1
                if mv_promo(m):
                    stats["promo"] += 1
            if fast.in_check(b, b.turn):
                stats["check"] += 1
            if not all(b.alive):
                stats["dead"] += 1
            b.make(rng.choice(a))
    return i, stats, disagreements, empty


def crosscheck(count, seed=0, workers=1, quiet=False):
    """Compare both generators over `count` random positions.

    `workers` 0 means every core. Splitting is by seed, so a run with W workers
    covers different positions than a run with one -- the seed and worker count
    together identify the sample, and both are printed.
    """
    import multiprocessing
    import os

    nproc = (os.cpu_count() or 1) if workers == 0 else max(1, workers)
    section("movegen cross-check over %d positions (%d proc, seed %d)"
            % (count, nproc, seed))

    if nproc == 1:
        chunks = [(count, seed)]
    else:
        size = max(500, count // (nproc * 8))
        chunks = []
        remaining = count
        while remaining > 0:
            take = min(size, remaining)
            chunks.append((take, seed * 1000003 + len(chunks)))
            remaining -= take

    stats = dict.fromkeys(STAT_KINDS, 0)
    disagreements = []
    done = empty = 0
    started = time.time()

    def absorb(result):
        nonlocal done, empty
        n, s, d, e = result
        done += n
        empty += e
        disagreements.extend(d)
        for k in STAT_KINDS:
            stats[k] += s[k]

    if nproc == 1:
        absorb(crosscheck_chunk(chunks[0]))
    else:
        with multiprocessing.Pool(nproc) as pool:
            noisy = not quiet and count >= 20000
            for result in pool.imap_unordered(crosscheck_chunk, chunks):
                absorb(result)
                if noisy:
                    rate = done / max(1e-9, time.time() - started)
                    left = (count - done) / max(rate, 1e-9)
                    print("    %d/%d  %.0f pos/s  eta %dm%02ds"
                          % (done, count, rate, left // 60, left % 60),
                          flush=True)

    secs = time.time() - started
    ok = check("generators agree over %d positions" % count, not disagreements,
               "%d disagreements" % len(disagreements))
    for fen, why in disagreements[:3]:
        print("    %s\n%s" % (why, fen))
    print("        %d positions in %.1fs (%.0f/s), %d terminal; %s"
          % (done, secs, done / max(secs, 1e-9), empty,
             ", ".join("%s=%d" % kv for kv in sorted(stats.items()))))
    for kind in STAT_KINDS:
        check("cross-check exercised %s" % kind, stats[kind] > 0)
    return ok


# --- main -------------------------------------------------------------------

def banner():
    import platform
    print("== Tetrarch selftest ==\n")
    bits = ["python %s" % platform.python_version(),
            "%s %s" % (platform.system(), platform.machine())]
    if HAVE_C:
        bits.append("C core loaded")
    else:
        bits.append("NO C CORE -- run ./setup.sh")
    print(" | ".join(bits))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--crosscheck", type=int, default=3000, metavar="N",
                    help="random positions through both movegens (default 3000)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4, metavar="N",
                    help="worker processes (default 4); 0 means every core")
    ap.add_argument("--perft", type=int, default=4, metavar="D",
                    help="perft depth for the pinned check (default 4)")
    ap.add_argument("--perft-deep", action="store_true",
                    help="perft 5 for every setup and mode; takes minutes")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    started = time.time()
    workers = (os.cpu_count() or 1) if args.workers == 0 else max(1, args.workers)
    banner()
    test_geometry()
    test_setups()
    test_fen4()
    test_make_unmake()
    test_pawn_directions()
    test_en_passant()
    test_multi_check()
    test_dead_seats()
    test_castling()
    test_perft(args.perft, workers)
    test_c_core(args.perft_deep)
    test_rotation()
    test_eval()
    test_search(workers)
    test_nnue()
    test_match_rotation()
    test_pgn4()
    test_js_replay()
    if args.crosscheck:
        crosscheck(args.crosscheck, args.seed, workers, args.quiet)
    if args.perft_deep:
        perft_deep()

    end_sections()
    elapsed = time.time() - started
    if FAILURES:
        print("\n== FAILED: %d of %d check(s): %s =="
              % (len(FAILURES), CHECKS[0], ", ".join(FAILURES[:6])
                 + (" and %d more" % (len(FAILURES) - 6) if len(FAILURES) > 6
                    else "")))
        return 1
    print("\n== ALL CHECKS PASSED ==  (%d checks, %.1fs, %d workers)"
          % (CHECKS[0], elapsed, workers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
