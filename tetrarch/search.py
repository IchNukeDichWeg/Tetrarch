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

    @property
    def nps(self):
        return int(self.nodes / self.elapsed) if self.elapsed > 0 else 0


def search(board, limits, info=None):
    """Iterative deepening. Returns a `Result`.

    `info` is called with the Result after each completed depth, for the
    protocol's `info` lines.
    """
    assert board.mode == MODE_TEAMS, "FFA search arrives in Phase 5 (§ brief)"
    assert all(board.alive), \
        "Teams ends when a seat is mated, so no seat is dead during search (§7)"

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

        r = core.search(board, depth, remaining_nodes)
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


# --- reference, used only by selftest ---------------------------------------

def _quiesce(board, alpha, beta):
    """Unpruned quiescence: stand pat, then every capture."""
    stand = evaluate(board)
    if stand >= beta:
        return stand
    if stand > alpha:
        alpha = stand
    for m in gen.gen_legal(board):
        to = (m >> 8) & 255
        if not board.sq[to] and ((m >> 16) & 15) != 2:      # not a capture
            continue
        undo = board.make(m)
        score = -_quiesce(board, -beta, -alpha)
        board.unmake(m, undo)
        if score >= beta:
            return score
        if score > alpha:
            alpha = score
    return alpha


def reference_score(board, depth, ply=0):
    """Plain negamax with a full window: no pruning, no ordering, no TT.

    The value the C search must return. Slow by construction -- this is a
    correctness oracle, not an engine.
    """
    if depth <= 0:
        return _quiesce(board, -INF, INF)
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
