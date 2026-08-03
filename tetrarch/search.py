"""Root search: iterative deepening and time management.

The architecture split (project brief, constraint 1): Python owns the root, the
C core owns the per-node loop. So this file decides *how deep* and *for how
long*, and `core.search` does the actual alpha-beta.

Also holds `reference_score`, a deliberately unpruned Python negamax used only
by `selftest.py`. Alpha-beta with a transposition table must return exactly the
same root score as plain minimax over the same tree -- that is the correctness
theorem for the whole search, and it is the only thing that would catch a TT
that silently returns wrong values. Pinned node counts would not: a wrong score
still produces a node count.

Teams only for now. In Teams the seat rotation alternates team every ply
(team = seat & 1, turn advances by one), so this is genuine two-player
zero-sum and plain negamax applies (§2). FFA needs paranoid search and arrives
in Phase 5.

Section references (§n) are to docs/RULES.md.
"""

import time

from .board import MODE_TEAMS, move_str
from . import core
from . import movegen as gen
from .eval_hand import evaluate

MATE_SCORE = 30000
INF = 32000
#: Do not start a depth we are unlikely to finish.
NEXT_DEPTH_FRACTION = 0.45


class Limits:
    """What bounds this search. Exactly one instrument, never a mixture."""

    def __init__(self, depth=None, nodes=None, movetime=None, clock=None,
                 inc=None, max_depth=48):
        self.depth = depth
        self.nodes = nodes
        self.movetime = movetime          # milliseconds for this move
        self.clock = clock                # milliseconds left for this seat
        self.inc = inc or 0
        self.max_depth = max_depth

    def budget_ms(self):
        """Milliseconds to spend on this move, or None for no clock bound."""
        if self.movetime is not None:
            return self.movetime
        if self.clock is not None:
            # Deliberately simple: a fixed fraction plus the increment. Time
            # management is a tuning target for Phase 7, not something to
            # invent now and then be unable to A/B against.
            return max(1, int(self.clock / 25.0) + int(self.inc * 0.75))
        return None


class Result:
    def __init__(self):
        self.best = 0
        self.score = 0
        self.depth = 0
        self.nodes = 0
        self.elapsed = 0.0
        #: Filled by search_multi only. The normal path leaves it empty and
        #: the caller walks the table itself, because there the root's own
        #: search stored every node below it.
        self.pv = []

    @property
    def nps(self):
        return int(self.nodes / self.elapsed) if self.elapsed > 0 else 0


def search(board, limits, info=None, repetitions=()):
    """Iterative deepening. Returns a `Result`.

    `info` is called with the Result after each completed depth, for the
    protocol's `info` lines. `repetitions` is the keys of the positions the
    game already passed through, so the search can see a repetition it is
    walking into (§10.2); the root's own key is not among them.
    """
    # Not asserts: python3 -O strips them, and this is the gate that stops a
    # negamax search running on an FFA position, where the score does not
    # negate ply to ply and the answer is silently meaningless (§2).
    if board.mode != MODE_TEAMS:
        raise ValueError("FFA search arrives in Phase 5 (§ brief)")
    if not all(board.alive):
        raise ValueError("Teams ends when a seat is mated, so no seat is dead "
                         "during search (§7)")

    started = time.perf_counter()
    budget = limits.budget_ms()
    out = Result()

    legal = gen.gen_legal(board)
    if not legal:
        return out
    out.best = legal[0]

    max_depth = limits.depth or limits.max_depth
    for depth in range(1, max_depth + 1):
        remaining_nodes = 0
        if limits.nodes:
            remaining_nodes = limits.nodes - out.nodes
            if remaining_nodes <= 0:
                break

        r = core.search(board, depth, remaining_nodes, repetitions)
        out.nodes += r.nodes
        out.elapsed = time.perf_counter() - started

        if r.aborted:
            # The node budget ran out mid-depth; that depth's score is
            # meaningless, so keep the last completed one.
            break

        out.best = r.best or out.best
        out.score = r.score
        out.depth = depth
        if info:
            info(out)

        if abs(out.score) >= MATE_SCORE - 100:
            break
        if budget is not None:
            elapsed_ms = out.elapsed * 1000.0
            if elapsed_ms >= budget * NEXT_DEPTH_FRACTION:
                break
        if limits.nodes and out.nodes >= limits.nodes:
            break

    return out


def search_multi(board, limits, lines=1, info=None, repetitions=()):
    """The best `lines` root moves, each with its own exact score.

    With `lines == 1` this defers to `search()` exactly, so the engine's own
    play is untouched by the feature existing -- every A/B in docs/AB.md was
    measured on that path and none of them is invalidated here.

    Above 1 it is an ANALYSIS mode and costs accordingly. Alpha-beta at the
    root only produces an exact score for the best move; the rest come back as
    bounds, which is useless for ranking. So each root move is searched with a
    full window instead, and the root loses its cutoffs. Expect several times
    the nodes of a normal search to the same depth. That is the price of the
    other lines being real numbers rather than upper bounds.

    Returns a list of `Result`, best first.
    """
    if board.mode != MODE_TEAMS:
        raise ValueError("FFA search arrives in Phase 5 (§ brief)")
    if not all(board.alive):
        raise ValueError("Teams ends when a seat is mated, so no seat is dead "
                         "during search (§7)")

    legal = gen.gen_legal(board)
    if not legal:
        return []
    if lines <= 1:
        best = search(board, limits, info, repetitions)
        return [best] if best.best else []

    lines = min(lines, len(legal))
    started = time.perf_counter()
    budget = limits.budget_ms()
    max_depth = limits.depth or limits.max_depth
    nodes = 0
    out = []
    # Each line is searched from the child, so the root itself joins the played
    # history -- otherwise a line that walks straight back into the current
    # position would not be seen as the repetition it is.
    history = list(repetitions) + [board.key]
    # Searched best-first at each new depth, which is worth a lot even without
    # root cutoffs: the transposition table is warm for the good moves.
    order = list(legal)

    for depth in range(2, max_depth + 1):
        scored, aborted = [], False
        for move in order:
            remaining = 0
            if limits.nodes:
                remaining = limits.nodes - nodes
                if remaining <= 0:
                    aborted = True
                    break
            undo = board.make(move)
            child = core.search(board, depth - 1, remaining, history)
            board.unmake(move, undo)
            nodes += child.nodes
            if child.aborted:
                aborted = True
                break
            scored.append((-child.score, move, child.best))
            if budget is not None and \
                    (time.perf_counter() - started) * 1000.0 >= budget:
                aborted = True
                break

        if aborted or len(scored) != len(order):
            break                       # a partial depth cannot be ranked

        scored.sort(key=lambda triple: -triple[0])
        order = [move for _, move, _ in scored]

        out = []
        for score, move, child_best in scored[:lines]:
            item = Result()
            item.best, item.score, item.depth = move, score, depth
            item.nodes, item.elapsed = nodes, time.perf_counter() - started
            # Each child was searched as its own root, and tt_search does not
            # store a root -- so the table has nothing for the position right
            # after `move`. Seed the walk with the child's own best move and
            # the table supplies the rest.
            undo = board.make(move)
            item.pv = [move] + core.pv(board, child_best)
            board.unmake(move, undo)
            out.append(item)
        if info:
            info(out)

        if abs(out[0].score) >= MATE_SCORE - 100:
            break
        if budget is not None and \
                out[0].elapsed * 1000.0 >= budget * NEXT_DEPTH_FRACTION:
            break
        if limits.nodes and nodes >= limits.nodes:
            break

    if not out:                          # never finished depth 2
        best = search(board, limits)
        out = [best] if best.best else []
    return out


# --- reference, used only by selftest ---------------------------------------

def _quiesce(board, alpha, beta, ply=0):
    """Unpruned quiescence: stand pat, then every capture.

    In check it searches every legal move instead of standing pat, and reports
    mate when there are none -- mirroring QSEvasions, which is CONFIRMED into
    the C default (+106.78 +/- 6.88, docs/AB.md). The oracle has to model the
    default, or the theorem it proves is about a search nobody runs.
    """
    in_chk = gen.in_check(board, board.turn)
    if in_chk:
        stand = -INF                    # a check cannot be declined
    else:
        stand = evaluate(board)
        if stand >= beta:
            return stand
        if stand > alpha:
            alpha = stand
    legal = 0
    for m in gen.gen_legal(board):
        to = (m >> 8) & 255
        if not in_chk and not board.sq[to] and ((m >> 16) & 15) != 2:
            continue                                        # not a capture
        legal += 1
        undo = board.make(m)
        score = -_quiesce(board, -beta, -alpha, ply + 1)
        board.unmake(m, undo)
        if score >= beta:
            return score
        if score > alpha:
            alpha = score
    if in_chk and legal == 0:
        return -(MATE_SCORE - ply)
    return alpha


def reference_score(board, depth, ply=0):
    """Plain negamax with a full window: no pruning, no ordering, no TT.

    The value the C search must return. Slow by construction -- this is a
    correctness oracle, not an engine.
    """
    if depth <= 0:
        return _quiesce(board, -INF, INF, ply)
    legal = gen.gen_legal(board)
    if not legal:
        return -(MATE_SCORE - ply) if gen.in_check(board, board.turn) else 0
    best = -INF
    for m in legal:
        undo = board.make(m)
        score = -reference_score(board, depth - 1, ply + 1)
        board.unmake(m, undo)
        if score > best:
            best = score
    return best


def pv_string(result):
    return move_str(result.best) if result.best else "0000"
