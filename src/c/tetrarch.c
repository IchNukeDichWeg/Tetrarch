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
