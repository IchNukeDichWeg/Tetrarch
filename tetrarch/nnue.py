"""NNUE feature encoding, net file format, and the reference forward pass.

This is the definition the trainer and the C core both follow. Section
references (§n) are to docs/RULES.md.

FEATURES
    feature(ptype, color, sq, persp)
        = (((color - persp) & 3) * 6 + ptype) * 160 + COMPACT[rot(sq, persp)]

    3840 of them: 4 relative colours x 6 piece types x 160 playable squares.

    The board has 4-fold rotational symmetry, so one weight set serves all four
    seats: `rot` turns the board until `persp` sits where Red sits, and the
    colour index becomes relative -- 0 is me, 1 the seat to my left, 2 my
    partner in Teams, 3 the seat to my right. That also gives 4x training-data
    augmentation for free, since every position can be labelled from four
    perspectives.

    A promoted queen indexes as a queen. It moves identically; the only thing
    that differs is its FFA capture value (§4.2, §8.1), which is a points
    matter and reaches the net through the extra inputs, not the board plane.

EXTRA INPUTS
    Seven values that bypass the accumulator and feed the first hidden layer
    directly, because a position with two dead seats is a different game and
    FFA is a points race:

        4  alive flags, rotated to perspective
        3  points differentials, mine minus each other seat, rotated

    A net without these plateaus and you cannot tell why.

QUANTISATION
    All integer, so Python and C agree bit for bit and nothing drifts across
    microarchitectures.

        acc[j]  = B1[j] + sum of W1[f][j] over active f          int32
        h0[j]   = clamp(acc[j] >> SHIFT1, 0, 127)                uint8
        x       = h0 (256) ++ extras (7)
        z[k]    = B[k] + sum x[i] * W[k][i]                      int32
        h[k]    = clamp(z[k] >> SHIFT2, 0, 127)
        score   = (B4 + sum h[i] * W4[i]) >> SHIFT_OUT           centipawns

    The accumulator is int32, not the int16 the project brief specifies. An
    int16 accumulator only stays in range if training clips weights, and a
    saturating or wrapping incremental update is not reversible by unmake --
    which would be a silent wrongness rather than a loud one. The cost is 2 kB
    for four accumulators.
"""

import struct

import numpy as np

from .board import (
    COMPACT, SQUARES, ROT90, PC_COLOR, PC_TYPE, DEAD_UNKNOWN,
    PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING, PQUEEN,
)

NFEATURE_TYPES = 6
NSQ_PLAYABLE = 160
NFEATURES = 4 * NFEATURE_TYPES * NSQ_PLAYABLE       # 3840
L1 = 256
NEXTRA = 7
L2 = 32
L3 = 32

SHIFT1 = 6
SHIFT2 = 6
SHIFT_OUT = 6
CRELU_MAX = 127

MAGIC = b"TTNN"
VERSION = 2
#: Formats this build can load. Version 1 nets keep the behaviour they were
#: trained and measured with; only a version 2 net gets the corrected extras.
SUPPORTED_VERSIONS = (1, 2)

#: PQUEEN folds onto QUEEN; every other type is itself.
FEATURE_TYPE = [PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING, QUEEN]


def _build_rotation_tables():
    """ROT_TO_PERSP[persp][sq]: the square `sq` lands on once the board is
    turned so that `persp` sits where Red sits."""
    tables = []
    for persp in range(4):
        mapping = list(range(256))
        for _ in range((4 - persp) & 3):
            mapping = [ROT90[sq] for sq in mapping]
        tables.append(mapping)
    return tables


ROT_TO_PERSP = _build_rotation_tables()


def feature_index(ptype, color, sq, persp):
    """The single feature this piece switches on, from `persp`'s point of view."""
    rel = (color - persp) & 3
    return ((rel * NFEATURE_TYPES + FEATURE_TYPE[ptype]) * NSQ_PLAYABLE
            + COMPACT[ROT_TO_PERSP[persp][sq]])


def active_features(board, persp):
    """Every feature switched on in `board` from `persp`'s point of view.

    Dead seats' pieces are included with their original colour: they are on the
    board and they block, and the alive flags in the extra inputs tell the net
    they cannot move (§9.1). Pieces whose origin was never recorded --  FEN4's
    bare `d` prefix -- have no colour to make relative and are skipped; they
    only occur when importing an FFA game, which Phase 5 will have to
    settle properly.
    """
    out = []
    for sq in SQUARES:
        p = board.sq[sq]
        if not p:
            continue
        color = PC_COLOR[p]
        if color == DEAD_UNKNOWN:
            continue
        out.append(feature_index(PC_TYPE[p], color, sq, persp))
    return out


#: Points differentials are clamped so the hidden-layer accumulation stays in
#: int32 no matter how lopsided an FFA game gets, and so the net sees a bounded
#: input like every other one. In Teams every seat has 0 points, so this only
#: bites in FFA.
EXTRA_CLAMP = 127


def extra_inputs(board, persp):
    """The seven raw values that bypass the accumulator, rotated to `persp`.

    Four alive flags in {0,1} and three points differences in [-127,127]. This
    is the RAW form; what the trainer and what inference each feed is derived
    from it below, and the two must agree.
    """
    out = [1 if board.alive[(persp + k) & 3] else 0 for k in range(4)]
    mine = board.points[persp]
    for k in range(1, 4):
        diff = mine - board.points[(persp + k) & 3]
        out.append(max(-EXTRA_CLAMP, min(EXTRA_CLAMP, diff)))
    return out


# The quantised net feeds every input at 127x the float model's value: the
# accumulator half is crelu'd to [0,127] where the float model clips to [0,1].
# The extras were exempt from that in version 1, and in opposite directions --
# an alive flag stayed 1 where it should have been 127, so it arrived 127x too
# quiet, while a points difference stayed at its raw [-127,127] where the float
# model had it on a [0,1] footing, so it trained 127x too loud. Both halves
# were consistent between the Python and C mirrors, which is exactly why the
# bit-exactness gate never saw it: they agreed with each other and neither
# agreed with the model that produced the weights.
#
# Version 2 puts both on the same footing as everything else. The pair of
# functions below is the single definition of that footing; version 1 is kept
# verbatim so every net measured in docs/AB.md still evaluates as it did.

def extras_float(board, persp, version=VERSION):
    """What the TRAINER feeds. Everything on a [-1,1] footing, like h0."""
    raw = extra_inputs(board, persp)
    if version == 1:
        return [float(v) for v in raw]
    return [float(v) for v in raw[:4]] + [v / float(EXTRA_CLAMP) for v in raw[4:]]


def extras_quantised(board, persp, version=VERSION):
    """What INFERENCE feeds. Exactly 127x extras_float, and int8-safe."""
    raw = extra_inputs(board, persp)
    if version == 1:
        return list(raw)
    return [v * EXTRA_CLAMP for v in raw[:4]] + list(raw[4:])


class Net:
    """A quantised net. Layout matches what the C core memory-maps."""

    __slots__ = ("w1", "b1", "w2", "b2", "w3", "b3", "w4", "b4", "version")

    def __init__(self, w1, b1, w2, b2, w3, b3, w4, b4, version=VERSION):
        self.version = version
        self.w1, self.b1 = w1, b1
        self.w2, self.b2 = w2, b2
        self.w3, self.b3 = w3, b3
        self.w4, self.b4 = w4, b4

    @classmethod
    def random(cls, seed=0, scale=8):
        """A small random net, for exercising the plumbing before one is
        trained. Weights are deliberately tiny so nothing saturates."""
        rng = np.random.default_rng(seed)

        def ints(shape, dtype, lo, hi):
            return rng.integers(lo, hi + 1, size=shape, dtype=np.int64).astype(dtype)

        return cls(
            ints((NFEATURES, L1), np.int16, -scale, scale),
            ints((L1,), np.int32, -scale, scale),
            ints((L2, L1 + NEXTRA), np.int8, -scale, scale),
            ints((L2,), np.int32, -scale * 64, scale * 64),
            ints((L3, L2), np.int8, -scale, scale),
            ints((L3,), np.int32, -scale * 64, scale * 64),
            ints((L3,), np.int8, -scale, scale),
            ints((1,), np.int32, -scale * 64, scale * 64),
        )

    # -- file format ------------------------------------------------------

    def save(self, path):
        # self.version, not VERSION: re-saving a net that was loaded must not
        # relabel it, or a version 1 net acquires version 2 semantics without
        # its weights ever having been trained for them.
        header = struct.pack("<4s10I", MAGIC, self.version, NFEATURES, L1, NEXTRA,
                             L2, L3, SHIFT1, SHIFT2, SHIFT_OUT, 0)
        with open(path, "wb") as fh:
            fh.write(header)
            for array, dtype in ((self.w1, np.int16), (self.b1, np.int32),
                                 (self.w2, np.int8), (self.b2, np.int32),
                                 (self.w3, np.int8), (self.b3, np.int32),
                                 (self.w4, np.int8), (self.b4, np.int32)):
                fh.write(np.ascontiguousarray(array, dtype=dtype).tobytes())

    @classmethod
    def load(cls, path):
        with open(path, "rb") as fh:
            raw = fh.read()
        magic, version, nfeat, l1, nextra, l2, l3, s1, s2, so, _pad = \
            struct.unpack_from("<4s10I", raw, 0)
        if magic != MAGIC:
            raise ValueError("%s is not a Tetrarch net" % path)
        if version not in SUPPORTED_VERSIONS:
            raise ValueError("net version %d, supported %s"
                             % (version, SUPPORTED_VERSIONS))
        if (nfeat, l1, nextra, l2, l3) != (NFEATURES, L1, NEXTRA, L2, L3):
            raise ValueError("net geometry %s does not match this build"
                             % ((nfeat, l1, nextra, l2, l3),))
        if (s1, s2, so) != (SHIFT1, SHIFT2, SHIFT_OUT):
            raise ValueError("net quantisation %s does not match this build"
                             % ((s1, s2, so),))

        offset = struct.calcsize("<4s10I")

        def take(shape, dtype):
            nonlocal offset
            count = int(np.prod(shape))
            array = np.frombuffer(raw, dtype=dtype, count=count,
                                  offset=offset).reshape(shape)
            offset += count * np.dtype(dtype).itemsize
            return array.copy()

        return cls(take((NFEATURES, L1), np.int16), take((L1,), np.int32),
                   take((L2, L1 + NEXTRA), np.int8), take((L2,), np.int32),
                   take((L3, L2), np.int8), take((L3,), np.int32),
                   take((L3,), np.int8), take((1,), np.int32),
                   version=version)

    # -- inference --------------------------------------------------------

    def accumulator(self, board, persp):
        """int32[L1] from scratch. The C core keeps this incrementally; a full
        refresh is the reference it is checked against."""
        acc = self.b1.astype(np.int64).copy()
        features = active_features(board, persp)
        if features:
            acc += self.w1[features].sum(axis=0, dtype=np.int64)
        return acc

    def evaluate(self, board, persp=None):
        """Centipawns from `persp`'s point of view (default: the side to move)."""
        if persp is None:
            persp = board.turn
        acc = self.accumulator(board, persp)
        h0 = _crelu(acc >> SHIFT1)
        x = np.concatenate([h0, np.array(
            extras_quantised(board, persp, self.version), dtype=np.int64)])
        h1 = _crelu((self.w2.astype(np.int64) @ x
                     + self.b2.astype(np.int64)) >> SHIFT2)
        h2 = _crelu((self.w3.astype(np.int64) @ h1
                     + self.b3.astype(np.int64)) >> SHIFT2)
        out = int(self.w4.astype(np.int64) @ h2) + int(self.b4[0])
        return int(out >> SHIFT_OUT)


def _crelu(x):
    return np.clip(x, 0, CRELU_MAX)
