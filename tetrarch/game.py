"""What happens when the seat to move has no legal moves.

The two modes disagree completely, and every game loop -- the GUI, the match
runner, data generation -- has to make the same call. It lives here once rather
than three times, because the failure mode of a second copy is a game that ends
differently depending on which program is playing it.

Teams ends at the first stuck seat: checkmating one opponent wins for the whole
team, and stalemate is a draw (§7). FFA does not end -- the stuck seat is
eliminated, its pieces stay on the board as obstacles worth nothing (§9.1), and
the rest play on until one seat is left.

Section references (§n) are to docs/RULES.md.
"""

from .board import MODE_TEAMS, SEAT_NAMES
from . import movegen as gen

#: A draw pays every seat still in the game +10 (§8.2): threefold repetition,
#: insufficient material, or fifty moves. Teams draws pay nothing.
DRAW_POINTS = 10


def resolve(board):
    """Play out every forced elimination and report whether the game is over.

    Mutates `board`: in FFA the eliminations are part of the position. Returns
    None while play continues, else a dict with `over` (a short reason),
    `seat`, and `text`.

    Terminates: each pass either finds a legal move and returns, or removes a
    seat, and the alive mask only shrinks.
    """
    while True:
        if gen.gen_legal(board):
            return None

        seat = board.turn
        checked = gen.in_check(board, seat)

        if board.mode == MODE_TEAMS:
            over = "checkmate" if checked else "stalemate"
            return {"over": over, "seat": seat,
                    "text": "%s %s" % (over, SEAT_NAMES[seat])}

        board.eliminate(seat)
        if sum(board.alive) <= 1:
            winner = next(s for s in range(4) if board.alive[s])
            return {"over": "last seat standing", "seat": winner,
                    "text": "%s wins on %d points"
                            % (SEAT_NAMES[winner], board.points[winner])}


def award_draw(board):
    """+10 to every seat still in the game (§8.2). FFA only; Teams draws pay
    nothing, and the caller decides what counts as a draw -- repetition,
    fifty moves, insufficient material -- because each is detected elsewhere."""
    if board.mode == MODE_TEAMS:
        return
    board.points = list(board.points)
    for seat in range(4):
        if board.alive[seat]:
            board.points[seat] += DRAW_POINTS


def ffa_score(board, seat):
    """One seat's share of its three pairwise contests against the others.

    Survival outranks points: the game ends when three seats are eliminated
    (§7), so the survivor wins however few points it holds. Ties split rather
    than being broken on seat index -- two seats out on equal points are
    genuinely level, and breaking that tie by index would put seat bias back
    into the one number the rotation exists to remove.

    Ranges 0..1 with 0.5 the expectation between equal seats, and the four
    seats always sum to 2. That is what lets match.py feed it straight to
    elo(), and what makes it usable as a per-seat training target beside the
    Teams result on the same [0,1] scale.
    """
    def rank(s):
        return (not board.alive[s], -board.points[s])

    mine = rank(seat)
    won = 0.0
    for other in range(4):
        if other == seat:
            continue
        theirs = rank(other)
        won += 1.0 if mine < theirs else (0.5 if mine == theirs else 0.0)
    return won / 3.0


def placement(board):
    """Final seat order, best first: survival outranks points.

    The game ends when three seats are eliminated (§7), so the survivor wins
    however few points it holds. Points rank everyone else, and among seats
    eliminated with equal points the earlier seat index breaks the tie -- an
    arbitrary but stable rule, so a match summary never depends on dict order.
    """
    return sorted(range(4),
                  key=lambda s: (not board.alive[s], -board.points[s], s))
