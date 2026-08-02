"""Hand evaluation.

It was written as a throwaway -- "do not tune it, do not extend it, do not A/B
against it" -- to bootstrap net v0 and then be deleted. That reasoning held
while every net lost. It stopped holding when the hand eval turned out to be
the strongest evaluation in the project for four releases running, so it is now
also the baseline a net has to beat, and a baseline worth strengthening: a net
that only beats a deliberately bad opponent has not been shown to be good.

Extended deliberately, by decision, with the mobility term below.

Material on the FFA capture values (§8.1) plus a crude king-danger term.
Integer arithmetic only, because `selftest.py` asserts this agrees with the C
copy in `src/c/tetrarch.c` bit for bit -- floats would drift across
microarchitectures and the assertion would start failing for a reason that has
nothing to do with chess.

Section references (§n) are to docs/RULES.md.
"""

from .board import (
    SQUARES, QUEEN_DIRS, KNIGHT_DELTAS, DIAG, ORTHO, VALID, PC_COLOR, PC_TYPE,
    DEAD_UNKNOWN, PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING, PQUEEN, NTYPE,
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

#: Centipawns per square a piece can reach. The reference implementation's
#: evaluation carries
#: mobility as its largest positional term -- larger than king safety -- and
#: this had none at all, which is the most obvious hole in it.
#: Mirrors the C toggle. Default off, like every feature that has not yet won
#: an A/B; selftest keeps the two in step and compares both states.
USE_MOBILITY = False

MOBILITY = 3
#: Counted mobility per piece is capped. Not for speed: the cap is what gives
#: the term a tight maximum, and lazy evaluation is only sound while the margin
#: it bails on is a true bound on everything the cheap half omits.
MOBILITY_CAP = 10
#: Pawns and kings are excluded. A pawn's mobility is nearly constant and says
#: little, and there are eight of them per seat paying the cost.
MOBILITY_DIRS = {
    KNIGHT: (KNIGHT_DELTAS, False),
    BISHOP: (DIAG, True),
    ROOK: (ORTHO, True),
    QUEEN: (QUEEN_DIRS, True),
    PQUEEN: (QUEEN_DIRS, True),
}


def piece_mobility(b, sq, piece):
    """Squares this piece could move to, ignoring pins and check, capped.

    A square counts when it is empty or holds something this seat does not own
    -- the same rule the generator uses, so the number means what a player
    would mean by it.
    """
    entry = MOBILITY_DIRS.get(PC_TYPE[piece])
    if entry is None:
        return 0
    dirs, sliding = entry
    mine = PC_COLOR[piece]
    count = 0
    for d in dirs:
        t = sq
        while True:
            t = (t + d) & 255
            if not VALID[t]:
                break
            other = b.sq[t]
            if other:
                if PC_COLOR[other] != mine:
                    count += 1
                break
            count += 1
            if not sliding or count >= MOBILITY_CAP:
                break
        if count >= MOBILITY_CAP:
            return MOBILITY_CAP
    return count


def mobility(b):
    """Mobility, from the side to move's team, in centipawns."""
    me = b.turn & 1
    total = 0
    for sq in SQUARES:
        p = b.sq[sq]
        if not p:
            continue
        color = PC_COLOR[p]
        if color == DEAD_UNKNOWN or not b.alive[color]:
            continue
        m = piece_mobility(b, sq, p) * MOBILITY
        total += m if (color & 1) == me else -m
    return total


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

    return total + (mobility(b) if USE_MOBILITY else 0)
