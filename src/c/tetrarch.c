/* Tetrarch C core: board, move generation, perft.
 *
 * This file declares NO chess constants of its own. Every table -- VALID,
 * COMPACT, the Zobrist keys, the piece encoding, pawn geometry, promotion
 * ranks, castling squares -- is pushed in from tetrarch/board.py through
 * tt_init() at startup. A duplicated table is a divergence waiting for one
 * side to be edited; the Python reference stays the single definition.
 *
 * The generator mirrors tetrarch/movegen.py statement for statement, including
 * iteration order, so the two produce identical move lists and not merely
 * identical counts.
 *
 * Section references (§n) are to docs/RULES.md.
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define NSQ 256
#define NPIECE 36            /* 1 + 5 colours * 7 types */
#define NTYPE 7
#define MAX_MOVES 1024
#define MAX_DEPTH 64

/* Piece types, mirroring board.py. */
#define PAWN 0
#define KNIGHT 1
#define BISHOP 2
#define ROOK 3
#define QUEEN 4
#define KING 5
#define PQUEEN 6

#define DEAD_UNKNOWN 4
#define MODE_FFA 0

/* Move flags. Promotion is orthogonal and lives in bits 20-22 (§5.6). */
#define F_NORMAL 0
#define F_DOUBLE 1
#define F_EP 2
#define F_CASTLE_SHORT 3
#define F_CASTLE_LONG 4

#define MV_FROM(m) ((int)((m) & 255))
#define MV_TO(m) ((int)(((m) >> 8) & 255))
#define MV_FLAG(m) ((int)(((m) >> 16) & 15))
#define MV_PROMO(m) ((int)(((m) >> 20) & 7))
#define MK_MOVE(f, t, fl, pr) \
    ((uint32_t)(f) | ((uint32_t)(t) << 8) | ((uint32_t)(fl) << 16) | \
     ((uint32_t)(pr) << 20))

/* Castling record layout inside params.castle[colour][home][side]. */
#define C_ROOK_FROM 0
#define C_KING_TO 1
#define C_ROOK_TO 2
#define C_NBETWEEN 3
#define C_BETWEEN 4          /* 3 entries */
#define C_SAFE 7             /* 3 entries */

typedef struct {
    uint64_t zob_piece[NPIECE][NSQ];
    uint64_t zob_ep[4][NSQ];
    uint64_t zob_turn[4];
    uint64_t zob_ck[4];
    uint64_t zob_cq[4];
    uint64_t zob_alive[4];
    uint8_t valid[NSQ];
    uint8_t compact[NSQ];
    uint8_t pc_color[NPIECE];
    uint8_t pc_type[NPIECE];
    uint8_t pawn_coord[4][NSQ];
    uint8_t rook_home[NSQ];       /* owning seat + 1, or 0 */
    int32_t pawn_push[4];
    int32_t pawn_takes[4][2];
    int32_t knight_deltas[8];
    int32_t queen_dirs[8];
    int32_t diag[4];
    int32_t ortho[4];
    int32_t promo_coord[2];       /* indexed by mode */
    int32_t promo_choices[2][4];
    int32_t n_promo_choices[2];
    int32_t king_home[4][2];      /* the two central squares per seat */
    int32_t castle[4][2][2][10];
    int32_t piece_value[NTYPE];   /* throwaway eval: deleted at Phase 4 */
    int32_t king_danger;
    uint8_t rot_to_persp[4][NSQ]; /* board turned so `persp` sits where Red is */
    int32_t feature_type[NTYPE];  /* PQUEEN folds onto QUEEN */
} TtParams;

typedef struct {
    uint64_t key;
    int32_t halfmove;
    int16_t ep_target[4];
    int16_t ep_victim[4];
    int16_t kings[4];
    uint16_t points[4];
    uint8_t sq[NSQ];
    uint8_t turn;
    uint8_t mode;
    uint8_t pawn_base_rank;
    uint8_t alive[4];
    uint8_t ck[4];
    uint8_t cq[4];
} TtBoard;

typedef struct {
    uint64_t key;
    int32_t halfmove;
    int16_t ep_target[4];
    int16_t ep_victim[4];
    int16_t kings[4];
    int16_t victim_sq;
    uint8_t ck[4];
    uint8_t cq[4];
    uint8_t captured;
    uint8_t victim;
    uint8_t mover;
} TtUndo;

static TtParams P;
static int initialised = 0;

/* --- introspection, so the binding can assert layout agreement ---------- */

int tt_params_size(void) { return (int)sizeof(TtParams); }
int tt_board_size(void) { return (int)sizeof(TtBoard); }
int tt_undo_size(void) { return (int)sizeof(TtUndo); }

void tt_init(const TtParams *p)
{
    memcpy(&P, p, sizeof(TtParams));
    initialised = 1;
}

int tt_ready(void) { return initialised; }

/* --- helpers ------------------------------------------------------------ */

static inline int same_team(int mode, int a, int b)
{
    if (a == DEAD_UNKNOWN || b == DEAD_UNKNOWN) return 0;
    return mode == MODE_FFA ? (a == b) : ((a & 1) == (b & 1));
}

/* Can `mover` capture this piece? Dead seats' pieces are fair game to all. */
static inline int is_enemy(const TtBoard *b, uint8_t piece, int mover)
{
    int c = P.pc_color[piece];
    if (c == DEAD_UNKNOWN || !b->alive[c]) return 1;
    return !same_team(b->mode, c, mover);
}

/* Could this piece deliver check to `me`? Dead seats' pieces block but do not
 * attack (§9.1). */
static inline int hostile(const TtBoard *b, uint8_t piece, int me)
{
    int c = P.pc_color[piece];
    return c < 4 && b->alive[c] && !same_team(b->mode, c, me);
}

static inline int queenish(int t) { return t == QUEEN || t == PQUEEN; }

int tt_is_attacked(const TtBoard *b, int sq, int me)
{
    int i, t;
    uint8_t p;

    for (i = 0; i < 8; i++) {
        t = (sq + P.knight_deltas[i]) & 255;
        if (P.valid[t]) {
            p = b->sq[t];
            if (p && P.pc_type[p] == KNIGHT && hostile(b, p, me)) return 1;
        }
    }
    for (i = 0; i < 8; i++) {
        t = (sq + P.queen_dirs[i]) & 255;
        if (P.valid[t]) {
            p = b->sq[t];
            if (p && P.pc_type[p] == KING && hostile(b, p, me)) return 1;
        }
    }
    /* A pawn on sq-d attacks sq exactly when d is one of that seat's own
     * capture deltas, which differ per seat (§4.1). All are diagonal. */
    for (i = 0; i < 4; i++) {
        int d = P.diag[i];
        t = (sq - d) & 255;
        if (P.valid[t]) {
            p = b->sq[t];
            if (p && P.pc_type[p] == PAWN && hostile(b, p, me)) {
                int c = P.pc_color[p];
                if (P.pawn_takes[c][0] == d || P.pawn_takes[c][1] == d) return 1;
            }
        }
    }
    for (i = 0; i < 4; i++) {
        int d = P.ortho[i];
        t = (sq + d) & 255;
        while (P.valid[t]) {
            p = b->sq[t];
            if (p) {
                int pt = P.pc_type[p];
                if ((pt == ROOK || queenish(pt)) && hostile(b, p, me)) return 1;
                break;
            }
            t = (t + d) & 255;
        }
    }
    for (i = 0; i < 4; i++) {
        int d = P.diag[i];
        t = (sq + d) & 255;
        while (P.valid[t]) {
            p = b->sq[t];
            if (p) {
                int pt = P.pc_type[p];
                if ((pt == BISHOP || queenish(pt)) && hostile(b, p, me)) return 1;
                break;
            }
            t = (t + d) & 255;
        }
    }
    return 0;
}

int tt_in_check(const TtBoard *b, int color)
{
    int k = b->kings[color];
    if (k < 0) return 0;
    return tt_is_attacked(b, k, color);
}

uint64_t tt_recompute_key(const TtBoard *b)
{
    uint64_t k = P.zob_turn[b->turn];
    int sq, c;
    for (sq = 0; sq < NSQ; sq++) {
        uint8_t p = b->sq[sq];
        if (P.valid[sq] && p) k ^= P.zob_piece[p][sq];
    }
    for (c = 0; c < 4; c++) {
        if (b->ck[c]) k ^= P.zob_ck[c];
        if (b->cq[c]) k ^= P.zob_cq[c];
        if (b->alive[c]) k ^= P.zob_alive[c];
        if (b->ep_target[c] >= 0) k ^= P.zob_ep[c][b->ep_target[c]];
    }
    return k;
}

/* --- move generation ---------------------------------------------------- */

/* Live en-passant offers `me` could accept. Returns the count; targets and
 * victims are written to the caller's arrays (§5). */
static int ep_offers(const TtBoard *b, int me, int *targets, int *victims)
{
    int n = 0, owner;
    for (owner = 0; owner < 4; owner++) {
        int target, victim_sq;
        uint8_t victim, occupant;
        if (owner == me || b->ep_target[owner] < 0) continue;
        target = b->ep_target[owner];
        victim_sq = b->ep_victim[owner];
        victim = b->sq[victim_sq];
        if (!victim || P.pc_type[victim] != PAWN) continue;
        if (!is_enemy(b, victim, me)) continue;
        occupant = b->sq[target];
        if (occupant && !is_enemy(b, occupant, me)) continue;
        targets[n] = target;
        victims[n] = victim_sq;
        n++;
    }
    return n;
}

int tt_gen_pseudo(const TtBoard *b, uint32_t *out)
{
    int me = b->turn, n = 0, sq, i, j;
    int promo_coord = P.promo_coord[b->mode];
    int nchoice = P.n_promo_choices[b->mode];
    const int32_t *choices = P.promo_choices[b->mode];
    int push = P.pawn_push[me];
    int base = b->pawn_base_rank;
    int ep_target[4], ep_victim[4];
    int n_offers = ep_offers(b, me, ep_target, ep_victim);

    for (sq = 0; sq < NSQ; sq++) {
        uint8_t p;
        int ptype, sliding, nd;
        const int32_t *deltas;
        if (!P.valid[sq]) continue;
        p = b->sq[sq];
        if (!p || P.pc_color[p] != me) continue;
        ptype = P.pc_type[p];

        if (ptype == PAWN) {
            int to = (sq + push) & 255;
            if (P.valid[to] && !b->sq[to]) {
                if (P.pawn_coord[me][to] == promo_coord) {
                    for (j = 0; j < nchoice; j++)
                        out[n++] = MK_MOVE(sq, to, F_NORMAL, choices[j]);
                } else {
                    out[n++] = MK_MOVE(sq, to, F_NORMAL, 0);
                }
                if (base && P.pawn_coord[me][sq] == base - 1) {
                    int to2 = (sq + 2 * push) & 255;
                    if (P.valid[to2] && !b->sq[to2])
                        out[n++] = MK_MOVE(sq, to2, F_DOUBLE, 0);
                }
            }
            for (i = 0; i < 2; i++) {
                int flag = F_NORMAL, is_offer = 0;
                to = (sq + P.pawn_takes[me][i]) & 255;
                if (!P.valid[to]) continue;
                for (j = 0; j < n_offers; j++)
                    if (ep_target[j] == to) { is_offer = 1; break; }
                if (is_offer) {
                    /* An en-passant capture supersedes the plain capture onto
                     * the same square: same from/to, but it also removes the
                     * passing pawn (§5.5). */
                    flag = F_EP;
                } else {
                    uint8_t target = b->sq[to];
                    if (!(target && is_enemy(b, target, me))) continue;
                }
                if (P.pawn_coord[me][to] == promo_coord) {
                    for (j = 0; j < nchoice; j++)
                        out[n++] = MK_MOVE(sq, to, flag, choices[j]);
                } else {
                    out[n++] = MK_MOVE(sq, to, flag, 0);
                }
            }
            continue;
        }

        if (ptype == KNIGHT) {
            deltas = P.knight_deltas; nd = 8; sliding = 0;
        } else if (ptype == KING) {
            deltas = P.queen_dirs; nd = 8; sliding = 0;
        } else if (ptype == BISHOP) {
            deltas = P.diag; nd = 4; sliding = 1;
        } else if (ptype == ROOK) {
            deltas = P.ortho; nd = 4; sliding = 1;
        } else if (queenish(ptype)) {
            deltas = P.queen_dirs; nd = 8; sliding = 1;
        } else {
            continue;
        }

        for (i = 0; i < nd; i++) {
            int d = deltas[i];
            int to = (sq + d) & 255;
            while (P.valid[to]) {
                uint8_t target = b->sq[to];
                if (target) {
                    if (is_enemy(b, target, me)) out[n++] = MK_MOVE(sq, to, F_NORMAL, 0);
                    break;
                }
                out[n++] = MK_MOVE(sq, to, F_NORMAL, 0);
                if (!sliding) break;
                to = (to + d) & 255;
            }
        }
    }

    /* Castling, reading the geometry the Python reference derived (§6.1). */
    if (b->ck[me] || b->cq[me]) {
        int king = b->kings[me];
        int home = -1;
        if (king >= 0) {
            if (P.king_home[me][0] == king) home = 0;
            else if (P.king_home[me][1] == king) home = 1;
        }
        if (home >= 0) {
            int side;
            for (side = 0; side < 2; side++) {
                const int32_t *g = P.castle[me][home][side];
                uint8_t rook;
                int blocked = 0, k;
                if (!(side == 0 ? b->ck[me] : b->cq[me])) continue;
                rook = b->sq[g[C_ROOK_FROM]];
                if (!rook || P.pc_type[rook] != ROOK || P.pc_color[rook] != me)
                    continue;
                for (k = 0; k < g[C_NBETWEEN]; k++)
                    if (b->sq[g[C_BETWEEN + k]]) { blocked = 1; break; }
                if (blocked) continue;
                for (k = 0; k < 3; k++)
                    if (tt_is_attacked(b, g[C_SAFE + k], me)) { blocked = 1; break; }
                if (blocked) continue;
                out[n++] = MK_MOVE(king, g[C_KING_TO],
                                   side == 0 ? F_CASTLE_SHORT : F_CASTLE_LONG, 0);
            }
        }
    }
    return n;
}

/* --- make / unmake ------------------------------------------------------ */

void tt_make(TtBoard *b, uint32_t m, TtUndo *u)
{
    int frm = MV_FROM(m), to = MV_TO(m), flag = MV_FLAG(m), promo = MV_PROMO(m);
    int mover = b->turn, c, t;
    uint8_t piece = b->sq[frm], captured = b->sq[to], placed;
    uint64_t key;
    int victim_sq = -1;
    uint8_t victim = 0;

    if (flag == F_EP) {
        /* The pawn removed sits on the recorded victim square, not the square
         * we move to (§5.2), and the entry belongs to whichever seat pushed --
         * never to the mover. Seats' target squares can never collide. */
        for (c = 0; c < 4; c++) {
            if (b->ep_target[c] == to) {
                victim_sq = b->ep_victim[c];
                victim = b->sq[victim_sq];
                break;
            }
        }
    }

    u->captured = captured;
    u->halfmove = b->halfmove;
    u->key = b->key;
    u->mover = (uint8_t)mover;
    u->victim_sq = (int16_t)victim_sq;
    u->victim = victim;
    memcpy(u->ep_target, b->ep_target, sizeof(u->ep_target));
    memcpy(u->ep_victim, b->ep_victim, sizeof(u->ep_victim));
    memcpy(u->ck, b->ck, sizeof(u->ck));
    memcpy(u->cq, b->cq, sizeof(u->cq));
    memcpy(u->kings, b->kings, sizeof(u->kings));

    key = b->key ^ P.zob_turn[mover];

    /* The moving seat's own offer expires now; everyone else's survives, and
     * that is what gives the square its three-ply life (§5.1). */
    if (b->ep_target[mover] >= 0) {
        key ^= P.zob_ep[mover][b->ep_target[mover]];
        b->ep_target[mover] = -1;
        b->ep_victim[mover] = -1;
    }

    if (captured) key ^= P.zob_piece[captured][to];
    key ^= P.zob_piece[piece][frm];
    b->sq[frm] = 0;

    placed = promo ? (uint8_t)(1 + mover * NTYPE + promo) : piece;

    if (flag == F_EP) {
        key ^= P.zob_piece[victim][victim_sq];
        b->sq[victim_sq] = 0;
        for (c = 0; c < 4; c++) {
            if (b->ep_target[c] >= 0 && b->ep_victim[c] == victim_sq) {
                key ^= P.zob_ep[c][b->ep_target[c]];
                b->ep_target[c] = -1;
                b->ep_victim[c] = -1;
            }
        }
        b->sq[to] = placed;
        key ^= P.zob_piece[placed][to];
    } else {
        b->sq[to] = placed;
        key ^= P.zob_piece[placed][to];
        if (flag == F_DOUBLE) {
            int target = (frm + to) / 2;
            b->ep_target[mover] = (int16_t)target;
            b->ep_victim[mover] = (int16_t)to;
            key ^= P.zob_ep[mover][target];
        } else if (flag == F_CASTLE_SHORT || flag == F_CASTLE_LONG) {
            int home = (P.king_home[mover][0] == frm) ? 0 : 1;
            const int32_t *g = P.castle[mover][home][flag == F_CASTLE_SHORT ? 0 : 1];
            uint8_t r = b->sq[g[C_ROOK_FROM]];
            b->sq[g[C_ROOK_FROM]] = 0;
            b->sq[g[C_ROOK_TO]] = r;
            key ^= P.zob_piece[r][g[C_ROOK_FROM]] ^ P.zob_piece[r][g[C_ROOK_TO]];
        }
    }

    if (P.pc_type[piece] == KING) b->kings[mover] = (int16_t)to;
    if (captured && P.pc_type[captured] == KING) {
        int cc = P.pc_color[captured];
        if (cc < 4) b->kings[cc] = -1;
    }

    /* Rights lost: the king moved, a rook left home, or a rook was captured
     * on its home square. Checked against the actual king square, which is the
     * check Athena omits (§6.4). */
    if (P.pc_type[piece] == KING && P.pc_color[piece] < 4) {
        int cc = P.pc_color[piece];
        if (b->ck[cc]) { b->ck[cc] = 0; key ^= P.zob_ck[cc]; }
        if (b->cq[cc]) { b->cq[cc] = 0; key ^= P.zob_cq[cc]; }
    }
    for (t = 0; t < 2; t++) {
        int sq = t ? to : frm;
        int owner = P.rook_home[sq];
        int home, king;
        if (!owner) continue;
        owner -= 1;
        if (!(b->ck[owner] || b->cq[owner])) continue;
        king = b->kings[owner];
        home = -1;
        if (king >= 0) {
            if (P.king_home[owner][0] == king) home = 0;
            else if (P.king_home[owner][1] == king) home = 1;
        }
        if (home < 0) continue;      /* king already away: rights are not real */
        if (sq == P.castle[owner][home][0][C_ROOK_FROM]) {
            if (b->ck[owner]) { b->ck[owner] = 0; key ^= P.zob_ck[owner]; }
        } else if (sq == P.castle[owner][home][1][C_ROOK_FROM]) {
            if (b->cq[owner]) { b->cq[owner] = 0; key ^= P.zob_cq[owner]; }
        }
    }

    if (P.pc_type[piece] == PAWN || captured || flag == F_EP) b->halfmove = 0;
    else b->halfmove++;

    t = mover;
    for (c = 0; c < 4; c++) {
        t = (t + 1) & 3;
        if (b->alive[t]) break;
    }
    b->turn = (uint8_t)t;
    key ^= P.zob_turn[b->turn];
    b->key = key;
}

void tt_unmake(TtBoard *b, uint32_t m, const TtUndo *u)
{
    int frm = MV_FROM(m), to = MV_TO(m), flag = MV_FLAG(m), promo = MV_PROMO(m);
    int mover = u->mover;

    if (promo) b->sq[frm] = (uint8_t)(1 + mover * NTYPE + PAWN);
    else b->sq[frm] = b->sq[to];
    b->sq[to] = u->captured;

    if (flag == F_EP) {
        b->sq[u->victim_sq] = u->victim;
    } else if (flag == F_CASTLE_SHORT || flag == F_CASTLE_LONG) {
        int home = (P.king_home[mover][0] == frm) ? 0 : 1;
        const int32_t *g = P.castle[mover][home][flag == F_CASTLE_SHORT ? 0 : 1];
        b->sq[g[C_ROOK_FROM]] = b->sq[g[C_ROOK_TO]];
        b->sq[g[C_ROOK_TO]] = 0;
    }

    memcpy(b->ep_target, u->ep_target, sizeof(b->ep_target));
    memcpy(b->ep_victim, u->ep_victim, sizeof(b->ep_victim));
    memcpy(b->ck, u->ck, sizeof(b->ck));
    memcpy(b->cq, u->cq, sizeof(b->cq));
    memcpy(b->kings, u->kings, sizeof(b->kings));
    b->halfmove = u->halfmove;
    b->turn = u->mover;
    b->key = u->key;
}

int tt_gen_legal(TtBoard *b, uint32_t *out)
{
    uint32_t buf[MAX_MOVES];
    TtUndo u;
    int n = tt_gen_pseudo(b, buf);
    int me = b->turn, i, k = 0;
    for (i = 0; i < n; i++) {
        int king, ok;
        tt_make(b, buf[i], &u);
        king = b->kings[me];
        ok = king < 0 || !tt_is_attacked(b, king, me);
        tt_unmake(b, buf[i], &u);
        if (ok) out[k++] = buf[i];
    }
    return k;
}

/* --- perft -------------------------------------------------------------- */

static uint32_t perft_buf[MAX_DEPTH][MAX_MOVES];

static uint64_t perft_inner(TtBoard *b, int depth)
{
    uint32_t *buf = perft_buf[depth];
    TtUndo u;
    uint64_t total = 0;
    int n, i;

    if (depth == 0) return 1;
    n = tt_gen_legal(b, buf);
    if (depth == 1) return (uint64_t)n;
    for (i = 0; i < n; i++) {
        tt_make(b, buf[i], &u);
        total += perft_inner(b, depth - 1);
        tt_unmake(b, buf[i], &u);
    }
    return total;
}

uint64_t tt_perft(TtBoard *b, int depth)
{
    if (depth < 0 || depth >= MAX_DEPTH) return 0;
    return perft_inner(b, depth);
}

/* Walks the legal tree to `depth`, checking after every make that the
 * incrementally maintained Zobrist key equals a full recompute, and after every
 * unmake that the piece array is restored exactly.
 *
 * Perft cannot see either failure: a wrong incremental key still counts the
 * right number of nodes, and it only surfaces once the transposition table
 * starts trusting it. Returns the number of mismatches. */
static uint64_t key_mismatches;

static void key_walk(TtBoard *b, int depth)
{
    uint32_t buf[MAX_MOVES];
    uint8_t before[NSQ];
    TtUndo u;
    int n, i;

    if (depth == 0) return;
    n = tt_gen_legal(b, buf);
    memcpy(before, b->sq, NSQ);
    for (i = 0; i < n; i++) {
        tt_make(b, buf[i], &u);
        if (b->key != tt_recompute_key(b)) key_mismatches++;
        key_walk(b, depth - 1);
        tt_unmake(b, buf[i], &u);
        if (memcmp(before, b->sq, NSQ) != 0) key_mismatches++;
    }
}

uint64_t tt_key_check(TtBoard *b, int depth)
{
    key_mismatches = 0;
    if (depth > 0 && depth < MAX_DEPTH) key_walk(b, depth);
    return key_mismatches;
}

/* Per-move node counts, for locating a divergence. Writes `moves` and `nodes`
 * arrays supplied by the caller; returns the move count. */
int tt_divide(TtBoard *b, int depth, uint32_t *moves, uint64_t *nodes)
{
    uint32_t buf[MAX_MOVES];
    TtUndo u;
    int n = tt_gen_legal(b, buf), i;
    for (i = 0; i < n; i++) {
        tt_make(b, buf[i], &u);
        moves[i] = buf[i];
        nodes[i] = depth > 0 ? perft_inner(b, depth - 1) : 1;
        tt_unmake(b, buf[i], &u);
    }
    return n;
}

/* --- evaluation --------------------------------------------------------- *
 *
 * throwaway: deleted at Phase 4, replaced by NNUE. Material on the FFA capture
 * values (§8.1) plus a crude king-danger term. Integer only, and mirrored
 * statement for statement by tetrarch/eval_hand.py -- selftest asserts the two
 * agree bit for bit, which is the only reason to keep them in step at all.
 *
 * Returned from the perspective of the side to move's TEAM. In Teams the seat
 * rotation alternates team every ply (team = seat & 1, and the turn advances by
 * one), so plain negamax applies with no special casing (§2).
 */

/* --- NNUE ---------------------------------------------------------------- *
 *
 * Geometry and quantisation mirror tetrarch/nnue.py, which is the definition.
 * The net itself is pushed across by the binding rather than read from disk
 * here: parsing the file in one place means C and Python can never disagree
 * about what a net contains, the same argument as for every other table.
 *
 * This refreshes the whole accumulator on every call. Incremental update on
 * make/unmake is the point of NNUE and is the next step, but it gets its own
 * differential gate first -- an incremental accumulator that drifts is exactly
 * the kind of bug perft and node pins cannot see.
 */

#define NN_FEATURES 3840
#define NN_L1 256
#define NN_EXTRA 7
#define NN_L2 32
#define NN_L3 32
#define NN_SHIFT1 6
#define NN_SHIFT2 6
#define NN_SHIFT_OUT 6
#define NN_CRELU 127
#define NN_EXTRA_CLAMP 127

typedef struct {
    const int16_t *w1;
    const int32_t *b1;
    const int8_t *w2;
    const int32_t *b2;
    const int8_t *w3;
    const int32_t *b3;
    const int8_t *w4;
    const int32_t *b4;
} TtNetView;

static int16_t *nn_w1;
static int32_t nn_b1[NN_L1];
static int8_t nn_w2[NN_L2 * (NN_L1 + NN_EXTRA)];
static int32_t nn_b2[NN_L2];
static int8_t nn_w3[NN_L3 * NN_L2];
static int32_t nn_b3[NN_L3];
static int8_t nn_w4[NN_L3];
static int32_t nn_b4;
static int nn_loaded = 0;

int tt_net_view_size(void) { return (int)sizeof(TtNetView); }

/* dims: features, l1, extra, l2, l3, shift1, shift2, shift_out */
void tt_nnue_dims(int32_t *out)
{
    out[0] = NN_FEATURES; out[1] = NN_L1; out[2] = NN_EXTRA;
    out[3] = NN_L2; out[4] = NN_L3;
    out[5] = NN_SHIFT1; out[6] = NN_SHIFT2; out[7] = NN_SHIFT_OUT;
}

int tt_load_net(const TtNetView *v)
{
    if (!nn_w1) {
        nn_w1 = (int16_t *)malloc((size_t)NN_FEATURES * NN_L1 * sizeof(int16_t));
        if (!nn_w1) return 0;
    }
    memcpy(nn_w1, v->w1, (size_t)NN_FEATURES * NN_L1 * sizeof(int16_t));
    memcpy(nn_b1, v->b1, sizeof(nn_b1));
    memcpy(nn_w2, v->w2, sizeof(nn_w2));
    memcpy(nn_b2, v->b2, sizeof(nn_b2));
    memcpy(nn_w3, v->w3, sizeof(nn_w3));
    memcpy(nn_b3, v->b3, sizeof(nn_b3));
    memcpy(nn_w4, v->w4, sizeof(nn_w4));
    nn_b4 = v->b4[0];
    nn_loaded = 1;
    return 1;
}

void tt_unload_net(void) { nn_loaded = 0; }

int tt_net_loaded(void) { return nn_loaded; }

static inline int32_t nn_crelu(int32_t x, int shift)
{
    x >>= shift;
    if (x < 0) return 0;
    return x > NN_CRELU ? NN_CRELU : x;
}

/* The seven values that bypass the accumulator, rotated to `persp`. */
static void nn_extras(const TtBoard *b, int persp, int32_t *out)
{
    int k;
    for (k = 0; k < 4; k++) out[k] = b->alive[(persp + k) & 3] ? 1 : 0;
    for (k = 1; k < 4; k++) {
        int32_t diff = (int32_t)b->points[persp]
                     - (int32_t)b->points[(persp + k) & 3];
        if (diff > NN_EXTRA_CLAMP) diff = NN_EXTRA_CLAMP;
        if (diff < -NN_EXTRA_CLAMP) diff = -NN_EXTRA_CLAMP;
        out[3 + k] = diff;
    }
}

static int32_t nnue_eval(const TtBoard *b)
{
    int32_t acc[NN_L1];
    int32_t x[NN_L1 + NN_EXTRA];
    int32_t h1[NN_L2], h2[NN_L3];
    int32_t out;
    int sq, j, k, persp = b->turn;

    memcpy(acc, nn_b1, sizeof(acc));
    for (sq = 0; sq < NSQ; sq++) {
        uint8_t p;
        int color, rel, feature;
        const int16_t *row;
        if (!P.valid[sq]) continue;
        p = b->sq[sq];
        if (!p) continue;
        color = P.pc_color[p];
        /* A piece with no recorded origin has no colour to make relative, so
         * it switches on no feature (nnue.py does the same). */
        if (color == DEAD_UNKNOWN) continue;
        rel = (color - persp) & 3;
        feature = (rel * 6 + P.feature_type[P.pc_type[p]]) * 160
                  + P.compact[P.rot_to_persp[persp][sq]];
        row = nn_w1 + (size_t)feature * NN_L1;
        for (j = 0; j < NN_L1; j++) acc[j] += row[j];
    }

    for (j = 0; j < NN_L1; j++) x[j] = nn_crelu(acc[j], NN_SHIFT1);
    nn_extras(b, persp, x + NN_L1);

    for (k = 0; k < NN_L2; k++) {
        const int8_t *row = nn_w2 + (size_t)k * (NN_L1 + NN_EXTRA);
        int32_t z = nn_b2[k];
        for (j = 0; j < NN_L1 + NN_EXTRA; j++) z += x[j] * row[j];
        h1[k] = nn_crelu(z, NN_SHIFT2);
    }
    for (k = 0; k < NN_L3; k++) {
        const int8_t *row = nn_w3 + (size_t)k * NN_L2;
        int32_t z = nn_b3[k];
        for (j = 0; j < NN_L2; j++) z += h1[j] * row[j];
        h2[k] = nn_crelu(z, NN_SHIFT2);
    }
    out = nn_b4;
    for (j = 0; j < NN_L3; j++) out += h2[j] * nn_w4[j];
    return out >> NN_SHIFT_OUT;
}

static int32_t hand_eval(const TtBoard *b)
{
    int me = b->turn & 1;
    int32_t total = 0;
    int sq, c, i;

    for (sq = 0; sq < NSQ; sq++) {
        uint8_t p;
        int pc;
        if (!P.valid[sq]) continue;
        p = b->sq[sq];
        if (!p) continue;
        pc = P.pc_color[p];
        /* Dead seats' pieces are worth nothing to capture (§9.1). They are
         * still on the board and still block; a material eval cannot see
         * that, and the throwaway is not the place to try. */
        if (pc == DEAD_UNKNOWN || !b->alive[pc]) continue;
        total += ((pc & 1) == me ? 1 : -1) * P.piece_value[P.pc_type[p]];
    }

    for (c = 0; c < 4; c++) {
        int k, danger = 0;
        if (!b->alive[c]) continue;
        k = b->kings[c];
        if (k < 0) continue;
        for (i = 0; i < 8; i++) {
            int t = (k + P.queen_dirs[i]) & 255;
            if (P.valid[t] && tt_is_attacked(b, t, c)) danger++;
        }
        total += ((c & 1) == me ? -1 : 1) * danger * P.king_danger;
    }
    return total;
}

/* One binary, two evals: a net is loaded or it is not. That is what makes the
 * NNUE-vs-hand A/B a single setoption apart rather than two builds. */
int32_t tt_eval(const TtBoard *b)
{
    return nn_loaded ? nnue_eval(b) : hand_eval(b);
}

/* --- transposition table ------------------------------------------------ */

#define TT_EXACT 0
#define TT_LOWER 1
#define TT_UPPER 2
#define MATE_SCORE 30000
#define INF_SCORE 32000

typedef struct {
    uint64_t key;
    int32_t score;
    uint32_t best;
    int16_t depth;
    uint8_t flag;
    uint8_t pad;
} TtEntry;

/* Defined with the search below; the table reset clears it too. */
static void history_clear(void);

static TtEntry *tt_table;
static uint64_t tt_mask;
static uint64_t tt_entries;

/* Allocates the largest power-of-two entry count fitting in `mb` megabytes. */
int tt_alloc(int mb)
{
    uint64_t want, n = 1;
    if (mb < 1) mb = 1;
    want = ((uint64_t)mb * 1024u * 1024u) / sizeof(TtEntry);
    while (n * 2 <= want) n *= 2;
    if (tt_table) free(tt_table);
    tt_table = (TtEntry *)calloc((size_t)n, sizeof(TtEntry));
    if (!tt_table) { tt_entries = tt_mask = 0; return 0; }
    tt_entries = n;
    tt_mask = n - 1;
    return 1;
}

void tt_clear(void)
{
    if (tt_table) memset(tt_table, 0, (size_t)tt_entries * sizeof(TtEntry));
    /* History is learned from the same tree the table describes; keeping it
     * across a reset would order by moves that meant something elsewhere, and
     * would make pinned node counts depend on whatever ran before. */
    history_clear();
}

uint64_t tt_size(void) { return tt_entries; }

/* --- search ------------------------------------------------------------- */

/* --- principal variation search (toggle: off by default) ------------------
 *
 * Search the first move with a full window, then test the rest with a null
 * window and re-search only the ones that beat alpha. Returns the same value
 * as plain alpha-beta -- selftest's minimax oracle covers that -- so this is
 * purely a node reduction, and it pays exactly as far as the move ordering is
 * good enough that the first move usually is best.
 */
static int use_pvs = 0;

void tt_set_pvs(int on) { use_pvs = on ? 1 : 0; }
int tt_get_pvs(void) { return use_pvs; }

static uint32_t search_buf[MAX_DEPTH][MAX_MOVES];
static int32_t order_buf[MAX_DEPTH][MAX_MOVES];
static uint64_t search_nodes;
static uint64_t search_limit;
static int search_aborted;

static inline int is_capture(const TtBoard *b, uint32_t m)
{
    return b->sq[MV_TO(m)] != 0 || MV_FLAG(m) == F_EP;
}

/* --- killer moves ---------------------------------------------------------
 *
 * Two quiet moves per ply that last caused a beta cutoff there. The search had
 * no other quiet ordering at all, so this was the largest single ordering gap.
 *
 * CONFIRMED and default ON. Teams / classic, fixed nodes 20,000:
 *   +50.42 +/- 6.41 Elo over 10,000 games
 *   Dist: 57, 8, 388, 31, 969, 47, 763, 21, 216
 * against a null self-test of -2.64 +/- 6.24 on the same harness. See
 * docs/AB.md. The toggle stays so it can be turned off for a future A/B.
 */
#define KILLERS_PER_PLY 2
#define KILLER_BASE (1 << 14)

static uint32_t killers[MAX_DEPTH][KILLERS_PER_PLY];
static int use_killers = 1;

void tt_set_killers(int on) { use_killers = on ? 1 : 0; }
int tt_get_killers(void) { return use_killers; }

static void killers_clear(void) { memset(killers, 0, sizeof(killers)); }

/* --- history heuristic (toggle: off by default, pending its A/B) ---------- *
 *
 * A per-seat from/to table of how often a quiet move caused a cutoff. Killers
 * only order two moves per ply; this orders the whole quiet tail beneath them.
 *
 * Scores land in [0, KILLER_BASE), strictly below the second killer, and are
 * only consulted when the toggle is on -- with it off, score_moves produces
 * byte-identical output, which selftest pins.
 *
 * The update uses the usual gravity rule: adding `bonus - h*bonus/MAX` pulls
 * large entries toward MAX instead of letting them run away, so the table
 * self-normalises and never needs an ageing pass. Values stay inside int16 by
 * construction.
 */
#define HISTORY_MAX KILLER_BASE
#define HISTORY_CAP 400

static int16_t history[4][NSQ][NSQ];
static int use_history = 0;

void tt_set_history(int on) { use_history = on ? 1 : 0; }
int tt_get_history(void) { return use_history; }

static void history_clear(void) { memset(history, 0, sizeof(history)); }

static void history_bonus(int seat, uint32_t m, int depth)
{
    int from = MV_FROM(m), to = MV_TO(m);
    int32_t h = history[seat][from][to];
    int32_t bonus = depth * depth;
    if (bonus > HISTORY_CAP) bonus = HISTORY_CAP;
    h += bonus - (int32_t)((int64_t)h * bonus / HISTORY_MAX);
    if (h < 0) h = 0;
    if (h >= HISTORY_MAX) h = HISTORY_MAX - 1;
    history[seat][from][to] = (int16_t)h;
}

static void killer_store(int ply, uint32_t m)
{
    if (ply >= MAX_DEPTH || killers[ply][0] == m) return;
    killers[ply][1] = killers[ply][0];
    killers[ply][0] = m;
}

/* MVV-LVA, with the transposition move first. */
static void score_moves(const TtBoard *b, uint32_t *moves, int32_t *scores,
                        int n, uint32_t ttmove, int ply)
{
    int i;
    for (i = 0; i < n; i++) {
        uint32_t m = moves[i];
        int32_t s = 0;
        int capture = is_capture(b, m);
        if (m == ttmove) {
            s = 1 << 24;
        } else if (capture) {
            uint8_t victim = b->sq[MV_TO(m)];
            int vv = victim ? P.piece_value[P.pc_type[victim]]
                            : P.piece_value[PAWN];
            int av = P.piece_value[P.pc_type[b->sq[MV_FROM(m)]]];
            s = (1 << 16) + vv * 16 - av;
        } else {
            if (use_killers && ply < MAX_DEPTH) {
                if (m == killers[ply][0]) s = KILLER_BASE + 1;
                else if (m == killers[ply][1]) s = KILLER_BASE;
            }
            if (!s && use_history)
                s = history[b->turn][MV_FROM(m)][MV_TO(m)];
        }
        if (MV_PROMO(m)) s += (1 << 15);
        scores[i] = s;
    }
}

/* Selection sort one move at a time: most of the list is never reached after
 * a cutoff, so sorting it all up front would be wasted work. */
static void pick_move(uint32_t *moves, int32_t *scores, int n, int i)
{
    int best = i, j;
    for (j = i + 1; j < n; j++)
        if (scores[j] > scores[best]) best = j;
    if (best != i) {
        uint32_t tm = moves[i]; moves[i] = moves[best]; moves[best] = tm;
        int32_t ts = scores[i]; scores[i] = scores[best]; scores[best] = ts;
    }
}

static int32_t qsearch(TtBoard *b, int32_t alpha, int32_t beta, int ply)
{
    uint32_t *moves;
    int32_t *scores;
    TtUndo u;
    int n, i, me = b->turn;
    int32_t stand;

    if (++search_nodes >= search_limit) { search_aborted = 1; return 0; }
    if (ply >= MAX_DEPTH - 2) return tt_eval(b);

    stand = tt_eval(b);
    if (stand >= beta) return stand;
    if (stand > alpha) alpha = stand;

    /* Captures are filtered out of the full pseudo-legal list rather than
     * produced by a second, captures-only generator. That generator would be
     * invisible to perft and to the bench signature, and would need its own
     * differential gate; not having it is cheaper than gating it. */
    moves = search_buf[ply];
    scores = order_buf[ply];
    n = tt_gen_pseudo(b, moves);
    score_moves(b, moves, scores, n, 0, -1);

    for (i = 0; i < n; i++) {
        int king;
        pick_move(moves, scores, n, i);
        if (!is_capture(b, moves[i])) continue;
        tt_make(b, moves[i], &u);
        king = b->kings[me];
        if (king >= 0 && tt_is_attacked(b, king, me)) {
            tt_unmake(b, moves[i], &u);
            continue;
        }
        int32_t score = -qsearch(b, -beta, -alpha, ply + 1);
        tt_unmake(b, moves[i], &u);
        if (search_aborted) return 0;
        if (score >= beta) return score;
        if (score > alpha) alpha = score;
    }
    return alpha;
}

static int32_t alphabeta(TtBoard *b, int depth, int32_t alpha, int32_t beta,
                         int ply)
{
    uint32_t *moves, ttmove = 0, best_move = 0;
    int32_t *scores, best = -INF_SCORE, orig_alpha = alpha;
    TtUndo u;
    TtEntry *slot = 0;
    int n, i, legal = 0, me = b->turn, in_chk;

    if (++search_nodes >= search_limit) { search_aborted = 1; return 0; }

    if (tt_table) {
        slot = &tt_table[b->key & tt_mask];
        if (slot->key == b->key) {
            ttmove = slot->best;
            if (slot->depth >= depth && ply > 0) {
                /* A mate score is stored relative to the mating node, not the
                 * root, or the same entry reports a different distance at every
                 * depth it is probed from. */
                int32_t s = slot->score;
                if (s > MATE_SCORE - MAX_DEPTH) s -= ply;
                else if (s < -(MATE_SCORE - MAX_DEPTH)) s += ply;
                if (slot->flag == TT_EXACT) return s;
                if (slot->flag == TT_LOWER && s >= beta) return s;
                if (slot->flag == TT_UPPER && s <= alpha) return s;
            }
        }
    }

    if (depth <= 0) return qsearch(b, alpha, beta, ply);
    if (ply >= MAX_DEPTH - 2) return tt_eval(b);

    in_chk = tt_in_check(b, me);
    moves = search_buf[ply];
    scores = order_buf[ply];
    n = tt_gen_pseudo(b, moves);
    score_moves(b, moves, scores, n, ttmove, ply);

    for (i = 0; i < n; i++) {
        int king;
        int32_t score;
        int is_capture_before;
        pick_move(moves, scores, n, i);
        is_capture_before = is_capture(b, moves[i]);
        tt_make(b, moves[i], &u);
        king = b->kings[me];
        if (king >= 0 && tt_is_attacked(b, king, me)) {
            tt_unmake(b, moves[i], &u);
            continue;
        }
        legal++;
        if (!use_pvs || legal == 1) {
            score = -alphabeta(b, depth - 1, -beta, -alpha, ply + 1);
        } else {
            score = -alphabeta(b, depth - 1, -alpha - 1, -alpha, ply + 1);
            if (!search_aborted && score > alpha && score < beta)
                score = -alphabeta(b, depth - 1, -beta, -alpha, ply + 1);
        }
        tt_unmake(b, moves[i], &u);
        if (search_aborted) return 0;
        if (score > best) {
            best = score;
            best_move = moves[i];
            if (score > alpha) alpha = score;
            if (alpha >= beta) {
                /* Only quiet moves become killers: a capture is already
                 * ordered ahead of them by MVV-LVA, so storing one would
                 * displace a useful quiet for no gain. */
                if (!is_capture_before) {
                    if (use_killers) killer_store(ply, moves[i]);
                    if (use_history) history_bonus(me, moves[i], depth);
                }
                break;
            }
        }
    }

    if (!legal) {
        /* Checkmate ends the game for the whole team in Teams (§7); stalemate
         * on your own turn is a draw. */
        return in_chk ? -(MATE_SCORE - ply) : 0;
    }

    if (slot) {
        int32_t s = best;
        if (s > MATE_SCORE - MAX_DEPTH) s += ply;
        else if (s < -(MATE_SCORE - MAX_DEPTH)) s -= ply;
        slot->key = b->key;
        slot->score = s;
        slot->best = best_move;
        slot->depth = (int16_t)depth;
        slot->flag = (uint8_t)(best <= orig_alpha ? TT_UPPER
                               : (best >= beta ? TT_LOWER : TT_EXACT));
    }
    return best;
}

typedef struct {
    uint64_t nodes;
    int32_t score;
    uint32_t best;
    int32_t depth;
    int32_t aborted;
    int32_t pad;
} TtResult;

int tt_result_size(void) { return (int)sizeof(TtResult); }

/* One fixed-depth search. Iterative deepening and time management live in
 * Python at the root, per the architecture; this is the per-node loop only. */
void tt_search(TtBoard *b, int depth, uint64_t node_limit, TtResult *out)
{
    uint32_t *moves, best_move = 0;
    int32_t *scores, best = -INF_SCORE, alpha = -INF_SCORE;
    TtUndo u;
    int n, i, legal = 0, me = b->turn;

    search_nodes = 0;
    search_limit = node_limit ? node_limit : (uint64_t)-1;
    search_aborted = 0;
    /* Killers are per-search, not per-game: a stale table from another
     * position orders by moves that meant something somewhere else. */
    if (use_killers) killers_clear();

    moves = search_buf[0];
    scores = order_buf[0];
    n = tt_gen_pseudo(b, moves);
    score_moves(b, moves, scores, n,
                (tt_table && tt_table[b->key & tt_mask].key == b->key)
                ? tt_table[b->key & tt_mask].best : 0, 0);

    for (i = 0; i < n; i++) {
        int king;
        int32_t score;
        pick_move(moves, scores, n, i);
        tt_make(b, moves[i], &u);
        king = b->kings[me];
        if (king >= 0 && tt_is_attacked(b, king, me)) {
            tt_unmake(b, moves[i], &u);
            continue;
        }
        legal++;
        /* Claim the move before searching it. If the node budget runs out
         * inside the first subtree we still have to return something legal --
         * otherwise a shallow budget at a deep target yields no move at all. */
        if (legal == 1) best_move = moves[i];
        score = -alphabeta(b, depth - 1, -INF_SCORE, -alpha, 1);
        tt_unmake(b, moves[i], &u);
        if (search_aborted) break;
        if (score > best) {
            best = score;
            best_move = moves[i];
            if (score > alpha) alpha = score;
        }
    }

    out->nodes = search_nodes;
    out->score = legal ? best : (tt_in_check(b, me) ? -MATE_SCORE : 0);
    out->best = best_move;
    out->depth = depth;
    out->aborted = search_aborted;
}
