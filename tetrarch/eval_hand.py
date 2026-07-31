"""Throwaway hand evaluation.

# throwaway: deleted at Phase 4

This exists for exactly one reason: NNUE is the eval from day one, but a net
needs labelled positions and there is no engine yet to produce them. So this
plays badly enough to self-play a few million positions, net v0 gets trained on
them, and then this file goes away. Do not tune it, do not extend it, do not
A/B against it.

Material on the FFA capture values (§8.1) plus a crude king-danger term.
Integer arithmetic only, because `selftest.py` asserts this agrees with the C
copy in `src/c/tetrarch.c` bit for bit -- floats would drift across
microarchitectures and the assertion would start failing for a reason that has
nothing to do with chess.

Section references (§n) are to docs/RULES.md.
"""

from .board import (
    SQUARES, QUEEN_DIRS, VALID, PC_COLOR, PC_TYPE, DEAD_UNKNOWN,
    PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING, PQUEEN, NTYPE,
)
from .movegen import is_attacked

#: The FFA capture values, x100 (§8.1). Bishop is 5 and not 3 -- the diagonals
#: are long on a 14x14 board. A promoted queen is a 1-point queen (§4.2).
PIECE_VALUE = [0] * NTYPE
PIECE_VALUE[PAWN] = 100
PIECE_VALUE[KNIGHT] = 300
PIECE_VALUE[BISHOP] = 500
PIECE_VALUE[ROOK] = 500
PIECE_VALUE[QUEEN] = 900
PIECE_VALUE[KING] = 0
PIECE_VALUE[PQUEEN] = 100

KING_DANGER = 12


def evaluate(b):
    """Score from the perspective of the side to move's team.

    In Teams the seat rotation alternates team every ply (team = seat & 1, and
    the turn advances by one), so this sign convention feeds plain negamax with
    no special casing (§2).
    """
    me = b.turn & 1
    total = 0

    for sq in SQUARES:
        p = b.sq[sq]
        if not p:
            continue
        color = PC_COLOR[p]
        # Dead seats' pieces are worth nothing to capture (§9.1). They still
        # block, which a material eval cannot see and the throwaway will not try.
        if color == DEAD_UNKNOWN or not b.alive[color]:
            continue
        value = PIECE_VALUE[PC_TYPE[p]]
        total += value if (color & 1) == me else -value

    for color in range(4):
        if not b.alive[color]:
            continue
        king = b.kings[color]
        if king < 0:
            continue
        danger = 0
        for d in QUEEN_DIRS:
            t = (king + d) & 255
            if VALID[t] and is_attacked(b, t, color):
                danger += 1
        penalty = danger * KING_DANGER
        total += -penalty if (color & 1) == me else penalty

    return total
