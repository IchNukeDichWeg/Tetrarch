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
import json
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
from tetrarch.board import SQUARES as SQUARES_ALL
from tetrarch import search
from tetrarch import pgn4

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

#: The §3.3 table, transcribed from the live lobby.
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
    check_all("king squares match the live lobby", kings)
    check_all("queen squares match the live lobby", queens)
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
    # Both toggle states: the shipped default AND the one being measured. A
    # comparison that only covers the state nobody is running is no gate.
    rows = []
    for on in (False, True):
        eval_hand.USE_MOBILITY = on
        core.set_mobility(on)
        rng = random.Random(19)
        bad = 0
        for _ in range(250):
            b = random_position(rng)
            if eval_hand.evaluate(b) != core.evaluate(b):
                bad += 1
        rows.append(("mobility %s" % ("on" if on else "off"), bad == 0))
    eval_hand.USE_MOBILITY = False
    core.set_mobility(False)
    check_all("Python and C eval agree bit for bit", rows)
    check("mobility defaults off, in both", not core.mobility_enabled()
          and not eval_hand.USE_MOBILITY)

    # The term has to be bounded, or the lazy-eval margin built on it is a
    # guess and the cutoff guarantee goes with it.
    rng = random.Random(23)
    worst = 0
    for _ in range(120):
        b = random_position(rng)
        for sq in SQUARES_ALL:
            piece = b.sq[sq]
            if piece:
                worst = max(worst, eval_hand.piece_mobility(b, sq, piece))
    check("no piece ever exceeds the mobility cap",
          worst <= eval_hand.MOBILITY_CAP,
          "worst seen %d, cap %d" % (worst, eval_hand.MOBILITY_CAP))
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

    # In TEAMS the score is from the side to move's team and the team flips
    # every ply, so advancing the turn by one seat must negate it exactly.
    # That property is what lets the search be plain negamax, so it is pinned.
    #
    # It does NOT hold in FFA, and must not: there a seat is its own team, so
    # the score reads "me against the other three" and the next seat's score is
    # a different quantity, not the negative of this one. That is precisely why
    # FFA cannot use negamax and needs a paranoid search. The check below used
    # to run on both modes and passed only because the evaluation asked for
    # seat parity in FFA too, which was wrong and invisible.
    rng = random.Random(23)
    asym = ffa_seen = ffa_asym = 0
    teams_seen = 0
    for _ in range(200):
        b = random_position(rng)
        flipped = b.copy()
        flipped.turn = (b.turn + 1) & 3
        flipped.recompute_key()
        negates = eval_hand.evaluate(b) == -eval_hand.evaluate(flipped)
        if b.mode == MODE_TEAMS:
            teams_seen += 1
            if not negates:
                asym += 1
        else:
            ffa_seen += 1
            if not negates:
                ffa_asym += 1
    check("FFA scores each seat against the other three, so it does not negate",
          ffa_asym > 0, "%d of %d FFA positions" % (ffa_asym, ffa_seen))
    check("the sample covered both modes", teams_seen > 0 and ffa_seen > 0)
    check("eval negates when the turn advances one seat, in Teams", asym == 0,
          "%d positions" % asym)


#: Search node counts at fixed depth with a 16 MB table, cleared before each.
#: The eval and the search are integer-only, so unlike a float eval these do
#: not drift across microarchitectures -- but they DO move whenever ordering,
#: pruning or the table changes, which is the point of pinning them.
#: Re-pinned whenever a feature is confirmed into the default (docs/AB.md).
#: Killers, LMR, LMP, then quiescence evasions. A confirmed feature changes
#: the tree, so the pins move
#: with it -- the point is to re-measure, never to relax them.
#: Re-pinned when SEEPrune became the default. Depths 1-3 are unmoved, since
#: quiescence barely runs that shallow; from depth 4 the tree is three to four
#: times smaller, which is the feature working and is worth 16.32 Elo at fixed
#: nodes (docs/AB.md). A change here is either a deliberate default flip or a
#: bug, and never a wash.
SEARCH_PINS = {
    "classic": [40, 84, 311, 971, 5982],
    "modern": [40, 86, 322, 775, 6527],
    "by": [40, 86, 322, 1217, 4789],
    "byg": [40, 86, 322, 1217, 5371],
    "rg": [40, 80, 342, 1088, 5155],
}


#: Positions compared against the unpruned oracle. The cost per position spans
#: two orders of magnitude -- an unpruned quiescence over an open board with
#: queens is enormous -- so this is deliberately modest; the check is a
#: theorem, and 90 comparisons establish it as well as 400 would.
#: Raised from 30 when SEEPrune showed the gate could be blind. A toggle
#: wrongly left out of INEXACT_TOGGLES disagrees with the oracle on about 2% of
#: comparisons (measured: 7 of 360), so 77 comparisons caught it four times in
#: five and 30 positions was a coin-flip away from silence. This costs a few
#: seconds and buys a gate that actually fires.
MINIMAX_POSITIONS = 90


#: Every toggle that makes the search return something other than the exact
#: minimax value. The oracle compares against unpruned minimax, so all of them
#: have to be off for the comparison to mean anything -- and the list has to be
#: declared in one place, because the failure mode when a new one is added is
#: silent. SEEPrune was default-on for a day before anyone checked, and the
#: comparison it invalidated kept passing: 7 of 360 positions disagree with the
#: oracle when it is on, and selftest sampled around them by skipping depth 3
#: on the busier positions.
INEXACT_TOGGLES = (
    ("lmr", core.set_lmr, core.lmr_enabled),
    ("lmp", core.set_lmp, core.lmp_enabled),
    ("see prune", core.set_see_prune, core.see_prune_enabled),
)


def _minimax_job(index):
    """One seeded sparse position, compared at depths 1-3. Seeded by index so
    a worker can rebuild it without shipping a Board across the pipe."""
    restore = [(setter, getter()) for _n, setter, getter in INEXACT_TOGGLES]
    for _n, setter, _g in INEXACT_TOGGLES:
        setter(False)
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
    for setter, was in restore:
        setter(was)
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
    check("lmr default on (confirmed)", core.lmr_enabled())
    check("lazy eval default on (confirmed)", core.lazy_eval_enabled())
    check("lmp default on (confirmed)", core.lmp_enabled())
    check("qsearch evasions default on (confirmed)",
          core.qs_evasions_enabled())
    check("repetition detection default on", core.rep_detect_enabled())
    check("SEE ordering default off (dormant)", not core.see_order_enabled())
    check("SEE pruning default on (confirmed)", core.see_prune_enabled())

    # A dormant toggle that does nothing when switched on is worse than no
    # toggle: the A/B would measure two identical engines and report a null.
    moved = []
    for name, setter, default in (("SEEOrder", core.set_see_order, False),
                                  ("SEEPrune", core.set_see_prune, True)):
        setter(default)
        core.clear_hash()
        before = core.search(start_board("classic"), 6).nodes
        setter(not default)
        core.clear_hash()
        after = core.search(start_board("classic"), 6).nodes
        setter(default)
        moved.append((name, before != after))
    check_all("each SEE toggle changes the search when enabled", moved)

    # The legality fast path decides most moves are legal without making them,
    # from the pins computed once per node. It is one-sided by construction --
    # it answers "certainly legal" or "do not know" -- so the only way it can
    # be wrong is by calling a move legal that the full attack scan rejects.
    # perft gates gen_legal exactly, but the search has its own three copies of
    # the filter, so the two answers are compared directly on every move of
    # every position rather than sampled.
    rng = random.Random(5150)
    bad = seen = in_check = 0
    for _ in range(120):
        b = start_board(rng.choice(SETUPS))
        for _ in range(rng.randrange(0, 45)):
            moves = list(fast.gen_legal(b))
            if not moves:
                break
            b.make(rng.choice(moves))
            bad += core.legality_disagreements(b)
            seen += 1
            if fast.in_check(b, b.turn):
                in_check += 1
    check("legality fast path never contradicts the scan", bad == 0,
          "%d positions, %d of them in check" % (seen, in_check))
    check("and the sample reached positions in check", in_check > 0)

    # Random play does not reach the one case the fast path has to decline.
    # 89,819 sampled positions produced 834 with an en passant available and
    # not one where taking it exposed the king, so the exclusion of F_EP was
    # unfalsifiable by sampling and is pinned by construction instead.
    #
    # King, our capturing pawn, the victim pawn and an enemy rook share the
    # d-file. En passant empties BOTH pawn squares, and the pin scan cannot
    # see it: the scan stops at our own pawn on d6 and never learns that d7
    # is about to empty as well.
    ep_case = ("R-0,0,1,1-0,0,0,0-0,0,0,0-0,0,0,0-0-"
               "{'enPassant':('','c7:d7','','')}-"
               "3,dyR,dyN,dyB,dyK,dyQ,dyB,dyN,dyR,3/"
               "3,dyP,dyP,dyP,dyP,dyP,dyP,dyP,dyP,3/14/"
               "bR,bP,1,bR,8,dgP,dgR/bN,bP,10,dgP,dgN/bB,bP,10,dgP,dgB/"
               "bK,bP,10,dgP,dgQ/bQ,bP,1,bP,8,dgP,dgK/bB,bP,1,rP,8,dgP,dgB/"
               "bN,bP,10,dgP,dgN/bR,bP,1,rK,8,dgP,dgR/14/"
               "3,rP,rP,rP,rP,rP,rP,rP,rP,3/3,rR,rN,rB,rQ,rK,rB,rN,rR,3")
    epb = Board.from_fen4(ep_case, MODE_TEAMS)
    ep_moves = [move_str(m) for m in fast.gen_pseudo(epb)
                if mv_flag(m) == F_EP]
    check("the en passant case still offers the capture", ep_moves == ["d6c7"],
          str(ep_moves))
    check("and taking it is illegal, by discovered check",
          "d6c7" not in [move_str(m) for m in fast.gen_legal(epb)])
    check("the fast path declines it rather than allowing it",
          core.legality_disagreements(epb) == 0)

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
    # The oracle is plain minimax, so the inexact reductions have to be off for
    # the comparison to mean anything.
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

    # LMR is NOT exact, so it gets no score-equality check -- only that it
    # reaches the search, shrinks the tree, and leaves the pins alone when off.
    core.clear_hash()
    reduced = core.search(start_board("classic"), 6).nodes
    core.set_lmr(False)
    core.clear_hash()
    full = core.search(start_board("classic"), 6).nodes
    core.set_lmr(True)
    check("the lmr toggle still reaches the search", reduced != full,
          "%d on, %d off at depth 6" % (reduced, full))
    check("lmr shrinks the tree", reduced < full,
          "%.1f%% of the unreduced tree" % (100.0 * reduced / full))

    # Lazy eval is a speed change, so the thing to pin is that it does NOT
    # move the tree. It is not exact by construction -- a bail returns the
    # material term rather than the true eval -- so this is an empirical
    # property worth watching rather than a guarantee.
    moved = []
    for setup in SETUPS:
        core.set_lazy_eval(False)
        core.clear_hash()
        plain = core.search(start_board(setup), 5).nodes
        core.set_lazy_eval(True)
        core.clear_hash()
        lazy = core.search(start_board(setup), 5).nodes
        moved.append((setup, plain == lazy))
    check_all("lazy eval leaves the tree unchanged", moved)

    # LMP drops moves outright. It may MISS a mate -- that is the trade -- but
    # it must never INVENT one, which is what would happen if a node pruned
    # every move and then reported no legal moves.
    rng = random.Random(88)
    invented = compared = 0
    for _ in range(120):
        b = random_position(rng)
        if b.mode != MODE_TEAMS or not all(b.alive) or not fast.gen_legal(b):
            continue
        compared += 1
        core.set_lmp(False)
        core.clear_hash()
        plain = core.search(b, 4).score
        core.set_lmp(True)
        core.clear_hash()
        pruned = core.search(b, 4).score
        if abs(pruned) >= 29900 and abs(plain) < 29900:
            invented += 1
    core.set_lmp(True)          # restore the confirmed default
    check("lmp never invents a mate", invented == 0,
          "%d of %d positions" % (invented, compared))
    core.clear_hash()
    check("toggling lmp returns to the pinned tree",
          core.search(start_board("classic"), 5).nodes == SEARCH_PINS["classic"][4])

    # Quiescence cannot stand pat while in check: it scores lost positions as
    # quiet and cannot see a mate at all. Asserted as a property over in-check
    # positions rather than one hand-built fixture -- a fixture that stops
    # being mate tests nothing and says nothing about why.
    rng = random.Random(5)
    only_with = examined = 0
    for _ in range(400):
        if examined >= 40:
            break
        b = random_position(rng)
        if b.mode != MODE_TEAMS or not all(b.alive) or not fast.gen_legal(b):
            continue
        if not any(fast.in_check(b, c) for c in range(4)):
            continue
        examined += 1
        core.set_qs_evasions(False)
        core.clear_hash()
        without = core.search(b, 2).score
        core.set_qs_evasions(True)
        core.clear_hash()
        with_ev = core.search(b, 2).score
        if abs(with_ev) >= 29900 and abs(without) < 29900:
            only_with += 1
    core.set_qs_evasions(True)           # restore the default
    check("quiescence sees mates only with evasions on", only_with > 0,
          "%d of %d in-check positions" % (only_with, examined))
    core.clear_hash()
    check("qsearch evasions on matches the pinned tree",
          core.search(start_board("classic"), 5).nodes == SEARCH_PINS["classic"][4])

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


def test_repetition():
    section("repetition (10.2)")
    if not HAVE_C:
        check("C core is built", False, "run ./setup.sh")
        return

    # The search cannot be caught repeating by simply searching deeper: all
    # four seats have to move and return for a position to recur, so the
    # shallowest real repetition is eight plies down and costs more than this
    # file should spend. Instead the played-game history is the lever -- a key
    # injected there is indistinguishable, inside the search, from a position
    # that genuinely occurred, and it bites at ply 1.
    #
    # Where it is injected matters. The side to move is part of the key, so a
    # position can only recur a whole seat cycle back; the scan knows that and
    # strides four plies at a time. A key planted anywhere else is one the
    # search is right to ignore, so PAD puts it exactly four plies above the
    # node that must see it.
    core.set_hash(16)
    rng = random.Random(7)
    cases = []
    for _ in range(40):
        b = start_board("classic")
        for _ in range(rng.randrange(4, 20)):
            moves = list(fast.gen_legal(b))
            if not moves:
                break
            b.make(rng.choice(moves))
        if not list(fast.gen_legal(b)):
            continue
        b.halfmove = 40                      # a long reversible stretch
        core.clear_hash()
        plain = core.search(b, 4)
        if not plain.best:
            continue
        # A pawn move or a capture zeroes the clock, so no earlier position can
        # be reached across it. Only a quiet best move tests anything.
        if PC_TYPE[b.sq[mv_from(plain.best)]] == PAWN or b.sq[mv_to(plain.best)]:
            continue
        u = b.make(plain.best)
        child = b.key
        b.unmake(plain.best, u)
        # rep_root = len(history); the root sits at index len(history) and the
        # child at len(history) + 1, so a stride of four lands on index 1.
        history = [0, child, 0, 0]
        cases.append((b, plain, child, history))
        if len(cases) == 5:
            break

    check("found positions whose best move is quiet", len(cases) == 5,
          "got %d" % len(cases))

    drawn, unrelated, bounded, offcycle = [], [], [], []
    for b, plain, child, history in cases:
        name = move_str(plain.best)
        plainly = (plain.best, plain.score)

        core.clear_hash()
        seen = core.search(b, 4, repetitions=history)
        drawn.append((name, seen.best != plain.best or seen.score == 0))

        # A key on no path at all must leave the search untouched, or the check
        # above would pass for a search merely disturbed by any history.
        core.clear_hash()
        other = core.search(b, 4, repetitions=[0, child ^ 0x9E3779B97F4A7C15,
                                               0, 0])
        unrelated.append((name, (other.best, other.score) == plainly))

        # The real key one ply off the seat cycle is a position with a
        # different side to move, so it cannot be the same position.
        core.clear_hash()
        skew = core.search(b, 4, repetitions=[0, 0, child, 0])
        offcycle.append((name, (skew.best, skew.score) == plainly))

        # The same real key, now out of the clock's reach, must also do
        # nothing: that bound is what stops a repetition being seen across a
        # capture or a pawn move.
        b.halfmove = 0
        core.clear_hash()
        cut = core.search(b, 4, repetitions=history)
        b.halfmove = 40
        bounded.append((name, (cut.best, cut.score) == plainly))

    check_all("a repeated position scores the draw", drawn)
    check_all("a key on no path changes nothing", unrelated)
    check_all("a key off the seat cycle changes nothing", offcycle)
    check_all("the halfmove clock bounds the scan", bounded)


def test_see():
    section("static exchange evaluation")
    if not HAVE_C:
        check("C core is built", False, "run ./setup.sh")
        return

    def pos(extra, turn=RED):
        b = Board()
        b.mode = MODE_TEAMS
        for c in range(4):
            b.alive[c] = True
        kings = {"h1": (RED, KING), "a8": (BLUE, KING),
                 "g14": (YELLOW, KING), "n7": (GREEN, KING)}
        for name, (col, typ) in dict(kings, **extra).items():
            sq = sq_from_name(name)
            b.sq[sq] = make_piece(col, typ)
            if typ == KING:
                b.kings[col] = sq
        b.turn = turn
        b.recompute_key()
        return b

    def see(b, token):
        move = [m for m in fast.gen_pseudo(b) if move_str(m) == token]
        return core.see(b, move[0]) if move else None

    # Hand-computed, because the point of SEE is a specific number and a
    # property test would pass on an implementation that always said zero.
    cases = [
        ("an undefended pawn is worth a pawn",
         {"g5": (RED, ROOK), "g9": (BLUE, PAWN)}, "g5g9", 100),
        ("rook takes pawn defended by a pawn",
         {"g5": (RED, ROOK), "g9": (BLUE, PAWN), "f10": (BLUE, PAWN)},
         "g5g9", -400),
        # The case the usual early-exit gets wrong: it answers -400 because it
        # stops before seeing that the second rook recaptures.
        ("...with a second rook behind it, the exchange runs out",
         {"g5": (RED, ROOK), "g9": (BLUE, PAWN), "f10": (BLUE, PAWN),
          "g2": (RED, ROOK)}, "g5g9", -300),
        ("queen for queen with a recapture is level",
         {"g5": (RED, QUEEN), "g9": (BLUE, QUEEN), "f10": (BLUE, PAWN)},
         "g5g9", 0),
        ("pawn takes knight",
         {"f8": (RED, PAWN), "g9": (BLUE, KNIGHT)}, "f8g9", 300),
    ]
    results = []
    for name, extra, token, want in cases:
        got = see(pos(extra), token)
        results.append((name, got == want))
    check_all("hand-computed exchanges", results)

    # A teammate defends too: Teams is what makes the two-player swap-off valid
    # at all, so a defender of the other seat on our team has to count.
    mate = pos({"g5": (RED, ROOK), "g9": (BLUE, PAWN), "f10": (BLUE, PAWN),
                "g12": (YELLOW, ROOK)})
    check("a teammate's defender counts", see(mate, "g5g9") == -300,
          str(see(mate, "g5g9")))

    # FFA has no alternating swap-off, and returning a plausible number there
    # would be worse than refusing.
    ffa = pos({"g5": (RED, ROOK), "g9": (BLUE, PAWN)})
    ffa.mode = MODE_FFA
    ffa.recompute_key()
    check("FFA returns 0 rather than a number nobody should trust",
          core.see(ffa, [m for m in fast.gen_pseudo(ffa)
                         if move_str(m) == "g5g9"][0]) == 0)


def test_book():
    section("opening book (book.py)")
    if not HAVE_C:
        check("C core is built", False, "run ./setup.sh")
        return
    import book as book_mod

    # Built rather than mocked: the point of a book is that every position in
    # it is playable and level, and only actually running the filter shows
    # whether it is. Small and shallow so it stays a second, not a minute.
    core.set_hash(16)
    band, depth = 60, 4
    jobs = [(i, 4242 + i, book_mod.setup_for(i, "all"), 8, depth, band)
            for i in range(160)]
    found = [c for c in (book_mod.candidate(j) for j in jobs) if c]
    check("the filter keeps something", len(found) >= 10,
          "%d of %d walks" % (len(found), len(jobs)))

    over = [s for _st, _f, _k, s in found if abs(s) > band]
    check("every position is inside the band", not over,
          "worst %d" % (max(map(abs, over)) if over else 0))

    # Walks may legitimately transpose, so distinctness is not the assertion.
    # What has to hold is that the key identifies the position: two walks that
    # reach the same board must share a key, or the dedup in main() keeps both
    # and the book quietly over-weights whatever is easy to reach.
    by_key = {}
    collide = differ = 0
    for _st, fen4, key, _s in found:
        if key in by_key:
            if by_key[key] == fen4:
                collide += 1
            else:
                differ += 1
        by_key[key] = fen4
    check("equal positions share a key, unequal ones do not", differ == 0,
          "%d walks, %d distinct, %d exact repeats" % (len(found), len(by_key), collide))

    # Round-robin has to stay even, because a book that drifts towards one
    # setup silently retrains the net on the setup it already knew.
    spread = {}
    for i in range(500):
        s = book_mod.setup_for(i, "all")
        spread[s] = spread.get(s, 0) + 1
    check("`all` spreads evenly over the setups",
          len(spread) == len(SETUPS) and max(spread.values()) - min(spread.values()) <= 1,
          str(spread))
    check("a single setup is left alone",
          {book_mod.setup_for(i, "rg") for i in range(20)} == {"rg"})

    # And the file round-trips, since both consumers read it back.
    path = os.path.join(tempfile.gettempdir(), "tetrarch_book_selftest.txt")
    with open(path, "w") as handle:
        handle.write("# a comment\n\n")
        for st, fen4, _k, _s in found[:5]:
            handle.write("%s %s\n" % (st, fen4))
    rows = book_mod.load(path)
    check("the book file round-trips", len(rows) == 5
          and all(len(r) == 2 for r in rows))
    os.remove(path)


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

    # --- the incremental accumulator ------------------------------------
    #
    # The search maintains all four accumulators across make/unmake instead of
    # rebuilding them per evaluation. Neither perft nor the pinned node counts
    # can see a wrong accumulator: it changes evaluations, not the shape of
    # the tree. So it gets its own differential gate against a rebuild.
    #
    # The moves are driven through the C core, not the Python board, because
    # the incremental path only exists in C -- replaying in Python would test
    # the rebuild and report success.
    if HAVE_C:
        core.load_net(net)
        rng = random.Random(21)
        pairs = deep = 0
        bad_pair = bad_deep = 0
        for g in range(12):
            b = start_board(SETUPS[g % len(SETUPS)])
            cb = core.CBoard(b)
            cb.eval()                       # establishes the accumulator
            played = []
            for _ply in range(40):
                legal = cb.legal()
                if not legal:
                    break
                # Every legal move made and immediately unmade: this is what
                # catches a move type whose delta is wrong (castling moves two
                # pieces, en passant removes one that is not on the target).
                for m in legal:
                    cb.make(m)
                    if cb.acc_matches() is not True:
                        bad_pair += 1
                    cb.unmake(m)
                    if cb.acc_matches() is not True:
                        bad_pair += 1
                    pairs += 1
                m = rng.choice(legal)
                cb.make(m)
                played.append(m)
            # And a deep unwind, which is what catches an asymmetry that only
            # shows up once the stack is more than one move deep.
            for m in reversed(played):
                cb.unmake(m)
                if cb.acc_matches() is not True:
                    bad_deep += 1
                deep += 1
        check("incremental accumulator matches a rebuild, make/unmake",
              bad_pair == 0, "%d pairs" % pairs)
        check("and through a deep unwind", bad_deep == 0, "%d moves" % deep)
        core.unload_net()
    os.remove(path)


#: The example from the Wikibooks notation page -- real game output, and
#: the era when `classic` was the default. `Qa7-b8` and `Qn8-m8` only resolve
#: on classic, which is an independent check of §3.
WIKIBOOK_PGN4 = """[Variant "Teams"]
[Result "0-1"]
[Site "4-player-chess"]

1. d2-d4 .. b8-c8 .. k13-k11 .. m8-l8
2. d4-d5 .. b4-d4 .. k11-k10 .. Qn8-m8
3. e2-e4 .. Qa7-b8 .. g13-g12 .. Nn5-l6
"""


def test_resume():
    section("gen_data resume (dataset integrity)")
    import gen_data

    path = os.path.join(SCRATCH, "selftest-resume.jsonl")
    # Five games written, but indices up to 12 were attempted: dead openings
    # write no line, and interrupting a worker pool leaves dispatched games
    # unwritten. Counting lines would restart at 5 and regenerate 5..12.
    with open(path, "w") as fh:
        for i in (0, 1, 4, 9, 12):
            fh.write(json.dumps({"i": i, "moves": "a", "scores": [1],
                                 "result": 1.0}) + "\n")
    nxt, lines = gen_data.resume_index(path)
    check("resume starts past the highest index, not the line count",
          nxt == 13, "next=%d lines=%d" % (nxt, lines))
    check("and reports the games actually present", lines == 5, str(lines))

    with open(path, "a") as fh:
        fh.write('{"i": 13, "mov')            # interrupted mid-write
    nxt, lines = gen_data.resume_index(path)
    check("a truncated final line is not counted as a game",
          (nxt, lines) == (13, 5), "next=%d lines=%d" % (nxt, lines))
    os.remove(path)


def test_pgn4_named_start():
    section("PGN4 named starts (import compatibility)")
    # A NAME goes into StartFen4, not a position, and handing that
    # to the FEN4 reader raised -- Tetrarch rejected genuine exported games.
    # The table below is read off its own exports for all five setups in both
    # modes; the code names the POSITION, so it does not vary with the mode.
    codes = {"4PCo": "classic", "4PCb": "by", "4PCn": "byg", "4PCrg": "rg"}
    rows = []
    for code, setup in codes.items():
        text = ('[StartFen4 "%s"]\n[Variant "Teams"]\n'
                '[RuleVariants "EnPassant"]\n[CurrentMove "0"]\n'
                '[TimeControl "2 | 10"]\n\n1. d2-d4\n' % code)
        try:
            got = pgn4.parse(text).start
            rows.append((code, got == start_board(setup)))
        except Exception as exc:                                # noqa: BLE001
            rows.append(("%s (%s)" % (code, exc), False))
    check_all("the standard start codes resolve", rows)

    # modern is the one setup it writes no tag for, being the live default.
    modern = ('[Variant "Teams"]\n[RuleVariants "EnPassant"]\n'
              '[CurrentMove "0"]\n\n1. d2-d4\n')
    check("no StartFen4 plus RuleVariants means modern",
          pgn4.parse(modern).start == start_board("modern"))
    # ...but a pre-2022 file has neither, and those were classic. The Wikibook
    # game below depends on it, which is why this is not simply "modern".
    old = '[Variant "Teams"]\n\n1. d2-d4\n'
    check("and a bare header still means classic",
          pgn4.parse(old).start == start_board("classic"))

    # An explicit position must beat any name lookup.
    fen = start_board("modern").to_fen4().replace("\n", "")
    check("an explicit FEN4 still takes precedence",
          pgn4.parse('[Variant "Teams"]\n[StartFen4 "%s"]\n\n1. d2-d4\n'
                     % fen).start.to_fen4()
          == start_board("modern").to_fen4())

    # And what we write matches a real export, so it loads elsewhere: every
    # setup and mode has to survive a round trip through our own reader.
    trips = []
    for mode in (MODE_TEAMS, MODE_FFA):
        for setup in SETUPS:
            board = start_board(setup, mode)
            back = pgn4.parse(pgn4.write(board, []))
            trips.append(("%s/%s" % (setup, "teams" if mode else "ffa"),
                          back.start == board))
    check_all("every setup round trips as a named start", trips)
    check("classic is written the standard way",
          '[StartFen4 "4PCo"]' in pgn4.write(start_board("classic"), []))
    check("modern is written with no StartFen4 at all",
          "StartFen4" not in pgn4.write(start_board("modern"), []))


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
    check("real movetext replays", len(frames) == 13,
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
    test_repetition()
    test_see()
    test_book()
    test_nnue()
    test_pgn4_named_start()
    test_resume()
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
