"""Fast move generator: padded-mailbox deltas and ray-cast attack detection.

Same rules as `movegen_slow.py`, different machinery throughout. Where the slow
generator walks (file, rank) pairs and answers "is this square attacked?" by
enumerating every enemy piece, this one adds deltas to a square index, masks
with 255 and reads VALID, and casts rays outward from the square in question.

`selftest.py` asserts the two agree move-for-move. A bug would have to be made
twice, in two different representations, to survive that.

Section references (§n) are to docs/RULES.md.
"""

from .board import (
    VALID, SQUARES, DIAG, ORTHO, QUEEN_DIRS, KNIGHT_DELTAS,
    PAWN_PUSH, PAWN_TAKES, pawn_coord,
    KING, KNIGHT, BISHOP, ROOK, PAWN, PC_COLOR, PC_TYPE, QUEENISH,
    F_NORMAL, F_DOUBLE, F_EP, F_CASTLE_SHORT, F_CASTLE_LONG,
    CASTLE_GEO, SHORT, LONG, make_move, same_team,
)


def _hostile(b, piece, me):
    """Could this piece deliver check to `me`?

    Dead seats' pieces are inactive: they block sliders but attack nothing
    (§9.1). Pieces with no recorded origin are always dead.
    """
    c = PC_COLOR[piece]
    return c < 4 and b.alive[c] and not same_team(b.mode, c, me)


def is_attacked(b, sq, me):
    """Is `sq` attacked by anyone hostile to `me`?

    Returns on the first attacker found. A seat can be checked by up to three
    players at once (§7), but legality only needs to know whether the count is
    non-zero.
    """
    board = b.sq

    for d in KNIGHT_DELTAS:
        t = (sq + d) & 255
        if VALID[t]:
            p = board[t]
            if p and PC_TYPE[p] == KNIGHT and _hostile(b, p, me):
                return True

    for d in QUEEN_DIRS:
        t = (sq + d) & 255
        if VALID[t]:
            p = board[t]
            if p and PC_TYPE[p] == KING and _hostile(b, p, me):
                return True

    # A pawn on `sq - d` attacks `sq` exactly when d is one of that seat's own
    # capture deltas -- which differ per seat, so the pawn's colour decides
    # (§4.1). Every seat's captures are diagonal, so four probes cover all four.
    for d in DIAG:
        t = (sq - d) & 255
        if VALID[t]:
            p = board[t]
            if (p and PC_TYPE[p] == PAWN and _hostile(b, p, me)
                    and d in PAWN_TAKES[PC_COLOR[p]]):
                return True

    for d in ORTHO:
        t = (sq + d) & 255
        while VALID[t]:
            p = board[t]
            if p:
                pt = PC_TYPE[p]
                if (pt == ROOK or pt in QUEENISH) and _hostile(b, p, me):
                    return True
                break
            t = (t + d) & 255

    for d in DIAG:
        t = (sq + d) & 255
        while VALID[t]:
            p = board[t]
            if p:
                pt = PC_TYPE[p]
                if (pt == BISHOP or pt in QUEENISH) and _hostile(b, p, me):
                    return True
                break
            t = (t + d) & 255

    return False


def in_check(b, color):
    king = b.kings[color]
    if king < 0:
        return False
    return is_attacked(b, king, color)


def ep_offers(b, me):
    """Live en-passant offers `me` could accept: ``{target: victim_sq}`` (§5)."""
    board = b.sq
    out = {}
    for owner in range(4):
        if owner == me:
            continue
        entry = b.ep[owner]
        if entry is None:
            continue
        target, victim_sq = entry
        victim = board[victim_sq]
        if not victim or PC_TYPE[victim] != PAWN:
            continue
        if not b.is_enemy(victim, me):
            continue
        occupant = board[target]
        if occupant and not b.is_enemy(occupant, me):
            continue
        out[target] = victim_sq
    return out


def gen_pseudo(b):
    """Every pseudo-legal move for the side to move. Order is unspecified."""
    me = b.turn
    assert b.alive[me], "a dead seat has no moves (§9)"
    board = b.sq
    moves = []
    add = moves.append

    promo_coord = b.promo_coord()
    choices = b.promo_choices()
    base = b.pawn_base_rank
    push = PAWN_PUSH[me]
    takes = PAWN_TAKES[me]
    offers = ep_offers(b, me)
    is_enemy = b.is_enemy

    for frm in SQUARES:
        p = board[frm]
        if not p or PC_COLOR[p] != me:
            continue
        ptype = PC_TYPE[p]

        if ptype == PAWN:
            to = (frm + push) & 255
            if VALID[to] and not board[to]:
                if pawn_coord(me, to) == promo_coord:
                    for choice in choices:
                        add(make_move(frm, to, F_NORMAL, choice))
                else:
                    add(make_move(frm, to))
                # Double push, from the seat's own base rank only (§4.1).
                if base and pawn_coord(me, frm) == base - 1:
                    to2 = (frm + 2 * push) & 255
                    if VALID[to2] and not board[to2]:
                        add(make_move(frm, to2, F_DOUBLE))
            for d in takes:
                to = (frm + d) & 255
                if not VALID[to]:
                    continue
                if to in offers:
                    # An en-passant capture supersedes the plain capture onto
                    # the same square: same from/to, but it also removes the
                    # passing pawn (§5.5). Emitting both would give two moves
                    # with identical notation.
                    flag = F_EP
                else:
                    target = board[to]
                    if not (target and is_enemy(target, me)):
                        continue
                    flag = F_NORMAL
                if pawn_coord(me, to) == promo_coord:
                    for choice in choices:
                        add(make_move(frm, to, flag, choice))
                else:
                    add(make_move(frm, to, flag))
            continue

        if ptype == KNIGHT:
            deltas, sliding = KNIGHT_DELTAS, False
        elif ptype == KING:
            deltas, sliding = QUEEN_DIRS, False
        elif ptype == BISHOP:
            deltas, sliding = DIAG, True
        elif ptype == ROOK:
            deltas, sliding = ORTHO, True
        elif ptype in QUEENISH:
            deltas, sliding = QUEEN_DIRS, True
        else:
            continue

        for d in deltas:
            to = (frm + d) & 255
            while VALID[to]:
                target = board[to]
                if target:
                    if is_enemy(target, me):
                        add(make_move(frm, to))
                    break
                add(make_move(frm, to))
                if not sliding:
                    break
                to = (to + d) & 255

    moves.extend(_gen_castles(b, me))
    return moves


def _gen_castles(b, me):
    """Castling, reading the precomputed geometry (§6.1)."""
    if not (b.ck[me] or b.cq[me]):
        return []
    king = b.kings[me]
    sides = CASTLE_GEO.get((me, king))
    if sides is None:
        return []                       # king has moved: no rights are real
    out = []
    for side, flag, right in ((SHORT, F_CASTLE_SHORT, b.ck[me]),
                              (LONG, F_CASTLE_LONG, b.cq[me])):
        if not right:
            continue
        rook_from, king_to, _rook_to, between, safe = sides[side]
        rook = b.sq[rook_from]
        if not rook or PC_TYPE[rook] != ROOK or PC_COLOR[rook] != me:
            continue
        if any(b.sq[s] for s in between):
            continue
        if any(is_attacked(b, s, me) for s in safe):
            continue
        out.append(make_move(king, king_to, flag))
    return out


def gen_legal(b):
    """Pseudo-legal moves filtered with make/unmake and a targeted check test."""
    me = b.turn
    out = []
    for m in gen_pseudo(b):
        undo = b.make(m)
        king = b.kings[me]
        ok = king < 0 or not is_attacked(b, king, me)
        b.unmake(m, undo)
        if ok:
            out.append(m)
    return out


def perft(b, depth):
    if depth == 0:
        return 1
    if depth == 1:
        return len(gen_legal(b))
    total = 0
    for m in gen_legal(b):
        undo = b.make(m)
        total += perft(b, depth - 1)
        b.unmake(m, undo)
    return total


def divide(b, depth):
    """Per-move node counts, for locating a perft divergence."""
    from .board import move_str
    out = {}
    for m in gen_legal(b):
        undo = b.make(m)
        out[move_str(m)] = perft(b, depth - 1)
        b.unmake(m, undo)
    return out
