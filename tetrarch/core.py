"""ctypes binding to the C core.

The C library declares no chess constants of its own. Everything it needs --
VALID, COMPACT, the Zobrist keys, the piece encoding, pawn geometry, promotion
ranks and castling squares -- is built here from `board.py` and pushed across
in one `tt_init` call. `board.py` stays the single definition; a duplicated
table is a divergence waiting for one side to be edited.

Struct layouts are asserted against the C `sizeof` at load time, so a mismatch
is a loud startup failure rather than silently misread memory.

The library is located relative to this file, never from a hardcoded path.
"""

import ctypes
import os
import sys

from . import board as B
from . import eval_hand

NSQ = B.NSQ
NPIECE = B.NPIECE
MAX_MOVES = 1024
DEFAULT_TT_MB = 64

_HERE = os.path.dirname(os.path.abspath(__file__))
#: TETRARCH_LIB points the binding at a different build. The reason it exists
#: is measurement: comparing two builds means alternating between them in one
#: session, because anything else compares them under different machine load.
#: Windows builds a DLL and carries no `lib` prefix; everywhere else keeps the .so
#: this has always loaded. Nothing else about the binding changes -- the C is
#: identical and MinGW exports all 58 symbols without any annotation.
_LIBNAME = "tetrarch.dll" if sys.platform == "win32" else "libtetrarch.so"
LIB_PATH = os.environ.get("TETRARCH_LIB") or os.path.join(
    os.path.dirname(_HERE), "build", _LIBNAME)


class TtParams(ctypes.Structure):
    _fields_ = [
        ("zob_piece", ctypes.c_uint64 * NSQ * NPIECE),
        ("zob_ep", ctypes.c_uint64 * NSQ * 4),
        ("zob_turn", ctypes.c_uint64 * 4),
        ("zob_ck", ctypes.c_uint64 * 4),
        ("zob_cq", ctypes.c_uint64 * 4),
        ("zob_alive", ctypes.c_uint64 * 4),
        ("valid", ctypes.c_uint8 * NSQ),
        ("compact", ctypes.c_uint8 * NSQ),
        ("pc_color", ctypes.c_uint8 * NPIECE),
        ("pc_type", ctypes.c_uint8 * NPIECE),
        ("pawn_coord", ctypes.c_uint8 * NSQ * 4),
        ("rook_home", ctypes.c_uint8 * NSQ),
        ("pawn_push", ctypes.c_int32 * 4),
        ("pawn_takes", ctypes.c_int32 * 2 * 4),
        ("knight_deltas", ctypes.c_int32 * 8),
        ("queen_dirs", ctypes.c_int32 * 8),
        ("diag", ctypes.c_int32 * 4),
        ("ortho", ctypes.c_int32 * 4),
        ("promo_coord", ctypes.c_int32 * 2),
        ("promo_choices", ctypes.c_int32 * 4 * 2),
        ("n_promo_choices", ctypes.c_int32 * 2),
        ("king_home", ctypes.c_int32 * 2 * 4),
        ("castle", ctypes.c_int32 * 10 * 2 * 2 * 4),
        ("piece_value", ctypes.c_int32 * B.NTYPE),
        ("king_danger", ctypes.c_int32),
        ("mobility", ctypes.c_int32),
        ("mobility_cap", ctypes.c_int32),
        ("rot_to_persp", ctypes.c_uint8 * NSQ * 4),
        ("feature_type", ctypes.c_int32 * B.NTYPE),
    ]


class TtNetView(ctypes.Structure):
    """Pointers to a net's arrays. The C side memcpys immediately, so the
    numpy arrays only have to outlive the call."""
    _fields_ = [
        ("version", ctypes.c_int32),
        ("w1", ctypes.POINTER(ctypes.c_int16)),
        ("b1", ctypes.POINTER(ctypes.c_int32)),
        ("w2", ctypes.POINTER(ctypes.c_int8)),
        ("b2", ctypes.POINTER(ctypes.c_int32)),
        ("w3", ctypes.POINTER(ctypes.c_int8)),
        ("b3", ctypes.POINTER(ctypes.c_int32)),
        ("w4", ctypes.POINTER(ctypes.c_int8)),
        ("b4", ctypes.POINTER(ctypes.c_int32)),
    ]


class TtResult(ctypes.Structure):
    _fields_ = [
        ("nodes", ctypes.c_uint64),
        ("score", ctypes.c_int32),
        ("best", ctypes.c_uint32),
        ("depth", ctypes.c_int32),
        ("aborted", ctypes.c_int32),
        ("pad", ctypes.c_int32),
    ]


class TtBoard(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint64),
        ("halfmove", ctypes.c_int32),
        ("ep_target", ctypes.c_int16 * 4),
        ("ep_victim", ctypes.c_int16 * 4),
        ("kings", ctypes.c_int16 * 4),
        ("points", ctypes.c_uint16 * 4),
        ("sq", ctypes.c_uint8 * NSQ),
        ("turn", ctypes.c_uint8),
        ("mode", ctypes.c_uint8),
        ("pawn_base_rank", ctypes.c_uint8),
        ("alive", ctypes.c_uint8 * 4),
        ("ck", ctypes.c_uint8 * 4),
        ("cq", ctypes.c_uint8 * 4),
    ]


class CoreUnavailable(RuntimeError):
    pass


_lib = None


def build_params():
    """Every parameter the C core runs on, derived from the Python reference."""
    p = TtParams()

    for piece in range(NPIECE):
        for sq in range(NSQ):
            p.zob_piece[piece][sq] = B.ZOB_PIECE[piece][sq]
    for c in range(4):
        for sq in range(NSQ):
            p.zob_ep[c][sq] = B.ZOB_EP[c][sq]
        p.zob_turn[c] = B.ZOB_TURN[c]
        p.zob_ck[c] = B.ZOB_CK[c]
        p.zob_cq[c] = B.ZOB_CQ[c]
        p.zob_alive[c] = B.ZOB_ALIVE[c]

    for sq in range(NSQ):
        p.valid[sq] = B.VALID[sq]
        p.compact[sq] = B.COMPACT[sq]
        p.rook_home[sq] = B.ROOK_HOME.get(sq, -1) + 1
        for c in range(4):
            p.pawn_coord[c][sq] = B.pawn_coord(c, sq) & 0xFF
    for i in range(NPIECE):
        p.pc_color[i] = B.PC_COLOR[i]
        p.pc_type[i] = B.PC_TYPE[i]

    for c in range(4):
        p.pawn_push[c] = B.PAWN_PUSH[c]
        for i in range(2):
            p.pawn_takes[c][i] = B.PAWN_TAKES[c][i]
    for i in range(8):
        p.knight_deltas[i] = B.KNIGHT_DELTAS[i]
        p.queen_dirs[i] = B.QUEEN_DIRS[i]
    for i in range(4):
        p.diag[i] = B.DIAG[i]
        p.ortho[i] = B.ORTHO[i]

    for mode in (B.MODE_FFA, B.MODE_TEAMS):
        p.promo_coord[mode] = B.PROMO_COORD[mode]
        choices = B.PROMO_CHOICES[mode]
        p.n_promo_choices[mode] = len(choices)
        for i, choice in enumerate(choices):
            p.promo_choices[mode][i] = choice

    # Castling geometry, in the order the C core indexes it: the seat's two
    # possible king homes, low square first, then short side then long.
    for c in range(4):
        homes = sorted(k for (cc, k) in B.CASTLE_GEO if cc == c)
        assert len(homes) == 2, homes
        for h, king_sq in enumerate(homes):
            p.king_home[c][h] = king_sq
            sides = B.CASTLE_GEO[(c, king_sq)]
            for side in (B.SHORT, B.LONG):
                rook_from, king_to, rook_to, between, safe = sides[side]
                assert len(between) <= 3 and len(safe) == 3
                g = p.castle[c][h][side]
                g[0] = rook_from
                g[1] = king_to
                g[2] = rook_to
                g[3] = len(between)
                for i, sq in enumerate(between):
                    g[4 + i] = sq
                for i, sq in enumerate(safe):
                    g[7 + i] = sq

    # throwaway: deleted at Phase 4 along with eval_hand.py
    for t in range(B.NTYPE):
        p.piece_value[t] = eval_hand.PIECE_VALUE[t]
    p.king_danger = eval_hand.KING_DANGER
    p.mobility = eval_hand.MOBILITY
    p.mobility_cap = eval_hand.MOBILITY_CAP

    # NNUE geometry, from the same definition the trainer uses.
    from . import nnue
    for persp in range(4):
        for sq in range(NSQ):
            p.rot_to_persp[persp][sq] = nnue.ROT_TO_PERSP[persp][sq]
    for t in range(B.NTYPE):
        p.feature_type[t] = nnue.FEATURE_TYPE[t]
    return p


def load(path=None):
    """Load and initialise the C core. Idempotent; raises if unavailable."""
    global _lib
    if _lib is not None:
        return _lib
    lib_path = path or LIB_PATH
    if not os.path.exists(lib_path):
        raise CoreUnavailable(
            "%s not built -- run ./setup.sh" % os.path.basename(lib_path))
    lib = ctypes.CDLL(lib_path)

    # A .so built before the current Python is the likeliest failure here:
    # `git pull` updates the source but never rebuilds. Without this the
    # first missing symbol surfaces as a bare AttributeError, often from
    # inside a worker process, which reads like a code bug rather than a
    # stale build.
    try:

        lib.tt_params_size.restype = ctypes.c_int
        lib.tt_board_size.restype = ctypes.c_int
        lib.tt_ready.restype = ctypes.c_int
        lib.tt_init.argtypes = [ctypes.POINTER(TtParams)]
        lib.tt_perft.restype = ctypes.c_uint64
        lib.tt_perft.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_int]
        lib.tt_gen_legal.restype = ctypes.c_int
        lib.tt_gen_legal.argtypes = [ctypes.POINTER(TtBoard),
                                     ctypes.POINTER(ctypes.c_uint32)]
        lib.tt_gen_pseudo.restype = ctypes.c_int
        lib.tt_gen_pseudo.argtypes = [ctypes.POINTER(TtBoard),
                                      ctypes.POINTER(ctypes.c_uint32)]
        lib.tt_recompute_key.restype = ctypes.c_uint64
        lib.tt_recompute_key.argtypes = [ctypes.POINTER(TtBoard)]
        lib.tt_is_attacked.restype = ctypes.c_int
        lib.tt_is_attacked.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_int,
                                       ctypes.c_int]
        lib.tt_key_check.restype = ctypes.c_uint64
        lib.tt_key_check.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_int]
        lib.tt_eval.restype = ctypes.c_int32
        lib.tt_eval.argtypes = [ctypes.POINTER(TtBoard)]
        lib.tt_alloc.restype = ctypes.c_int
        lib.tt_alloc.argtypes = [ctypes.c_int]
        lib.tt_size.restype = ctypes.c_uint64
        lib.tt_result_size.restype = ctypes.c_int
        lib.tt_search.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_int,
                                  ctypes.c_uint64, ctypes.POINTER(TtResult)]
        lib.tt_nnue_dims.argtypes = [ctypes.POINTER(ctypes.c_int32)]
        lib.tt_load_net.restype = ctypes.c_int
        lib.tt_load_net.argtypes = [ctypes.POINTER(TtNetView)]
        lib.tt_pv.restype = ctypes.c_int
        lib.tt_pv.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_uint32,
                              ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
        lib.tt_set_mobility.argtypes = [ctypes.c_int]
        lib.tt_get_mobility.restype = ctypes.c_int
        lib.tt_net_loaded.restype = ctypes.c_int
        lib.tt_nnue_acc_matches.restype = ctypes.c_int
        lib.tt_nnue_acc_matches.argtypes = [ctypes.POINTER(TtBoard)]
        lib.tt_undo_size.restype = ctypes.c_int
        lib.tt_make.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_uint32,
                                ctypes.c_void_p]
        lib.tt_unmake.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_uint32,
                                  ctypes.c_void_p]
        lib.tt_set_killers.argtypes = [ctypes.c_int]
        lib.tt_get_killers.restype = ctypes.c_int
        lib.tt_set_history.argtypes = [ctypes.c_int]
        lib.tt_get_history.restype = ctypes.c_int
        lib.tt_set_rep_history.argtypes = [ctypes.POINTER(ctypes.c_uint64),
                                           ctypes.c_int]
        lib.tt_clear.argtypes = []
        lib.tt_unload_net.argtypes = []
        lib.tt_set_rep_detect.argtypes = [ctypes.c_int]
        lib.tt_set_see_order.argtypes = [ctypes.c_int]
        lib.tt_get_see_order.restype = ctypes.c_int
        lib.tt_set_see_prune.argtypes = [ctypes.c_int]
        lib.tt_get_see_prune.restype = ctypes.c_int
        lib.tt_see.restype = ctypes.c_int32
        lib.tt_see.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_uint32]
        lib.tt_legality_disagreements.restype = ctypes.c_int
        lib.tt_legality_disagreements.argtypes = [ctypes.POINTER(TtBoard)]
        lib.tt_search_makes.restype = ctypes.c_uint64
        lib.tt_search_evals.restype = ctypes.c_uint64
        lib.tt_search_gens.restype = ctypes.c_uint64
        lib.tt_bench_eval.restype = ctypes.c_double
        lib.tt_bench_eval.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_int]
        lib.tt_bench_gen.restype = ctypes.c_double
        lib.tt_bench_gen.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_int,
                                     ctypes.c_int]
        lib.tt_bench_makeunmake.restype = ctypes.c_double
        lib.tt_bench_makeunmake.argtypes = [ctypes.POINTER(TtBoard),
                                            ctypes.c_int]
        lib.tt_get_rep_detect.restype = ctypes.c_int
        lib.tt_set_pvs.argtypes = [ctypes.c_int]
        lib.tt_get_pvs.restype = ctypes.c_int
        lib.tt_set_lmr.argtypes = [ctypes.c_int]
        lib.tt_get_lmr.restype = ctypes.c_int
        lib.tt_set_lmr_params.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.tt_set_lazy_eval.argtypes = [ctypes.c_int]
        lib.tt_get_lazy_eval.restype = ctypes.c_int
        lib.tt_set_lmp.argtypes = [ctypes.c_int]
        lib.tt_get_lmp.restype = ctypes.c_int
        lib.tt_set_lmp_params.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.tt_set_qs_evasions.argtypes = [ctypes.c_int]
        lib.tt_get_qs_evasions.restype = ctypes.c_int
        lib.tt_divide.restype = ctypes.c_int
        lib.tt_divide.argtypes = [ctypes.POINTER(TtBoard), ctypes.c_int,
                                  ctypes.POINTER(ctypes.c_uint32),
                                  ctypes.POINTER(ctypes.c_uint64)]

        # Layout agreement. A silent mismatch here would misread every field.
        if lib.tt_params_size() != ctypes.sizeof(TtParams):
            raise CoreUnavailable("TtParams layout mismatch: C %d, Python %d"
                                  % (lib.tt_params_size(), ctypes.sizeof(TtParams)))
        if lib.tt_board_size() != ctypes.sizeof(TtBoard):
            raise CoreUnavailable("TtBoard layout mismatch: C %d, Python %d"
                                  % (lib.tt_board_size(), ctypes.sizeof(TtBoard)))
        if lib.tt_result_size() != ctypes.sizeof(TtResult):
            raise CoreUnavailable("TtResult layout mismatch: C %d, Python %d"
                                  % (lib.tt_result_size(), ctypes.sizeof(TtResult)))

        # NNUE geometry has to be a compile-time constant in C, so it is declared
        # in both places; a mismatch is a loud startup failure rather than a net
        # that reads its own weights wrong.
        from . import nnue
        dims = (ctypes.c_int32 * 8)()
        lib.tt_nnue_dims(dims)
        want = (nnue.NFEATURES, nnue.L1, nnue.NEXTRA, nnue.L2, nnue.L3,
                nnue.SHIFT1, nnue.SHIFT2, nnue.SHIFT_OUT)
        if tuple(dims) != want:
            raise CoreUnavailable("NNUE geometry mismatch: C %s, Python %s"
                                  % (tuple(dims), want))

        params = build_params()
        lib.tt_init(ctypes.byref(params))
        if not lib.tt_ready():
            raise CoreUnavailable("tt_init did not take")
        if not lib.tt_alloc(DEFAULT_TT_MB):
            raise CoreUnavailable("could not allocate the transposition table")
        _lib = lib
    except AttributeError as exc:
        raise CoreUnavailable(
            "%s is stale (%s) -- run ./setup.sh to rebuild"
            % (os.path.basename(lib_path), exc)) from None
    return lib


def set_hash(mb):
    """Resize and clear the transposition table."""
    lib = load()
    if not lib.tt_alloc(int(mb)):
        raise CoreUnavailable("could not allocate a %d MB transposition table" % mb)
    return int(lib.tt_size())


def clear_hash():
    load().tt_clear()


def load_net(net):
    """Push a `nnue.Net` into the C core. From then on tt_eval is the net."""
    import numpy as np
    lib = load()
    arrays = [np.ascontiguousarray(net.w1, dtype=np.int16),
              np.ascontiguousarray(net.b1, dtype=np.int32),
              np.ascontiguousarray(net.w2, dtype=np.int8),
              np.ascontiguousarray(net.b2, dtype=np.int32),
              np.ascontiguousarray(net.w3, dtype=np.int8),
              np.ascontiguousarray(net.b3, dtype=np.int32),
              np.ascontiguousarray(net.w4, dtype=np.int8),
              np.ascontiguousarray(net.b4, dtype=np.int32)]
    types = [ctypes.c_int16, ctypes.c_int32, ctypes.c_int8, ctypes.c_int32,
             ctypes.c_int8, ctypes.c_int32, ctypes.c_int8, ctypes.c_int32]
    view = TtNetView(getattr(net, "version", 1),
                     *[a.ctypes.data_as(ctypes.POINTER(t))
                       for a, t in zip(arrays, types)])
    if not lib.tt_load_net(ctypes.byref(view)):
        raise CoreUnavailable("could not allocate the net")
    # Every stored score was produced by the previous evaluation function, so
    # the table is now poison: probes would serve NNUE scores to a hand-eval
    # search and the other way round. Costs nothing here and silently corrupts
    # any A/B that switches eval at runtime if forgotten.
    lib.tt_clear()
    return True


def unload_net():
    """Back to the throwaway hand eval. Clears the table for the same reason
    load_net does."""
    lib = load()
    lib.tt_unload_net()
    lib.tt_clear()


MAX_PV = 32


def pv(board, first, limit=MAX_PV):
    """The principal variation, walked out of the transposition table.

    `first` is the move the search returned; the root is not in the table on
    purpose (see tt_pv). Only meaningful straight after searching this
    position, and it can come back short -- a stale entry is dropped rather
    than printed, because a wrong variation is worse than a brief one.
    """
    buf = (ctypes.c_uint32 * MAX_PV)()
    n = load().tt_pv(ctypes.byref(to_c(board)), ctypes.c_uint32(first or 0),
                     buf, min(limit, MAX_PV))
    return [buf[i] for i in range(n)]


def set_mobility(on):
    """Mobility in the hand eval. Mirrors eval_hand.USE_MOBILITY, which
    selftest keeps in step so the two evals stay comparable."""
    load().tt_set_mobility(1 if on else 0)


def mobility_enabled():
    return bool(load().tt_get_mobility())


def net_loaded():
    return bool(load().tt_net_loaded())


class CBoard:
    """A TtBoard the C core mutates in place, so make/unmake run the same code
    path the search runs. Only selftest needs this -- the engine never drives
    the C board move by move from Python."""

    def __init__(self, board):
        self.lib = load()
        self.b = to_c(board)
        # One undo record per ply, exactly as the C search keeps on its stack.
        # A single shared buffer would be overwritten by every make, and
        # unwinding would then replay the last move's record repeatedly.
        self._undo = []

    def make(self, move):
        self._undo.append(
            ctypes.create_string_buffer(self.lib.tt_undo_size()))
        self.lib.tt_make(ctypes.byref(self.b), ctypes.c_uint32(move),
                         ctypes.cast(self._undo[-1], ctypes.c_void_p))

    def unmake(self, move):
        undo = self._undo.pop()
        self.lib.tt_unmake(ctypes.byref(self.b), ctypes.c_uint32(move),
                           ctypes.cast(undo, ctypes.c_void_p))

    def acc_matches(self):
        r = self.lib.tt_nnue_acc_matches(ctypes.byref(self.b))
        return None if r < 0 else bool(r)

    def legal(self):
        buf = (ctypes.c_uint32 * MAX_MOVES)()
        n = self.lib.tt_gen_legal(ctypes.byref(self.b), buf)
        return [buf[i] for i in range(n)]

    def eval(self):
        return self.lib.tt_eval(ctypes.byref(self.b))


def nnue_acc_matches(board):
    """Does the incrementally maintained accumulator equal a rebuild from the
    board? None when nothing is being maintained (no net, or no eval yet).

    The differential gate for the incremental update: perft and node pins
    cannot see a wrong accumulator, because it changes evaluations rather than
    the shape of the tree."""
    r = load().tt_nnue_acc_matches(ctypes.byref(to_c(board)))
    return None if r < 0 else bool(r)


def set_killers(on):
    """Killer-move ordering. CONFIRMED +50.42 +/- 6.41, default on (docs/AB.md)."""
    load().tt_set_killers(1 if on else 0)


def killers_enabled():
    return bool(load().tt_get_killers())


def set_rep_detect(on):
    """Repetition detection (§10.2). Default on: without it the search cannot
    score a draw it is walking into. The toggle exists to measure that."""
    load().tt_set_rep_detect(1 if on else 0)


def rep_detect_enabled():
    return bool(load().tt_get_rep_detect())


def set_history(on):
    """History-heuristic ordering for the quiet tail. Off by default: at the
    depths this engine currently reaches it does nothing (docs/AB.md)."""
    load().tt_set_history(1 if on else 0)


def history_enabled():
    return bool(load().tt_get_history())


def set_pvs(on):
    """Principal variation search. Off by default: it costs nodes rather than
    saving them at the depths this engine reaches (docs/AB.md)."""
    load().tt_set_pvs(1 if on else 0)


def pvs_enabled():
    return bool(load().tt_get_pvs())


def set_lmr(on):
    """Late move reductions. CONFIRMED +35.07 +/- 6.51, default on (docs/AB.md).

    Not exact: it can miss a line the full search would find, which is why it
    needed games rather than a node count.
    """
    load().tt_set_lmr(1 if on else 0)


def lmr_enabled():
    return bool(load().tt_get_lmr())


def set_lmr_params(min_depth, min_move):
    load().tt_set_lmr_params(int(min_depth), int(min_move))


def set_lazy_eval(on):
    """Skip the king-danger term when material already settles the bound.

    CONFIRMED +42.88 +/- 6.50 on fixed time, default on (docs/AB.md). Not
    exact -- a bail returns the material term rather than the true eval -- but
    it measured node-identical over 80 positions, which selftest watches.
    """
    load().tt_set_lazy_eval(1 if on else 0)


def lazy_eval_enabled():
    return bool(load().tt_get_lazy_eval())


def set_lmp(on):
    """Late move pruning. CONFIRMED +36.09 +/- 6.69, default on (docs/AB.md).

    A hard prune: it drops moves outright, so it can miss a mate the full
    search finds. It never invents one -- pruning requires a legal move to
    already have been found.
    """
    load().tt_set_lmp(1 if on else 0)


def lmp_enabled():
    return bool(load().tt_get_lmp())


def set_lmp_params(max_depth, base):
    load().tt_set_lmp_params(int(max_depth), int(base))


def set_qs_evasions(on):
    """Search every legal move in quiescence when in check, instead of
    standing pat. CONFIRMED +106.78 +/- 6.88, default on (docs/AB.md)."""
    load().tt_set_qs_evasions(1 if on else 0)


def qs_evasions_enabled():
    return bool(load().tt_get_qs_evasions())


def ffa_capture_table():
    """The C core's copy of RULES.md §8.1, so selftest can pin it against
    board.CAPTURE_POINTS rather than trust two hardcoded tables to agree."""
    lib = load()
    return [int(lib.tt_ffa_capture_points(t)) for t in range(B.NTYPE)]


def set_ffa_tt(on):
    """The transposition table inside the paranoid FFA search. Default on."""
    load().tt_set_ffa_tt(1 if on else 0)


def ffa_tt_enabled():
    return bool(load().tt_get_ffa_tt())


def ffa_elim_points():
    """The C core's copy of RULES.md §8.2, pinned against board.ELIM_POINTS."""
    return int(load().tt_ffa_elim_points())


def evaluate(b):
    """The C core's evaluation, for the bit-exactness assertion."""
    lib = load()
    cb = to_c(b)
    return int(lib.tt_eval(ctypes.byref(cb)))


def set_see_order(on):
    """Order captures by SEE rather than MVV-LVA. Default off."""
    load().tt_set_see_order(1 if on else 0)


def see_order_enabled():
    return bool(load().tt_get_see_order())


def set_see_prune(on):
    """Skip losing captures in quiescence. Default ON: CONFIRMED at +16.32
    +/- 4.58 fixed nodes and +23.09 +/- 4.56 fixed time (docs/AB.md)."""
    load().tt_set_see_prune(1 if on else 0)


def see_prune_enabled():
    return bool(load().tt_get_see_prune())


def see(b, move):
    """What a capture is worth after the exchange settles. Teams only;
    returns 0 in FFA, where recapture order does not alternate."""
    cb = to_c(b)
    return int(load().tt_see(ctypes.byref(cb), move))


def legality_disagreements(b):
    """Moves the legality fast path calls certainly legal that the full
    attack scan rejects. Zero is the only acceptable answer."""
    cb = to_c(b)
    return int(load().tt_legality_disagreements(ctypes.byref(cb)))


def search_makes():
    """Makes performed by the last tt_search. Reset per search."""
    return int(load().tt_search_makes())


def search_evals():
    """Evaluations performed by the last tt_search. Reset per search.

    Far below one per node: interior nodes recurse instead of evaluating, so
    this is essentially the quiescence leaves.
    """
    return int(load().tt_search_evals())


def search_gens():
    """Move generations by the last tt_search. Below one per node: a quiescence
    node standing pat above beta returns before generating, and so does an
    alpha-beta node taking a transposition cutoff."""
    return int(load().tt_search_gens())


def bench_component(b, what, iters):
    """Seconds for `iters` calls of one component, timed inside C.

    `what` is "eval", "gen", "legal" or "makeunmake". The board is converted
    once; a per-call conversion would cost more than any of them.
    """
    lib = load()
    cb = to_c(b)
    ref = ctypes.byref(cb)
    if what == "eval":
        return lib.tt_bench_eval(ref, iters)
    if what in ("gen", "legal"):
        return lib.tt_bench_gen(ref, iters, 1 if what == "legal" else 0)
    if what == "makeunmake":
        return lib.tt_bench_makeunmake(ref, iters)
    raise ValueError("unknown component %r" % what)


def set_repetitions(keys):
    """Hand the search the keys of positions already played (§10.2).

    The list is the game up to but not including the position about to be
    searched -- the root supplies its own key. It persists until replaced, so
    it has to be set per search, not per process.
    """
    lib = load()
    keys = list(keys)
    buf = (ctypes.c_uint64 * max(len(keys), 1))(*keys)
    lib.tt_set_rep_history(buf, len(keys))


def search(b, depth, node_limit=0, repetitions=()):
    """One fixed-depth search. Returns a TtResult.

    Iterative deepening and time management stay in Python at the root; this
    is the per-node loop only.
    """
    lib = load()
    set_repetitions(repetitions)
    cb = to_c(b)
    out = TtResult()
    lib.tt_search(ctypes.byref(cb), depth, node_limit, ctypes.byref(out))
    return out


def available():
    try:
        load()
        return True
    except (CoreUnavailable, OSError):
        return False


def to_c(b):
    """Convert a Python Board into the C representation."""
    cb = TtBoard()
    cb.key = b.key
    cb.halfmove = b.halfmove
    cb.turn = b.turn
    cb.mode = b.mode
    cb.pawn_base_rank = b.pawn_base_rank
    for i in range(NSQ):
        cb.sq[i] = b.sq[i]
    for c in range(4):
        cb.alive[c] = 1 if b.alive[c] else 0
        cb.ck[c] = 1 if b.ck[c] else 0
        cb.cq[c] = 1 if b.cq[c] else 0
        cb.kings[c] = b.kings[c]
        cb.points[c] = b.points[c]
        if b.ep[c] is None:
            cb.ep_target[c] = -1
            cb.ep_victim[c] = -1
        else:
            cb.ep_target[c] = b.ep[c][0]
            cb.ep_victim[c] = b.ep[c][1]
    return cb


def gen_legal(b):
    lib = load()
    cb = to_c(b)
    buf = (ctypes.c_uint32 * MAX_MOVES)()
    n = lib.tt_gen_legal(ctypes.byref(cb), buf)
    return [buf[i] for i in range(n)]


def gen_pseudo(b):
    lib = load()
    cb = to_c(b)
    buf = (ctypes.c_uint32 * MAX_MOVES)()
    n = lib.tt_gen_pseudo(ctypes.byref(cb), buf)
    return [buf[i] for i in range(n)]


def perft(b, depth):
    lib = load()
    cb = to_c(b)
    return int(lib.tt_perft(ctypes.byref(cb), depth))


def recompute_key(b):
    lib = load()
    cb = to_c(b)
    return int(lib.tt_recompute_key(ctypes.byref(cb)))


def is_attacked(b, sq, me):
    lib = load()
    cb = to_c(b)
    return bool(lib.tt_is_attacked(ctypes.byref(cb), sq, me))


def in_check(b, color):
    king = b.kings[color]
    if king < 0:
        return False
    return is_attacked(b, king, color)


def key_check(b, depth):
    """Mismatches between the incremental Zobrist key and a full recompute,
    plus unmake failures, over the whole legal tree to `depth`. 0 is correct."""
    lib = load()
    cb = to_c(b)
    return int(lib.tt_key_check(ctypes.byref(cb), depth))


def divide(b, depth):
    lib = load()
    cb = to_c(b)
    moves = (ctypes.c_uint32 * MAX_MOVES)()
    nodes = (ctypes.c_uint64 * MAX_MOVES)()
    n = lib.tt_divide(ctypes.byref(cb), depth, moves, nodes)
    return {B.move_str(moves[i]): int(nodes[i]) for i in range(n)}
