"""Slow, obvious move generator. The reference against which the fast one is
checked.

Deliberately written to share as little machinery as possible with
`movegen.py`: it works in (file, rank) pairs rather than mailbox deltas, tests
its own bounds rather than consulting VALID, derives castling squares from
first principles rather than reading CASTLE_GEO, and answers "is this square
attacked?" by enumerating every enemy piece's moves rather than casting rays.

Two generators only earn their keep if a bug would have to occur twice to go
unnoticed. Optimising this file would defeat its purpose.

Section references (§n) are to docs/RULES.md.
"""

from .board import (
    DEAD_UNKNOWN, KING, KNIGHT, BISHOP, ROOK, PAWN,
    PC_COLOR, PC_TYPE, QUEENISH,
    F_NORMAL, F_DOUBLE, F_EP, F_CASTLE_SHORT, F_CASTLE_LONG,
    make_move, sq_of, file_of, rank_of, same_team,
)

_DIAG = ((1, 1), (1, -1), (-1, 1), (-1, -1))
_ORTHO = ((1, 0), (-1, 0), (0, 1), (0, -1))
_ALL8 = _ORTHO + _DIAG
_KNIGHT = ((1, 2), (2, 1), (-1, 2), (-2, 1),
           (1, -2), (2, -1), (-1, -2), (-2, -1))

#: forward step and the two capture steps, as (dfile, drank) per seat (§4.1)
_PAWN_FWD = ((0, 1), (1, 0), (0, -1), (-1, 0))
_PAWN_CAP = (((-1, 1), (1, 1)),      # Red
             ((1, 1), (1, -1)),      # Blue
             ((-1, -1), (1, -1)),    # Yellow
             ((-1, 1), (-1, -1)))    # Green


def on_board(file, rank):
    """Playable-square test, recomputed rather than read from VALID (§1)."""
    if not (0 <= file < 14 and 0 <= rank < 14):
        return False
    return not ((file < 3 or file > 10) and (rank < 3 or rank > 10))


def _pawn_progress(color, file, rank):
    """How far this pawn has advanced, 0-indexed from its own back rank."""
    if color == 0:
        return rank
    if color == 1:
        return file
    if color == 2:
        return 13 - rank
    return 13 - file


def attacks_square(b, from_file, from_rank, target_file, target_rank):
    """Does the piece on (from_file, from_rank) attack that target square?

    Attack, not move: pawns attack diagonally only, and castling is not an
    attack. Dead seats' pieces attack nothing -- they are inactive (§9.1).
    """
    piece = b.sq[sq_of(from_file, from_rank)]
    if not piece:
        return False
    color = PC_COLOR[piece]
    if color == DEAD_UNKNOWN or not b.alive[color]:
        return False
    ptype = PC_TYPE[piece]
    df = target_file - from_file
    dr = target_rank - from_rank

    if ptype == PAWN:
        return (df, dr) in _PAWN_CAP[color]
    if ptype == KNIGHT:
        return (df, dr) in _KNIGHT
    if ptype == KING:
        return (df, dr) in _ALL8

    if ptype == BISHOP:
        dirs = _DIAG
    elif ptype == ROOK:
        dirs = _ORTHO
    elif ptype in QUEENISH:
        dirs = _ALL8
    else:
        return False

    for sf, sr in dirs:
        f, r = from_file + sf, from_rank + sr
        while on_board(f, r):
            if f == target_file and r == target_rank:
                return True
            if b.sq[sq_of(f, r)]:
                break
            f += sf
            r += sr
    return False


def is_attacked(b, sq, me):
    """Is `sq` attacked by anyone who could capture a piece belonging to `me`?

    In FFA that is all three opponents; in Teams, the other team. A seat can be
    checked by up to three players at once (§7) -- this returns on the first,
    which is all legality needs.
    """
    tf, tr = file_of(sq), rank_of(sq)
    for r in range(14):
        for f in range(14):
            if not on_board(f, r):
                continue
            piece = b.sq[sq_of(f, r)]
            if not piece:
                continue
            color = PC_COLOR[piece]
            if color == DEAD_UNKNOWN or not b.alive[color]:
                continue
            if same_team(b.mode, color, me):
                continue
            if attacks_square(b, f, r, tf, tr):
                return True
    return False


def in_check(b, color):
    king = b.kings[color]
    if king < 0:
        return False
    return is_attacked(b, king, color)


def _add_pawn(moves, b, frm, to, color, flag=F_NORMAL):
    """Append a pawn move, expanding promotions (§4.2).

    `flag` may be F_EP: an en-passant capture can land on a promotion square,
    so the two are independent (see the move encoding in board.py).
    """
    tf, tr = file_of(to), rank_of(to)
    if _pawn_progress(color, tf, tr) == b.promo_coord():
        for choice in b.promo_choices():
            moves.append(make_move(frm, to, flag, choice))
    else:
        moves.append(make_move(frm, to, flag))


def ep_offers(b, me):
    """Live en-passant offers `me` could accept: ``{target: victim_sq}``.

    Seats' target squares can never collide -- Red's lie on rank 3, Blue's on
    file c, Yellow's on rank 12, Green's on file l -- so one square maps to at
    most one offer.
    """
    out = {}
    for owner in range(4):
        entry = b.ep[owner]
        if entry is None or owner == me:
            continue
        target, victim_sq = entry
        victim = b.sq[victim_sq]
        if not victim or PC_TYPE[victim] != PAWN:
            continue
        # The pawn there has to be the offer's owner's own. An offer only dies
        # when its owner moves again or when it is taken, so a normal capture
        # of the double-pushed pawn leaves the offer standing with someone
        # else's pawn on the square -- and without this the generator offers an
        # "en passant" onto a pawn that never double pushed and that the
        # capturing pawn does not attack. The owner cannot move while its own
        # offer is live, so an owner-coloured pawn there is uniquely the pushed
        # one (§5).
        if PC_COLOR[victim] != owner:
            continue
        if not b.is_enemy(victim, me):
            continue
        occupant = b.sq[target]
        if occupant and not b.is_enemy(occupant, me):
            continue                    # one of ours is standing on it
        out[target] = victim_sq
    return out


def gen_pseudo(b):
    """Every pseudo-legal move for the side to move. Order is unspecified."""
    me = b.turn
    assert b.alive[me], "a dead seat has no moves (§9)"
    moves = []
    offers = ep_offers(b, me)

    for rank in range(14):
        for file in range(14):
            if not on_board(file, rank):
                continue
            frm = sq_of(file, rank)
            piece = b.sq[frm]
            if not piece or PC_COLOR[piece] != me:
                continue
            ptype = PC_TYPE[piece]

            if ptype == PAWN:
                df, dr = _PAWN_FWD[me]
                f, r = file + df, rank + dr
                if on_board(f, r) and not b.sq[sq_of(f, r)]:
                    _add_pawn(moves, b, frm, sq_of(f, r), me)
                    # Double push, from the seat's own base rank only (§4.1).
                    base = b.pawn_base_rank
                    if base and _pawn_progress(me, file, rank) == base - 1:
                        f2, r2 = file + 2 * df, rank + 2 * dr
                        if on_board(f2, r2) and not b.sq[sq_of(f2, r2)]:
                            moves.append(make_move(frm, sq_of(f2, r2), F_DOUBLE))
                for cf, cr in _PAWN_CAP[me]:
                    f, r = file + cf, rank + cr
                    if not on_board(f, r):
                        continue
                    to = sq_of(f, r)
                    if to in offers:
                        continue    # emitted as an en-passant capture instead
                    target = b.sq[to]
                    if target and b.is_enemy(target, me):
                        _add_pawn(moves, b, frm, to, me)
                continue

            if ptype == KNIGHT:
                steps, sliding = _KNIGHT, False
            elif ptype == KING:
                steps, sliding = _ALL8, False
            elif ptype == BISHOP:
                steps, sliding = _DIAG, True
            elif ptype == ROOK:
                steps, sliding = _ORTHO, True
            elif ptype in QUEENISH:
                steps, sliding = _ALL8, True
            else:
                continue

            for sf, sr in steps:
                f, r = file + sf, rank + sr
                while on_board(f, r):
                    target = b.sq[sq_of(f, r)]
                    if not target:
                        moves.append(make_move(frm, sq_of(f, r)))
                    else:
                        if b.is_enemy(target, me):
                            moves.append(make_move(frm, sq_of(f, r)))
                        break
                    if not sliding:
                        break
                    f += sf
                    r += sr

    moves.extend(_gen_en_passant(b, me, offers))
    moves.extend(_gen_castles(b, me))
    return moves


def _gen_en_passant(b, me, offers):
    """En-passant captures.

    Any pawn that attacks the skipped square may capture, so both flanking
    pawns get a move -- Athena generates both but removes the wrong pawn for
    one of them, and 4pchess generates only one (§5.4). The pawn removed is
    always the one recorded on the victim square.

    Walks backwards from the target square to the pawns that attack it; the
    fast generator walks forwards from each pawn, which is the point.
    """
    out = []
    for target in offers:
        tf, tr = file_of(target), rank_of(target)
        for cf, cr in _PAWN_CAP[me]:
            f, r = tf - cf, tr - cr     # where such a pawn would have to stand
            if not on_board(f, r):
                continue
            frm = sq_of(f, r)
            mine = b.sq[frm]
            if mine and PC_COLOR[mine] == me and PC_TYPE[mine] == PAWN:
                _add_pawn(out, b, frm, target, me, F_EP)
    return out


def _gen_castles(b, me):
    """Castling, with the geometry rebuilt from scratch every time (§6.1)."""
    if not (b.ck[me] or b.cq[me]):
        return []
    king = b.kings[me]
    if king < 0:
        return []
    kf, kr = file_of(king), rank_of(king)

    # The home row: Red rank 1 and Yellow rank 14 run along files d-k; Blue
    # file a and Green file n run along ranks 4-11.
    if me in (0, 2):
        if kr != (0 if me == 0 else 13) or kf not in (6, 7):
            return []
        ends = [sq_of(3, kr), sq_of(10, kr)]
        step_f, step_r = 1, 0
    else:
        if kf != (0 if me == 1 else 13) or kr not in (6, 7):
            return []
        ends = [sq_of(kf, 3), sq_of(kf, 10)]
        step_f, step_r = 0, 1

    out = []
    for rook_sq in ends:
        rf, rr = file_of(rook_sq), rank_of(rook_sq)
        gap = abs(rf - kf) + abs(rr - kr)
        assert gap in (3, 4)
        side = F_CASTLE_SHORT if gap == 3 else F_CASTLE_LONG
        if not (b.ck[me] if gap == 3 else b.cq[me]):
            continue
        rook = b.sq[rook_sq]
        if not rook or PC_TYPE[rook] != ROOK or PC_COLOR[rook] != me:
            continue
        sign = 1 if (rf - kf) + (rr - kr) > 0 else -1
        df, dr = step_f * sign, step_r * sign

        blocked = False
        for i in range(1, gap):
            if b.sq[sq_of(kf + df * i, kr + dr * i)]:
                blocked = True
                break
        if blocked:
            continue
        # Origin, transit and destination must all be unattacked (§6.1).
        if any(is_attacked(b, sq_of(kf + df * i, kr + dr * i), me)
               for i in (0, 1, 2)):
            continue
        out.append(make_move(king, sq_of(kf + df * 2, kr + dr * 2), side))
    return out


def gen_legal(b):
    """Pseudo-legal moves filtered by making each one on a copy of the board."""
    me = b.turn
    out = []
    for m in gen_pseudo(b):
        trial = b.copy()
        trial.make(m)
        if not in_check(trial, me):
            out.append(m)
    return out


def perft(b, depth):
    if depth == 0:
        return 1
    total = 0
    for m in gen_legal(b):
        undo = b.make(m)
        total += perft(b, depth - 1)
        b.unmake(m, undo)
    return total
