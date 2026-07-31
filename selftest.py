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
    RED, BLUE, YELLOW, GREEN, DEAD_UNKNOWN,
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


def check(name, ok, detail=""):
    CHECKS[0] += 1
    if not ok:
        FAILURES.append("%s%s" % (name, (": " + detail) if detail else ""))
        print("  FAIL  %s%s" % (name, (": " + detail) if detail else ""))
    return ok


def section(title):
    print("\n== %s" % title)


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
    for setup in SETUPS:
        b = start_board(setup)
        kings = tuple(name_of(k) for k in b.kings)
        check("%s king squares" % setup, kings == EXPECTED_KINGS[setup],
              "%s != %s" % (kings, EXPECTED_KINGS[setup]))
        queens = []
        for c in range(4):
            found = [name_of(sq) for sq in SQUARES
                     if b.sq[sq] == make_piece(c, QUEEN)]
            queens.append(found[0] if len(found) == 1 else str(found))
        check("%s queen squares" % setup, tuple(queens) == EXPECTED_QUEENS[setup],
              "%s != %s" % (tuple(queens), EXPECTED_QUEENS[setup]))
        check("%s has 160 squares, 64 pieces" % setup,
              sum(1 for sq in SQUARES if b.sq[sq]) == 64)
        check("%s opens with 20 moves" % setup, len(both_legal(b)) == 20)

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

    for setup in SETUPS:
        for mode in (MODE_FFA, MODE_TEAMS):
            src = start_board(setup, mode)
            rt = Board.from_fen4(src.to_fen4(), mode)
            check("round trip %s/%s" % (setup, MODE_NAMES[mode]), rt == src)
            check("round trip %s/%s key" % (setup, MODE_NAMES[mode]),
                  rt.key == src.key)
            check("round trip %s/%s is a fixed point" % (setup, MODE_NAMES[mode]),
                  rt.to_fen4() == src.to_fen4())

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
    for setup in SETUPS:
        b = start_board(setup)
        for color in range(4):
            geo = CASTLE_GEO[(color, b.kings[color])]
            for side, name in ((0, "short"), (1, "long")):
                rook_from, king_to, rook_to, between, safe = geo[side]
                gap = len(between)
                check("%s %s %s gap" % (setup, "RBYG"[color], name),
                      gap == (2 if side == 0 else 3), str(gap))
                check("%s %s %s rook is home" % (setup, "RBYG"[color], name),
                      b.sq[rook_from] == make_piece(color, ROOK))
                check("%s %s %s king moves two" % (setup, "RBYG"[color], name),
                      abs(king_to - b.kings[color]) in (2, 32))

    # A full castle, both sides, for every seat in every setup.
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
                castles = [m for m in both_legal(b)
                           if mv_flag(m) in (F_CASTLE_SHORT, F_CASTLE_LONG)]
                got = [m for m in castles if mv_flag(m) == flag]
                if not check("%s %s can castle %s" % (setup, "RBYG"[color], side),
                             len(got) == 1, str([move_str(m) for m in castles])):
                    continue
                king_from = b.kings[color]
                b.make(got[0])
                check("%s %s %s lands correctly" % (setup, "RBYG"[color], side),
                      b.sq[king_to] == make_piece(color, KING)
                      and b.sq[rook_to] == make_piece(color, ROOK)
                      and b.sq[king_from] == 0 and b.sq[rook_from] == 0)

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


def test_perft(depth=4):
    section("perft to depth %d (§12)" % depth)
    for setup in SETUPS:
        pins = PERFT_PINS[setup]
        for d in range(1, min(depth, len(pins) - 1) + 1):
            b = start_board(setup)
            got = fast.perft(b, d)
            check("%s perft(%d)" % (setup, d), got == pins[d],
                  "%d != %d" % (got, pins[d]))
    check("modern matches Athena to the pinned depth",
          PERFT_PINS["modern"][:depth + 1] == ATHENA_MODERN[:depth + 1])

    # FFA and Teams give identical counts this shallow: promotion needs a pawn
    # to travel six of its own moves (21+ plies), and no cross-team capture is
    # reachable either, so the two modes' rule differences cannot bite (§12).
    for setup in SETUPS:
        check("%s modes agree at depth 3" % setup,
              fast.perft(start_board(setup, MODE_FFA), 3) ==
              fast.perft(start_board(setup, MODE_TEAMS), 3))

    # Both generators must agree on the tree, not just the leaf count.
    for setup in SETUPS:
        b = start_board(setup)
        check("%s perft(3) agrees between generators" % setup,
              slow.perft(b.copy(), 3) == fast.perft(b.copy(), 3))


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
    for setup in SETUPS:
        pins = PERFT_PINS[setup]
        for mode in (MODE_TEAMS, MODE_FFA):
            for d in range(1, len(pins)):
                b = start_board(setup, mode)
                got = core.perft(b, d)
                check("C %s/%s perft(%d)" % (setup, MODE_NAMES[mode], d),
                      got == pins[d], "%d != %d" % (got, pins[d]))

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
    for setup in SETUPS:
        for mode in (MODE_TEAMS, MODE_FFA):
            bad = core.key_check(start_board(setup, mode), 3)
            check("C key/unmake integrity %s/%s" % (setup, MODE_NAMES[mode]),
                  bad == 0, "%d mismatches" % bad)

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
    for setup in SETUPS:
        b = start_board(setup)
        check("%s four quarter turns is the identity" % setup, rotate(b, 4) == b)
        counts = [fast.perft(rotate(b, k), 3) for k in range(4)]
        check("%s perft is invariant under rotation" % setup,
              len(set(counts)) == 1, str(counts))
        for k in range(1, 4):
            r = rotate(b, k)
            check("%s rotation %d keeps 64 pieces" % (setup, k),
                  sum(1 for sq in SQUARES if r.sq[sq]) == 64)
            check("%s rotation %d shifts the turn" % (setup, k),
                  r.turn == (b.turn + k) & 3)
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
SEARCH_PINS = {
    "classic": [40, 380, 3552, 26044, 228628],
    "modern": [40, 308, 4815, 31170, 238879],
    "by": [40, 308, 4437, 34221, 220568],
    "byg": [40, 308, 4437, 33912, 282296],
    "rg": [40, 364, 3306, 36865, 230482],
}


def test_search():
    section("search (Phase 3 gate)")
    if not HAVE_C:
        check("C core is built", False, "run ./setup.sh")
        return

    core.set_hash(16)
    for setup in SETUPS:
        for i, expect in enumerate(SEARCH_PINS[setup]):
            core.clear_hash()
            got = core.search(start_board(setup), i + 1).nodes
            check("%s search nodes at depth %d" % (setup, i + 1), got == expect,
                  "%d != %d" % (got, expect))

    # The correctness theorem for the whole search: alpha-beta with a
    # transposition table must return exactly the plain minimax value. Pinned
    # node counts cannot see a wrong score -- a wrong score still has a count.
    rng = random.Random(3)
    bad = tested = 0
    for _ in range(40):
        b = _sparse_teams(rng)
        if not fast.gen_legal(b):
            continue
        for depth in (1, 2, 3):
            core.clear_hash()
            got = core.search(b, depth).score
            want = search.reference_score(b.copy(), depth)
            tested += 1
            if got != want:
                bad += 1
    check("alpha-beta equals unpruned minimax (%d comparisons)" % tested,
          bad == 0, "%d mismatches" % bad)

    # Mate is found and scored from the mating team's point of view.
    mate = position({"a5": (RED, KING), "n9": (YELLOW, KING),
                     "b7": (BLUE, KING), "m7": (GREEN, KING),
                     "d7": (RED, QUEEN), "d8": (RED, ROOK)}, turn=RED)
    core.clear_hash()
    r = core.search(mate, 3)
    check("a forced mate scores as a mate", r.score > 29000 - 100
          or r.score < -(29000 - 100), "score %d" % r.score)

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
    for setup in SETUPS:
        b = start_board(setup)
        sets = [sorted(nnue.active_features(rotate(b, k), k)) for k in range(4)]
        check("%s features are rotation invariant" % setup,
              all(s == sets[0] for s in sets))
        check("%s has 64 active features at the start" % setup,
              len(sets[0]) == 64, str(len(sets[0])))

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
        check("%s PGN4 round trips" % setup,
              len(back.tokens) == len(moves)
              and replayed[-1]["fen4"] == b.to_fen4().replace("\n", ""))

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
            for result in pool.imap_unordered(crosscheck_chunk, chunks):
                absorb(result)
                if not quiet:
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
    print("  %d positions in %.1fs (%.0f/s); %d terminal"
          % (done, secs, done / max(secs, 1e-9), empty))
    print("  coverage: %s" % ", ".join("%s=%d" % kv for kv in sorted(stats.items())))
    for kind in STAT_KINDS:
        check("cross-check exercised %s" % kind, stats[kind] > 0)
    return ok


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--crosscheck", type=int, default=3000, metavar="N",
                    help="random positions through both movegens (default 3000)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=1, metavar="N",
                    help="processes for the cross-check; 0 means every core")
    ap.add_argument("--perft", type=int, default=4, metavar="D",
                    help="perft depth for the pinned check (default 4)")
    ap.add_argument("--perft-deep", action="store_true",
                    help="perft 5 for every setup and mode; takes minutes")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    started = time.time()
    test_geometry()
    test_setups()
    test_fen4()
    test_make_unmake()
    test_pawn_directions()
    test_en_passant()
    test_multi_check()
    test_dead_seats()
    test_castling()
    test_perft(args.perft)
    test_c_core(args.perft_deep)
    test_rotation()
    test_eval()
    test_search()
    test_nnue()
    test_pgn4()
    if args.crosscheck:
        crosscheck(args.crosscheck, args.seed, args.workers, args.quiet)
    if args.perft_deep:
        perft_deep()

    print("\n%d checks, %d failures, %.1fs"
          % (CHECKS[0], len(FAILURES), time.time() - started))
    if FAILURES:
        print("\nFAILED:")
        for f in FAILURES:
            print("  " + f)
        return 1
    print("selftest OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
