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

from . import board as B
from . import eval_hand

NSQ = B.NSQ
NPIECE = B.NPIECE
MAX_MOVES = 1024
DEFAULT_TT_MB = 64

_HERE = os.path.dirname(os.path.abspath(__file__))
LIB_PATH = os.path.join(os.path.dirname(_HERE), "build", "libtetrarch.so")


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
        ("rot_to_persp", ctypes.c_uint8 * NSQ * 4),
        ("feature_type", ctypes.c_int32 * B.NTYPE),
    ]


class TtNetView(ctypes.Structure):
    """Pointers to a net's arrays. The C side memcpys immediately, so the
    numpy arrays only have to outlive the call."""
    _fields_ = [
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
    lib.tt_net_loaded.restype = ctypes.c_int
    lib.tt_set_killers.argtypes = [ctypes.c_int]
    lib.tt_get_killers.restype = ctypes.c_int
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
    view = TtNetView(*[a.ctypes.data_as(ctypes.POINTER(t))
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


def net_loaded():
    return bool(load().tt_net_loaded())


def set_killers(on):
    """Killer-move ordering. CONFIRMED +50.42 +/- 6.41, default on (docs/AB.md)."""
    load().tt_set_killers(1 if on else 0)


def killers_enabled():
    return bool(load().tt_get_killers())


def evaluate(b):
    """The C core's evaluation, for the bit-exactness assertion."""
    lib = load()
    cb = to_c(b)
    return int(lib.tt_eval(ctypes.byref(cb)))


def search(b, depth, node_limit=0):
    """One fixed-depth search. Returns a TtResult.

    Iterative deepening and time management stay in Python at the root; this
    is the per-node loop only.
    """
    lib = load()
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
