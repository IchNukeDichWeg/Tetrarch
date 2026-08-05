"""Board geometry, position state, FEN4 I/O, make/unmake and Zobrist hashing.

Pure Python. This is the source of truth: the C core reads its parameters from
here at startup and `selftest.py` asserts the two agree.

Section references (§n) are to docs/RULES.md.

Geometry (§1): 14x14 with the four 3x3 corners removed, held in a 16-wide padded
mailbox so that `sq = rank*16 + file`. Files 14 and 15 are permanently invalid,
which is what makes `sq += delta` safe for every piece delta: a knight or king
step off any real file lands either on a real file or in the padding, never
wrapped onto the far side of the next rank.

Indices are masked with `& 255` before every VALID lookup. Stepping south from
rank 1 produces a negative index; masking sends it into ranks 14-15, which are
padding and therefore invalid. Python would tolerate the negative index, C would
not, so both mask and both behave identically.
"""

import random

# --- geometry ---------------------------------------------------------------

FILE_NAMES = "abcdefghijklmn"
NFILE = NRANK = 14
NSQ = 256
NPLAYABLE = 160


def sq_of(file, rank):
    return rank * 16 + file


def file_of(sq):
    return sq & 15


def rank_of(sq):
    return sq >> 4


def _playable(file, rank):
    if not (0 <= file < NFILE and 0 <= rank < NRANK):
        return False
    # The removed 3x3 corners: files a-c / l-n crossed with ranks 1-3 / 12-14.
    return not ((file < 3 or file > 10) and (rank < 3 or rank > 10))


VALID = bytes(1 if _playable(i & 15, i >> 4) else 0 for i in range(NSQ))

_compact = [255] * NSQ
_n = 0
for _sq in range(NSQ):
    if VALID[_sq]:
        _compact[_sq] = _n
        _n += 1
assert _n == NPLAYABLE, _n
COMPACT = bytes(_compact)
del _compact, _n, _sq

SQUARES = tuple(sq for sq in range(NSQ) if VALID[sq])

# Directions. North is toward rank 14, east toward file n.
N, S, E, W = 16, -16, 1, -1
NE, NW, SE, SW = N + E, N + W, S + E, S + W
DIAG = (NE, NW, SE, SW)
ORTHO = (N, S, E, W)
QUEEN_DIRS = ORTHO + DIAG
KNIGHT_DELTAS = (2 * N + E, 2 * N + W, 2 * S + E, 2 * S + W,
                 N + 2 * E, N + 2 * W, S + 2 * E, S + 2 * W)


def name_of(sq):
    return FILE_NAMES[file_of(sq)] + str(rank_of(sq) + 1)


def sq_from_name(text):
    text = text.strip()
    if len(text) < 2 or text[0] not in FILE_NAMES:
        raise ValueError("bad square %r" % text)
    rank = int(text[1:])
    if not 1 <= rank <= 14:
        raise ValueError("bad rank in %r" % text)
    return sq_of(FILE_NAMES.index(text[0]), rank - 1)


# --- seats, modes, pieces ---------------------------------------------------

RED, BLUE, YELLOW, GREEN = 0, 1, 2, 3
#: 5th slot for FEN4's bare `d` prefix: a dead piece whose origin was not
#: recorded (§9.1). It never moves and belongs to no team.
DEAD_UNKNOWN = 4
NCOLOR = 5
SEAT_NAMES = "RBYG"

MODE_FFA, MODE_TEAMS = 0, 1
MODE_NAMES = ("ffa", "teams")

PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING, PQUEEN = range(7)
NTYPE = 7
#: PQUEEN is a queen produced by promotion. In FFA it is worth 1 point rather
#: than 9 (§4.2, §8.1). It moves exactly as a queen. FEN4 has no encoding for
#: it, so the distinction does not survive a FEN4 round trip (§11.2).
TYPE_CHARS = "PNBRQKQ"

EMPTY = 0


def make_piece(color, ptype):
    return 1 + color * NTYPE + ptype


NPIECE = 1 + NCOLOR * NTYPE
PC_COLOR = bytes([0] + [(i - 1) // NTYPE for i in range(1, NPIECE)])
PC_TYPE = bytes([0] + [(i - 1) % NTYPE for i in range(1, NPIECE)])

#: Both queen encodings answer to the queen movement routine.
QUEENISH = frozenset((QUEEN, PQUEEN))

#: What capturing each piece type scores in FFA (§8.1). Rules, not parameters:
#: an A/B must never be able to move them. A promoted queen is worth 1, not 9,
#: which is why PQUEEN is a separate type at all; a bishop is 5, not 3, because
#: the diagonals are long on 14x14. KING's +20 is unreachable in engine play --
#: a live king cannot be captured in a legal position, and zombie kings (§9.2)
#: are Phase 6+ -- and is listed so the table matches the published one.
#: The spare-king row (+3) is omitted: Tetrarch has one king per seat.
#: Mirrored in src/c/tetrarch.c as FFA_CAPTURE_POINTS; selftest pins the pair.
CAPTURE_POINTS = (1, 3, 5, 5, 9, 20, 1)


def same_team(mode, a, b):
    """Team membership for capture legality. Dead-origin pieces join no team."""
    if a == DEAD_UNKNOWN or b == DEAD_UNKNOWN:
        return False
    if mode == MODE_FFA:
        return a == b
    return (a & 1) == (b & 1)          # R+Y even, B+G odd (§2)


# --- pawn geometry (§4.1, §4.2) ---------------------------------------------

#: forward delta per seat
PAWN_PUSH = (N, E, S, W)
#: the two capture deltas per seat
PAWN_TAKES = ((NE, NW), (NE, SE), (SE, SW), (NW, SW))
#: Axis each seat's pawns travel along: 0 = rank varies, 1 = file varies.
PAWN_AXIS = (0, 1, 0, 1)
#: Sign of travel along that axis.
PAWN_SIGN = (+1, +1, -1, -1)


def pawn_coord(color, sq):
    """The pawn's own 0-indexed coordinate along its direction of travel."""
    c = rank_of(sq) if PAWN_AXIS[color] == 0 else file_of(sq)
    return c if PAWN_SIGN[color] > 0 else 13 - c


#: Promotion coordinate, 0-indexed, indexed by mode. FFA promotes on the seat's
#: 8th rank, Teams on its 11th (§4.2).
PROMO_COORD = (7, 10)
#: What a pawn may promote to. FFA forces a 1-point queen and forbids
#: underpromotion; Teams allows the usual four (§4.2).
PROMO_CHOICES = ((PQUEEN,), (QUEEN, ROOK, BISHOP, KNIGHT))


# --- castling geometry (§6.1) -----------------------------------------------

#: (low rook square, high rook square, step, low central square, high central).
#: "low"/"high" are in square-index order along each seat's home row.
_HOME = (
    (sq_of(3, 0), sq_of(10, 0), 1, sq_of(6, 0), sq_of(7, 0)),        # Red
    (sq_of(0, 3), sq_of(0, 10), 16, sq_of(0, 6), sq_of(0, 7)),       # Blue
    (sq_of(3, 13), sq_of(10, 13), 1, sq_of(6, 13), sq_of(7, 13)),    # Yellow
    (sq_of(13, 3), sq_of(13, 10), 16, sq_of(13, 6), sq_of(13, 7)),   # Green
)

SHORT, LONG = 0, 1


def _build_castle_geometry():
    """Precompute castling squares for both possible king homes per seat.

    Derived, never tabulated by hand (§6.1): the short side is whichever rook
    is three squares away, which is always the one the king starts nearer.
    Because a seat's king can only ever sit on one of two central squares while
    it still holds rights, there are just eight entries -- and all five setups
    fall out of the same rule, which is exactly the bug Athena hit (§6.4).
    """
    geo = {}
    for color in range(4):
        low_rook, high_rook, step, lo, hi = _HOME[color]
        for king_sq in (lo, hi):
            sides = {}
            for rook in (low_rook, high_rook):
                direction = step if rook > king_sq else -step
                gap = abs(rook - king_sq) // step
                assert gap in (3, 4), (color, king_sq, rook, gap)
                side = SHORT if gap == 3 else LONG
                king_to = king_sq + 2 * direction
                rook_to = king_sq + direction
                between = tuple(king_sq + direction * i for i in range(1, gap))
                safe = (king_sq, king_sq + direction, king_to)
                sides[side] = (rook, king_to, rook_to, between, safe)
            geo[(color, king_sq)] = sides
    return geo


CASTLE_GEO = _build_castle_geometry()
#: seat home rook squares -> owning seat, for rights bookkeeping
ROOK_HOME = {}
for _c in range(4):
    ROOK_HOME[_HOME[_c][0]] = _c
    ROOK_HOME[_HOME[_c][1]] = _c
del _c


def castle_geometry(color, king_sq):
    """``{side: (rook_from, king_to, rook_to, between, safe)}`` or ``None``.

    ``None`` means the king is not on a home central square, so it has moved
    and holds no rights whatever the FEN4 claimed.
    """
    return CASTLE_GEO.get((color, king_sq))


def rook_sides(color, king_sq):
    """``(short_rook_sq, long_rook_sq)``, or ``None`` if the king is not home."""
    sides = CASTLE_GEO.get((color, king_sq))
    if sides is None:
        return None
    return sides[SHORT][0], sides[LONG][0]


# --- moves ------------------------------------------------------------------
#
# Packed into an int so that move lists compare exactly between the two
# generators and so the encoding ports to C unchanged.
#
#   bits  0-7   from square
#   bits  8-15  to square
#   bits 16-19  flag
#   bits 20-22  promotion piece type, 0 for none
#
# Promotion is orthogonal to the flag rather than being one of its values,
# because an en-passant capture can *also* promote: Blue's skipped square c8
# lies on Red's FFA promotion rank, so a Red pawn on b7 or d7 capturing en
# passant onto c8 promotes as it captures (§4.2, §5.2). One flag value could
# not say both.

F_NORMAL, F_DOUBLE, F_EP, F_CASTLE_SHORT, F_CASTLE_LONG = range(5)


def make_move(frm, to, flag=F_NORMAL, promo=0):
    return frm | (to << 8) | (flag << 16) | (promo << 20)


def is_promotion(m):
    return (m >> 20) & 7 != 0


def mv_from(m):
    return m & 255


def mv_to(m):
    return (m >> 8) & 255


def mv_flag(m):
    return (m >> 16) & 15


def mv_promo(m):
    return (m >> 20) & 7


def move_str(m):
    s = name_of(mv_from(m)) + name_of(mv_to(m))
    promo = (m >> 20) & 7
    if promo:
        s += TYPE_CHARS[promo].lower()
    return s


# --- Zobrist ----------------------------------------------------------------
#
# Covers everything the repetition rule treats as part of the position (§10.2):
# pieces, side to move, both castling arrays, all four en-passant entries and
# the alive mask. Points are deliberately excluded -- they are monotonic, so
# including them would make repetition unreachable. The halfmove clock is not
# hashed either.

_rng = random.Random(0x7E74A2C4)
ZOB_PIECE = [[_rng.getrandbits(64) for _ in range(NSQ)] for _ in range(NPIECE)]
ZOB_TURN = [_rng.getrandbits(64) for _ in range(4)]
ZOB_CK = [_rng.getrandbits(64) for _ in range(4)]
ZOB_CQ = [_rng.getrandbits(64) for _ in range(4)]
ZOB_EP = [[_rng.getrandbits(64) for _ in range(NSQ)] for _ in range(4)]
ZOB_ALIVE = [_rng.getrandbits(64) for _ in range(4)]
del _rng


# --- FEN4 extra-options block (§11.4) ---------------------------------------

#: fen4's preferred emission order is its struct field order.
EXTRA_ORDER = ("royal", "lives", "resigned", "flagged", "stalemated",
               "gameOver", "zombieImmune", "zombieType", "enPassant",
               "pawnsBaseRank", "uniquify", "std2pc")
EXTRA_DEFAULT = {
    "royal": "('','','','')",
    "resigned": "(false,false,false,false)",
    "flagged": "(false,false,false,false)",
    "stalemated": "(false,false,false,false)",
    "gameOver": "''",
    "zombieImmune": "(false,false,false,false)",
    "zombieType": "('','','','')",
    "enPassant": "('','','','')",
    "pawnsBaseRank": "2",
    "uniquify": "0",
    "std2pc": "false",
}


def _split_extra(text):
    """Parse ``{'k':v,...}`` into a dict of raw value strings.

    Value extent follows fen4: a parenthesised value ends at ``)``, anything
    else at the next ``,`` or ``}``.
    """
    if not text.startswith("{") or not text.endswith("}") or text == "{}":
        raise ValueError("malformed extra-options block %r" % text)
    body = text[1:-1]
    out = {}
    i = 0
    while i < len(body):
        colon = body.find(":", i)
        if colon < 0:
            raise ValueError("extra option without ':' in %r" % text)
        key = body[i:colon].strip()
        if len(key) < 2 or key[0] != "'" or key[-1] != "'":
            raise ValueError("extra option key not quoted: %r" % key)
        key = key[1:-1]
        j = colon + 1
        if j < len(body) and body[j] == "(":
            end = body.find(")", j)
            if end < 0:
                raise ValueError("unbalanced parens in %r" % text)
            end += 1
        else:
            end = body.find(",", j)
            if end < 0:
                end = len(body)
        if key == "kingSquares":
            key = "royal"
        if key not in EXTRA_ORDER:
            # fen4 errors on unknown tags rather than dropping them, and so do
            # we: silently discarding position state is how you lose a game to
            # a rule you never implemented.
            raise ValueError("unknown FEN4 option %r" % key)
        if key in out:
            raise ValueError("repeated extra option %r" % key)
        out[key] = body[j:end].strip()
        i = end
        while i < len(body) and body[i] == ",":
            i += 1
    return out


def _tuple4(raw):
    if not (raw.startswith("(") and raw.endswith(")")):
        raise ValueError("expected a 4-tuple, got %r" % raw)
    parts = raw[1:-1].split(",")
    if len(parts) != 4:
        raise ValueError("expected 4 elements in %r" % raw)
    return [p.strip() for p in parts]


def _unquote(raw):
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1]
    raise ValueError("expected a quoted string, got %r" % raw)


def _parse_piece(seg):
    """Parse one FEN4 board segment into a piece code."""
    dead = False
    if seg[0] == "d":
        dead = True
        seg = seg[1:]
        if not seg:
            raise ValueError("bare 'd' is not a piece")
    if len(seg) == 1:
        if not dead:
            raise ValueError("piece %r has no colour" % seg)
        color = DEAD_UNKNOWN
        shape = seg[0]
    elif len(seg) == 2:
        if seg[0] not in "rbyg":
            raise ValueError("bad piece colour %r" % seg[0])
        color = "rbyg".index(seg[0])
        shape = seg[1]
    else:
        raise ValueError("bad piece %r" % seg)
    if shape not in "PNBRQK":
        raise ValueError("unsupported piece shape %r; fairy pieces are not "
                         "implemented" % shape)
    return make_piece(color, "PNBRQK".index(shape))


class Board:
    """A four-player chess position.

    The setup (`classic`, `modern`, `by`, `byg`, `rg`) is deliberately *not*
    stored. It is only a starting-position generator: everything that depends
    on it -- castling squares above all -- is derived from where the king
    actually stands (§6.1).
    """

    __slots__ = ("sq", "turn", "alive", "points", "ck", "cq", "ep", "kings",
                 "halfmove", "mode", "pawn_base_rank", "key", "extra")

    def __init__(self, mode=MODE_TEAMS):
        self.sq = bytearray(NSQ)
        self.turn = RED
        self.alive = [True] * 4
        self.points = [0] * 4
        self.ck = [False] * 4
        self.cq = [False] * 4
        #: per seat: None, or (target square, victim square) (§5.1, §5.2)
        self.ep = [None] * 4
        #: cached king squares; -1 when a seat has no king on the board
        self.kings = [-1] * 4
        self.halfmove = 0
        self.mode = mode
        self.pawn_base_rank = 2
        self.extra = {}
        self.key = 0

    # -- basics --------------------------------------------------------------

    def copy(self):
        b = Board(self.mode)
        b.sq = bytearray(self.sq)
        b.turn = self.turn
        b.alive = list(self.alive)
        b.points = list(self.points)
        b.ck = list(self.ck)
        b.cq = list(self.cq)
        b.ep = list(self.ep)
        b.kings = list(self.kings)
        b.halfmove = self.halfmove
        b.pawn_base_rank = self.pawn_base_rank
        b.extra = dict(self.extra)
        b.key = self.key
        return b

    def __eq__(self, other):
        return (self.sq == other.sq and self.turn == other.turn
                and self.alive == other.alive and self.points == other.points
                and self.ck == other.ck and self.cq == other.cq
                and self.ep == other.ep and self.halfmove == other.halfmove
                and self.mode == other.mode and self.kings == other.kings
                and self.pawn_base_rank == other.pawn_base_rank)

    def find_kings(self):
        self.kings = [-1] * 4
        for sq in SQUARES:
            p = self.sq[sq]
            if p and PC_TYPE[p] == KING:
                c = PC_COLOR[p]
                if c < 4:
                    self.kings[c] = sq
        return self.kings

    def is_enemy(self, piece, mover):
        """Can `mover` capture `piece`? Dead seats' pieces are fair game to all."""
        c = PC_COLOR[piece]
        if c == DEAD_UNKNOWN or not self.alive[c]:
            return True
        return not same_team(self.mode, c, mover)

    def next_turn(self):
        """The next living seat. Elimination removes a seat from the rotation."""
        t = self.turn
        for _ in range(4):
            t = (t + 1) & 3
            if self.alive[t]:
                return t
        return self.turn

    def promo_coord(self):
        return PROMO_COORD[self.mode]

    def promo_choices(self):
        return PROMO_CHOICES[self.mode]

    # -- Zobrist -------------------------------------------------------------

    def recompute_key(self):
        k = ZOB_TURN[self.turn]
        for sq in SQUARES:
            p = self.sq[sq]
            if p:
                k ^= ZOB_PIECE[p][sq]
        for c in range(4):
            if self.ck[c]:
                k ^= ZOB_CK[c]
            if self.cq[c]:
                k ^= ZOB_CQ[c]
            if self.alive[c]:
                k ^= ZOB_ALIVE[c]
            if self.ep[c] is not None:
                k ^= ZOB_EP[c][self.ep[c][0]]
        self.key = k
        return k

    # -- FEN4 in (§11) -------------------------------------------------------

    @classmethod
    def from_fen4(cls, text, mode=MODE_TEAMS):
        b = cls(mode)
        # The board section starts after the LAST '-' in the whole string.
        # fen4 does exactly this (rfind), so a '-' inside a gameOver message
        # would break both parsers identically (§11.3).
        cut = text.rfind("-")
        if cut < 0:
            raise ValueError("no '-' in FEN4")
        meta, board_text = text[:cut], text[cut + 1:]

        fields = [f.strip() for f in meta.strip().split("-")]
        if len(fields) not in (6, 7):
            raise ValueError("FEN4 metadata has %d fields, expected 6 or 7"
                             % len(fields))
        if fields[0] not in SEAT_NAMES:
            raise ValueError("bad turn %r" % fields[0])
        b.turn = SEAT_NAMES.index(fields[0])

        def flags(raw):
            parts = raw.split(",")
            if len(parts) != 4 or any(p.strip() not in ("0", "1") for p in parts):
                raise ValueError("expected four 0/1 values, got %r" % raw)
            return [p.strip() == "1" for p in parts]

        b.alive = [not d for d in flags(fields[1])]
        b.ck = flags(fields[2])
        b.cq = flags(fields[3])

        pts = fields[4].split(",")
        if len(pts) != 4:
            raise ValueError("expected four point values, got %r" % fields[4])
        b.points = [int(p) for p in pts]
        b.halfmove = int(fields[5])

        if len(fields) == 7:
            b.extra = _split_extra(fields[6])
            if "pawnsBaseRank" in b.extra:
                b.pawn_base_rank = int(b.extra["pawnsBaseRank"])
            if "enPassant" in b.extra:
                for c, entry in enumerate(_tuple4(b.extra["enPassant"])):
                    entry = _unquote(entry)
                    if entry:
                        target, victim = entry.split(":")
                        b.ep[c] = (sq_from_name(target), sq_from_name(victim))

        rows = board_text.strip().split("/")
        if len(rows) != 14:
            raise ValueError("FEN4 board has %d ranks, expected 14" % len(rows))
        for row_index, row in enumerate(rows):
            rank = 13 - row_index          # first row written is rank 14
            file = 0
            for seg in row.split(","):
                seg = seg.strip()
                if not seg:
                    raise ValueError("empty segment on rank %d" % (rank + 1))
                if file >= NFILE:
                    raise ValueError("too many files on rank %d" % (rank + 1))
                if seg[0].isdigit():
                    file += int(seg)
                    continue
                if seg in ("X", "x"):
                    # A wall. Athena and 4pchess write the removed corners as
                    # lowercase 'x'; fen4 uses 'X' for genuinely blocked squares
                    # on custom boards (§11.3). We accept both, but only where
                    # the square is already unplayable -- a wall in the middle
                    # of the board is a custom variant we do not implement.
                    if VALID[sq_of(file, rank)]:
                        raise ValueError(
                            "wall on playable square %s; custom boards are not "
                            "supported" % name_of(sq_of(file, rank)))
                    file += 1
                    continue
                b.sq[sq_of(file, rank)] = _parse_piece(seg)
                file += 1
            if file != NFILE:
                raise ValueError("rank %d covers %d files, expected 14"
                                 % (rank + 1, file))

        for sq in range(NSQ):
            if b.sq[sq] and not VALID[sq]:
                raise ValueError("piece on unplayable square %s" % name_of(sq))
        b.find_kings()
        b.recompute_key()
        return b

    # -- FEN4 out ------------------------------------------------------------

    def to_fen4(self):
        """Canonical FEN4, byte-compatible with fen4's writer.

        Newlines after the metadata and between ranks are part of the canonical
        form -- fen4's own round-trip test asserts them (§11.3).
        """
        out = [SEAT_NAMES[self.turn]]
        out.append("-" + ",".join("0" if a else "1" for a in self.alive))
        out.append("-" + ",".join("1" if c else "0" for c in self.ck))
        out.append("-" + ",".join("1" if c else "0" for c in self.cq))
        out.append("-" + ",".join(str(p) for p in self.points))
        out.append("-%d-" % self.halfmove)

        extra = dict(self.extra)
        extra["pawnsBaseRank"] = str(self.pawn_base_rank)
        extra["enPassant"] = "(%s)" % ",".join(
            "'%s:%s'" % (name_of(e[0]), name_of(e[1])) if e else "''"
            for e in self.ep)
        parts = ["'%s':%s" % (k, extra[k]) for k in EXTRA_ORDER
                 if k in extra and extra[k] != EXTRA_DEFAULT.get(k)]
        if parts:
            out.append("{%s}-" % ",".join(parts))
        out.append("\n")

        rows = []
        for rank in range(13, -1, -1):
            cells = []
            empties = 0
            for file in range(NFILE):
                p = self.sq[sq_of(file, rank)]
                if not p:
                    empties += 1
                    continue
                if empties:
                    cells.append(str(empties))
                    empties = 0
                cells.append(self._piece_str(p))
            if empties:
                cells.append(str(empties))
            rows.append(",".join(cells))
        out.append("/\n".join(rows))
        return "".join(out)

    def _piece_str(self, piece):
        color = PC_COLOR[piece]
        shape = TYPE_CHARS[PC_TYPE[piece]]
        if color == DEAD_UNKNOWN:
            return "d" + shape
        if not self.alive[color]:
            return "d" + SEAT_NAMES[color].lower() + shape
        return SEAT_NAMES[color].lower() + shape

    # -- make / unmake -------------------------------------------------------

    def _capture_points(self, victim):
        """What the mover scores for taking `victim` (§8.1). Dead seats' pieces
        are worth nothing (§9.1): they stay on the board as obstacles, and a
        DEAD_UNKNOWN piece owns no seat at all. Teams has no points."""
        if self.mode != MODE_FFA or not victim:
            return 0
        owner = PC_COLOR[victim]
        if owner == DEAD_UNKNOWN or not self.alive[owner]:
            return 0
        return CAPTURE_POINTS[PC_TYPE[victim]]

    def make(self, m):
        """Apply `m`, returning an undo token for `unmake`.

        Pseudo-legal moves are accepted; legality is the generator's job.
        """
        frm, to = m & 255, (m >> 8) & 255
        flag = (m >> 16) & 15
        mover = self.turn
        piece = self.sq[frm]
        captured = self.sq[to]

        victim_sq = -1
        victim = EMPTY
        if flag == F_EP:
            # The pawn removed sits on the recorded victim square, which is NOT
            # the square we move to (§5.2), and the entry belongs to whichever
            # seat pushed -- never to the mover. Seats' target squares can
            # never collide (Red's lie on rank 3, Blue's on file c, and so on),
            # so this scan is unambiguous.
            for c in range(4):
                e = self.ep[c]
                if e is not None and e[0] == to:
                    victim_sq, victim = e[1], self.sq[e[1]]
                    break
            if victim_sq < 0:
                raise ValueError("en-passant move %s with no matching offer"
                                 % move_str(m))

        # Stored rather than recomputed on unmake: §9.1 makes the award depend
        # on the alive mask, which may have moved in between.
        scored = self._capture_points(captured) + self._capture_points(victim)
        undo = (captured, self.ep[:], self.ck[:], self.cq[:], self.kings[:],
                self.halfmove, self.key, mover, victim_sq, victim, scored)
        if scored:
            self.points = list(self.points)
            self.points[mover] += scored

        key = self.key ^ ZOB_TURN[mover]

        # The moving seat's own en-passant offer expires now (§5.1). Everyone
        # else's survives -- that is what gives the square its three-ply life.
        if self.ep[mover] is not None:
            key ^= ZOB_EP[mover][self.ep[mover][0]]
            self.ep = self.ep[:]
            self.ep[mover] = None

        if captured:
            key ^= ZOB_PIECE[captured][to]
        key ^= ZOB_PIECE[piece][frm]
        self.sq[frm] = EMPTY

        promo = (m >> 20) & 7
        placed = make_piece(mover, promo) if promo else piece

        if flag == F_EP:
            key ^= ZOB_PIECE[victim][victim_sq]
            self.sq[victim_sq] = EMPTY
            self.ep = self.ep[:]
            for c in range(4):
                e = self.ep[c]
                if e is not None and e[1] == victim_sq:
                    key ^= ZOB_EP[c][e[0]]
                    self.ep[c] = None
            self.sq[to] = placed
            key ^= ZOB_PIECE[placed][to]
        else:
            self.sq[to] = placed
            key ^= ZOB_PIECE[placed][to]
            if flag == F_DOUBLE:
                target = (frm + to) // 2
                self.ep = self.ep[:]
                self.ep[mover] = (target, to)
                key ^= ZOB_EP[mover][target]
            elif flag == F_CASTLE_SHORT or flag == F_CASTLE_LONG:
                side = SHORT if flag == F_CASTLE_SHORT else LONG
                rook_from, _kt, rook_to, _b, _s = CASTLE_GEO[(mover, frm)][side]
                rook = self.sq[rook_from]
                self.sq[rook_from] = EMPTY
                self.sq[rook_to] = rook
                key ^= ZOB_PIECE[rook][rook_from] ^ ZOB_PIECE[rook][rook_to]

        if PC_TYPE[piece] == KING:
            self.kings = self.kings[:]
            self.kings[mover] = to
        if captured and PC_TYPE[captured] == KING:
            c = PC_COLOR[captured]
            if c < 4:
                self.kings = self.kings[:]
                self.kings[c] = -1

        key ^= self._drop_castling(frm, to, piece)

        if PC_TYPE[piece] == PAWN or captured or flag == F_EP:
            self.halfmove = 0
        else:
            self.halfmove += 1

        self.turn = self.next_turn()
        key ^= ZOB_TURN[self.turn]
        self.key = key
        return undo

    def _drop_castling(self, frm, to, piece):
        """Clear rights this move invalidates. Returns the Zobrist delta.

        Three ways to lose a right: the king moves, a rook leaves its home
        square, or a rook is captured on its home square. Athena checks none of
        these against the actual king square, which is how its castle table
        drifted out of sync with its own start position (§6.4).
        """
        delta = 0
        color = PC_COLOR[piece]
        if PC_TYPE[piece] == KING and color < 4:
            if self.ck[color]:
                self.ck = self.ck[:]
                self.ck[color] = False
                delta ^= ZOB_CK[color]
            if self.cq[color]:
                self.cq = self.cq[:]
                self.cq[color] = False
                delta ^= ZOB_CQ[color]
        for sq in (frm, to):
            c = ROOK_HOME.get(sq)
            if c is None or not (self.ck[c] or self.cq[c]):
                continue
            sides = rook_sides(c, self.kings[c])
            if sides is None:
                continue          # king already away: the rights are not real
            if sq == sides[0]:
                if self.ck[c]:
                    self.ck = self.ck[:]
                    self.ck[c] = False
                    delta ^= ZOB_CK[c]
            elif sq == sides[1]:
                if self.cq[c]:
                    self.cq = self.cq[:]
                    self.cq[c] = False
                    delta ^= ZOB_CQ[c]
        return delta

    def unmake(self, m, undo):
        (captured, ep, ck, cq, kings, halfmove, key, mover,
         victim_sq, victim, scored) = undo
        if scored:
            self.points = list(self.points)
            self.points[mover] -= scored
        frm, to = m & 255, (m >> 8) & 255
        flag = (m >> 16) & 15

        if (m >> 20) & 7:
            self.sq[frm] = make_piece(mover, PAWN)
        else:
            self.sq[frm] = self.sq[to]
        self.sq[to] = captured

        if flag == F_EP:
            self.sq[victim_sq] = victim
        elif flag == F_CASTLE_SHORT or flag == F_CASTLE_LONG:
            side = SHORT if flag == F_CASTLE_SHORT else LONG
            rook_from, _kt, rook_to, _b, _s = CASTLE_GEO[(mover, frm)][side]
            self.sq[rook_from] = self.sq[rook_to]
            self.sq[rook_to] = EMPTY

        self.ep = ep
        self.ck = ck
        self.cq = cq
        self.kings = kings
        self.halfmove = halfmove
        self.turn = mover
        self.key = key


# --- starting positions (§3) ------------------------------------------------

_CLASSIC = (
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

#: Each preset is `classic` with the named seats' king and queen exchanged
#: (§3.2). Adding a sixth arrangement costs one line.
SETUP_SWAPS = {
    "classic": (),
    "modern": (BLUE, GREEN),
    "by": (BLUE, YELLOW),
    "byg": (BLUE, YELLOW, GREEN),
    "rg": (RED, GREEN),
}
SETUPS = tuple(SETUP_SWAPS)
DEFAULT_SETUP = "classic"


def _build_rot90():
    """(file, rank) -> (rank, 13 - file). Maps the board onto itself, corners
    included, and sends each seat's home to the next seat's clockwise."""
    table = list(range(NSQ))
    for sq in range(NSQ):
        f, r = file_of(sq), rank_of(sq)
        if f < NFILE and r < NRANK:
            table[sq] = sq_of(r, 13 - f)
    return table


ROT90 = _build_rot90()


def rotate(b, quarter_turns):
    """The position turned `quarter_turns` x 90 degrees clockwise.

    Seats shift with the board: Red's army lands where Blue's was, and so on.
    Used by `match.py` to play one opening from all four orientations, which is
    how seat bias is cancelled -- in FFA seat identity is worth more Elo than
    most engine changes, so this is not optional.
    """
    quarter_turns &= 3
    if quarter_turns == 0:
        return b.copy()
    out = Board(b.mode)
    out.halfmove = b.halfmove
    out.pawn_base_rank = b.pawn_base_rank
    out.extra = dict(b.extra)

    mapping = list(range(NSQ))
    for _ in range(quarter_turns):
        mapping = [ROT90[sq] for sq in mapping]

    for sq in SQUARES:
        p = b.sq[sq]
        if not p:
            continue
        color = PC_COLOR[p]
        if color != DEAD_UNKNOWN:
            color = (color + quarter_turns) & 3
        out.sq[mapping[sq]] = make_piece(color, PC_TYPE[p])

    for c in range(4):
        nc = (c + quarter_turns) & 3
        out.alive[nc] = b.alive[c]
        out.points[nc] = b.points[c]
        out.ck[nc] = b.ck[c]
        out.cq[nc] = b.cq[c]
        out.ep[nc] = (None if b.ep[c] is None
                      else (mapping[b.ep[c][0]], mapping[b.ep[c][1]]))
    out.turn = (b.turn + quarter_turns) & 3
    out.find_kings()
    out.recompute_key()
    return out


def start_board(setup=DEFAULT_SETUP, mode=MODE_TEAMS):
    if setup not in SETUP_SWAPS:
        raise ValueError("unknown setup %r; expected one of %s"
                         % (setup, ", ".join(SETUPS)))
    b = Board.from_fen4(_CLASSIC, mode)
    for color in SETUP_SWAPS[setup]:
        lo, hi = _HOME[color][3], _HOME[color][4]
        assert {b.sq[lo], b.sq[hi]} == {make_piece(color, KING),
                                        make_piece(color, QUEEN)}
        b.sq[lo], b.sq[hi] = b.sq[hi], b.sq[lo]
    b.find_kings()
    b.recompute_key()
    return b
