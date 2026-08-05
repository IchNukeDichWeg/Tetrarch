"""PGN4 reading and writing.

The standard notation for four-player games. Section references (§n) are to
docs/RULES.md; §11.5 has the summary and this file has the details.

    [Variant "Teams"]
    [Site "4-player-chess"]
    [Result "0-1"]

    1. d2-d4 .. b8-c8 .. k13-k11 .. m8-l8
    2. d4-d5 .. b4-d4 .. k11-k10 .. Qn8-m8
    3. e2-e4 .. Qa7-b8 .. g13-g12 .. Nn5-l6 { comment }
    8. #

Movetext is long algebraic and always carries both squares, so resolving a move
never needs disambiguation -- the token is matched against the legal move list
by from/to, which also validates it. A token that matches nothing is reported
with the ply it failed at rather than silently skipped.

Parsing is deliberately permissive, because the input is whatever a human
pasted out of a real game: unknown tags are kept, comments and clock annotations
are dropped, and the elimination markers (`#` checkmate, `R` resign, `T`
timeout, `S` stalemate) are recorded rather than treated as moves.
"""

import re

from .board import (
    Board, start_board, SETUPS, DEFAULT_SETUP, MODE_FFA, MODE_TEAMS, SEAT_NAMES,
    TYPE_CHARS, PC_TYPE, PC_COLOR, PAWN, mv_from, mv_to, mv_flag, mv_promo,
    name_of, sq_from_name, move_str, F_CASTLE_SHORT, F_CASTLE_LONG, F_EP,
)
from . import movegen as gen
from . import game as game_rules

TAG_RE = re.compile(r'\[\s*(\w+)\s*"(.*?)"\s*\]', re.DOTALL)
COMMENT_RE = re.compile(r"\{.*?\}", re.DOTALL)
MOVE_NUMBER_RE = re.compile(r"^\d+\.$")
#: `Qf4xh2+`, `d2-d4`, `h7-h8=Q`, `O-O`
MOVE_RE = re.compile(
    r"^(?P<piece>[KQRBNP])?"
    r"(?P<frm>[a-n](?:1[0-4]|[1-9]))"
    r"(?P<sep>[-x])"
    r"(?P<to>[a-n](?:1[0-4]|[1-9]))"
    r"(?:=(?P<promo>[QRBNqrbn]))?"
    r"(?P<suffix>[+#]*)$")
#: A seat leaving the game, written where its move would be.
TERMINATORS = {"#": "checkmate", "R": "resign", "T": "timeout",
               "S": "stalemate", "D": "draw"}


#: Named starting positions written into StartFen4 instead of a
#: position. CONFIRMED by reading its own exports for all five setups in both
#: modes (§3.6): the code names the POSITION, so it is the same in Teams and
#: FFA, and `modern` carries no tag at all because it is the live default.
NAMED_STARTS = {
    "4pc": "modern",
    "4pco": "classic",
    "4pcb": "by",
    "4pcn": "byg",
    "4pcrg": "rg",
}
#: The other direction, with the standard casing -- the lookup above is
#: lowercased, but what we WRITE has to match its exports character for
#: character. `modern` is absent on purpose: no tag is written for it,
#: and matching that is what makes our PGN4 load there.
START_NAMES = {
    "classic": "4PCo",
    "by": "4PCb",
    "byg": "4PCn",
    "rg": "4PCrg",
}

#: What goes in RuleVariants, verbatim from real exports. Teams names
#: only en passant; FFA adds the dead-king rule and its promotion override.
RULE_VARIANTS = {
    MODE_TEAMS: "EnPassant",
    MODE_FFA: "DeadKingWalking EnPassant PromoteTo=D",
}


class Pgn4Error(ValueError):
    """Carries the ply so the viewer can show exactly where it broke."""

    def __init__(self, message, ply=None, token=None):
        super().__init__(message)
        self.ply = ply
        self.token = token


class Game:
    __slots__ = ("tags", "tokens", "start", "mode", "setup")

    def __init__(self, tags, tokens, start, mode, setup):
        self.tags = tags
        self.tokens = tokens        # move and terminator tokens, in order
        self.start = start          # the starting Board
        self.mode = mode
        self.setup = setup

    @property
    def variant(self):
        return self.tags.get("Variant", "Teams")


def parse(text):
    """Parse PGN4 into a `Game`. Raises `Pgn4Error` on malformed input."""
    tags = {key: value for key, value in TAG_RE.findall(text)}

    body = TAG_RE.sub(" ", text)
    body = COMMENT_RE.sub(" ", body)

    variant = tags.get("Variant", "Teams").strip().lower()
    mode = MODE_FFA if variant.startswith("ffa") or variant.startswith("free") \
        else MODE_TEAMS

    setup = _named_setup(tags) or DEFAULT_SETUP

    # StartFen4 does not always hold a position. Real exports
    # carry a NAMED start -- [StartFen4 "4PCo"] -- and feeding that to the FEN4
    # reader raises, which used to reject a real game outright.
    #
    # A FEN4 always contains the '-' separating its metadata from the ranks
    # (§11.1), and no named code does, so the two are told apart by that rather
    # than by a list of names nobody has enumerated.
    named = None
    for key in ("StartFen4", "StartFen"):
        if key in tags:
            named = tags[key].strip()
            break
    if named and "-" in named:
        start = Board.from_fen4(named, mode)
    else:
        if named:
            setup = NAMED_STARTS.get(named.lower(), setup)
        elif "RuleVariants" in tags and not _named_setup(tags):
            # No StartFen4 at all is genuinely ambiguous: it means "whatever
            # the default was when this was written", and that was classic
            # before 2022 and modern after. A RuleVariants tag is the tell --
            # one is written on every modern export, and the pre-2022
            # files that omit StartFen4 have no RuleVariants either.
            setup = "modern"
        start = start_board(setup, mode)

    tokens = []
    for raw in body.replace("..", " ").split():
        token = raw.strip()
        if not token or MOVE_NUMBER_RE.match(token):
            continue
        if token in TERMINATORS:
            tokens.append(token)
            continue
        # Results and stray annotations: 1-0, 0-1, 1/2-1/2, "Red: 31" etc.
        if _is_result(token):
            continue
        if MOVE_RE.match(token) or token.upper().startswith("O-O"):
            tokens.append(token)
            continue
        # Anything else is annotation noise; keep going rather than refuse a
        # game because a field we have not seen was added.
    return Game(tags, tokens, start, mode, setup)


def _named_setup(tags):
    """A setup named outright by one of the tags, or None."""
    for key in ("Setup", "SubVariant", "RuleVariants"):
        value = tags.get(key, "").strip().lower()
        for candidate in ("classic", "modern", "byg", "by", "rg"):
            if candidate in value:
                return candidate
    return None


def _is_result(token):
    if token in ("1-0", "0-1", "1/2-1/2", "*"):
        return True
    return bool(re.match(r"^[\d./:+-]+$", token)) and not MOVE_RE.match(token)


def resolve(board, token):
    """The legal move this token names, or None if it is a terminator."""
    stripped = token.rstrip("+#") or token      # a bare "#" strips to nothing
    if stripped in TERMINATORS:
        return None

    legal = gen.gen_legal(board)

    if stripped.upper().startswith("O-O"):
        want = F_CASTLE_LONG if stripped.upper() == "O-O-O" else F_CASTLE_SHORT
        for m in legal:
            if mv_flag(m) == want:
                return m
        raise Pgn4Error("castling %s is not legal here" % token, token=token)

    match = MOVE_RE.match(token)
    if not match:
        raise Pgn4Error("cannot read move %r" % token, token=token)
    frm = sq_from_name(match.group("frm"))
    to = sq_from_name(match.group("to"))
    promo = match.group("promo")

    candidates = [m for m in legal if mv_from(m) == frm and mv_to(m) == to]
    if not candidates:
        raise Pgn4Error("%s is not legal for %s here"
                        % (token, SEAT_NAMES[board.turn]), token=token)
    if promo:
        letter = promo.upper()
        for m in candidates:
            if mv_promo(m) and TYPE_CHARS[mv_promo(m)] == letter:
                return m
        # FFA forces a 1-point queen and forbids underpromotion (§4.2), so a
        # written =Q there still resolves.
        if letter == "Q":
            for m in candidates:
                if mv_promo(m):
                    return m
        raise Pgn4Error("no promotion to %s at %s" % (letter, token),
                        token=token)
    return candidates[0]


def replay(game, limit=None):
    """Step the game. Returns a list of frames, one per position.

    Frame 0 is the starting position; frame n follows the nth move. Each frame
    carries the FEN4 and enough state for a viewer to render it without
    knowing any rules.
    """
    board = game.start.copy()
    frames = [_frame(board, None, None, 0)]
    terminations = []

    for ply, token in enumerate(game.tokens):
        if limit is not None and ply >= limit:
            break
        # `or token`: a bare "#" is the checkmate TERMINATOR, and stripping
        # "+#" from it leaves nothing, so it fell through and was read as a
        # move. Unreachable until something wrote one.
        stripped = token.rstrip("+#") or token
        if stripped in TERMINATORS:
            terminations.append({"ply": ply, "seat": SEAT_NAMES[board.turn],
                                 "reason": TERMINATORS[stripped]})
            # The seat leaves; its pieces stay on the board as dead material
            # and the rotation skips it (§9).
            #
            # Checkmate and stalemate pay §8.2, so they go through the board's
            # own elimination and the replayed points match the played ones.
            # Resign and timeout do NOT: those are §9.2's dead king walking,
            # whose scoring is out of scope, and paying them +20 would invent
            # points no game awarded.
            if TERMINATORS[stripped] in ("checkmate", "stalemate"):
                board.eliminate(board.turn)
            else:
                board.alive[board.turn] = False
                board.turn = board.next_turn()
                board.recompute_key()
            frames.append(_frame(board, None, token, ply + 1))
            continue
        move = resolve(board, token)
        board.make(move)
        frames.append(_frame(board, move, token, ply + 1))

    return frames, terminations


def _frame(board, move, token, ply):
    return {
        "ply": ply,
        "fen4": board.to_fen4().replace("\n", ""),
        "turn": SEAT_NAMES[board.turn],
        "alive": list(board.alive),
        "points": list(board.points),
        "halfmove": board.halfmove,
        "move": move_str(move) if move else None,
        "token": token,
        "check": [gen.in_check(board, c) if board.alive[c] else False
                  for c in range(4)],
    }


# --- writing ----------------------------------------------------------------

def move_token(board, move):
    """The PGN4 token for `move`, given the position before it is played."""
    flag = mv_flag(move)
    if flag == F_CASTLE_SHORT:
        return "O-O"
    if flag == F_CASTLE_LONG:
        return "O-O-O"

    piece = board.sq[mv_from(move)]
    letter = "" if PC_TYPE[piece] == PAWN else TYPE_CHARS[PC_TYPE[piece]]
    captures = bool(board.sq[mv_to(move)]) or flag == F_EP
    token = "%s%s%s%s" % (letter, name_of(mv_from(move)),
                          "x" if captures else "-", name_of(mv_to(move)))
    if mv_promo(move):
        token += "=" + TYPE_CHARS[mv_promo(move)]

    after = board.copy()
    after.make(move)
    checked = [c for c in range(4)
               if after.alive[c] and gen.in_check(after, c)]
    if checked:
        mated = all(not gen.gen_legal(_at_turn(after, c)) for c in checked)
        token += "#" if mated else "+"
    return token


def _at_turn(board, color):
    other = board.copy()
    other.turn = color
    other.recompute_key()
    return other


def start_code(start):
    """The standard name for this position, "" for modern, or None.

    None means "not a standard start", and the caller has to write a real
    FEN4. Modern returns "" because no tag is written for it, and a
    reader that defaults to modern would be misled by an explicit one.
    """
    for setup in SETUPS:
        if start == start_board(setup, start.mode):
            return START_NAMES.get(setup, "")
    return None


def write(start, moves, tags=None):
    """PGN4 text for a game given its starting position and move list.

    A standard start is written the standard way -- the named code,
    or no tag at all for modern -- rather than as a FEN4 blob, so the result
    loads in other 4PC viewers. Anything else still gets a real FEN4.
    """
    given = dict(tags or {})
    if "StartFen4" not in given:
        code = start_code(start)
        if code is None:
            given["StartFen4"] = start.to_fen4().replace("\n", "")
        elif code:
            given["StartFen4"] = code
    given.setdefault("Variant", "Teams" if start.mode == MODE_TEAMS else "FFA")
    given.setdefault("RuleVariants", RULE_VARIANTS[start.mode])
    given.setdefault("Site", "Tetrarch")
    # the standard order, so a diff against a real export is short.
    order = ["StartFen4", "Variant", "RuleVariants", "CurrentMove",
             "TimeControl", "Site"]
    tags = {k: given[k] for k in order if k in given}
    tags.update({k: v for k, v in given.items() if k not in tags})

    lines = ['[%s "%s"]' % (key, value) for key, value in tags.items()]
    lines.append("")

    board = start.copy()
    tokens = []
    for move in moves:
        tokens.append(move_token(board, move))
        board.make(move)
        # An FFA seat with no legal moves leaves the game, and the file has to
        # SAY so: readers advance past it on a standalone terminator token and
        # have no other way to know (§7). Without one, a replay plays the next
        # move for the wrong seat -- and so did this loop, so every token it
        # wrote after the first elimination was resolved from the wrong turn.
        # A cascade is possible, hence the loop.
        while True:
            why = game_rules.eliminate_stuck(board)
            if why is None:
                break
            tokens.append("#" if why == "checkmate" else "S")
    # Four slots to a round, terminators included: a reader consumes tokens in
    # order and each one advances the turn, so the grouping is unchanged.
    for i in range(0, len(tokens), 4):
        lines.append("%d. %s" % (i // 4 + 1, " .. ".join(tokens[i:i + 4])))
    return "\n".join(lines) + "\n"
