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

/* clock_gettime and CLOCK_MONOTONIC are POSIX, not ISO C, and glibc hides them
 * under -std=c11 unless this is set before any header is pulled in. Apple's
 * headers expose them either way, so the omission compiled cleanly on macOS
 * and only broke on Linux. It has to stay above the includes. */
#define _POSIX_C_SOURCE 199309L

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

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
    /* Points are unbounded, so unlike every other Zobrist term they cannot be
     * a table. These are the per-seat odd multipliers the mixer below uses. */
    uint64_t zob_points[4];
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
    int32_t piece_value[NTYPE];   /* hand eval, mirrored from eval_hand.py */
    int32_t king_danger;
    int32_t mobility;             /* centipawns per reachable square */
    int32_t mobility_cap;         /* per piece; what bounds the term */
    uint8_t rot_to_persp[4][NSQ]; /* board turned so `persp` sits where Red is */
    int32_t feature_type[NTYPE];  /* PQUEEN folds onto QUEEN */
} TtParams;

/* RULES.md 8.1. Deliberately not in P: these are rules, not tuned parameters,
 * and an A/B must never be able to move them. A promoted queen is worth 1, not
 * 9, which is why PQUEEN is a separate piece type at all.
 * KING is unreachable in engine play -- a live king cannot be captured in a
 * legal position, and zombie kings (9.2) are Phase 6+. It is listed so the
 * table matches the published one rather than quietly omitting a row.
 * ponytail: no spare-king row (+3); Tetrarch has one king per seat. */
/* RULES.md 8.2. Checkmating an opponent pays +20; being stalemated pays the
 * stalemated seat itself +20. The zombie-king row (+10 to each remaining seat)
 * is unreachable while 9.2 is out of scope. */
#define FFA_ELIM_POINTS 20

/* RULES.md 8.3, indexed [kings checked - 2][the checker is a queen].
 * A queen forks two lines by standing still, which is why it is paid less. */
static const uint8_t FFA_MULTICHECK_POINTS[2][2] = {
    /* two kings   */ { 5, 1 },
    /* three kings */ { 20, 5 },
};

static const uint8_t FFA_CAPTURE_POINTS[NTYPE] = {
    1,   /* PAWN   */
    3,   /* KNIGHT */
    5,   /* BISHOP -- 5, not 3: the diagonals are long on 14x14 */
    5,   /* ROOK   */
    9,   /* QUEEN  */
    20,  /* KING   */
    1,   /* PQUEEN */
};
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

/* What restoring an elimination needs: the key, and the points 8.2 moved. */
typedef struct {
    uint64_t key;
    uint16_t points[4];
} FfaUndo;

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
    uint8_t scored;
} TtUndo;

static TtParams P;
static int initialised = 0;
static int ph_ok = 0;              /* points_hash's memo is valid; see there */

/* NNUE accumulator hooks. The net itself is defined further down; the deltas
 * are applied here, where the board actually changes. */
static int  nn_delta_on_for(uint64_t key);
static inline void nn_toggle(uint8_t piece, int sq, int sign);
static void build_ray_steps(void);
static void build_attack_tables(void);
static void nn_acc_set_key(uint64_t key);
static void nn_build_tables(void);

/* --- introspection, so the binding can assert layout agreement ---------- */

int tt_params_size(void) { return (int)sizeof(TtParams); }
int tt_board_size(void) { return (int)sizeof(TtBoard); }
int tt_undo_size(void) { return (int)sizeof(TtUndo); }

void tt_init(const TtParams *p)
{
    memcpy(&P, p, sizeof(TtParams));
    build_ray_steps();
    ph_ok = 0;                     /* points_hash's memo used the old zobrist */
    nn_build_tables();
    build_attack_tables();
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
 * attack (§9.1). No caller is left -- the attack tables below and
 * compute_evasions use the colour-mask form -- but it stays as the definition
 * those masks are checked against, so it is marked unused rather than cut. */
static inline __attribute__((unused)) int hostile(const TtBoard *b,
                                                  uint8_t piece, int me)
{
    int c = P.pc_color[piece];
    return c < 4 && b->alive[c] && !same_team(b->mode, c, me);
}

/* What tt_is_attacked needs to know about a piece, in two table loads
 * instead of a type compare chain plus hostile()'s three tests. Built once in
 * tt_init from the pushed-in piece encoding; piece 0 (empty) has neither, so
 * the empty square fails both tests without a separate branch.
 *
 * att_cbit is 1 << colour, so DEAD_UNKNOWN (colour 4) lands on bit 4, which
 * no hostile mask ever sets -- exactly hostile()'s `c < 4`. */
#define AC_KNIGHT 1
#define AC_KING   2
#define AC_PAWN   4
#define AC_ORTHO  8              /* rook, queen, promoted queen */
#define AC_DIAG   16             /* bishop, queen, promoted queen */
static uint8_t att_class[NPIECE];
static uint8_t att_cbit[NPIECE];
static uint8_t att_dir_class[8];  /* AC_ORTHO or AC_DIAG for queen_dirs[i] */
static uint8_t see_pawn_slot[8];  /* j with diag[j] == -queen_dirs[i], else 4 */
/* The on-board knight jumps from each square, in knight_deltas order. The
 * order is irrelevant to the answer; it is kept so nobody has to argue it. */
static uint8_t kn_to[NSQ][8];
static uint8_t kn_n[NSQ];
static uint8_t kg_to[NSQ][8];     /* the same for king steps, queen_dirs order */
static uint8_t kg_n[NSQ];

static void build_attack_tables(void)
{
    int p, i, sq;
    for (p = 0; p < NPIECE; p++) {
        int t = P.pc_type[p];
        att_class[p] = (uint8_t)(t == KNIGHT ? AC_KNIGHT
                       : t == KING ? AC_KING
                       : t == PAWN ? AC_PAWN
                       : t == ROOK ? AC_ORTHO
                       : t == BISHOP ? AC_DIAG
                       : (t == QUEEN || t == PQUEEN) ? (AC_ORTHO | AC_DIAG) : 0);
        att_cbit[p] = (uint8_t)(1u << P.pc_color[p]);
    }
    att_class[0] = att_cbit[0] = 0;
    for (i = 0; i < 8; i++) {
        int d = P.queen_dirs[i], j, diag = 0;
        for (j = 0; j < 4; j++) if (P.diag[j] == d) diag = 1;
        att_dir_class[i] = diag ? AC_DIAG : AC_ORTHO;
    }
    for (i = 0; i < 8; i++) {
        int j;
        see_pawn_slot[i] = 4;
        for (j = 0; j < 4; j++)
            if (P.diag[j] == -P.queen_dirs[i]) see_pawn_slot[i] = (uint8_t)j;
    }
    for (sq = 0; sq < NSQ; sq++) {
        kn_n[sq] = kg_n[sq] = 0;
        if (!P.valid[sq]) continue;
        for (i = 0; i < 8; i++) {
            int t = (sq + P.knight_deltas[i]) & 255;
            if (P.valid[t]) kn_to[sq][kn_n[sq]++] = (uint8_t)t;
            t = (sq + P.queen_dirs[i]) & 255;
            if (P.valid[t]) kg_to[sq][kg_n[sq]++] = (uint8_t)t;
        }
    }
}

/* A pure predicate, so the order of the probes is free to change: only speed
 * depends on it. Two things make this cheaper than the five separate loops it
 * replaced (knights, kings, pawns, rook lines, bishop lines):
 *
 * - Hostility is decided once per call, as a mask over colours, rather than
 *   per piece found. A seat is hostile when alive and not on `me`'s side,
 *   which is what hostile() asks of a piece's colour.
 * - The king and pawn probes read the same eight neighbours the ray walks
 *   start on, so they are folded into the first step of each ray instead of
 *   being two more passes over those squares. A pawn on sq+d attacks sq when
 *   -d is one of its seat's capture deltas; those are all diagonal, so the
 *   test fails by itself on an orthogonal neighbour.
 *
 * ponytail: per-seat occupancy (occ[]) could reject a whole ray with no
 * hostile piece on it, but it is only valid when occ_key matches, and a ray
 * from a king usually ends on the first square at our own piece anyway. */
int tt_is_attacked(const TtBoard *b, int sq, int me)
{
    unsigned hm = 0;
    int c, i, n;

    for (c = 0; c < 4; c++)
        if (b->alive[c] && !same_team(b->mode, c, me)) hm |= 1u << c;
    if (!hm) return 0;               /* nothing on the board can attack us */

    n = kn_n[sq];
    for (i = 0; i < n; i++) {
        uint8_t p = b->sq[kn_to[sq][i]];
        if ((att_class[p] & AC_KNIGHT) && (att_cbit[p] & hm)) return 1;
    }
    for (i = 0; i < 8; i++) {
        int d = P.queen_dirs[i], t = (sq + d) & 255;
        unsigned want = att_dir_class[i];
        uint8_t p;
        if (!P.valid[t]) continue;
        p = b->sq[t];
        if (p) {
            if (att_cbit[p] & hm) {
                unsigned cl = att_class[p];
                if (cl & (want | AC_KING)) return 1;
                if (cl & AC_PAWN) {
                    const int32_t *tk = P.pawn_takes[P.pc_color[p]];
                    if (tk[0] == -d || tk[1] == -d) return 1;
                }
            }
            continue;
        }
        for (t = (t + d) & 255; P.valid[t]; t = (t + d) & 255) {
            p = b->sq[t];
            if (p) {
                if ((att_class[p] & want) && (att_cbit[p] & hm)) return 1;
                break;
            }
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

/* The points standing, hashed for the FFA transposition key.
 *
 * The FFA evaluation READS the points: nn_extras feeds three points
 * differences into the net. So two positions identical on the board but
 * standing at different points evaluate differently, and while the table was
 * keyed on b->key alone they shared a slot -- the paranoid search stored a
 * score computed at one standing and returned it at another. Standings diverge
 * on identical placements routinely: 8.3 pays multi-check bonuses on quiet
 * moves and 8.2 pays elimination to a path-dependent seat.
 *
 * This is deliberately NOT folded into b->key. That key is also the repetition
 * key, and points are monotonic: including them there would mean no FFA
 * position could ever repeat, silently disabling a shipped feature. The two
 * consumers want different things, so only the table's key carries this.
 *
 * Absolute points per seat, not the differences the eval actually sees. Two
 * standings differing by a constant then get separate entries even though the
 * eval scores them alike -- that costs a table hit, never a wrong answer,
 * which is the safe direction for a correctness fix.
 * ponytail: key the three differences from seat 0 if the split ever measures.
 *
 * Splitmix64's finaliser after the multiply, because the low bits of a bare
 * multiply barely move and the table indexes with the low bits.
 *
 * Memoised on the last standing it hashed, packed whole into one word. Every
 * paranoid node probes, so this ran once per node -- twelve multiplies -- yet
 * the standing only moves on a capture, a multi-check or an elimination: a
 * quiet move hands its child the standing its parent hashed, and the probes
 * arrive in exactly that order. The tag is all four points, so a hit returns
 * what the computation would have, never an approximation of it. tt_init
 * drops the memo because zob_points may change under it. */
static uint64_t ph_tag, ph_val;

static uint64_t points_hash(const TtBoard *b)
{
    uint64_t k = 0, tag;
    int c;
    memcpy(&tag, b->points, sizeof(tag));
    if (ph_ok && tag == ph_tag) return ph_val;
    for (c = 0; c < 4; c++) {
        uint64_t x = (uint64_t)(b->points[c] + 1u) * P.zob_points[c];
        x ^= x >> 30; x *= 0xBF58476D1CE4E5B9ull;
        x ^= x >> 27; x *= 0x94D049BB133111EBull;
        k ^= x ^ (x >> 31);
    }
    ph_tag = tag;
    ph_val = k;
    ph_ok = 1;
    return k;
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
        /* The pawn on the victim square has to be the owner's own. An offer
         * only dies when its owner moves again or when the offer is taken, so
         * a normal capture of the double-pushed pawn leaves the offer standing
         * with someone else's pawn on that square -- and without this test the
         * generator will happily "capture en passant" a pawn that never double
         * pushed and that the capturing pawn does not attack. The owner cannot
         * move while its own offer is live, so an owner-coloured pawn there
         * identifies the pushed pawn uniquely. */
        if (P.pc_color[victim] != owner) continue;
        if (!is_enemy(b, victim, me)) continue;
        occupant = b->sq[target];
        if (occupant && !is_enemy(b, occupant, me)) continue;
        targets[n] = target;
        victims[n] = victim_sq;
        n++;
    }
    return n;
}

/* Per-seat occupancy over the 256-slot mailbox, four uint64 per seat.
 *
 * tt_gen_pseudo used to walk all 256 padded slots to find the mover's ~16
 * pieces: 240 of 256 iterations were pure test-and-skip.
 *
 * A file-static with a validity key rather than a TtBoard field -- the field
 * grows the struct by 128 bytes and forces a matching core.py layout change,
 * where a static with the same key-token discipline nn_acc already uses needs
 * neither, and degrades to a rebuild rather than to a wrong answer when a
 * board arrives from outside C (a fresh position, the next A/B game).
 *
 * NODE-IDENTITY: the words are visited low to high and __builtin_ctzll takes
 * the low bit first, so squares come out in ascending `sq` order -- exactly
 * the order the 0..NSQ scan produced. The move list is therefore identical
 * element for element, not merely equivalent, which is what keeps the bench
 * signature fixed.
 *
 * ponytail: four words over the 256-slot mailbox, not three over the 160
 * compact squares. The compact map would save one word per iteration but
 * needs an inverse table and makes the ascending-order argument depend on
 * compact[] being monotonic in sq. Indexing by sq keeps node-identity
 * self-evident. Switch to 160 bits if this ever shows up hot again.
 *
 * FIVE rows, not four. The row is pc_color, and a grey piece of unknown
 * origin (FEN4 `d`, §9.1) has colour DEAD_UNKNOWN = 4. With four rows the
 * rebuild, and make/unmake capturing such a piece, read and wrote occ[4][*]:
 * past the array, onto whatever the linker put next -- on this build the
 * `initialised` flag, which picked up a bit per grey square below rank 3
 * (tt_ready() read 2097177 by the end of selftest's C section), and a
 * different layout puts P, the rules tables, there instead. Row 4 is maintained
 * and never read: no seat moves grey pieces, so the generator only ever walks
 * occ[b->turn]. Guarding every occ_set/occ_clr site with `colour < 4` was the
 * alternative; one spare row costs 32 bytes and no branch on the hot path. */
static uint64_t occ[5][4];
static uint64_t occ_key;
static int occ_ok = 0;

static inline void occ_set(int seat, int sq)
{
    occ[seat][sq >> 6] |= 1ULL << (sq & 63);
}
static inline void occ_clr(int seat, int sq)
{
    occ[seat][sq >> 6] &= ~(1ULL << (sq & 63));
}

static void occ_rebuild(const TtBoard *b)
{
    int sq;
    memset(occ, 0, sizeof(occ));
    for (sq = 0; sq < NSQ; sq++) {
        uint8_t p;
        if (!P.valid[sq]) continue;
        p = b->sq[sq];
        if (p) occ_set(P.pc_color[p], sq);
    }
    occ_key = b->key;
    occ_ok = 1;
}

/* Counted for the profile, like makes: the rate is not one per node and
 * assuming it was made the tool contradict its own output. */
static uint64_t search_gens;

/* `restrict`: the move buffer is a search, perft or caller-owned array and
 * never overlaps the board or P. Without it every store to out[] could, as
 * far as the compiler knows, rewrite P's int32 tables, which forces their
 * reloads inside the loops. */
int tt_gen_pseudo(const TtBoard *b, uint32_t *restrict out)
{
    search_gens++;
    int me = b->turn, n = 0, sq, i, j, wi;
    uint64_t w;
    int promo_coord = P.promo_coord[b->mode];
    int nchoice = P.n_promo_choices[b->mode];
    const int32_t *choices = P.promo_choices[b->mode];
    int push = P.pawn_push[me];
    int base = b->pawn_base_rank;
    int dbl = base ? base - 1 : -1;
    const uint8_t *pcoord = P.pawn_coord[me];
    const int32_t *takes = P.pawn_takes[me];
    int ep_target[4], ep_victim[4];
    int n_offers = ep_offers(b, me, ep_target, ep_victim);
    /* is_enemy() as a mask over colours, decided once per call rather than
     * per target square: DEAD_UNKNOWN and dead seats are fair game to all,
     * and otherwise it is the other side. Bit c answers is_enemy for colour
     * c; att_cbit[p] is 1 << colour, and 0 for an empty square. */
    unsigned em = 1u << DEAD_UNKNOWN;
    int c;
    for (c = 0; c < 4; c++)
        if (!b->alive[c] || !same_team(b->mode, c, me)) em |= 1u << c;

    if (!occ_ok || occ_key != b->key) occ_rebuild(b);

    for (wi = 0; wi < 4; wi++)
    for (w = occ[me][wi]; w; w &= w - 1) {
        const int32_t *deltas;
        const uint8_t *steps;
        int nd;
        sq = (wi << 6) + __builtin_ctzll(w);

        switch (P.pc_type[b->sq[sq]]) {
        case PAWN: {
            int to = (sq + push) & 255;
            if (P.valid[to] && !b->sq[to]) {
                if (pcoord[to] == promo_coord) {
                    for (j = 0; j < nchoice; j++)
                        out[n++] = MK_MOVE(sq, to, F_NORMAL, choices[j]);
                } else {
                    out[n++] = MK_MOVE(sq, to, F_NORMAL, 0);
                }
                /* dbl is -1 when there is no base rank, which no coordinate
                 * equals: the old `base &&` test folded into the compare. */
                if (pcoord[sq] == dbl) {
                    int to2 = (sq + 2 * push) & 255;
                    if (P.valid[to2] && !b->sq[to2])
                        out[n++] = MK_MOVE(sq, to2, F_DOUBLE, 0);
                }
            }
            for (i = 0; i < 2; i++) {
                int flag = F_NORMAL, is_offer = 0;
                to = (sq + takes[i]) & 255;
                if (!P.valid[to]) continue;
                for (j = 0; j < n_offers; j++)
                    if (ep_target[j] == to) { is_offer = 1; break; }
                if (is_offer) {
                    /* An en-passant capture supersedes the plain capture onto
                     * the same square: same from/to, but it also removes the
                     * passing pawn (§5.5). */
                    flag = F_EP;
                } else {
                    if (!(att_cbit[b->sq[to]] & em)) continue;
                }
                if (pcoord[to] == promo_coord) {
                    for (j = 0; j < nchoice; j++)
                        out[n++] = MK_MOVE(sq, to, flag, choices[j]);
                } else {
                    out[n++] = MK_MOVE(sq, to, flag, 0);
                }
            }
            continue;
        }
        /* Knights and kings walk their on-board steps in delta order: the
         * same squares in the same order as testing every delta. */
        case KNIGHT: steps = kn_to[sq]; nd = kn_n[sq]; break;
        case KING:   steps = kg_to[sq]; nd = kg_n[sq]; break;
        case BISHOP: deltas = P.diag; nd = 4; goto slide;
        case ROOK:   deltas = P.ortho; nd = 4; goto slide;
        case QUEEN:
        case PQUEEN: deltas = P.queen_dirs; nd = 8; goto slide;
        default:     continue;
        }

        for (i = 0; i < nd; i++) {
            int to = steps[i];
            uint8_t target = b->sq[to];
            if (!target || (att_cbit[target] & em))
                out[n++] = MK_MOVE(sq, to, F_NORMAL, 0);
        }
        continue;

    slide:
        for (i = 0; i < nd; i++) {
            int d = deltas[i];
            int to = (sq + d) & 255;
            while (P.valid[to]) {
                uint8_t target = b->sq[to];
                if (target) {
                    if (att_cbit[target] & em) out[n++] = MK_MOVE(sq, to, F_NORMAL, 0);
                    break;
                }
                out[n++] = MK_MOVE(sq, to, F_NORMAL, 0);
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

/* Counted so a per-call cost can be turned into a per-node share; a
 * uint64 increment is nothing against the work either side of it. */
static uint64_t search_makes;

/* The published table, for selftest. Two hardcoded copies of a rules constant
 * drift silently -- the engine would score one game and the match runner
 * another -- so the pair is pinned rather than trusted. */
int tt_ffa_capture_points(int ptype)
{
    return (ptype < 0 || ptype >= NTYPE) ? -1 : FFA_CAPTURE_POINTS[ptype];
}

int tt_ffa_elim_points(void) { return FFA_ELIM_POINTS; }

/* Defined below, with the ray geometry it needs (§8.3). */
static int ffa_multicheck_points(const TtBoard *b, int to, int mover);

/* What the mover scores for taking `victim`. Dead seats' pieces are worth
 * nothing (9.1) -- they stay on the board as obstacles, and DEAD_UNKNOWN owns
 * no seat at all. Teams has no points. */
static int ffa_capture_points(const TtBoard *b, uint8_t victim)
{
    int owner;
    if (b->mode != MODE_FFA || !victim) return 0;
    owner = P.pc_color[victim];
    if (owner >= 4 || !b->alive[owner]) return 0;
    return FFA_CAPTURE_POINTS[P.pc_type[victim]];
}

void tt_make(TtBoard *b, uint32_t m, TtUndo *u)
{
    search_makes++;
    int frm = MV_FROM(m), to = MV_TO(m), flag = MV_FLAG(m), promo = MV_PROMO(m);
    int mover = b->turn, c, t;
    uint8_t piece = b->sq[frm], captured = b->sq[to], placed;
    uint64_t key;
    int victim_sq = -1;
    uint8_t victim = 0;
    int nn = nn_delta_on_for(b->key);
    /* Same continuity test the accumulator uses, and for the same reason: a
     * board we did not walk here ourselves must not have deltas applied to a
     * bitboard describing some other position. Decided once, up front, before
     * b->key moves. */
    int oc = occ_ok && occ_key == b->key;

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
        /* An en-passant flag with NO live offer is not an en-passant move.
         * The generator only emits F_EP for a square in ep_offers, so this
         * cannot be reached through the search -- but the exported ctypes
         * surface the test suite drives can hand one in, and the block below
         * then wrote b->sq[-1], which lands on points[3]'s high byte. One
         * guard here and the matching one in tt_unmake degenerate it to the
         * plain move it actually describes. */
        if (victim_sq < 0) flag = 0;
    }

    u->captured = captured;
    u->halfmove = b->halfmove;
    u->key = b->key;
    u->mover = (uint8_t)mover;
    u->victim_sq = (int16_t)victim_sq;
    u->victim = victim;
    /* Stored rather than recomputed: unmake runs after the alive mask may have
     * moved, and 9.1 makes the award depend on it. */
    u->scored = (uint8_t)(ffa_capture_points(b, captured)
                          + ffa_capture_points(b, victim));
    b->points[mover] = (uint16_t)(b->points[mover] + u->scored);
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
    if (nn && captured) nn_toggle(captured, to, -1);
    if (oc && captured) occ_clr(P.pc_color[captured], to);
    key ^= P.zob_piece[piece][frm];
    if (nn) nn_toggle(piece, frm, -1);
    if (oc) occ_clr(mover, frm);
    b->sq[frm] = 0;

    placed = promo ? (uint8_t)(1 + mover * NTYPE + promo) : piece;

    if (flag == F_EP) {
        key ^= P.zob_piece[victim][victim_sq];
        if (nn) nn_toggle(victim, victim_sq, -1);
        if (oc) occ_clr(P.pc_color[victim], victim_sq);
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
        if (nn) nn_toggle(placed, to, +1);
        if (oc) occ_set(mover, to);
    } else {
        b->sq[to] = placed;
        key ^= P.zob_piece[placed][to];
        if (nn) nn_toggle(placed, to, +1);
        if (oc) occ_set(mover, to);
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
            if (nn) {
                nn_toggle(r, g[C_ROOK_FROM], -1);
                nn_toggle(r, g[C_ROOK_TO], +1);
            }
            if (oc) {
                occ_clr(mover, g[C_ROOK_FROM]);
                occ_set(mover, g[C_ROOK_TO]);
            }
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
    if (nn) nn_acc_set_key(key);
    if (oc) occ_key = key;

    /* Last, not with the capture award at the top: 8.3 asks what the moved
     * piece attacks FROM ITS NEW SQUARE, over the occupancy the move leaves
     * behind. A capture that opens a line changes the answer. */
    if (b->mode == MODE_FFA) {
        int bonus = ffa_multicheck_points(b, to, mover);
        if (bonus) {
            u->scored = (uint8_t)(u->scored + bonus);
            b->points[mover] = (uint16_t)(b->points[mover] + bonus);
        }
    }
}

void tt_unmake(TtBoard *b, uint32_t m, const TtUndo *u)
{
    int frm = MV_FROM(m), to = MV_TO(m), flag = MV_FLAG(m), promo = MV_PROMO(m);
    int mover = u->mover;
    int oc = occ_ok && occ_key == b->key;

    /* The matching half of tt_make's guard: make degenerated an en-passant
     * flag with no live offer to a plain move, so undo the plain move. */
    if (flag == F_EP && u->victim_sq < 0) flag = 0;

    b->points[mover] = (uint16_t)(b->points[mover] - u->scored);

    /* Before anything moves: integer add/subtract is exactly invertible, so
     * undoing make's toggles restores the accumulator make was handed. */
    if (nn_delta_on_for(b->key)) {
        uint8_t placed = b->sq[to];
        uint8_t orig = promo ? (uint8_t)(1 + mover * NTYPE + PAWN) : placed;
        nn_toggle(placed, to, -1);
        nn_toggle(orig, frm, +1);
        if (u->captured) nn_toggle(u->captured, to, +1);
        if (flag == F_EP) {
            nn_toggle(u->victim, u->victim_sq, +1);
        } else if (flag == F_CASTLE_SHORT || flag == F_CASTLE_LONG) {
            int home = (P.king_home[mover][0] == frm) ? 0 : 1;
            const int32_t *g =
                P.castle[mover][home][flag == F_CASTLE_SHORT ? 0 : 1];
            uint8_t r = b->sq[g[C_ROOK_TO]];
            nn_toggle(r, g[C_ROOK_TO], -1);
            nn_toggle(r, g[C_ROOK_FROM], +1);
        }
        nn_acc_set_key(u->key);
    }

    if (promo) b->sq[frm] = (uint8_t)(1 + mover * NTYPE + PAWN);
    else b->sq[frm] = b->sq[to];
    b->sq[to] = u->captured;
    if (oc) {
        /* Exactly make's toggles run backwards. The mover leaves `to` and
         * returns to `frm`; a captured piece comes back on `to` under its own
         * seat, which is why the clear and the set are not a single move. */
        occ_clr(mover, to);
        occ_set(mover, frm);
        if (u->captured) occ_set(P.pc_color[u->captured], to);
    }

    if (flag == F_EP) {
        b->sq[u->victim_sq] = u->victim;
        if (oc) occ_set(P.pc_color[u->victim], u->victim_sq);
    } else if (flag == F_CASTLE_SHORT || flag == F_CASTLE_LONG) {
        int home = (P.king_home[mover][0] == frm) ? 0 : 1;
        const int32_t *g = P.castle[mover][home][flag == F_CASTLE_SHORT ? 0 : 1];
        b->sq[g[C_ROOK_FROM]] = b->sq[g[C_ROOK_TO]];
        b->sq[g[C_ROOK_TO]] = 0;
        if (oc) {
            occ_clr(mover, g[C_ROOK_TO]);
            occ_set(mover, g[C_ROOK_FROM]);
        }
    }

    memcpy(b->ep_target, u->ep_target, sizeof(b->ep_target));
    memcpy(b->ep_victim, u->ep_victim, sizeof(b->ep_victim));
    memcpy(b->ck, u->ck, sizeof(b->ck));
    memcpy(b->cq, u->cq, sizeof(b->cq));
    memcpy(b->kings, u->kings, sizeof(b->kings));
    b->halfmove = u->halfmove;
    b->turn = u->mover;
    b->key = u->key;
    if (oc) occ_key = u->key;
}

/* --- pins, and the legality fast path -------------------------------------
 *
 * Every move used to be made and then checked with a full attack scan on the
 * king. Most moves cannot possibly expose their own king, and knowing which
 * costs one scan per node instead of one per move.
 *
 * `ray_step` is the between/through table: the step from a towards b when the
 * two are on a line, 0 otherwise. It says both whether b is on a ray from a
 * and which way to walk it, which is all the geometry the pin test needs.
 *
 * The fast path is deliberately one-sided. It returns "certainly legal" or
 * "do not know", never "illegal", so anything it declines falls through to the
 * scan that was always there. A pin the scan finds and this misses costs
 * speed; only a pin this claims is absent when it is present would be a bug,
 * which is why the differential in selftest.py compares the two answers on
 * every move of every position rather than sampling. */
static int8_t ray_step[NSQ][NSQ];

/* Is (dst - src) & 255 one of the eight knight deltas? `(src + d) & 255 ==
 * dst` holds exactly when `d == dst - src` mod 256, so one lookup answers what
 * piece_attacks used to loop eight deltas for -- the same answer for every
 * pair of squares, by arithmetic rather than by test. That loop ran on every
 * knight move of every FFA make (8.3 asks whether the moved piece hits each
 * other live king). */
static uint8_t knight_hit[256];

static void build_ray_steps(void)
{
    int a, i;
    memset(knight_hit, 0, sizeof(knight_hit));
    for (i = 0; i < 8; i++) knight_hit[P.knight_deltas[i] & 255] = 1;
    memset(ray_step, 0, sizeof(ray_step));
    for (a = 0; a < NSQ; a++) {
        if (!P.valid[a]) continue;
        for (i = 0; i < 8; i++) {
            int d = P.queen_dirs[i];
            int t = (a + d) & 255;
            while (P.valid[t]) {
                ray_step[a][t] = (int8_t)d;
                t = (t + d) & 255;
            }
        }
    }
}

static int is_diag(int d)
{
    int i;
    for (i = 0; i < 4; i++) if (P.diag[i] == d) return 1;
    return 0;
}

/* Does the piece standing on `src` attack `dst`? One piece, not the square's
 * whole attacker set -- tt_is_attacked answers a different and much more
 * expensive question, and 8.3 asks about the piece that moved. */
static int piece_attacks(const TtBoard *b, int src, int dst)
{
    uint8_t p = b->sq[src];
    int type, step, t;
    if (!p || src == dst) return 0;
    type = P.pc_type[p];

    if (type == KNIGHT) return knight_hit[(dst - src) & 255];

    step = ray_step[src][dst];
    if (!step) return 0;                       /* not on any line at all */

    if (type == PAWN) {
        /* Capture deltas are per seat (§4.1) and are the only squares a pawn
         * attacks -- the push is not an attack. */
        const int32_t *takes = P.pawn_takes[P.pc_color[p]];
        if (((src + takes[0]) & 255) != dst && ((src + takes[1]) & 255) != dst)
            return 0;
        return 1;
    }
    if (type == KING) return ((src + step) & 255) == dst;
    if (type == ROOK && is_diag(step)) return 0;
    if (type == BISHOP && !is_diag(step)) return 0;

    for (t = (src + step) & 255; t != dst; t = (t + step) & 255)
        if (b->sq[t]) return 0;                /* blocked */
    return 1;
}

/* 8.3: the bonus for a move that checks more than one king at once.
 *
 * The rule names ONE checking piece -- "with a queen" against "with any other
 * piece" -- so what is counted is the kings the MOVED piece attacks, not every
 * king that happens to be in check after the move. A discovered check by some
 * third piece is not covered by the wording at any value, and no source
 * settles it; recorded as open item 11 rather than guessed at.
 *
 * A promoted queen counts as a queen: the discount exists because a queen
 * forks two lines without moving, and a 1-point queen forks identically.
 *
 * Only the destination square is tested, so a castling rook that lands on a
 * double check goes uncredited. Same reason: the rule names one piece, and
 * which of the two a castle counts as is not written down anywhere.
 *
 * Dead seats are skipped. Their kings stay on the board (§9.1) and b->kings
 * still points at them, so without this a seat could be paid for "checking" a
 * king that left the game.
 */
static int ffa_multicheck_points(const TtBoard *b, int to, int mover)
{
    int seat, checked = 0, missed = 0, queenish, type;
    if (b->mode != MODE_FFA) return 0;
    type = P.pc_type[b->sq[to]];
    for (seat = 0; seat < 4; seat++) {
        int k = b->kings[seat];
        if (seat == mover || !b->alive[seat] || k < 0) continue;
        if (piece_attacks(b, to, k)) checked++;
        /* There are at most three other kings, so two misses leave one hit
         * at most, and only two or more pays. Nearly every move hits no king
         * at all, so this skips the third test on nearly every make without
         * changing a single award. Gathering the candidate kings first and
         * testing a third only after a hit came out SLOWER (+0.46% FFA
         * instructions): the gather cost more than the test it saved. */
        else if (++missed == 2) return 0;
    }
    if (checked < 2) return 0;
    queenish = (type == QUEEN || type == PQUEEN);
    return FFA_MULTICHECK_POINTS[checked - 2][queenish];
}


typedef struct { int16_t sq; int16_t dir; } Pin;

/* Own pieces standing between the king and an enemy slider that would attack
 * it if they moved off the line. At most one per direction, so at most 8. */
static int compute_pins(const TtBoard *b, int me, int king, Pin *pins)
{
    int i, n = 0, c;
    unsigned hm = 0;
    if (king < 0) return 0;
    /* hostile() as a colour mask, decided once: see tt_is_attacked. */
    for (c = 0; c < 4; c++)
        if (b->alive[c] && !same_team(b->mode, c, me)) hm |= 1u << c;
    for (i = 0; i < 8; i++) {
        int d = P.queen_dirs[i];
        unsigned want = att_dir_class[i];
        int t = (king + d) & 255, own = -1;
        while (P.valid[t]) {
            uint8_t p = b->sq[t];
            if (p) {
                if (own < 0) {
                    /* A teammate's piece shields the king too, but this seat
                     * cannot move it, so nothing behind it is pinned for us. */
                    if (P.pc_color[p] != me) break;
                    own = t;
                } else {
                    if ((att_class[p] & want) && (att_cbit[p] & hm)) {
                        pins[n].sq = (int16_t)own;
                        pins[n].dir = (int16_t)d;
                        n++;
                    }
                    break;
                }
            }
            t = (t + d) & 255;
        }
    }
    return n;
}

/* 1 when the move cannot leave this seat's own king attacked, 0 when it might
 * and the scan has to decide. Never says illegal. */
static int surely_legal(uint32_t m, int king, const Pin *pins, int npins,
                        int in_chk)
{
    int frm, to, i;
#ifdef NO_PIN_FASTPATH
    /* Always "do not know", so every move takes the scan. The engine behaves
     * exactly as it did before this existed, which is what makes the claim
     * that it changes nothing measurable rather than argued. */
    (void)m; (void)king; (void)pins; (void)npins; (void)in_chk;
    (void)frm; (void)to; (void)i;
    return 0;
#else
    /* In check, the king itself moving, or castling: all need the scan. En
     * passant clears a third square, so it can open a line no pin test saw. */
    if (in_chk || king < 0 || MV_FLAG(m) == F_EP) return 0;
    frm = MV_FROM(m);
    if (frm == king) return 0;
    for (i = 0; i < npins; i++) {
        if (pins[i].sq != frm) continue;
        /* Pinned: still legal while it stays on the line it is shielding. A
         * slider cannot pass through the pinner, so anywhere reachable on that
         * ray either blocks still or captures the pinner. */
        to = MV_TO(m);
        return ray_step[king][to] == pins[i].dir;
    }
    return 1;                      /* not pinned, not the king, not in check */
#endif
}

/* The other side of the same coin: in check, most pseudo-legal moves are
 * illegal, and every one of them used to be made, scanned and unmade. In a
 * bench round that was 122k of the 179k makes inside quiescence -- the
 * evasion search tries every move, and a quiet move by a piece that neither
 * captures the checker nor blocks the line answers nothing.
 *
 * `ev` is a 256-bit set over the mailbox: the squares a move by anything
 * other than the king must land on. Each checker contributes its own square
 * plus, for a slider, the squares between it and the king; the sets of all
 * checkers are intersected, so a double check leaves the set empty and only
 * king moves survive to the scan. Intersecting rather than counting checkers
 * means no argument about whose lines can cross is needed.
 *
 * Why a move landing outside the set is CERTAINLY illegal: the checker still
 * stands (the move did not land on it, and only en passant removes a piece
 * from a square other than `to`, so F_EP is declined), every square between
 * it and the king is still empty (they were empty, `to` is not one of them,
 * and vacating `frm` only empties more), the king has not moved (frm != king;
 * castling is a king move and falls out here too), and nothing about who is
 * hostile to whom changes inside tt_make. So the same attack is there after
 * the move and the scan would reject it. Like surely_legal this is one-sided:
 * "certainly illegal" or "do not know", and selftest compares it against the
 * scan on every move of every position it samples. */
static void compute_evasions(const TtBoard *b, int me, int king, uint64_t ev[4])
{
    unsigned hm = 0;
    int i, c, n;
    ev[0] = ev[1] = ev[2] = ev[3] = ~0ULL;
    /* The same probes as tt_is_attacked, collecting every checker instead of
     * stopping at the first; see there for the tables and the folding of the
     * king and pawn probes into each ray's first step. */
    for (c = 0; c < 4; c++)
        if (b->alive[c] && !same_team(b->mode, c, me)) hm |= 1u << c;
#define EV_ONLY(s) do {                                                    \
        uint64_t keep_ = ev[(s) >> 6] & (1ULL << ((s) & 63));              \
        ev[0] = ev[1] = ev[2] = ev[3] = 0;                                 \
        ev[(s) >> 6] = keep_;                                              \
    } while (0)
    n = kn_n[king];
    for (i = 0; i < n; i++) {
        int t = kn_to[king][i];
        uint8_t p = b->sq[t];
        if ((att_class[p] & AC_KNIGHT) && (att_cbit[p] & hm)) EV_ONLY(t);
    }
    for (i = 0; i < 8; i++) {
        int d = P.queen_dirs[i], t = (king + d) & 255;
        unsigned want = att_dir_class[i];
        uint8_t p;
        if (!P.valid[t]) continue;
        p = b->sq[t];
        if (p) {
            if (att_cbit[p] & hm) {
                unsigned cl = att_class[p];
                const int32_t *tk = P.pawn_takes[P.pc_color[p]];
                /* Adjacent: its own square is the whole answer. */
                if ((cl & (want | AC_KING))
                    || ((cl & AC_PAWN) && (tk[0] == -d || tk[1] == -d)))
                    EV_ONLY(t);
            }
            continue;
        }
        for (t = (t + d) & 255; P.valid[t]; t = (t + d) & 255) {
            p = b->sq[t];
            if (p) {
                if ((att_class[p] & want) && (att_cbit[p] & hm)) {
                    /* The line from the king out to the checker, inclusive. */
                    uint64_t line[4] = { 0, 0, 0, 0 };
                    int s = king;
                    do {
                        s = (s + d) & 255;
                        line[s >> 6] |= 1ULL << (s & 63);
                    } while (s != t);
                    ev[0] &= line[0]; ev[1] &= line[1];
                    ev[2] &= line[2]; ev[3] &= line[3];
                }
                break;
            }
        }
    }
#undef EV_ONLY
}

/* 1 when the move certainly leaves this seat's king attacked; only meaningful
 * in check, with `ev` from compute_evasions. Never says legal. */
static inline int surely_illegal(uint32_t m, int king, const uint64_t ev[4])
{
#ifdef NO_PIN_FASTPATH
    (void)m; (void)king; (void)ev;
    return 0;
#else
    int to = MV_TO(m);
    if (MV_FLAG(m) == F_EP || MV_FROM(m) == king) return 0;
    return !((ev[to >> 6] >> (to & 63)) & 1);
#endif
}

/* Differential for selftest: does the fast path ever call a move certainly
 * legal that the scan rejects, or (in check) certainly illegal that the scan
 * accepts? Returns the number of such moves, so 0 is the only acceptable
 * answer. Counting rather than flagging means a regression reports how wrong
 * it is. */
int tt_legality_disagreements(TtBoard *b)
{
    uint32_t buf[MAX_MOVES];
    Pin pins[8];
    TtUndo u;
    int n = tt_gen_pseudo(b, buf);
    int me = b->turn, i, bad = 0;
    int king0 = b->kings[me];
    int in_chk = king0 >= 0 && tt_is_attacked(b, king0, me);
    int npins = compute_pins(b, me, king0, pins);
    uint64_t evade[4];
    if (in_chk) compute_evasions(b, me, king0, evade);
    for (i = 0; i < n; i++) {
        int king, really, legal_claim, illegal_claim;
        legal_claim = surely_legal(buf[i], king0, pins, npins, in_chk);
        illegal_claim = in_chk && surely_illegal(buf[i], king0, evade);
        if (!legal_claim && !illegal_claim) continue;
        tt_make(b, buf[i], &u);
        king = b->kings[me];
        really = king < 0 || !tt_is_attacked(b, king, me);
        tt_unmake(b, buf[i], &u);
        /* Both claims at once is itself a contradiction. */
        if (legal_claim && !really) bad++;
        if (illegal_claim && really) bad++;
    }
    return bad;
}

/* --- static exchange evaluation -------------------------------------------
 *
 * What a capture is really worth once both sides have finished trading on the
 * square. Move ordering currently scores a capture as victim*16 - attacker,
 * which rates "queen takes defended pawn" as brilliant.
 *
 * The two-player swap-off applies unchanged in TEAMS and only there. Recapture
 * order follows the seat rotation, and team = seat & 1 with the turn advancing
 * by one, so the side to move on the square alternates every ply exactly as it
 * does in two-player chess. In FFA it does not -- three separate opponents may
 * each decline -- so this is not used there, and tt_see says so rather than
 * returning a number nobody should trust.
 *
 * Pins and legality are ignored, as everywhere else this algorithm is used: a
 * recapture that turns out to be illegal makes the estimate pessimistic, never
 * unsound as an ordering key. */

/* The attackers of `to`, gathered once per exchange rather than rescanned per
 * recapture.
 *
 * This used to be see_attacker(): every recapture walked all eight knight
 * squares, four pawn squares, eight rays and eight king squares again, over a
 * 256-byte `gone` array zeroed per call. Only one square changes between two
 * recaptures -- the attacker just spent -- so almost all of that rescan
 * re-read what the last one read.
 *
 * What is kept is exactly what the rescan looked at, in its order: the knight
 * squares in knight_deltas order, the pawn squares in diag order, the first
 * unspent piece on each ray in queen_dirs order, the kings in queen_dirs order.
 * The pick is the same strict `value < best` over that same order, kings only
 * when nothing else answers, so ties go to the same square and the x-rays
 * that follow are the same.
 *
 * Why only one ray ever needs re-walking: a spent piece is a knight (on no
 * ray from `to`), or the first piece of its ray -- an adjacent pawn or king
 * is the first piece of the ray it stands on, and a slider is picked only as
 * a ray's first piece. So spending it moves that one ray's front outward and
 * nothing else; every square further out is still unspent, except `frm`,
 * which the walk skips the way `gone[frm]` made it transparent before. A
 * spent knight, pawn or king has its team cleared so it can never be picked
 * again.
 *
 * `tm` is the team as a bit: 1 << (colour & 1) for a seat, 0 for
 * DEAD_UNKNOWN, which same_team() never matches. Teams only; tt_see returns
 * before any of this in FFA. */
typedef struct {
    int16_t sq[8];
    uint8_t tm[8];
    uint8_t ray[8];      /* pawns and kings: the ray they front */
    int n;
} SeeList;

typedef struct {
    SeeList kn, pw, kg;
    int16_t fr[8];       /* first unspent piece on each ray, or -1 */
    uint8_t fr_tm[8];    /* its team if it is a slider that hits along it */
    int32_t fr_val[8];
} SeeState;

static inline unsigned see_team(int colour)
{
    return colour < 4 ? 1u << (colour & 1) : 0u;
}

static inline void see_front(const TtBoard *b, SeeState *s, int i, int start,
                             int frm)
{
    int d = P.queen_dirs[i], t = (start + d) & 255;
    s->fr[i] = -1;
    s->fr_tm[i] = 0;
    for (; P.valid[t]; t = (t + d) & 255) {
        uint8_t p = b->sq[t];
        if (!p || t == frm) continue;
        s->fr[i] = (int16_t)t;
        if (att_class[p] & att_dir_class[i]) {
            s->fr_tm[i] = (uint8_t)see_team(P.pc_color[p]);
            s->fr_val[i] = P.piece_value[P.pc_type[p]];
        }
        return;
    }
}

static void see_gather(const TtBoard *b, SeeState *s, int to, int frm)
{
    int i, n = kn_n[to];
    s->kn.n = s->kg.n = 0;
    for (i = 0; i < n; i++) {
        int t = kn_to[to][i];
        uint8_t p = b->sq[t];
        if ((att_class[p] & AC_KNIGHT) && t != frm) {
            s->kn.sq[s->kn.n] = (int16_t)t;
            s->kn.tm[s->kn.n++] = (uint8_t)see_team(P.pc_color[p]);
        }
    }
    /* Pawns sit in fixed slots by diag index, so the pick still sees them in
     * the diag order the old pawn loop used even though they are found here
     * in queen_dirs order. Kings are found in queen_dirs order, which is
     * already the old king loop's. */
    for (i = 0; i < 4; i++) s->pw.tm[i] = 0;
    for (i = 0; i < 8; i++) {
        int d = P.queen_dirs[i], t = (to + d) & 255;
        uint8_t p;
        s->fr[i] = -1;
        s->fr_tm[i] = 0;
        if (!P.valid[t]) continue;
        p = b->sq[t];
        if (!p || t == frm) {
            see_front(b, s, i, t, frm);
            continue;
        }
        /* The first piece on this ray is adjacent: the old king and pawn
         * probes read exactly these squares. */
        s->fr[i] = (int16_t)t;
        if (att_class[p] & att_dir_class[i]) {
            s->fr_tm[i] = (uint8_t)see_team(P.pc_color[p]);
            s->fr_val[i] = P.piece_value[P.pc_type[p]];
        } else if (att_class[p] & AC_KING) {
            s->kg.sq[s->kg.n] = (int16_t)t;
            s->kg.tm[s->kg.n] = (uint8_t)see_team(P.pc_color[p]);
            s->kg.ray[s->kg.n++] = (uint8_t)i;
        } else if ((att_class[p] & AC_PAWN) && see_pawn_slot[i] < 4) {
            int c = P.pc_color[p];
            if (P.pawn_takes[c][0] == -d || P.pawn_takes[c][1] == -d) {
                int j = see_pawn_slot[i];
                s->pw.sq[j] = (int16_t)t;
                s->pw.tm[j] = (uint8_t)see_team(c);
                s->pw.ray[j] = (uint8_t)i;
            }
        }
    }
}

/* Does `seat`'s team have any unspent attacker of `to` left? see_next's
 * candidates, kings included, without picking or spending one. */
static int see_any(const SeeState *s, int seat)
{
    unsigned want = 1u << (seat & 1);
    int i;
    for (i = 0; i < s->kn.n; i++) if (s->kn.tm[i] & want) return 1;
    for (i = 0; i < 4; i++) if (s->pw.tm[i] & want) return 1;
    for (i = 0; i < 8; i++) if (s->fr_tm[i] & want) return 1;
    for (i = 0; i < s->kg.n; i++) if (s->kg.tm[i] & want) return 1;
    return 0;
}

/* Cheapest unspent piece of `seat`'s team attacking `to`; spends it and
 * returns its square, or -1. */
static int see_next(const TtBoard *b, SeeState *s, int to, int frm, int seat)
{
    unsigned want = 1u << (seat & 1);
    int best = -1, kind = 0, idx = 0, i;
    int32_t best_val = 1 << 30;

    for (i = 0; i < s->kn.n; i++)
        if ((s->kn.tm[i] & want) && P.piece_value[KNIGHT] < best_val) {
            best = s->kn.sq[i]; best_val = P.piece_value[KNIGHT];
            kind = 1; idx = i;
        }
    for (i = 0; i < 4; i++)
        if ((s->pw.tm[i] & want) && P.piece_value[PAWN] < best_val) {
            best = s->pw.sq[i]; best_val = P.piece_value[PAWN];
            kind = 2; idx = i;
        }
    for (i = 0; i < 8; i++)
        if ((s->fr_tm[i] & want) && s->fr_val[i] < best_val) {
            best = s->fr[i]; best_val = s->fr_val[i];
            kind = 3; idx = i;
        }
    if (best < 0) {
        for (i = 0; i < s->kg.n; i++)
            if (s->kg.tm[i] & want) {
                best = s->kg.sq[i]; kind = 4; idx = i;
                break;
            }
    }
    switch (kind) {
    case 1: s->kn.tm[idx] = 0; break;
    case 2: s->pw.tm[idx] = 0; see_front(b, s, s->pw.ray[idx], best, frm); break;
    case 3: see_front(b, s, idx, best, frm); break;
    case 4: s->kg.tm[idx] = 0; see_front(b, s, s->kg.ray[idx], best, frm); break;
    default: break;
    }
    (void)to;
    return best;
}

int32_t tt_see(const TtBoard *b, uint32_t m)
{
    SeeState st;
    int32_t gain[40];
    int to = MV_TO(m), frm = MV_FROM(m), d = 0;
    int seat = b->turn, attacker;
    int32_t attacker_val;

    if (b->mode == MODE_FFA) return 0;          /* see the note above */

    if (MV_FLAG(m) == F_EP) {
        /* An en-passant capture onto an OCCUPIED skipped square takes both the
         * occupant and the passing pawn (§5.5), and tt_make removes both. SEE
         * crediting only the pawn under-prices the move by a whole piece, and
         * SEEPrune -- which is default on and prunes on the sign -- would then
         * discard a capture that wins material outright. */
        gain[0] = P.piece_value[PAWN]
                  + (b->sq[to] ? P.piece_value[P.pc_type[b->sq[to]]] : 0);
    } else {
        gain[0] = b->sq[to] ? P.piece_value[P.pc_type[b->sq[to]]] : 0;
    }

    attacker_val = P.piece_value[P.pc_type[b->sq[frm]]];
    see_gather(b, &st, to, frm);

    /* The usual implementation breaks out early once neither side would enter
     * the continuation. That is a real speedup and it is why most engines do
     * it, but it returns a BOUND rather than the value: on a rook takes
     * defended pawn with our own rook behind it, the exact answer is -300 and
     * the pruned one is -400. Engines get away with that because they only ask
     * `SEE >= threshold`. The intended use here is a sign test in quiescence,
     * where a bound that crosses zero would prune a capture that is actually
     * sound, so this runs the exchange out. The list is at most one entry per
     * attacker, so the cost is bounded and small. */
    for (;;) {
        d++;
        gain[d] = attacker_val - gain[d - 1];
        seat = (seat + 1) & 3;
        attacker = see_next(b, &st, to, frm, seat);
        if (attacker < 0 || d >= 38) break;
        attacker_val = P.piece_value[P.pc_type[b->sq[attacker]]];
        /* A king may recapture only onto a square nobody can take it on. The
         * piece table prices the king at 0 -- right for material, where it is
         * never captured -- so the swap-off used to let the other side "take"
         * it for free and fold that back as a real line: rook takes a pawn
         * the king alone defends, with a second rook behind, scored -400
         * where it wins +100, and SEEPrune threw the capture away. Pins are
         * ignored everywhere here; this is not a pin, it is the one move the
         * exchange may never contain. The test sees through the king's own
         * square, as the next capturer would: see_next has already spent the
         * king and moved its ray's front past it. */
        if (P.pc_type[b->sq[attacker]] == KING
            && see_any(&st, (seat + 1) & 3))
            break;
    }
    /* Fold back: at every step the side to move could have stopped instead. */
    while (--d > 0)
        if (-gain[d] < gain[d - 1]) gain[d - 1] = -gain[d];
    return gain[0];
}

/* 1 when tt_see(b, m) >= 0 is already certain from the first capture alone,
 * so a caller that only wants the sign can skip the exchange.
 *
 * Exact, not a heuristic, and it follows from the fold above rather than from
 * chess intuition. The fold only ever lowers an entry (it takes the min with
 * -gain[d]), so the final gain[1] <= its initial attacker_val - gain[0], and
 * the final gain[0] = min(gain[0], -gain[1]) >= min(V, V - A), where V is the
 * initial gain[0] and A the first attacker's value. With V >= 0 and V >= A
 * that is >= 0 whatever the rest of the exchange does. FFA returns 0, which
 * is >= 0 too. V is computed exactly as tt_see computes it, en-passant onto
 * an occupied square included, so the two cannot disagree on any move.
 *
 * Taking a piece worth at least the taker is most captures quiescence sees,
 * and each one used to run the full swap-off -- the attacker gather and a
 * pick per step -- only to report a sign this already knew. */
static inline int see_surely_nonneg(const TtBoard *b, uint32_t m)
{
    int to = MV_TO(m);
    int32_t v = b->sq[to] ? P.piece_value[P.pc_type[b->sq[to]]] : 0;
    int32_t a = P.piece_value[P.pc_type[b->sq[MV_FROM(m)]]];
    if (MV_FLAG(m) == F_EP) v += P.piece_value[PAWN];
    return v >= 0 && v >= a;
}

int tt_gen_legal(TtBoard *b, uint32_t *out)
{
    uint32_t buf[MAX_MOVES];
    Pin pins[8];
    TtUndo u;
    int n = tt_gen_pseudo(b, buf);
    int me = b->turn, i, k = 0;
    int king0 = b->kings[me];
    int in_chk = king0 >= 0 && tt_is_attacked(b, king0, me);
    int npins = compute_pins(b, me, king0, pins);
    uint64_t evade[4];
    if (in_chk) compute_evasions(b, me, king0, evade);
    for (i = 0; i < n; i++) {
        int king, ok;
        if (surely_legal(buf[i], king0, pins, npins, in_chk)) {
            out[k++] = buf[i];
            continue;
        }
        if (in_chk && surely_illegal(buf[i], king0, evade)) continue;
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
 * The accumulator is maintained incrementally across make/unmake. All four
 * perspectives are kept, because the seat to move changes every ply and a
 * perspective's features are a permutation no other perspective can supply.
 *
 * The deltas hang off exactly the sites where tt_make already XORs a Zobrist
 * key for a (piece, square). That is deliberate: the two updates then have
 * identical trigger conditions, so a missed toggle also corrupts the key, and
 * selftest already checks keys bit for bit against the Python reference.
 *
 * Correctness rests on integer add/subtract being exactly invertible, so
 * unmake restores the accumulator to the value make found -- there is no
 * accumulating error term. What can still go wrong is a MISSED or WRONG
 * toggle, which perft and node pins cannot see, so tt_nnue_acc_matches()
 * exposes a differential check and selftest runs it after every move of
 * random games.
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
    int32_t version;          /* 1 keeps the old extras, 2 puts them on the
                               * same 127x footing as the accumulator half */
    const int16_t *w1;
    const int32_t *b1;
    const int8_t *w2;
    const int32_t *b2;
    const int8_t *w3;
    const int32_t *b3;
    const int8_t *w4;
    const int32_t *b4;
} TtNetView;

/* L1 weights, NN_FEATURES rows of NN_L1 int16, 64-byte aligned. malloc
 * returns 16-byte alignment, and with a 512-byte row stride every row then
 * sits at the same offset into its line -- 48 on Linux/glibc here -- so half
 * of nn_apply's 32-byte AVX-512 loads straddle two lines on every row.
 * tt_load_net over-allocates and rounds up. (The AVX2 path's 16-byte loads
 * never straddle at a multiple of 16, which is why only one path cared.) */
static int16_t *nn_w1;
static int32_t nn_b1[NN_L1];
/* L2 rows, split out of the single (NN_L1 + NN_EXTRA)-stride block the .nnue
 * file and tetrarch/nnue.py still ship. The packed stride is 263 bytes -- not
 * a multiple of 16 -- so every one of the 32 rows starts at a different
 * misalignment and every vector load in nn_dot straddles a cache line across
 * the whole 8,416-byte block. Splitting gives nn_w2main a 256-byte stride off
 * a 64-byte-aligned base, so every row is aligned and the extras stop being
 * interleaved into the hot scan. tt_load_net does the split; the FILE FORMAT
 * AND THE BINDING DO NOT CHANGE. */
static int8_t nn_w2main[NN_L2 * NN_L1] __attribute__((aligned(64)));
static int8_t nn_w2extra[NN_L2 * NN_EXTRA];
static int32_t nn_b2[NN_L2];
/* nn_b2 less 128 * (row sum of the extras' weights): see nn_l2's VNNI path. */
static int32_t nn_b2vnni[NN_L2];
static int8_t nn_w3[NN_L3 * NN_L2];
/* The same L2 and L3 weights again, COLUMN-BLOCKED for the x86 path (see
 * nn_sums_run): block c holds input bytes 4c..4c+3 of every output row, row
 * after row, so one 32-byte load is those four inputs' weights for eight
 * outputs. nn_w2col runs on past the 256 main inputs into two blocks of the
 * extras, padded with a zero weight, which the VNNI path reads; AVX2 reads
 * nn_w2excol instead, the extras as int16 pairs. Built by tt_load_net
 * alongside the row copies, which the scalar and NEON paths still read. */
#define NN_EXPAIRS ((NN_EXTRA + 1) / 2)
_Static_assert(NN_EXTRA < 8, "the extras pad to two 4-byte blocks");
static int8_t nn_w2col[(NN_L1 + 8) * NN_L2] __attribute__((aligned(64)));
static int16_t nn_w2excol[NN_EXPAIRS * NN_L2 * 2] __attribute__((aligned(64)));
static int8_t nn_w3col[NN_L2 * NN_L3] __attribute__((aligned(64)));
static int32_t nn_b3[NN_L3];
static int8_t nn_w4[NN_L3];
static int32_t nn_b4;
static int nn_loaded = 0;
static int nn_version = 1;

/* The maintained accumulator, one per perspective, plus the board key it
 * belongs to. The key is the validity token: anything that reaches a board
 * without going through make/unmake fails the comparison and forces a
 * refresh, so a missed path degrades to the old cost rather than to a wrong
 * evaluation. */
/* 64-byte aligned for the aligned zmm loads in nn_apply. */
static int32_t nn_acc[4][NN_L1] __attribute__((aligned(64)));
static uint64_t nn_acc_key;
static int nn_acc_ok = 0;

/* The feature a piece on a square switches on, seen from `persp`: a piece
 * half (colour made relative to the perspective, and type) plus a square half
 * (the square turned to the perspective, then compacted). */
static int nn_piece_base(int persp, int color, int type)
{
    return (((color - persp) & 3) * 6 + type) * 160;
}

static int nn_square_index(int sq, int persp)
{
    return P.compact[P.rot_to_persp[persp][sq]];
}

static int nn_feature_of(int sq, int persp, int color, int type)
{
    return nn_piece_base(persp, color, type) + nn_square_index(sq, persp);
}

/* One perspective, one feature row, applied now. */
static void nn_row(int persp, int feature, int sign)
{
    const int16_t *row = nn_w1 + (size_t)feature * NN_L1;
    int32_t *acc = nn_acc[persp];
    int j;
    if (sign > 0) { for (j = 0; j < NN_L1; j++) acc[j] += row[j]; }
    else          { for (j = 0; j < NN_L1; j++) acc[j] -= row[j]; }
}

/* Lazy perspectives.
 *
 * A profile of the search put this work at 23% of the time -- it ran on every
 * make and unmake for all four perspectives, while an evaluation reads only
 * `nn_acc[b->turn]`. Three quarters of it was paid at nodes that never looked
 * at it.
 *
 * Toggles are therefore queued per perspective and applied when that
 * perspective is read. The queue cancels: pushing the exact inverse of the
 * entry on top pops it instead, so descending into a subtree and unwinding
 * out of it again costs no row arithmetic at all for a perspective nobody
 * evaluated. Cancelling only an exact inverse is what makes this safe -- the
 * pair is arithmetically a no-op whatever order it arrived in, so a queue
 * that fails to cancel is slower and never wrong.
 *
 * The queue is bounded and flushes when full, so a pathological line costs
 * time rather than correctness. */
#define NN_PEND_MAX 1024
/* One entry is `feature << 1 | (sign < 0)`: an entry's exact inverse is
 * `e ^ 1`, so the cancellation test is a single compare. */
static int32_t nn_pend[4][NN_PEND_MAX];
static int nn_pend_n[4];

/* Apply a flush's rows in ONE pass over the accumulator.
 *
 * nn_flush used to call nn_row once per queued row, and every nn_row is a
 * full read-modify-write of the 1 KB accumulator. A flush is 5.9 rows on
 * average over the bench (2,231,897 rows in 379,536 flushes), so most of that
 * traffic was the accumulator going out to L1 and straight back. Here a slice
 * of it is held in registers while every row's slice is added or subtracted
 * into it, and it is loaded and stored once per flush instead of once per row.
 * The caller splits the rows into an add list and a subtract list, so the
 * inner loops carry no sign branch.
 *
 * Bit-exact: the accumulator is int32 and is never clamped or saturated, the
 * widened int16 weights are added exactly, and integer addition is
 * associative, so the order the rows land in cannot change a bit of it. No
 * partial sum is near overflow: |w1| < 2^15 and a queue is at most 1,024 rows.
 *
 * Intrinsics rather than the plain loop because neither plain form came out
 * well. Written as a fused C loop over a local slice, GCC vectorises it but
 * round-trips the slice through a stack copy at every pass: 392M instructions
 * in nn_flush over the bench against 350M for the old row-at-a-time loop,
 * i.e. worse. GCC's generic vector extensions kept the slice on the stack
 * entirely under AVX2. -DNN_SCALAR forces the reference loop, as for nn_dot,
 * and it is also what NEON gets: the compiler vectorises it by itself. A
 * first attempt at vectorising nn_row alone was 4% slower (docs/AB.md, lazy
 * perspectives); this is a different change -- it removes accumulator
 * traffic rather than speeding up the arithmetic the compiler already
 * vectorised. */
#if !defined(NN_SCALAR) && (defined(__AVX512BW__) || defined(__AVX2__))
#include <immintrin.h>
#endif
#if !defined(NN_SCALAR) && defined(__AVX512BW__)
/* 128 lanes = 8 zmm per pass, two passes. */
static void nn_apply(int32_t *acc, const int32_t *adds, int na,
                     const int32_t *subs, int ns)
{
    int c, i;
    for (c = 0; c < NN_L1; c += 128) {
        const int16_t *w = nn_w1 + c;
        __m512i t0 = _mm512_load_si512((const void *)(acc + c + 0));
        __m512i t1 = _mm512_load_si512((const void *)(acc + c + 16));
        __m512i t2 = _mm512_load_si512((const void *)(acc + c + 32));
        __m512i t3 = _mm512_load_si512((const void *)(acc + c + 48));
        __m512i t4 = _mm512_load_si512((const void *)(acc + c + 64));
        __m512i t5 = _mm512_load_si512((const void *)(acc + c + 80));
        __m512i t6 = _mm512_load_si512((const void *)(acc + c + 96));
        __m512i t7 = _mm512_load_si512((const void *)(acc + c + 112));
#define NN_W(k) _mm512_cvtepi16_epi32( \
                    _mm256_loadu_si256((const __m256i *)(row + 16 * (k))))
        for (i = 0; i < na; i++) {
            const int16_t *row = w + adds[i];
            t0 = _mm512_add_epi32(t0, NN_W(0)); t1 = _mm512_add_epi32(t1, NN_W(1));
            t2 = _mm512_add_epi32(t2, NN_W(2)); t3 = _mm512_add_epi32(t3, NN_W(3));
            t4 = _mm512_add_epi32(t4, NN_W(4)); t5 = _mm512_add_epi32(t5, NN_W(5));
            t6 = _mm512_add_epi32(t6, NN_W(6)); t7 = _mm512_add_epi32(t7, NN_W(7));
        }
        for (i = 0; i < ns; i++) {
            const int16_t *row = w + subs[i];
            t0 = _mm512_sub_epi32(t0, NN_W(0)); t1 = _mm512_sub_epi32(t1, NN_W(1));
            t2 = _mm512_sub_epi32(t2, NN_W(2)); t3 = _mm512_sub_epi32(t3, NN_W(3));
            t4 = _mm512_sub_epi32(t4, NN_W(4)); t5 = _mm512_sub_epi32(t5, NN_W(5));
            t6 = _mm512_sub_epi32(t6, NN_W(6)); t7 = _mm512_sub_epi32(t7, NN_W(7));
        }
#undef NN_W
        _mm512_store_si512((void *)(acc + c + 0), t0);
        _mm512_store_si512((void *)(acc + c + 16), t1);
        _mm512_store_si512((void *)(acc + c + 32), t2);
        _mm512_store_si512((void *)(acc + c + 48), t3);
        _mm512_store_si512((void *)(acc + c + 64), t4);
        _mm512_store_si512((void *)(acc + c + 80), t5);
        _mm512_store_si512((void *)(acc + c + 96), t6);
        _mm512_store_si512((void *)(acc + c + 112), t7);
    }
}
#elif !defined(NN_SCALAR) && defined(__AVX2__)
/* 64 lanes = 8 ymm per pass, four passes. All 16 ymm would leave no
 * register for the widened row. */
static void nn_apply(int32_t *acc, const int32_t *adds, int na,
                     const int32_t *subs, int ns)
{
    int c, i;
    for (c = 0; c < NN_L1; c += 64) {
        const int16_t *w = nn_w1 + c;
        __m256i t0 = _mm256_load_si256((const __m256i *)(acc + c + 0));
        __m256i t1 = _mm256_load_si256((const __m256i *)(acc + c + 8));
        __m256i t2 = _mm256_load_si256((const __m256i *)(acc + c + 16));
        __m256i t3 = _mm256_load_si256((const __m256i *)(acc + c + 24));
        __m256i t4 = _mm256_load_si256((const __m256i *)(acc + c + 32));
        __m256i t5 = _mm256_load_si256((const __m256i *)(acc + c + 40));
        __m256i t6 = _mm256_load_si256((const __m256i *)(acc + c + 48));
        __m256i t7 = _mm256_load_si256((const __m256i *)(acc + c + 56));
#define NN_W(k) _mm256_cvtepi16_epi32( \
                    _mm_loadu_si128((const __m128i *)(row + 8 * (k))))
        for (i = 0; i < na; i++) {
            const int16_t *row = w + adds[i];
            t0 = _mm256_add_epi32(t0, NN_W(0)); t1 = _mm256_add_epi32(t1, NN_W(1));
            t2 = _mm256_add_epi32(t2, NN_W(2)); t3 = _mm256_add_epi32(t3, NN_W(3));
            t4 = _mm256_add_epi32(t4, NN_W(4)); t5 = _mm256_add_epi32(t5, NN_W(5));
            t6 = _mm256_add_epi32(t6, NN_W(6)); t7 = _mm256_add_epi32(t7, NN_W(7));
        }
        for (i = 0; i < ns; i++) {
            const int16_t *row = w + subs[i];
            t0 = _mm256_sub_epi32(t0, NN_W(0)); t1 = _mm256_sub_epi32(t1, NN_W(1));
            t2 = _mm256_sub_epi32(t2, NN_W(2)); t3 = _mm256_sub_epi32(t3, NN_W(3));
            t4 = _mm256_sub_epi32(t4, NN_W(4)); t5 = _mm256_sub_epi32(t5, NN_W(5));
            t6 = _mm256_sub_epi32(t6, NN_W(6)); t7 = _mm256_sub_epi32(t7, NN_W(7));
        }
#undef NN_W
        _mm256_store_si256((__m256i *)(acc + c + 0), t0);
        _mm256_store_si256((__m256i *)(acc + c + 8), t1);
        _mm256_store_si256((__m256i *)(acc + c + 16), t2);
        _mm256_store_si256((__m256i *)(acc + c + 24), t3);
        _mm256_store_si256((__m256i *)(acc + c + 32), t4);
        _mm256_store_si256((__m256i *)(acc + c + 40), t5);
        _mm256_store_si256((__m256i *)(acc + c + 48), t6);
        _mm256_store_si256((__m256i *)(acc + c + 56), t7);
    }
}
#else
static void nn_apply(int32_t *acc, const int32_t *adds, int na,
                     const int32_t *subs, int ns)
{
    int i, j;
    for (i = 0; i < na; i++) {
        const int16_t *row = nn_w1 + adds[i];
        for (j = 0; j < NN_L1; j++) acc[j] += row[j];
    }
    for (i = 0; i < ns; i++) {
        const int16_t *row = nn_w1 + subs[i];
        for (j = 0; j < NN_L1; j++) acc[j] -= row[j];
    }
}
#endif

/* REJECTED here, so it is not re-derived: cancelling inverse pairs that are
 * NOT adjacent in the queue. They exist -- a sibling line replaying a move
 * the previous line also made leaves +f ... -f with other entries between --
 * and they are 526,518 of the bench's 2,231,897 flushed rows (23.6%). But
 * pairing them off (a generation-stamped last-index-per-feature table, one
 * lookup per entry) saved only 6.9M instructions of 2,765M (0.25%), and the
 * cache simulation showed why: D1 read misses in nn_flush were unchanged
 * (4.59M both ways). The rows it skips were already L1 hits -- the second of
 * a pair re-reads the row the first just loaded, and the first is a feature
 * the earlier line's evaluation applied moments before. A forward scan for
 * the inverse in a 64-entry window cost 30M instructions MORE than it saved.
 * The FFA search, which queues only the root seat, flushes short queues at
 * every evaluation, and there the table was a net LOSS: nn_flush 89.6M ->
 * 99.3M on the FFA position, because most queues are too short to hold a
 * pair and still pay the pass. (callgrind, one bench round, x86-64-v3.) */
static void nn_flush(int persp)
{
    int n = nn_pend_n[persp], i, na = 0, ns = 0;
    int32_t *q = nn_pend[persp];
    int32_t subs[NN_PEND_MAX];
    if (n == 0) return;
    nn_pend_n[persp] = 0;
    for (i = 0; i < n; i++) {
        int32_t e = q[i];
        /* Row offsets into nn_w1. Adds are compacted into q itself, which
         * is being emptied; the write index never passes the read index. */
        if (e & 1) subs[ns++] = (e >> 1) * NN_L1;
        else       q[na++] = (e >> 1) * NN_L1;
    }
    nn_apply(nn_acc[persp], q, na, subs, ns);
}

static void nn_push(int persp, int32_t e)
{
    int n = nn_pend_n[persp];
    if (n > 0 && nn_pend[persp][n - 1] == (e ^ 1)) {
        nn_pend_n[persp] = n - 1;
        return;
    }
    if (n == NN_PEND_MAX) { nn_flush(persp); n = 0; }
    nn_pend[persp][n] = e;
    nn_pend_n[persp] = n + 1;
}

/* Applied to every perspective immediately. Only the rebuild uses this; the
 * search goes through the queue. */
static void nn_toggle_now(uint8_t piece, int sq, int sign)
{
    int persp, color = P.pc_color[piece], type;
    if (color == DEAD_UNKNOWN) return;
    type = P.feature_type[P.pc_type[piece]];
    for (persp = 0; persp < 4; persp++)
        nn_row(persp, nn_feature_of(sq, persp, color, type), sign);
}

/* nn_feature_of's two halves for all four perspectives, precomputed in
 * queue-entry units (feature * 2), so nn_toggle's work per perspective is two
 * loads and an add. In place it was the colour rotation, the type fold
 * through two tables and two dependent square lookups, per perspective, on
 * every make and unmake. Built from P by tt_init. A piece with no recorded
 * colour gets -1, which nn_toggle tests instead of pc_color. */
static int16_t nn_piece_entry[NPIECE][4];
static int16_t nn_square_entry[NSQ][4];

static void nn_build_tables(void)
{
    int p, sq, persp;
    for (p = 0; p < NPIECE; p++) {
        int color = P.pc_color[p];
        for (persp = 0; persp < 4; persp++)
            nn_piece_entry[p][persp] = (int16_t)(color == DEAD_UNKNOWN ? -1
                : 2 * nn_piece_base(persp, color, P.feature_type[P.pc_type[p]]));
    }
    for (sq = 0; sq < NSQ; sq++)
        for (persp = 0; persp < 4; persp++)
            nn_square_entry[sq][persp] = (int16_t)(2 * nn_square_index(sq, persp));
}

/* One-perspective maintenance, for the paranoid FFA search.
 *
 * Every FFA node is scored in the ROOT seat's terms, so of the four queues
 * nn_toggle feeds, three are never flushed during an FFA search: each entry
 * is pushed on the way down and cancelled by its exact inverse on the way
 * back up. That was pure overhead -- three feature computations and three
 * push-or-pop per toggle, 6.2% of an FFA search's instructions with the
 * inlining below -- for queues that end the search exactly where they
 * started.
 *
 * While nn_only >= 0 only that perspective is queued. The other three are
 * left untouched, so they still describe the board the search was entered
 * with, and the search hands that same board back. The one way that fails is
 * a refresh inside the search, which rebuilds all four from a board deep in
 * the tree; nn_single_begin refreshes at the root first so that cannot happen
 * on any path the search takes, and nn_single_end distrusts the accumulator
 * if it happened anyway. -1 is the ordinary mode, all four. */
static int nn_only = -1;
static int nn_only_refreshed;

static __attribute__((noinline)) void nn_toggle_all(uint8_t piece, int sq,
                                                    int sign)
{
    const int16_t *pe = nn_piece_entry[piece], *se = nn_square_entry[sq];
    int persp, neg = sign < 0;
    /* A piece with no recorded origin has no colour to make relative, so it
     * switches on no feature -- the same rule the full refresh applies. */
    if (pe[0] < 0) return;
    for (persp = 0; persp < 4; persp++)
        nn_push(persp, pe[persp] + se[persp] + neg);
}

/* The one-seat path is inlined into make and unmake and the four-seat path is
 * kept out of line, so the call FFA makes 800k times a bench round is two
 * table lookups and a push rather than a call that saves the registers the
 * four-way loop needs. Teams pays the same out-of-line call it always did. */
static inline void nn_toggle(uint8_t piece, int sq, int sign)
{
    int persp = nn_only, e;
    if (persp < 0) { nn_toggle_all(piece, sq, sign); return; }
    e = nn_piece_entry[piece][persp];
    if (e < 0) return;
    nn_push(persp, e + nn_square_entry[sq][persp] + (sign < 0));
}

/* Deltas are safe only when the accumulator actually describes the board in
 * front of us. `nn_acc_ok` alone is not that: it stays set after a search
 * ends, so a board arriving from outside -- the next self-play game, the next
 * A/B position -- would have make/unmake apply deltas to a stale accumulator
 * AND advance nn_acc_key to match, which defeats the staleness check in
 * nnue_eval and silently evaluates the whole search on garbage.
 *
 * Tying it to the key instead makes maintenance conditional on continuity: a
 * board we did not walk here ourselves fails the test, no deltas are applied,
 * the key is left alone, and the next evaluation refreshes. */
static int nn_delta_on_for(uint64_t key) { return nn_loaded && nn_acc_ok
                                                  && nn_acc_key == key; }
static void nn_acc_set_key(uint64_t key) { nn_acc_key = key; }

/* Rebuild all four perspectives from the board. The oracle the incremental
 * path is checked against, and the fallback whenever the key does not match. */
static void nn_refresh(const TtBoard *b)
{
    int persp, sq;
    for (persp = 0; persp < 4; persp++) {
        memcpy(nn_acc[persp], nn_b1, sizeof(nn_b1));
        /* Anything queued described the position we are abandoning. */
        nn_pend_n[persp] = 0;
    }
    for (sq = 0; sq < NSQ; sq++) {
        uint8_t p;
        if (!P.valid[sq]) continue;
        p = b->sq[sq];
        if (p) nn_toggle_now(p, sq, +1);
    }
    nn_acc_key = b->key;
    nn_acc_ok = 1;
    nn_only_refreshed = 1;
}

/* Enter and leave one-perspective mode (see nn_only). Refreshing at the root
 * costs nothing extra: a stale accumulator would be refreshed at the first
 * evaluation anyway, and a refresh is exact, so it is the same arithmetic
 * either way and no evaluation moves. */
static void nn_single_begin(const TtBoard *b, int persp)
{
    if (!nn_loaded) return;
    if (!nn_acc_ok || nn_acc_key != b->key) nn_refresh(b);
    nn_only_refreshed = 0;
    nn_only = persp;
}

static void nn_single_end(void)
{
    if (nn_only < 0) return;
    nn_only = -1;
    /* A refresh in mode rebuilt the other three from a board the search has
     * since left. Unreachable while every path below the root maintains the
     * key, but if one ever does not, this degrades to one refresh at the next
     * evaluation rather than to three silently wrong perspectives. */
    if (nn_only_refreshed) nn_acc_ok = 0;
}

/* Differential check for selftest: does the maintained accumulator equal a
 * rebuild from the board? Returns -1 when nothing is being maintained. The
 * maintained value is restored either way, so a chain of moves keeps being
 * tested rather than being silently repaired mid-game. */
int tt_nnue_acc_matches(const TtBoard *b)
{
    int32_t save[4][NN_L1];
    uint64_t save_key = nn_acc_key;
    int ok;
    int persp;
    if (!nn_loaded || !nn_acc_ok) return -1;
    for (persp = 0; persp < 4; persp++) nn_flush(persp);
    memcpy(save, nn_acc, sizeof(save));
    nn_refresh(b);
    ok = memcmp(save, nn_acc, sizeof(save)) == 0;
    memcpy(nn_acc, save, sizeof(save));
    nn_acc_key = save_key;
    return ok;
}

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
        /* Never freed, as before: it lives as long as the library. */
        void *mem = malloc((size_t)NN_FEATURES * NN_L1 * sizeof(int16_t) + 63);
        if (!mem) return 0;
        nn_w1 = (int16_t *)(((uintptr_t)mem + 63) & ~(uintptr_t)63);
    }
    memcpy(nn_w1, v->w1, (size_t)NN_FEATURES * NN_L1 * sizeof(int16_t));
    memcpy(nn_b1, v->b1, sizeof(nn_b1));
    {   /* One packed 263-wide row in, one aligned 256-wide row plus a 7-wide
         * extras row out. Nothing upstream of here is aware of the split. */
        int k;
        for (k = 0; k < NN_L2; k++) {
            const int8_t *src = v->w2 + (size_t)k * (NN_L1 + NN_EXTRA);
            memcpy(nn_w2main + (size_t)k * NN_L1, src, NN_L1);
            memcpy(nn_w2extra + (size_t)k * NN_EXTRA, src + NN_L1, NN_EXTRA);
        }
    }
    {   /* The column-blocked copies: element (row k, input j) goes to block
         * j/4, slot k, byte j%4. */
        int k, j;
        for (k = 0; k < NN_L2; k++)
            for (j = 0; j < NN_L1 + 8; j++)
                nn_w2col[(j / 4) * (NN_L2 * 4) + k * 4 + j % 4] =
                    j < NN_L1 + NN_EXTRA
                        ? v->w2[(size_t)k * (NN_L1 + NN_EXTRA) + j] : 0;
        for (k = 0; k < NN_L2; k++) {
            int32_t sum = 0;
            for (j = 0; j < NN_EXTRA; j++)
                sum += v->w2[(size_t)k * (NN_L1 + NN_EXTRA) + NN_L1 + j];
            nn_b2vnni[k] = v->b2[k] - 128 * sum;
        }
        for (k = 0; k < NN_L2; k++)
            for (j = 0; j < NN_EXPAIRS * 2; j++)
                nn_w2excol[(j / 2) * (NN_L2 * 2) + k * 2 + j % 2] = (int16_t)(
                    j < NN_EXTRA ? v->w2[(size_t)k * (NN_L1 + NN_EXTRA)
                                         + NN_L1 + j] : 0);
        for (k = 0; k < NN_L3; k++)
            for (j = 0; j < NN_L2; j++)
                nn_w3col[(j / 4) * (NN_L3 * 4) + k * 4 + j % 4] =
                    v->w3[(size_t)k * NN_L2 + j];
    }
    memcpy(nn_b2, v->b2, sizeof(nn_b2));
    memcpy(nn_w3, v->w3, sizeof(nn_w3));
    memcpy(nn_b3, v->b3, sizeof(nn_b3));
    memcpy(nn_w4, v->w4, sizeof(nn_w4));
    nn_b4 = v->b4[0];
    nn_loaded = 1;
    nn_version = v->version ? v->version : 1;
    nn_acc_ok = 0;                 /* different weights: rebuild on next eval */
    nn_pend_n[0] = nn_pend_n[1] = nn_pend_n[2] = nn_pend_n[3] = 0;
    return 1;
}

void tt_unload_net(void)
{
    int persp;
    nn_loaded = 0;
    nn_acc_ok = 0;
    for (persp = 0; persp < 4; persp++) nn_pend_n[persp] = 0;
}

int tt_net_loaded(void) { return nn_loaded; }

/* The seven values that bypass the accumulator, rotated to `persp`. */
static void nn_extras(const TtBoard *b, int persp, int32_t *out)
{
    /* Every other input reaches the quantised net at 127x its float value --
     * the accumulator half is crelu'd to [0,127] where the trainer clips to
     * [0,1]. An alive flag of 1 was 127x too quiet against that, so version 2
     * sends 127. The points differences already arrive on the right footing:
     * the trainer divides them by 127, so their raw [-127,127] IS the scaled
     * value, and scaling again would overflow the int8 input. */
    int k, alive = nn_version >= 2 ? NN_EXTRA_CLAMP : 1;
    for (k = 0; k < 4; k++) out[k] = b->alive[(persp + k) & 3] ? alive : 0;
    for (k = 1; k < 4; k++) {
        int32_t diff = (int32_t)b->points[persp]
                     - (int32_t)b->points[(persp + k) & 3];
        if (diff > NN_EXTRA_CLAMP) diff = NN_EXTRA_CLAMP;
        if (diff < -NN_EXTRA_CLAMP) diff = -NN_EXTRA_CLAMP;
        out[3 + k] = diff;
    }
}

/* Dot product of `n` int8 pairs, `n` a multiple of 32.
 *
 * `a` must be non-negative. Every caller passes crelu output, which is [0,127],
 * and the AVX2 path needs that: `maddubs` reads its first operand as unsigned.
 * The signed extras are deliberately not routed through here.
 *
 * This is bit-identical to the scalar loop, not merely close. The products are
 * exact in int32 and integer addition is associative, so the order the lanes
 * accumulate in cannot change the total. That matters more than the speed: an
 * evaluation that moved by a single centipawn would change which move is
 * chosen and quietly invalidate every A/B in docs/AB.md. */
/* -DNN_SCALAR forces the reference loop, so the vector paths can be shown
 * bit-identical on any machine rather than only where they compile. */
#if !defined(NN_SCALAR) && defined(__ARM_FEATURE_DOTPROD)
#include <arm_neon.h>
static inline int32_t nn_dot(const int8_t *a, const int8_t *b, int n)
{
    int32x4_t acc = vdupq_n_s32(0);
    int i;
    for (i = 0; i < n; i += 16)
        acc = vdotq_s32(acc, vld1q_s8(a + i), vld1q_s8(b + i));
    return vaddvq_s32(acc);
}
#elif !defined(NN_SCALAR) && defined(__AVX2__)
#include <immintrin.h>
/* x86 runs L2 and L3 column-blocked (nn_sums_run, below); NEON and the scalar
 * reference keep the row-at-a-time loop. */
#define NN_COLUMNS 1

/* acc + four adjacent u8 x s8 products per int32 lane, 256 bits wide.
 *
 * With VNNI this is one vpdpbusd, which sums in int32 and has no saturation
 * constraint at all. Without it, maddubs sums two products into one int16
 * WITH saturation. The widest pair is 127 * -128 * 2 = -32,512 against a
 * -32,768 floor (127 * 127 * 2 = 32,258 on the other side), so it cannot
 * saturate -- but the margin is under 1%, so it is a real constraint on the
 * quantisation rather than a comfortable one. Raising NN_CRELU past 127 would
 * break the AVX2-only path silently, and only that path. */
#if defined(__AVX512VNNI__) && defined(__AVX512VL__)
#define NN_VNNI 1
static inline __m256i nn_dpbusd(__m256i acc, __m256i a, __m256i b)
{
    return _mm256_dpbusd_epi32(acc, a, b);
}
#elif defined(__AVXVNNI__)
#define NN_VNNI 1
static inline __m256i nn_dpbusd(__m256i acc, __m256i a, __m256i b)
{
    return _mm256_dpbusd_avx_epi32(acc, a, b);
}
#else
static inline __m256i nn_dpbusd(__m256i acc, __m256i a, __m256i b)
{
    return _mm256_add_epi32(acc, _mm256_madd_epi16(_mm256_maddubs_epi16(a, b),
                                                   _mm256_set1_epi16(1)));
}
#endif

static inline int32_t nn_dot(const int8_t *a, const int8_t *b, int n)
{
    __m256i acc = _mm256_setzero_si256();
    __m128i lo;
    int i;
    for (i = 0; i < n; i += 32)
        acc = nn_dpbusd(acc, _mm256_loadu_si256((const __m256i *)(a + i)),
                        _mm256_loadu_si256((const __m256i *)(b + i)));
    lo = _mm_add_epi32(_mm256_castsi256_si128(acc),
                       _mm256_extracti128_si256(acc, 1));
    lo = _mm_hadd_epi32(lo, lo);
    lo = _mm_hadd_epi32(lo, lo);
    return _mm_cvtsi128_si32(lo);
}

/* The vector the column-blocked layers accumulate in: a zmm where AVX-512
 * VNNI is there to fill it, a ymm otherwise. NN_VPR is how many of them hold
 * one layer's 32 int32 sums. */
#if defined(__AVX512VNNI__) && defined(__AVX512BW__)
#define NN_ZMM 1
#define NN_VPR 2
typedef __m512i NnVec;
static inline NnVec nn_vzero(void) { return _mm512_setzero_si512(); }
static inline NnVec nn_vbcast(int32_t v) { return _mm512_set1_epi32(v); }
static inline NnVec nn_vloadu(const void *p) { return _mm512_loadu_si512(p); }
static inline NnVec nn_vadd(NnVec a, NnVec b) { return _mm512_add_epi32(a, b); }
static inline NnVec nn_vdp(NnVec acc, NnVec a, const int8_t *w)
{
    return _mm512_dpbusd_epi32(acc, a, _mm512_load_si512((const void *)w));
}
static inline void nn_vsplit(NnVec v, __m256i *s)
{
    s[0] = _mm512_castsi512_si256(v);
    s[1] = _mm512_extracti64x4_epi64(v, 1);
}
#else
#define NN_VPR 4
typedef __m256i NnVec;
static inline NnVec nn_vzero(void) { return _mm256_setzero_si256(); }
static inline NnVec nn_vbcast(int32_t v) { return _mm256_set1_epi32(v); }
static inline NnVec nn_vloadu(const void *p)
{
    return _mm256_loadu_si256((const __m256i *)p);
}
static inline NnVec nn_vadd(NnVec a, NnVec b) { return _mm256_add_epi32(a, b); }
static inline NnVec nn_vdp(NnVec acc, NnVec a, const int8_t *w)
{
    return nn_dpbusd(acc, a, _mm256_load_si256((const __m256i *)w));
}
static inline void nn_vsplit(NnVec v, __m256i *s) { s[0] = v; }
#endif

/* Independent accumulator chains per output vector. maddubs+madd+add carries
 * only the 1-cycle add from one block to the next, so one chain keeps up.
 * vpdpbusd carries its whole ~5-cycle latency into the next block, so a
 * single chain would leave it latency-bound at a fraction of its throughput.
 * Measured with tt_bench_eval on a Sapphire Rapids Xeon, ns per eval:
 *     zmm   groups 2 / 4 / 8     91.6 / 69.4 / 71.2
 *     ymm (AVX-VNNI only, built -march=alderlake)
 *           groups 1 / 2 / 4    132.4 / 91.3 / 106.8   (4 spills: 16 regs)
 *     AVX2  groups 1 / 2        142.6 / 142.2 */
#if defined(NN_ZMM)
#define NN_GROUPS 4
#elif defined(NN_VNNI)
#define NN_GROUPS 2
#else
#define NN_GROUPS 1
#endif
_Static_assert((NN_L1 / 4) % NN_GROUPS == 0 && (NN_L2 / 4) % NN_GROUPS == 0
               && NN_L2 == 32 && NN_L3 == 32,
               "nn_sums_run hands out whole groups of 32-output blocks");

/* One 32-output layer's running sums. */
typedef struct { NnVec v[NN_GROUPS][NN_VPR]; } NnSums;

static inline void nn_sums_init(NnSums *a, const int32_t *bias)
{
    int g, r;
    for (r = 0; r < NN_VPR; r++) {
        a->v[0][r] = nn_vloadu(bias + r * (32 / NN_VPR));
        for (g = 1; g < NN_GROUPS; g++) a->v[g][r] = nn_vzero();
    }
}

/* Four input bytes `four` against column block `wc`, into chain `g`. */
static inline void nn_sums_block(NnSums *a, int g, int32_t four,
                                 const int8_t *wc)
{
    NnVec xb = nn_vbcast(four);
    int r;
    for (r = 0; r < NN_VPR; r++)
        a->v[g][r] = nn_vdp(a->v[g][r], xb, wc + r * (128 / NN_VPR));
}

/* `n` unsigned input bytes against the column-blocked weights `w`.
 *
 * No horizontal reduction anywhere: a block of four inputs is broadcast to
 * every lane and each lane is one output, so the 32 sums come out already in
 * vector form. The row-at-a-time loop this replaced paid a full 8-to-1
 * reduction and a scalar crelu per output, 32 times per layer. */
static inline void nn_sums_run(NnSums *a, const int8_t *x, int n,
                               const int8_t *w)
{
    int c, g;
    for (c = 0; c < n / 4; c += NN_GROUPS)
        for (g = 0; g < NN_GROUPS; g++) {
            int32_t four;
            memcpy(&four, x + 4 * (c + g), 4);
            nn_sums_block(a, g, four, w + (size_t)(c + g) * 128);
        }
}

/* The chains summed, as four ymm of eight int32: outputs 0..31 in order. */
static inline void nn_sums_out(NnSums *a, __m256i *s)
{
    int g, r;
    for (r = 0; r < NN_VPR; r++) {
        for (g = 1; g < NN_GROUPS; g++)
            a->v[0][r] = nn_vadd(a->v[0][r], a->v[g][r]);
        nn_vsplit(a->v[0][r], s + r * (4 / NN_VPR));
    }
}

/* Four vectors of eight int32, outputs 0..31 in order, to crelu(v >> shift)
 * as 32 int8. The two saturating packs then max(0) are exactly
 * clamp(v, 0, 127): saturation is monotone and both of its bounds lie outside
 * [0, 127]. The packs interleave the 128-bit lanes, which the final permute
 * undoes. */
static inline void nn_crelu32(__m256i a, __m256i b, __m256i c, __m256i d,
                              int shift, int8_t *out)
{
    __m256i p = _mm256_packs_epi16(
        _mm256_packs_epi32(_mm256_srai_epi32(a, shift),
                           _mm256_srai_epi32(b, shift)),
        _mm256_packs_epi32(_mm256_srai_epi32(c, shift),
                           _mm256_srai_epi32(d, shift)));
    p = _mm256_max_epi8(p, _mm256_setzero_si256());
    p = _mm256_permutevar8x32_epi32(p, _mm256_setr_epi32(0, 4, 1, 5, 2, 6, 3, 7));
    _mm256_storeu_si256((__m256i *)out, p);
}

/* The accumulator to L2's input bytes. */
static inline void nn_l1(const int32_t *acc, int8_t *x)
{
    int j;
#if defined(NN_ZMM)
    /* nn_crelu32 64 at a time: the packs interleave four 128-bit lanes now,
     * so the permute gathers dword m of every lane into quarter m. */
    const __m512i order = _mm512_setr_epi32(0, 4, 8, 12, 1, 5, 9, 13,
                                            2, 6, 10, 14, 3, 7, 11, 15);
    for (j = 0; j < NN_L1; j += 64) {
        __m512i p = _mm512_packs_epi16(
            _mm512_packs_epi32(
                _mm512_srai_epi32(_mm512_loadu_si512(acc + j), NN_SHIFT1),
                _mm512_srai_epi32(_mm512_loadu_si512(acc + j + 16), NN_SHIFT1)),
            _mm512_packs_epi32(
                _mm512_srai_epi32(_mm512_loadu_si512(acc + j + 32), NN_SHIFT1),
                _mm512_srai_epi32(_mm512_loadu_si512(acc + j + 48), NN_SHIFT1)));
        p = _mm512_max_epi8(p, _mm512_setzero_si512());
        _mm512_storeu_si512(x + j, _mm512_permutexvar_epi32(order, p));
    }
#else
    for (j = 0; j < NN_L1; j += 32)
        nn_crelu32(_mm256_loadu_si256((const __m256i *)(acc + j)),
                   _mm256_loadu_si256((const __m256i *)(acc + j + 8)),
                   _mm256_loadu_si256((const __m256i *)(acc + j + 16)),
                   _mm256_loadu_si256((const __m256i *)(acc + j + 24)),
                   NN_SHIFT1, x + j);
#endif
}

/* L2. `x` is NN_L1 crelu bytes, then the extras as int8, then one zero.
 *
 * The extras run negative, so they cannot be the unsigned operand of maddubs
 * or vpdpbusd as they stand. With VNNI they go in offset by +128 -- the XOR
 * flips each byte's sign bit, which is exactly that -- as two more column
 * blocks, and the +128 * (row sum of the extras' weights) that adds is taken
 * back out of the bias at load time (nn_b2vnni). Integer, so exact. maddubs
 * cannot take the offset bytes, 255 * 127 * 2 saturates int16, so AVX2 runs
 * them through madd_epi16 instead, as int16 pairs against int16 weights:
 * exact too, |product pair| <= 2 * 127 * 128. Either way this replaces a
 * scalar tail per row; see docs/AB.md for why CACHING that tail was rejected
 * -- this does not cache, it stops doing it seven multiplies at a time.
 *
 * `extras` 0 drops the tail for tt_bench_nnue_stage. */
static inline void nn_l2(const int8_t *x, int8_t *h1, int extras)
{
    NnSums a;
    __m256i s[4];
#if defined(NN_VNNI)
    nn_sums_init(&a, extras ? nn_b2vnni : nn_b2);
    nn_sums_run(&a, x, NN_L1, nn_w2col);
    if (extras) {
        uint64_t e;
        memcpy(&e, x + NN_L1, 8);
        e ^= 0x8080808080808080ULL;
        nn_sums_block(&a, 0, (int32_t)(uint32_t)e,
                      nn_w2col + (size_t)NN_L1 / 4 * 128);
        nn_sums_block(&a, 1 % NN_GROUPS, (int32_t)(uint32_t)(e >> 32),
                      nn_w2col + ((size_t)NN_L1 / 4 + 1) * 128);
    }
    nn_sums_out(&a, s);
#else
    nn_sums_init(&a, nn_b2);
    nn_sums_run(&a, x, NN_L1, nn_w2col);
    nn_sums_out(&a, s);
    if (extras) {
        __m256i xe = _mm256_broadcastsi128_si256(_mm_cvtepi8_epi16(
            _mm_loadl_epi64((const __m128i *)(x + NN_L1))));
        __m256i xp[4];
        int p, r;
        xp[0] = _mm256_shuffle_epi32(xe, 0x00);
        xp[1] = _mm256_shuffle_epi32(xe, 0x55);
        xp[2] = _mm256_shuffle_epi32(xe, 0xAA);
        xp[3] = _mm256_shuffle_epi32(xe, 0xFF);
        for (r = 0; r < 4; r++)
            for (p = 0; p < NN_EXPAIRS; p++)
                s[r] = _mm256_add_epi32(s[r], _mm256_madd_epi16(xp[p],
                    _mm256_load_si256((const __m256i *)
                        (nn_w2excol + p * NN_L2 * 2 + r * 16))));
    }
#endif
    nn_crelu32(s[0], s[1], s[2], s[3], NN_SHIFT2, h1);
}

static inline void nn_l3(const int8_t *h1, int8_t *h2)
{
    NnSums a;
    __m256i s[4];
    nn_sums_init(&a, nn_b3);
    nn_sums_run(&a, h1, NN_L2, nn_w3col);
    nn_sums_out(&a, s);
    nn_crelu32(s[0], s[1], s[2], s[3], NN_SHIFT2, h2);
}
#else
static inline int32_t nn_dot(const int8_t *a, const int8_t *b, int n)
{
    int32_t z = 0;
    int i;
    for (i = 0; i < n; i++) z += a[i] * b[i];
    return z;
}
#endif

#ifndef NN_COLUMNS
/* The row-at-a-time layers, for NEON and the scalar reference. */
static inline int32_t nn_crelu(int32_t x, int shift)
{
    x >>= shift;
    if (x < 0) return 0;
    return x > NN_CRELU ? NN_CRELU : x;
}

static inline void nn_l1(const int32_t *acc, int8_t *x)
{
    int j;
    for (j = 0; j < NN_L1; j++) x[j] = (int8_t)nn_crelu(acc[j], NN_SHIFT1);
}

static inline void nn_l2(const int8_t *x, int8_t *h1, int extras)
{
    int j, k;
    for (k = 0; k < NN_L2; k++) {
        const int8_t *erow = nn_w2extra + (size_t)k * NN_EXTRA;
        int32_t z = nn_b2[k] + nn_dot(x, nn_w2main + (size_t)k * NN_L1, NN_L1);
        /* The extras run negative, so they stay off the vector path. Seven
         * values against 256 is not worth a second code path -- and CACHING
         * this tail was built and measured, and REJECTED: -1.7 to -4.4% in
         * FFA. The tail already hides under the 256-wide dot, so a cache only
         * adds a tag check and a dependent load. See docs/AB.md. */
        if (extras)
            for (j = 0; j < NN_EXTRA; j++) z += x[NN_L1 + j] * erow[j];
        h1[k] = (int8_t)nn_crelu(z, NN_SHIFT2);
    }
}

static inline void nn_l3(const int8_t *h1, int8_t *h2)
{
    int k;
    for (k = 0; k < NN_L3; k++) {
        const int8_t *row = nn_w3 + (size_t)k * NN_L2;
        h2[k] = (int8_t)nn_crelu(nn_b3[k] + nn_dot(h1, row, NN_L2), NN_SHIFT2);
    }
}
#endif

/* The net has always been perspective-relative -- the accumulator is kept for
 * all four seats and the features rotate -- so evaluating from a seat other
 * than the mover costs nothing but the argument. The paranoid FFA search needs
 * exactly that: every node scored in the root seat's terms. */
static int32_t nnue_eval_for(const TtBoard *b, int persp)
{
    const int32_t *acc;
    /* x carries one zero past the extras so they pad to int16 pairs. */
    int8_t x[NN_L1 + 2 * NN_EXPAIRS] __attribute__((aligned(32)));
    int8_t h1[NN_L2] __attribute__((aligned(32)));
    int8_t h2[NN_L3] __attribute__((aligned(32)));
    int32_t ex[NN_EXTRA];
    int32_t out;
    int j;

    if (!nn_acc_ok || nn_acc_key != b->key) nn_refresh(b);
    nn_flush(persp);
    acc = nn_acc[persp];

    /* crelu bounds these to [0,127] and the extras to [-127,127], so the whole
     * propagation is int8 by construction. */
    nn_l1(acc, x);
    nn_extras(b, persp, ex);
    for (j = 0; j < NN_EXTRA; j++) x[NN_L1 + j] = (int8_t)ex[j];
    for (j = NN_L1 + NN_EXTRA; j < (int)sizeof(x); j++) x[j] = 0;

    nn_l2(x, h1, 1);
    nn_l3(h1, h2);
    out = nn_b4 + nn_dot(h2, nn_w4, NN_L3);
    return out >> NN_SHIFT_OUT;
}

static int32_t nnue_eval(const TtBoard *b)
{
    return nnue_eval_for(b, b->turn);
}

/* The cheap half: material only. */
static int32_t hand_material_for(const TtBoard *b, int persp)
{
    int32_t total = 0;
    int sq;

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
        total += (same_team(b->mode, pc, persp) ? 1 : -1)
                 * P.piece_value[P.pc_type[p]];
    }
    return total;
}

/* The expensive half: 32 tt_is_attacked calls, which measured as 54% of all
 * search time. Bounded by 4 seats x 8 squares x king_danger, which is what
 * makes a lazy bail sound. */
static int32_t hand_danger_for(const TtBoard *b, int persp)
{
    int32_t total = 0;
    int c, i;

    for (c = 0; c < 4; c++) {
        int k, danger = 0;
        if (!b->alive[c]) continue;
        k = b->kings[c];
        if (k < 0) continue;
        for (i = 0; i < 8; i++) {
            int t = (k + P.queen_dirs[i]) & 255;
            if (P.valid[t] && tt_is_attacked(b, t, c)) danger++;
        }
        total += (same_team(b->mode, c, persp) ? -1 : 1)
                 * danger * P.king_danger;
    }
    return total;
}

/* --- mobility -------------------------------------------------------------
 *
 * Squares a piece could move to, ignoring pins and check. The reference
 * implementation's own
 * evaluation carries mobility as its largest positional term, larger than king
 * safety, and this had none.
 *
 * Capped per piece. The cap is not an optimisation: it is what gives the term
 * a provable maximum, and lazy evaluation stays sound only while the margin it
 * bails on bounds everything the cheap half leaves out.
 *
 * Pawns and kings are excluded -- a pawn's mobility is nearly constant and
 * there are eight per seat paying for it. Mirrors eval_hand.py exactly;
 * selftest compares the two bit for bit in both toggle states.
 */
static int32_t piece_mobility(const TtBoard *b, int sq, uint8_t piece)
{
    const int32_t *dirs;
    int ndirs, sliding, i, count = 0;
    int mine = P.pc_color[piece];

    switch (P.pc_type[piece]) {
    case KNIGHT: dirs = P.knight_deltas; ndirs = 8; sliding = 0; break;
    case BISHOP: dirs = P.diag;          ndirs = 4; sliding = 1; break;
    case ROOK:   dirs = P.ortho;         ndirs = 4; sliding = 1; break;
    case QUEEN:
    case PQUEEN: dirs = P.queen_dirs;    ndirs = 8; sliding = 1; break;
    default:     return 0;
    }

    for (i = 0; i < ndirs; i++) {
        int t = sq;
        for (;;) {
            uint8_t other;
            t = (t + dirs[i]) & 255;
            if (!P.valid[t]) break;
            other = b->sq[t];
            if (other) {
                /* is_enemy, not "a different colour": in Teams a partner's
                 * piece has a different pc_color and is NOT capturable, so
                 * the plain colour test counted moving onto a teammate and
                 * over-counted by one square per partner-blocked direction.
                 * This is the rule the generator uses, which is what the
                 * docstring already claimed. */
                if (is_enemy(b, other, mine)) count++;
                break;
            }
            count++;
            if (!sliding || count >= P.mobility_cap) break;
        }
        if (count >= P.mobility_cap) return P.mobility_cap;
    }
    return count;
}

static int32_t hand_mobility_for(const TtBoard *b, int persp)
{
    int32_t total = 0;
    int sq;

    for (sq = 0; sq < NSQ; sq++) {
        uint8_t p;
        int colour;
        int32_t m;
        if (!P.valid[sq]) continue;
        p = b->sq[sq];
        if (!p) continue;
        colour = P.pc_color[p];
        if (colour == DEAD_UNKNOWN || !b->alive[colour]) continue;
        m = piece_mobility(b, sq, p) * P.mobility;
        total += same_team(b->mode, colour, persp) ? m : -m;
    }
    return total;
}

/* Default OFF until it has won an A/B, like every other feature here. */
static int use_mobility = 0;

void tt_set_mobility(int on) { use_mobility = on ? 1 : 0; }
int tt_get_mobility(void) { return use_mobility; }

/* The perspective is a parameter because a paranoid FFA search scores every
 * node in the ROOT player's terms, not the mover's. In Teams the two are the
 * same thing -- the team to move -- so these wrappers keep every existing
 * caller and every pinned number exactly as they were. */
static int32_t hand_material(const TtBoard *b)
{
    return hand_material_for(b, b->turn);
}

static int32_t hand_danger(const TtBoard *b)
{
    return hand_danger_for(b, b->turn);
}

static int32_t hand_mobility(const TtBoard *b)
{
    return hand_mobility_for(b, b->turn);
}

/* The whole hand evaluation from one seat's point of view. Used by the
 * paranoid FFA search, which scores every node in the ROOT player's terms; in
 * Teams hand_eval already means this with persp = b->turn. */
static int32_t hand_eval_for(const TtBoard *b, int persp)
{
    return hand_material_for(b, persp) + hand_danger_for(b, persp)
           + (use_mobility ? hand_mobility_for(b, persp) : 0);
}

static int32_t hand_eval(const TtBoard *b)
{
    int32_t total = hand_material(b) + hand_danger(b);
    if (use_mobility) total += hand_mobility(b);
    return total;
}

/* --- lazy evaluation ------------------------------------------------------
 *
 * Compute material first and skip the king-danger term when the cheap half
 * already settles the bound by more than the expensive half could possibly
 * move it. That term was 54% of all search time.
 *
 * NOT exact. Every cutoff DECISION is identical -- the margin guarantees that
 * -- but the value returned on a bail is the material term rather than the
 * true eval, and fail-soft propagates it. It measured node-identical over 80
 * positions all the same, which selftest watches rather than assumes.
 *
 * CONFIRMED and default ON. Teams / classic, movetime 200:
 *   +42.88 +/- 6.50 Elo over 10,000 games
 *   Dist: 61, 13, 428, 44, 953, 46, 719, 17, 219
 * Screened on FIXED TIME, not fixed nodes: it changes speed and not the tree,
 * so a fixed-nodes campaign would have reported exactly zero. See docs/AB.md.
 */
static int use_lazy_eval = 1;

void tt_set_lazy_eval(int on) { use_lazy_eval = on ? 1 : 0; }
int tt_get_lazy_eval(void) { return use_lazy_eval; }

/* Everything hand_eval adds on top of material, bounded.
 *
 * King danger is at most 4 seats x 8 squares. Mobility is at most the capped
 * count for every piece a seat can have, and only two seats sit on each side,
 * so the widest the term can swing is 2 x 16 x cap x weight.
 *
 * Turning mobility on therefore widens the margin a long way, and a wider
 * margin bails less often -- lazy evaluation is worth +42.88, so mobility has
 * to pay for weakening it as well as for its own cost. That is a real trade
 * and it is what the fixed-time A/B measures. */
static int32_t lazy_margin(void)
{
    int32_t margin = 4 * 8 * P.king_danger;
    if (use_mobility) margin += 2 * 16 * P.mobility_cap * P.mobility;
    return margin;
}

/* How many evaluations the last search paid for. Counted in the three
 * functions that actually evaluate, not at their call sites: it lived only in
 * tt_eval_bounded, which the FFA leaf never reaches, so every FFA search
 * reported exactly 0 while evaluating at nearly every node -- 32% of the
 * frozen bench sample divided by a meaningless zero. Nothing here calls
 * anything else here, so there is no double count. */
static uint64_t search_evals;

/* One binary, two evals: a net is loaded or it is not. That is what makes the
 * NNUE-vs-hand A/B a single setoption apart rather than two builds. */
int32_t tt_eval(const TtBoard *b)
{
    search_evals++;
    return nn_loaded ? nnue_eval(b) : hand_eval(b);
}

/* tt_eval from an arbitrary seat's point of view. Same dispatch: one binary,
 * two evals. Without this the FFA search would silently run the hand eval on
 * a machine with a net loaded, and no test would have said so. */
static int32_t eval_for(const TtBoard *b, int persp)
{
    search_evals++;
    return nn_loaded ? nnue_eval_for(b, persp) : hand_eval_for(b, persp);
}

/* Same value as tt_eval unless the lazy toggle is on and the bound is already
 * settled. NNUE has no cheap/expensive split, so it is unaffected. */
static inline int32_t tt_eval_bounded(const TtBoard *b, int32_t alpha,
                                      int32_t beta)
{
    int32_t material, margin;
    search_evals++;
    if (nn_loaded) return nnue_eval(b);
    if (!use_lazy_eval) return hand_eval(b);
    material = hand_material(b);
    margin = lazy_margin();
    if (material - margin >= beta) return material;
    if (material + margin <= alpha) return material;
    /* Mobility belongs here, not only in hand_eval. With LazyEval on -- the
     * default since v3 -- every stand-pat the search sees comes through this
     * function, and hand_eval is reachable only from the MAX_DEPTH cap. So
     * turning Mobility on used to change exactly one thing: lazy_margin widened
     * from 384 to 1,344, bailing less often, while the term itself never ran.
     * The A/B that rejected mobility measured that widening. */
    return material + hand_danger(b)
           + (use_mobility ? hand_mobility(b) : 0);
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

/* --- late move reductions -------------------------------------------------
 *
 * Quiet moves tried late are unlikely to be best, so search them shallower
 * with a null window and only pay full depth if one raises alpha. NOT exact:
 * it can miss a line the full search would have found, which is why it needed
 * the games rather than a node count.
 *
 * CONFIRMED and default ON. Teams / classic, fixed nodes 20,000:
 *   +35.07 +/- 6.51 Elo over 10,000 games
 *   Dist: 88, 12, 415, 39, 1002, 45, 681, 16, 202
 * against a null self-test of -2.64 +/- 6.24. See docs/AB.md.
 *
 * It works where history and PVS did not because it attacks the branching
 * factor directly rather than assuming the ordering is already good.
 */
#define LMR_MAX_MOVE 64
static int use_lmr = 1;
static int lmr_min_depth = 3;
static int lmr_min_move = 3;
static int lmr_table[MAX_DEPTH][LMR_MAX_MOVE];
static int lmr_built = 0;

static void lmr_build(void)
{
    int d, m;
    for (d = 0; d < MAX_DEPTH; d++)
        for (m = 0; m < LMR_MAX_MOVE; m++) {
            double r = (d < 1 || m < 1) ? 0.0
                     : 0.75 + log((double)d) * log((double)m) / 2.25;
            lmr_table[d][m] = (int)r;
        }
    lmr_built = 1;
}

void tt_set_lmr(int on)
{
    if (!lmr_built) lmr_build();
    use_lmr = on ? 1 : 0;
}
int tt_get_lmr(void) { return use_lmr; }

void tt_set_lmr_params(int min_depth, int min_move)
{
    lmr_min_depth = min_depth < 1 ? 1 : min_depth;
    lmr_min_move = min_move < 1 ? 1 : min_move;
}

/* --- late move pruning ----------------------------------------------------
 *
 * LMR searches the quiet tail shallower; this drops it entirely at shallow
 * depth once enough quiet moves have been tried without a cutoff. At a
 * branching factor near 60 the tail is most of the tree.
 *
 * CONFIRMED and default ON. Teams / classic, fixed nodes 20,000:
 *   +36.09 +/- 6.69 Elo over 10,000 games
 *   Dist: 96, 6, 437, 36, 968, 33, 683, 16, 225
 * Mean depth at the instrument went 4.25 -> 5.25, a full extra ply.
 *
 * The guard that matters: never prune before at least one legal move has been
 * found. Pruning every move at a node would leave `legal` at zero and the node
 * would report checkmate -- inventing a mate that is not there, which is far
 * worse than searching too much.
 */
static int use_lmp = 1;
static int lmp_max_depth = 3;
static int lmp_base = 4;

void tt_set_lmp(int on) { use_lmp = on ? 1 : 0; }
int tt_get_lmp(void) { return use_lmp; }

void tt_set_lmp_params(int max_depth, int base)
{
    lmp_max_depth = max_depth < 0 ? 0 : max_depth;
    lmp_base = base < 1 ? 1 : base;
}

/* --- null-move pruning (toggle: off by default, pending its A/B) ----------
 *
 * Pass, and search the result shallower with a null window at beta. If our
 * team is still above beta after handing the opponents a free move, the real
 * moves will be too, and the node fails high without generating them. NOT
 * exact: it is wrong exactly where passing would be better than any move.
 *
 * Why it is not obviously unsound in four seats. The "free move" goes to ONE
 * opponent: after Red passes, Blue moves, then Yellow -- Red's partner, who
 * can still parry -- then Green. The opponents gain one tempo out of four, not
 * the game's only tempo as in chess, so the threat test is weaker, and so is
 * the risk. Zugzwang needs every move of a seat to be bad AND its partner to
 * be unable to help, which is rarer than in chess; the non-pawn guard covers
 * the endgames where it can happen.
 *
 * Teams only. In the paranoid FFA search a pass at a MIN node hands the turn
 * from one opponent to the next and tests nothing about the root.
 *
 * Cheap measurement, 300 Teams positions (book + 0-49 self-play plies), net
 * bundle, no games: mean depth at 20,000 nodes 4.72 -> 4.95, nodes for an
 * ID to depth 6 69.7% of base, and the 20,000-node move -3 +/- 14 cp (one
 * s.e.) against a 400,000-node oracle -- inside noise. R=3 (5.01, +6 cp) and
 * min depth 4 (4.87, +3 cp) are no better. The weaker of the two Teams
 * toggles: a third of RFP's depth gain, and little on top of it (5.45 ->
 * 5.63 together). Expected to be small; that is why it is separate.
 */
static int use_null_move = 0;
static int null_min_depth = 3;
static int null_r = 2;
/* Set at ply p when the move into ply p was a pass. A second pass in a row
 * would let both teams pass, which searches the same position two seats on
 * and proves nothing. */
static uint8_t null_into[MAX_DEPTH + 1];

void tt_set_null_move(int on) { use_null_move = on ? 1 : 0; }
int tt_get_null_move(void) { return use_null_move; }
void tt_set_null_params(int min_depth, int r)
{
    null_min_depth = min_depth < 2 ? 2 : min_depth;
    null_r = r < 1 ? 1 : r;
}

/* --- reverse futility pruning (toggle: off by default, pending its A/B) ----
 *
 * Also called static null move: at shallow depth, a static evaluation that
 * already beats beta by a margin per remaining ply returns without searching.
 * NOT exact. The margin has to cover what three other seats can do before
 * this one moves again, which is why it is wider than a chess engine's.
 *
 * Cheap measurement, same 300 positions as NullMove: mean depth at 20,000
 * nodes 4.72 -> 5.45 (margin 150, depth <= 3), ID-to-depth-6 nodes 47.1% of
 * base, oracle regret -11 +/- 21 cp (one s.e.). Margin 100 reaches 5.68
 * (-25 +/- 23), margin 250 only 5.18 (+1 +/- 18), depth <= 2 5.41. 150 is
 * the default because 100 is the kind of number a 2-player engine picks and
 * three seats move between here and the horizon; the A/B should try 100 as a
 * second arm. Pre-screen at fixed nodes 20,000, 400 games: +110.41 +/- 38.06
 * (docs/AB.md) -- a direction, not a confirm.
 * The eval it reads is NOT free at fixed time (one more evaluation per
 * shallow interior node), so fixed nodes will flatter it: screen both.
 *
 * CONFIRMED and default ON. Teams / classic, net-v5 both sides:
 *   fixed nodes 20,000   +133.95 +/- 7.00   10,000 games
 *   movetime 200         +155.54 +/- 11.10   4,000 games
 * The worry above did not hold: the clock gained MORE than the node count,
 * so the extra evaluation is repaid by the subtrees it never enters. Margin
 * 100 as a second arm is still untried. See docs/AB.md. */
static int use_rfp = 1;
static int rfp_max_depth = 3;
static int rfp_margin = 150;

void tt_set_rfp(int on) { use_rfp = on ? 1 : 0; }
int tt_get_rfp(void) { return use_rfp; }
void tt_set_rfp_params(int max_depth, int margin)
{
    rfp_max_depth = max_depth < 1 ? 1 : max_depth;
    rfp_margin = margin < 1 ? 1 : margin;
}

/* --- FFA: killers and late-move pruning/reduction in the paranoid search ---
 *
 * The paranoid loop had none of the Teams search's shallow pruning: no
 * killers (it passes ply -1 to score_moves), no LMR, no LMP. A ply there costs
 * a median 17x the one before it against Teams' 3.5x, which is exactly the
 * regime where LMR and LMP bought Teams a whole ply. Each is its own toggle
 * and its own A/B; the default each carries is the one its A/B gave it.
 *
 * Killers are an ordering change only: alpha-beta returns the same value in
 * any order, so FFAKillers moves the node count and not the score (up to the
 * table's path dependence). Killer slots are per ply, and in a paranoid tree
 * the ply fixes the seat to move while every seat is alive.
 *
 * LMR and LMP are NOT exact, and in a paranoid tree they are not symmetric
 * either: pruning at the root seat's node is pessimistic, pruning at an
 * opponent's is optimistic -- it assumes that opponent will not find a late
 * quiet move. Paranoid already assumes three opponents cooperating, so a
 * little optimism at their nodes may move the search toward the real game
 * rather than away from it; that is a hypothesis for the A/B, not a claim.
 *
 * Cheap measurement, 200 FFA positions (book + 0-79 self-play plies),
 * net-ffa1, no games. Base reaches a mean depth of 3.11 at 20,000 nodes --
 * less than one round, so the root seat never sees its own next move.
 *
 *                  depth @20k   ID nodes to depth 4   agrees w/ 400k base
 *   base             3.11         11,883,965  100%       45.0%
 *   FFAKillers       3.12         10,970,400   92%       44.5%
 *   FFALMR           3.76          1,949,528   16%       44.0%
 *   FFALMP           4.16            633,815    5%       49.5%
 *   LMR + LMP        4.97            237,384    2%       46.5%
 *   all three        5.06            222,059    2%       50.0%
 *
 * Agreement with a deep search is a weak instrument here: against a 400k
 * oracle that runs all three prunings instead, every row lands in 32.5-35.0%
 * and the order does not hold. The depth column is the reading to trust.
 *
 * FFALMP: CONFIRMED and default ON. FFA book, net-ffa1 both sides:
 *   fixed nodes 20,000   +121.23 +/- 5.36   10,000 games
 *   movetime 100         +104.33 +/- 7.58    4,000 games
 * The clock kept most of the node-count gain, which is what a ply the base
 * never reached should do. See docs/AB.md.
 *
 * FFALMR: CONFIRMED and default ON, measured on top of FFALMP:
 *   fixed nodes 20,000   +46.67 +/- 4.99   10,000 games
 *   movetime 100         +14.28 +/- 7.83    4,000 games
 * The clock kept under a third: with LMP on, the reductions buy less.
 *
 * FFAKillers: NULL at movetime 100 (+3.08 +/- 4.91 over 10,000 games on top
 * of both, +9.16 +/- 4.98 at fixed nodes), so it stays off. */
static int use_ffa_killers = 0;
static int use_ffa_lmr = 1;
static int use_ffa_lmp = 1;

void tt_set_ffa_killers(int on) { use_ffa_killers = on ? 1 : 0; }
int tt_get_ffa_killers(void) { return use_ffa_killers; }
void tt_set_ffa_lmr(int on) { use_ffa_lmr = on ? 1 : 0; }
int tt_get_ffa_lmr(void) { return use_ffa_lmr; }
void tt_set_ffa_lmp(int on) { use_ffa_lmp = on ? 1 : 0; }
int tt_get_ffa_lmp(void) { return use_ffa_lmp; }

static uint32_t search_buf[MAX_DEPTH][MAX_MOVES];
static int32_t order_buf[MAX_DEPTH][MAX_MOVES];
static uint64_t search_nodes;
static uint64_t search_limit;
static int search_aborted;

/* Repetition (§10.2). Positions already played arrive from Python before the
 * search; the search path is appended as it descends, so a single backward
 * scan covers both at once. Shared by both searches -- what a repeat is WORTH
 * differs between them and lives at the two call sites, but what counts as one
 * does not.
 *
 * The scan stops at the last irreversible move, which `halfmove` counts: no
 * position before a capture or a pawn advance can equal one after it. Since
 * the key includes the side to move and the alive mask, stopping short can
 * only ever miss a repetition, never invent one.
 *
 * A repeat scores 0 on the first hit rather than the third. Threefold is what
 * the rule pays out on, but a side that can repeat once can almost always
 * repeat again, and scoring the first one keeps the search from convincing
 * itself a shuffle is progress. Two extra plies of shuffling would have to be
 * searched to learn the same thing. */
#define REP_MAX (MAX_DEPTH + 1024)
static uint64_t rep_keys[REP_MAX];
static int rep_root;                  /* played positions, before the root */
/* On by default: without it the engine cannot score a draw it is walking
 * into, which is a rules gap rather than a tuning choice. The toggle exists so
 * the claim can be measured like any other, not because off is a real
 * configuration. */
static int use_repetitions = 1;

void tt_set_rep_history(const uint64_t *keys, int n)
{
    int i, cap = REP_MAX - MAX_DEPTH - 1;
    if (n < 0) n = 0;
    if (n > cap) { keys += n - cap; n = cap; }   /* keep the most recent */
    for (i = 0; i < n; i++) rep_keys[i] = keys[i];
    rep_root = n;
}

static int is_repetition(const TtBoard *b, int ply)
{
    int top = rep_root + ply;         /* written by the nodes above this one */
    int lo = top - b->halfmove;
    int step = 4, i;
    if (lo < 0) lo = 0;
    /* The key carries the side to move, so only a position an exact multiple
     * of the seat cycle back can match -- four plies while every seat is
     * alive, which in Teams is the whole game (§7). A dead seat shortens the
     * cycle, and rather than work out by how much, fall back to every ply:
     * this runs at every node and the wrong stride would silently stop
     * detecting anything. That fallback is what makes this correct for FFA
     * with a seat out, where the cycle is three; a stride of one can only
     * scan positions with a different seat to move, whose keys cannot match,
     * so it costs work and never invents a repetition. */
    if (!(b->alive[0] && b->alive[1] && b->alive[2] && b->alive[3])) step = 1;
    for (i = top - step; i >= lo; i -= step)
        if (rep_keys[i] == b->key) return 1;
    return 0;
}

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
/* Separate toggles because they were separate claims, and only one survived.
 *
 * SEEPrune is CONFIRMED and default on: +16.32 +/- 4.58 at fixed nodes and
 * +23.09 +/- 4.56 at fixed time, 20,000 games each. Winning by MORE on the
 * clock is the interesting part -- pruning a losing capture removes a subtree,
 * so the search is both better shaped and cheaper, and SEE's own per-capture
 * cost is more than repaid.
 *
 * SEEOrder stays off and unmeasured. It searched 0.7% MORE nodes in the first
 * look, which is enough to leave it alone: MVV-LVA leads with the most
 * valuable victim, a good cutoff bias even when the capture is unsound, and
 * sorting by the settled value throws that away. */
static int use_see_order = 0;
static int use_see_prune = 1;

void tt_set_see_order(int on) { use_see_order = on ? 1 : 0; }
int tt_get_see_order(void) { return use_see_order; }
void tt_set_see_prune(int on) { use_see_prune = on ? 1 : 0; }
int tt_get_see_prune(void) { return use_see_prune; }

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

uint64_t tt_search_makes(void) { return search_makes; }
uint64_t tt_search_evals(void) { return search_evals; }
uint64_t tt_search_gens(void) { return search_gens; }
void tt_set_rep_detect(int on) { use_repetitions = on ? 1 : 0; }
int tt_get_rep_detect(void) { return use_repetitions; }
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

/* MVV-LVA, with the transposition move first.
 *
 * Returns the number of captures and, through `loud` when it is non-null, the
 * number of moves that are a capture or a promotion -- the moves LMP may never
 * drop. Counted here because this loop already classifies every move; the
 * callers use the counts to stop their loops once nothing searchable is left. */
static int score_moves(const TtBoard *b, const uint32_t *restrict moves,
                       int32_t *restrict scores, int n, uint32_t ttmove,
                       int ply, int *loud)
{
    /* ply >= 0 is not redundant: quiescence passes -1 deliberately, so that
     * it consults no killer slot, and the upper bound alone lets -1 through
     * to killers[-1][0..1] -- eight bytes before the array, read for every
     * quiet move at every qsearch node. It is harmless only by accident of the
     * current link layout, and SEARCH_PINS depends on qsearch ordering, so the
     * pinned counts were resting on out-of-bounds memory staying zero.
     *
     * With the slots off, both killers read as 0, which no generated move
     * equals (a move never has from == to), so the compare below needs no
     * guard of its own. */
    const int kon = use_killers && ply >= 0 && ply < MAX_DEPTH;
    const uint32_t k0 = kon ? killers[ply][0] : 0;
    const uint32_t k1 = kon ? killers[ply][1] : 0;
    const int see_order = use_see_order, hist = use_history;
    int i, caps = 0, louds = 0;
    for (i = 0; i < n; i++) {
        uint32_t m = moves[i];
        uint8_t victim = b->sq[MV_TO(m)];
        int capture = victim != 0 || MV_FLAG(m) == F_EP;
        int32_t s;
        caps += capture;
        louds += capture || MV_PROMO(m);
        if (capture) {
            int vv = victim ? P.piece_value[P.pc_type[victim]]
                            : P.piece_value[PAWN];
            int av = P.piece_value[P.pc_type[b->sq[MV_FROM(m)]]];
            /* SEE keeps captures in the same band -- it spans about +/-900
             * against a 65,536 base, so a losing capture still sorts above
             * every killer and history move. Whether it should is a separate
             * question and a separate experiment. */
            s = (1 << 16) + (see_order ? tt_see(b, m) : vv * 16 - av);
        } else {
            s = m == k0 ? KILLER_BASE + 1 : (m == k1 ? KILLER_BASE : 0);
            if (!s && hist) s = history[b->turn][MV_FROM(m)][MV_TO(m)];
        }
        /* The TT move overrides whatever it scored above. */
        if (m == ttmove) s = 1 << 24;
        if (MV_PROMO(m)) s += (1 << 15);
        scores[i] = s;
    }
    if (loud) *loud = louds;
    return caps;
}

/* score_moves for quiescence outside check, where only captures are ever
 * searched: a capture gets exactly the score score_moves gives it and every
 * quiet gets 0. Returns the capture count, or -1 when the shortcut does not
 * apply and the caller must use score_moves instead.
 *
 * Why the order is identical. Quiescence passes no transposition move and
 * ply -1, so in score_moves a quiet scores at most (1 << 15) for a promotion
 * plus a history value below HISTORY_MAX -- QS_QUIET_MAX below. While every
 * capture scores above that, every capture outranks every quiet under both
 * scorings, so each selection pick until the last capture -- which is where
 * quiescence stops -- lands on the same capture: the first-index maximum among
 * the captures, whose scores and positions agree. Quiet values never decide a
 * pick and quiets are never searched, so the 0 changes nothing but the cost.
 * The bound holds by a wide margin for any sane piece values (the weakest
 * capture is 65,536 + 16 * 100 - 900), and it is checked rather than assumed,
 * so a tuning run with strange values falls back instead of reordering.
 *
 * Most of the moves scored anywhere are quiets at a quiescence node; for
 * those this is a load and a test instead of the TT-move, killer, history
 * and promotion checks. */
#define QS_QUIET_MAX ((1 << 15) + HISTORY_MAX - 1)

static int score_captures(const TtBoard *b, const uint32_t *restrict moves,
                          int32_t *restrict scores, int n)
{
    const int see_order = use_see_order;
    int32_t low = INT32_MAX;
    int i, caps = 0;
    for (i = 0; i < n; i++) {
        uint32_t m = moves[i];
        uint8_t victim = b->sq[MV_TO(m)];
        int32_t s = 0;
        if (victim || MV_FLAG(m) == F_EP) {
            int vv = victim ? P.piece_value[P.pc_type[victim]]
                            : P.piece_value[PAWN];
            int av = P.piece_value[P.pc_type[b->sq[MV_FROM(m)]]];
            s = (1 << 16) + (see_order ? tt_see(b, m) : vv * 16 - av);
            if (MV_PROMO(m)) s += (1 << 15);
            if (s < low) low = s;
            caps++;
        }
        scores[i] = s;
    }
    return low > QS_QUIET_MAX ? caps : -1;
}

/* Selection sort one move at a time: most of the list is never reached after
 * a cutoff, so sorting it all up front would be wasted work.
 *
 * Returns 1 when [i, n) is already non-increasing. The scan then picked i
 * itself and swapped nothing (a later move wins only on a strictly greater
 * score), [i+1, n) is still non-increasing, and nothing outside the scan
 * writes the scores -- so every later call at this node would pick its own
 * index and swap nothing too, and the caller stops calling. That is the
 * common case, not a corner: with history off every plain quiet scores 0, so
 * the tail after the last capture and killer is one flat run, and each pick
 * from it was an O(n - i) scan that changed nothing. Skipping those scans
 * cannot reorder anything. A scan that swaps reports 0 even if the swap left
 * the tail in order; the next scan finds out. Conservative is the safe
 * direction -- a missed shortcut costs a scan, never a different order.
 *
 * The FFA search found this first, as pick_move_tail (4c74f17): FFA quiets
 * all score 0, and the scans it skipped were 3.0% of an FFA search. It is
 * here now for every search loop, and paranoid uses this one.
 *
 * The scan itself is two passes. The first takes the maximum and the
 * non-increasing test with no branch on the data, so gcc vectorises it
 * (vpmaxsd under AVX2) where the old `if (scores[j] > scores[best]) best = j`
 * loop could not; the second walks to the first index holding that maximum.
 * The first index of the maximum is exactly what the strict `>` scan chose,
 * so the swap, and with it every later tie-break, is the same. */
static int pick_move(uint32_t *moves, int32_t *scores, int n, int i)
{
    int best, j, rises = 0;
    int32_t first = scores[i], top = first;
    for (j = i + 1; j < n; j++) {
        int32_t s = scores[j];
        top = s > top ? s : top;
        rises |= s > scores[j - 1];
    }
    if (!rises) return 1;
    if (top == first) return 0;
    for (best = i + 1; scores[best] != top; best++)
        ;
    {
        uint32_t tm = moves[i]; moves[i] = moves[best]; moves[best] = tm;
        scores[best] = first; scores[i] = top;
    }
    return 0;
}

/* --- quiescence check evasions --------------------------------------------
 *
 * CONFIRMED and default ON. Teams / classic, fixed nodes 20,000:
 *   +106.78 +/- 6.88 Elo over 10,000 games
 *   Dist: 28, 6, 224, 30, 832, 40, 895, 22, 423
 * The largest single gain in the engine, and it was predicted to be NEGATIVE:
 * it costs depth and wins nothing on node count. That reasoning came from
 * two-player chess. In 4PC a seat can be checked by three opponents rather
 * than one, so checks are far more frequent and a quiescence that mishandles
 * them is proportionally more broken. See docs/AB.md.
 *
 * Standing pat means "I could just do nothing here", which is exactly what a
 * side in check may not do. Without this, quiescence evaluates positions it
 * has no right to and can score a lost position as quiet.
 *
 * When in check it searches every legal move rather than captures only, and
 * reports mate if there are none -- without it quiescence cannot see a mate at
 * all. It costs nodes, and nodes are depth, so it was gated behind an A/B
 * rather than taken as a free correctness win. It won that A/B by more than
 * any other feature here.
 */
static int use_qs_evasions = 1;

void tt_set_qs_evasions(int on) { use_qs_evasions = on ? 1 : 0; }
int tt_get_qs_evasions(void) { return use_qs_evasions; }

static int32_t qsearch(TtBoard *b, int32_t alpha, int32_t beta, int ply)
{
    Pin pins[8];
    uint64_t evade[4];
    int king0, npins;
    uint32_t *moves;
    int32_t *scores;
    TtUndo u;
    int n, i, me = b->turn, in_chk = 0, legal = 0, caps, picked = 0;
    int sorted = 0;
    int32_t stand;

    if (++search_nodes >= search_limit) { search_aborted = 1; return 0; }
    if (ply >= MAX_DEPTH - 2) return tt_eval(b);

    if (use_qs_evasions) in_chk = tt_in_check(b, me);
    if (in_chk) {
        /* No stand-pat: the side to move cannot decline to answer a check. */
        stand = -INF_SCORE;
    } else {
        stand = tt_eval_bounded(b, alpha, beta);
        if (stand >= beta) return stand;
        if (stand > alpha) alpha = stand;
    }

    /* Captures are filtered out of the full pseudo-legal list rather than
     * produced by a second, captures-only generator. That generator would be
     * invisible to perft and to the bench signature, and would need its own
     * differential gate; not having it is cheaper than gating it. */
    moves = search_buf[ply];
    scores = order_buf[ply];
    n = tt_gen_pseudo(b, moves);
    caps = in_chk ? -1 : score_captures(b, moves, scores, n);
    if (caps < 0) caps = score_moves(b, moves, scores, n, 0, -1, 0);
    king0 = b->kings[me];
    /* Pins are found on first use, not here. They are a pure function of a
     * board every make/unmake pair below restores, so computing them later
     * gives the same list; and surely_legal never reads them in check, while
     * a node whose captures are all pruned or that has none never reads them
     * at all -- the common quiescence node. */
    npins = -1;
    if (in_chk) compute_evasions(b, me, king0, evade);

    /* Once the last capture has been picked, everything left is a quiet that
     * the loop would pick and immediately skip -- and pick_move is a selection
     * scan, so each of those costs O(n-i) comparisons to reach a `continue`.
     * Counting the captures up front lets the loop stop there. It cannot
     * change which moves are searched, in what order, or with what result: the
     * array, the scores and the selection are all untouched, and the tail
     * being dropped is exactly the moves that were already being discarded.
     * The scorer counts them, since it classifies every move anyway. */
    for (i = 0; i < n; i++) {
        int king, skip;
        if (!in_chk && picked == caps) break;
        if (!sorted) sorted = pick_move(moves, scores, n, i);
        /* In check every legal move is a candidate; otherwise captures only. */
        if (!in_chk && !is_capture(b, moves[i])) continue;
        picked++;
        /* A capture that loses material once the exchange settles is not a
         * line worth a subtree. Promotions are exempt: SEE prices the pieces
         * traded and not the piece gained, so it under-rates them. */
        if (use_see_prune && !in_chk && !MV_PROMO(moves[i])
            && !see_surely_nonneg(b, moves[i])
            && tt_see(b, moves[i]) < 0) continue;
        if (in_chk && surely_illegal(moves[i], king0, evade)) continue;
        if (npins < 0) npins = in_chk ? 0 : compute_pins(b, me, king0, pins);
        skip = surely_legal(moves[i], king0, pins, npins, in_chk);
        tt_make(b, moves[i], &u);
        king = b->kings[me];
        if (!skip && king >= 0 && tt_is_attacked(b, king, me)) {
            tt_unmake(b, moves[i], &u);
            continue;
        }
        legal++;
        int32_t score = -qsearch(b, -beta, -alpha, ply + 1);
        tt_unmake(b, moves[i], &u);
        if (search_aborted) return 0;
        if (score >= beta) return score;
        if (score > alpha) alpha = score;
    }
    /* Quiescence can now see a mate, which it previously could not: with no
     * legal answer to a check the position is lost, not quiet. */
    if (in_chk && legal == 0) return -(MATE_SCORE - ply);
    return alpha;
}

/* --- principal variation --------------------------------------------------
 *
 * Walked out of the transposition table rather than collected during the
 * search: every node already stores the move it chose, so following those from
 * the root costs nothing and needs no triangular array threaded through
 * alpha-beta.
 *
 * The price is that it can end early. An entry may have been overwritten, or
 * hold a move that is not legal in the position that now maps to that slot, so
 * the walk stops rather than printing a move nobody would play. A short PV is
 * honest; a wrong one is not.
 *
 * `first` is the root move the search returned. tt_search deliberately does
 * NOT store the root: writing an entry there would evict another and change
 * replacement pressure, and the node counts pinned in selftest -- and every
 * A/B behind them -- would move for the sake of a display feature. So the
 * caller hands the root move in and the table supplies the rest.
 */
#define MAX_PV 32

int tt_pv(TtBoard *b, uint32_t first, uint32_t *out, int max)
{
    uint32_t legal[MAX_MOVES];
    TtUndo undo[MAX_PV];
    uint64_t seen[MAX_PV];
    int n = 0, i, k;

    if (max <= 0) return 0;
    if (max > MAX_PV) max = MAX_PV;

    if (first) {
        k = tt_gen_legal(b, legal);
        for (i = 0; i < k; i++) if (legal[i] == first) break;
        if (i == k) return 0;              /* not a move in this position */
        out[0] = first;
        seen[0] = b->key;
        tt_make(b, first, &undo[0]);
        n = 1;
    }
    if (!tt_table) { if (n) tt_unmake(b, out[0], &undo[0]); return n ? 1 : 0; }

    while (n < max) {
        TtEntry *slot = &tt_table[b->key & tt_mask];
        uint32_t mv = slot->best;
        int repeat = 0;
        if (slot->key != b->key || !mv) break;
        for (i = 0; i < n; i++) if (seen[i] == b->key) { repeat = 1; break; }
        if (repeat) break;                 /* a cycle, not a variation */
        k = tt_gen_legal(b, legal);
        for (i = 0; i < k; i++) if (legal[i] == mv) break;
        if (i == k) break;                 /* stale slot: not legal here */
        seen[n] = b->key;
        out[n] = mv;
        tt_make(b, mv, &undo[n]);
        n++;
    }
    for (i = n - 1; i >= 0; i--) tt_unmake(b, out[i], &undo[i]);
    return n;
}

/* What a pass changes: the turn, the passing seat's own en-passant offer
 * (which expires when its owner moves, and a pass is that seat's move), and
 * the key. Nothing moves on the board, so the accumulator and the occupancy
 * bitboard stay correct and only their key tag follows -- the same carry
 * ffa_eliminate does, for the same reason.
 *
 * halfmove is zeroed so the repetition scan stops at the pass: a position on
 * the far side of a null move was reached by a line nobody can play, and a
 * "repetition" through it would score a draw that does not exist. */
typedef struct {
    uint64_t key;
    int32_t halfmove;
    int16_t ep_target;
    int16_t ep_victim;
    uint8_t turn;
} NullUndo;

static void null_make(TtBoard *b, NullUndo *u)
{
    int me = b->turn, c, t = me;
    uint64_t key = b->key ^ P.zob_turn[me];
    u->key = b->key;
    u->halfmove = b->halfmove;
    u->ep_target = b->ep_target[me];
    u->ep_victim = b->ep_victim[me];
    u->turn = (uint8_t)me;
    if (b->ep_target[me] >= 0) {
        key ^= P.zob_ep[me][b->ep_target[me]];
        b->ep_target[me] = -1;
        b->ep_victim[me] = -1;
    }
    for (c = 0; c < 4; c++) {
        t = (t + 1) & 3;
        if (b->alive[t]) break;
    }
    b->turn = (uint8_t)t;
    key ^= P.zob_turn[t];
    b->halfmove = 0;
    if (nn_delta_on_for(u->key)) nn_acc_set_key(key);
    if (occ_ok && occ_key == u->key) occ_key = key;
    b->key = key;
}

static void null_unmake(TtBoard *b, const NullUndo *u)
{
    if (nn_delta_on_for(b->key)) nn_acc_set_key(u->key);
    if (occ_ok && occ_key == b->key) occ_key = u->key;
    b->ep_target[u->turn] = u->ep_target;
    b->ep_victim[u->turn] = u->ep_victim;
    b->turn = u->turn;
    b->halfmove = u->halfmove;
    b->key = u->key;
}

/* Does `seat` own anything but pawns and its king? The zugzwang guard: a
 * seat down to king and pawns is where passing can genuinely beat moving. */
static int has_piece(const TtBoard *b, int seat)
{
    int sq;
    for (sq = 0; sq < NSQ; sq++) {
        uint8_t p = b->sq[sq];
        if (p && P.valid[sq] && P.pc_color[p] == seat
            && P.pc_type[p] != PAWN && P.pc_type[p] != KING)
            return 1;
    }
    return 0;
}

static int32_t alphabeta(TtBoard *b, int depth, int32_t alpha, int32_t beta,
                         int ply)
{
    uint32_t *moves, ttmove = 0, best_move = 0;
    int32_t *scores, best = -INF_SCORE, orig_alpha = alpha;
    TtUndo u;
    TtEntry *slot = 0;
    int n, i, legal = 0, quiets = 0, me = b->turn, in_chk;
    int louds = 0, louds_seen = 0, lmp_on = 0, sorted = 0;
    Pin pins[8];
    uint64_t evade[4];
    int king0, npins;

    if (++search_nodes >= search_limit) { search_aborted = 1; return 0; }

    /* Ahead of the transposition probe: a repetition is a property of the path
     * taken, not of the position, so a stored score from a different path must
     * not be allowed to answer first. For the same reason the draw is returned
     * without being stored. */
    if (use_repetitions && is_repetition(b, ply)) return 0;
    rep_keys[rep_root + ply] = b->key;

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

    /* The two pre-move prunings, both off by default. Each reads the static
     * evaluation, so it is computed only when one of them could use it; with
     * both off this block costs one branch and the tree is the pinned one.
     * Never in check, never against a mate bound.
     *
     * Internal iterative reduction (a ply off any depth >= 4 node with no
     * table move) was built here and dropped before an A/B: over 300 Teams
     * positions it cut nodes-to-depth-6 by only 5%, left mean depth at 20,000
     * nodes unmoved (4.72 -> 4.73), and its 20,000-node moves measured +17 cp
     * WORSE against a 400,000-node oracle (+/-12). With always-replace and a
     * per-search ID, a missing table move here usually means a genuinely new
     * node, and reducing those is reducing the tree blind. */
    if ((use_rfp || use_null_move) && ply > 0 && !in_chk
        && beta < MATE_SCORE - MAX_DEPTH && beta > -(MATE_SCORE - MAX_DEPTH)) {
        int rfp_on = use_rfp && depth <= rfp_max_depth;
        int null_on = use_null_move && depth >= null_min_depth
                      && !null_into[ply];
        if (rfp_on || null_on) {
            int32_t margin = rfp_on ? rfp_margin * depth : 0;
            int32_t ev = tt_eval_bounded(b, beta - margin - 1, beta);
            if (rfp_on && ev - margin >= beta) return ev;
            if (null_on && ev >= beta && has_piece(b, me)) {
                NullUndo nu;
                int32_t s;
                int r = null_r + (depth >= 7 ? 1 : 0);
                null_make(b, &nu);
                null_into[ply + 1] = 1;
                s = -alphabeta(b, depth - 1 - r, -beta, -beta + 1, ply + 1);
                null_into[ply + 1] = 0;
                null_unmake(b, &nu);
                if (search_aborted) return 0;
                if (s >= beta) {
                    /* A pass proves a bound, never a mate: the line it walked
                     * is not playable. */
                    if (s > MATE_SCORE - MAX_DEPTH) s = beta;
                    return s;
                }
            }
        }
    }

    moves = search_buf[ply];
    scores = order_buf[ply];
    n = tt_gen_pseudo(b, moves);
    score_moves(b, moves, scores, n, ttmove, ply, &louds);
    king0 = b->kings[me];
    npins = -1;                  /* on first use, as in qsearch */
    if (in_chk) compute_evasions(b, me, king0, evade);

    for (i = 0; i < n; i++) {
        int king, skip;
        int32_t score;
        int is_capture_before, reduction;
        /* Once LMP has dropped one move it drops every later quiet: `legal`
         * and `quiets` only grow and nothing else in the test changes inside
         * this loop. So when the last capture or promotion has also been
         * picked, every remaining iteration is a pick, a test and a
         * `continue`, and stopping here searches exactly the same moves. */
        if (lmp_on && louds_seen == louds) break;
        if (!sorted) sorted = pick_move(moves, scores, n, i);
        is_capture_before = is_capture(b, moves[i]);
        if (is_capture_before || MV_PROMO(moves[i])) louds_seen++;

        /* Drop the quiet tail without even making the move. `legal >= 1` is
         * not an optimisation: without it a node could prune every move and
         * then claim checkmate. */
        if (use_lmp && legal >= 1 && depth <= lmp_max_depth && !in_chk
            && !is_capture_before && !MV_PROMO(moves[i])
            && quiets >= lmp_base + depth * depth) {
            lmp_on = 1;
            continue;
        }
        if (!is_capture_before) quiets++;

        if (in_chk && surely_illegal(moves[i], king0, evade)) continue;
        if (npins < 0) npins = in_chk ? 0 : compute_pins(b, me, king0, pins);
        skip = surely_legal(moves[i], king0, pins, npins, in_chk);
        tt_make(b, moves[i], &u);
        king = b->kings[me];
        if (!skip && king >= 0 && tt_is_attacked(b, king, me)) {
            tt_unmake(b, moves[i], &u);
            continue;
        }
        legal++;
        reduction = 0;
        if (use_lmr && legal > lmr_min_move && depth >= lmr_min_depth
            && !is_capture_before && !MV_PROMO(moves[i]) && !in_chk) {
            reduction = lmr_table[depth < MAX_DEPTH ? depth : MAX_DEPTH - 1]
                                 [legal < LMR_MAX_MOVE ? legal : LMR_MAX_MOVE - 1];
            if (reduction > depth - 2) reduction = depth - 2;
            if (reduction < 0) reduction = 0;
        }
        if (reduction) {
            /* Scout shallow; only a move that raises alpha earns full depth. */
            score = -alphabeta(b, depth - 1 - reduction, -alpha - 1, -alpha,
                               ply + 1);
            if (!search_aborted && score > alpha)
                score = -alphabeta(b, depth - 1, -beta, -alpha, ply + 1);
        } else if (!use_pvs || legal == 1) {
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

/* --- component timing ------------------------------------------------------
 *
 * The Python wrappers each rebuild the whole TtBoard before they call across,
 * which costs more than the thing being measured. These take one board and do
 * the whole loop on this side, so what comes back is the component's own cost.
 *
 * Repeated `tt_eval` on one position finds the accumulator already matching
 * and skips the refresh -- which is what a real leaf does too, because the
 * deltas were paid by make on the way down. So what this measures is the
 * propagation from the accumulator through 256-32-32-1, and that is the part
 * a real search pays at every leaf. */
static double now_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + 1e-9 * (double)ts.tv_nsec;
}

double tt_bench_eval(TtBoard *b, int iters)
{
    double t0;
    int i;
    int64_t sink = 0;
    t0 = now_sec();
    for (i = 0; i < iters; i++) sink += tt_eval(b);
    t0 = now_sec() - t0;
    if (sink == 0x7FFFFFFFFFFFFFFFLL) t0 = -t0;   /* keep the loop alive */
    return t0;
}

/* nnue_eval_for, one stage at a time, as CUMULATIVE PREFIXES.
 *
 * `stage` runs the function up to and including that step and stops, so a
 * stage's own cost is the difference between two runs. Measuring a stage
 * alone would price it with a cache state it never sees in the real function;
 * a prefix keeps the order and the residency the evaluation actually has.
 *
 *   0  accumulator only -- the refresh check and the pending flush
 *   1  + crelu over L1 (256 values)
 *   2  + nn_extras (7 values, reads points and alive)
 *   3  + L2: 32 rows of a 256-wide dot, plus the 7-wide extras tail
 *   4  + L3: 32 rows of a 32-wide dot
 *   5  + output and shift  == the whole function
 *
 * A copy of the body rather than a flag inside it: the shipped path must not
 * grow a branch per stage for the sake of being measured.
 */
double tt_bench_nnue_stage(TtBoard *b, int persp, int stage, int iters)
{
    double t0;
    int i, j;
    int64_t sink = 0;
    int8_t x[NN_L1 + 2 * NN_EXPAIRS] __attribute__((aligned(32)));
    int8_t h1[NN_L2] __attribute__((aligned(32)));
    int8_t h2[NN_L3] __attribute__((aligned(32)));
    int32_t ex[NN_EXTRA];
    const int32_t *acc;

    if (!nn_loaded) return -1.0;
    t0 = now_sec();
    for (i = 0; i < iters; i++) {
        if (!nn_acc_ok || nn_acc_key != b->key) nn_refresh(b);
        nn_flush(persp);
        acc = nn_acc[persp];
        sink += acc[0];
        if (stage < 1) continue;

        nn_l1(acc, x);
        sink += x[0];
        if (stage < 2) continue;

        nn_extras(b, persp, ex);
        for (j = 0; j < NN_EXTRA; j++) x[NN_L1 + j] = (int8_t)ex[j];
        for (j = NN_L1 + NN_EXTRA; j < (int)sizeof(x); j++) x[j] = 0;
        if (stage < 3) continue;

        /* 6 is 3 with the extras tail removed. Stage 3 minus stage 6 is what
         * that tail costs, which is the thing the "cached extras" item
         * proposed to remove. */
        nn_l2(x, h1, stage != 6);
        sink += h1[0];
        if (stage == 6) continue;
        if (stage < 4) continue;

        nn_l3(h1, h2);
        sink += h2[0];
        if (stage < 5) continue;

        sink += (nn_b4 + nn_dot(h2, nn_w4, NN_L3)) >> NN_SHIFT_OUT;
    }
    t0 = now_sec() - t0;
    if (sink == 0x7FFFFFFFFFFFFFFFLL) t0 = -t0;
    return t0;
}

double tt_bench_gen(TtBoard *b, int iters, int legal)
{
    static uint32_t buf[MAX_MOVES];
    double t0;
    int i;
    int64_t sink = 0;
    t0 = now_sec();
    for (i = 0; i < iters; i++)
        sink += legal ? tt_gen_legal(b, buf) : tt_gen_pseudo(b, buf);
    t0 = now_sec() - t0;
    if (sink == 0x7FFFFFFFFFFFFFFFLL) t0 = -t0;
    return t0;
}

double tt_bench_makeunmake(TtBoard *b, int iters)
{
    static uint32_t buf[MAX_MOVES];
    TtUndo u;
    double t0;
    int i, n = tt_gen_legal(b, buf);
    if (n <= 0) return 0.0;
    t0 = now_sec();
    for (i = 0; i < iters; i++) {
        tt_make(b, buf[i % n], &u);
        tt_unmake(b, buf[i % n], &u);
    }
    return now_sec() - t0;
}

/* --- FFA: paranoid search --------------------------------------------------
 *
 * Teams is genuinely two-player -- team = seat & 1 and the turn advances by
 * one, so the side to move flips every ply and plain negamax applies. FFA is
 * not. If Red sacrifices a queen to wreck Blue, Yellow and Green both profit
 * and Red pays alone; there is no single number both sides negate.
 *
 * Paranoid (Korf 1991, Sturtevant 2000) collapses that back to two sides by
 * assuming the other three cooperate against the root player. It understates
 * the root's chances, which in a four-player game is the safe direction to be
 * wrong in, and it buys back alpha-beta: the value is held in the ROOT's terms
 * throughout, maximised at the root's nodes and minimised at everyone else's.
 *
 * Deliberately a separate function rather than a mode branch inside alphabeta.
 * Every measured Elo in this project lives on the Teams path, and sharing a
 * node loop would put all of it at risk of a subtle FFA-only edit. This shares
 * everything below the loop -- movegen, make/unmake, ordering, the pin filter
 * -- and duplicates only the loop.
 *
 * Two rules make FFA simpler here than Teams, not harder (§7):
 *   - No legal move ELIMINATES that seat, checkmate and stalemate alike, and
 *     play continues with the rest. There is no "the game is over" branch to
 *     get wrong.
 *   - The game ends when three are eliminated, so the terminal test is "one
 *     seat alive", not a mate.
 *
 * v0 evaluates with the hand evaluation. Every net in the tree was trained on
 * Teams self-play, where the alive mask never changes and points never move,
 * so a net's opinion about a three-player position is untrained rather than
 * merely unmeasured. */

/* Who the +20 goes to when `seat` is eliminated (8.2).
 *
 * Stalemate is unambiguous: the stalemated seat is paid. Checkmate is not --
 * "checkmating an opponent" names one player, and in 4PC several seats can be
 * checking at once. No source settles it (14, open item 10), so the credit
 * goes to the LAST MOVER, the nearest live seat walking backwards. That is the
 * move that ended the game for `seat`, it agrees with the checking seat in
 * every ordinary mate, and it needs no extra state. Elimination cascades stay
 * correct because an eliminated seat never moved: it is skipped on the walk.
 */
static void ffa_award_elimination(TtBoard *b, int seat)
{
    int king = b->kings[seat], c, t;
    if (b->mode != MODE_FFA) return;
    /* A seat with no king is NOT stalemated, so it is not paid the stalemate
     * bonus. Folding king < 0 into the stalemate branch scored that seat's
     * elimination backwards on any FEN4 that simply omits a king. Nobody is
     * paid here: who should be is §14 item 9's question, and inventing an
     * answer would put points in a game that awarded none. */
    if (king < 0) return;
    if (!tt_is_attacked(b, king, seat)) {
        b->points[seat] = (uint16_t)(b->points[seat] + FFA_ELIM_POINTS);
        return;
    }
    t = seat;
    for (c = 0; c < 3; c++) {
        t = (t + 3) & 3;
        if (b->alive[t]) {
            b->points[t] = (uint16_t)(b->points[t] + FFA_ELIM_POINTS);
            return;
        }
    }
}

static int ffa_alive_count(const TtBoard *b)
{
    return b->alive[0] + b->alive[1] + b->alive[2] + b->alive[3];
}

/* Take `seat` out of the game and hand the turn on. Returns what unmake needs.
 * Nothing moves on the board -- the seat's pieces stay as obstacles worth no
 * points (§9.1) -- so only the alive mask, the turn and the key change. */
static void ffa_eliminate(TtBoard *b, int seat, FfaUndo *u)
{
    int c, t = seat;
    u->key = b->key;
    memcpy(u->points, b->points, sizeof(u->points));
    ffa_award_elimination(b, seat);
    b->alive[seat] = 0;
    b->key ^= P.zob_alive[seat] ^ P.zob_turn[seat];
    for (c = 0; c < 4; c++) {
        t = (t + 1) & 3;
        if (b->alive[t]) break;
    }
    b->turn = (uint8_t)t;
    b->key ^= P.zob_turn[t];
    /* Nothing moved, so the accumulator still describes this position exactly
     * -- but the key it is tagged with does not, which would make
     * nn_delta_on_for false for the whole subtree, stop tt_make maintaining
     * the accumulator, and cost a full 160-square x 4-perspective nn_refresh
     * at the first evaluation below (and a second one on the way out).
     * Carrying the tag across keeps maintenance alive. The test must read the
     * key the accumulator currently claims, so it goes AFTER the key is
     * final and reads u->key. */
    if (nn_delta_on_for(u->key)) nn_acc_set_key(b->key);
    /* Elimination moves no pieces (section 9.1), so the occupancy bitboard is
     * still correct -- only the key it is tagged with moved. Without this the
     * whole subtree below an elimination rebuilds by scanning on every
     * generation, which is worse than the scan it replaced. */
    if (occ_ok && occ_key == u->key) occ_key = b->key;
}

static void ffa_restore(TtBoard *b, int seat, const FfaUndo *u)
{
    /* Symmetric, and FIRST: b->key is about to be overwritten, and the test
     * has to read the key the accumulator currently claims. */
    if (nn_delta_on_for(b->key)) nn_acc_set_key(u->key);
    if (occ_ok && occ_key == b->key) occ_key = u->key;
    b->alive[seat] = 1;
    b->turn = (uint8_t)seat;
    b->key = u->key;
    memcpy(b->points, u->points, sizeof(b->points));
}

/* On by default, matching the Teams search where the table is unconditional.
 * NOT a measured gain: it cuts an iterative deepening to depth 7 from 47.4M
 * nodes to 24.3M, but at fixed nodes it changes which moves get played, so the
 * Elo is an A/B nobody has run. The toggle exists so that A/B is possible,
 * not because off is a real configuration -- same footing as repetition
 * detection. */
static int use_ffa_tt = 1;

void tt_set_ffa_tt(int on) { use_ffa_tt = on ? 1 : 0; }
int tt_get_ffa_tt(void) { return use_ffa_tt; }

/* The transposition key for a paranoid node. The score is in the ROOT seat's
 * terms, so the position alone does not identify it: the same board searched
 * for a different root is a different question with a different answer. Mixing
 * the root in keeps FFA entries apart from each other and from Teams entries
 * sharing the table.
 *
 * The points standing goes in here too, and only here: the evaluation reads
 * it, so it is part of what identifies the position, but b->key doubles as the
 * repetition key where a monotonic term would stop any position ever repeating
 * (see points_hash).
 *
 * zob_turn[root] alone did NOT keep roots apart. b->key already carries
 * zob_turn[b->turn], so the two cancel whenever the root is to move -- every
 * root-to-move node of every root mapped to one key -- and (turn t, root r)
 * collided with (turn r, root t). Searching a placement for Red and then the
 * same placement with Blue to move for Blue handed the second search Red's
 * scores at ply 4: modern depth 5 came back -8488 where a cleared table says
 * -8500, and classic depth 5 changed its move. The root is therefore also
 * tagged in the top two bits. Those bits are above any tt_mask, so within one
 * search -- one root -- every index and every key equality is exactly what
 * it was, and only cross-root hits change. Replacing zob_turn[root] with a
 * fresh table was the other fix; it moves every FFA index and so every FFA
 * node count, for no gain over this. */
static uint64_t paranoid_key(const TtBoard *b, int root)
{
    return (b->key ^ P.zob_turn[root] ^ points_hash(b))
           ^ ((uint64_t)root << 62);
}

static int32_t paranoid(TtBoard *b, int depth, int32_t alpha, int32_t beta,
                        int root, int ply)
{
    uint32_t *moves, ttmove = 0, best_move = 0;
    int32_t *scores, orig_alpha = alpha, orig_beta = beta;
    TtEntry *slot = 0;
    uint64_t probe = 0;   /* read only when slot is set, which sets it; gcc 15 cannot see that */
    TtUndo u;
    Pin pins[8];
    uint64_t evade[4];
    int n, i, me = b->turn, legal = 0, quiets = 0, king0, npins, in_chk;
    int tail_sorted = 0, louds = 0, louds_seen = 0, lmp_on = 0;
    int maximising = (me == root);
    int32_t best;

    if (++search_nodes >= search_limit) { search_aborted = 1; return 0; }

    if (!b->alive[root]) return -(MATE_SCORE - ply);
    if (ffa_alive_count(b) <= 1) return MATE_SCORE - ply;

    /* A repetition ends the game (8.2), so the line stops here rather than
     * being searched on as though the position were fresh.
     *
     * The value is the STANDING, which the evaluation is the only estimate
     * of -- not zero. A Teams draw is zero because negamax is centred on
     * level; a paranoid score is one seat against three and sits near -8600
     * at the start, so returning zero here would read as an enormous win and
     * the engine would chase repetitions instead of avoiding them.
     *
     * The rule's +10 to each surviving seat is deliberately not applied: the
     * evaluation reads point DIFFERENCES only, and a payment every live seat
     * receives moves none of them. It is worth a fraction of a centipawn even
     * with a seat already out. One line to add if an FFA net ever makes the
     * points inputs sharp enough to notice. */
    if (use_repetitions && is_repetition(b, ply)) return eval_for(b, root);
    rep_keys[rep_root + ply] = b->key;

    /* Same bound logic as the Teams search, and for the same reasons -- see
     * alphabeta. The one difference is the key. */
    if (tt_table && use_ffa_tt) {
        probe = paranoid_key(b, root);
        slot = &tt_table[probe & tt_mask];
        if (slot->key == probe) {
            ttmove = slot->best;
            if (slot->depth >= depth) {
                int32_t s = slot->score;
                if (s > MATE_SCORE - MAX_DEPTH) s -= ply;
                else if (s < -(MATE_SCORE - MAX_DEPTH)) s += ply;
                if (slot->flag == TT_EXACT) return s;
                if (slot->flag == TT_LOWER && s >= beta) return s;
                if (slot->flag == TT_UPPER && s <= alpha) return s;
            }
        }
    }

    if (depth <= 0 || ply >= MAX_DEPTH - 2) return eval_for(b, root);

    moves = search_buf[ply];
    scores = order_buf[ply];
    n = tt_gen_pseudo(b, moves);
    /* -1 keeps the killer slots out of it unless FFAKillers is on; see the
     * note on that toggle. */
    score_moves(b, moves, scores, n, ttmove, use_ffa_killers ? ply : -1,
                &louds);
    king0 = b->kings[me];
    in_chk = king0 >= 0 && tt_is_attacked(b, king0, me);
    npins = -1;                  /* on first use, as in qsearch */
    if (in_chk) compute_evasions(b, me, king0, evade);

    best = maximising ? -INF_SCORE : INF_SCORE;
    for (i = 0; i < n; i++) {
        int king, skip, quiet, reduction = 0;
        int32_t score;
        /* The Teams LMP's early stop, for the same reason: once FFALMP has
         * dropped a quiet it drops every later one. */
        if (lmp_on && louds_seen == louds) break;
        if (!tail_sorted) tail_sorted = pick_move(moves, scores, n, i);
        quiet = !is_capture(b, moves[i]) && !MV_PROMO(moves[i]);
        if (!quiet) louds_seen++;
        /* The same guard as the Teams LMP, and here it matters more: a node
         * that pruned every move would read as having no legal move, and in
         * FFA that does not claim a mate, it ELIMINATES the seat. */
        if (use_ffa_lmp && quiet && legal >= 1 && depth <= lmp_max_depth
            && !in_chk && quiets >= lmp_base + depth * depth) {
            lmp_on = 1;
            continue;
        }
        if (quiet) quiets++;
        if (in_chk && surely_illegal(moves[i], king0, evade)) continue;
        if (npins < 0) npins = in_chk ? 0 : compute_pins(b, me, king0, pins);
        skip = surely_legal(moves[i], king0, pins, npins, in_chk);
        tt_make(b, moves[i], &u);
        king = b->kings[me];
        if (!skip && king >= 0 && tt_is_attacked(b, king, me)) {
            tt_unmake(b, moves[i], &u);
            continue;
        }
        legal++;
        if (use_ffa_lmr && quiet && !in_chk && legal > lmr_min_move
            && depth >= lmr_min_depth) {
            reduction = lmr_table[depth < MAX_DEPTH ? depth : MAX_DEPTH - 1]
                                 [legal < LMR_MAX_MOVE ? legal : LMR_MAX_MOVE - 1];
            if (reduction > depth - 2) reduction = depth - 2;
            if (reduction < 0) reduction = 0;
        }
        if (reduction) {
            /* A null window on the side this node moves: the root seat asks
             * "does this beat alpha?", an opponent "does this get under
             * beta?". Only a yes earns the full-depth search. */
            if (maximising) {
                score = paranoid(b, depth - 1 - reduction, alpha, alpha + 1,
                                 root, ply + 1);
                if (!search_aborted && score > alpha)
                    score = paranoid(b, depth - 1, alpha, beta, root, ply + 1);
            } else {
                score = paranoid(b, depth - 1 - reduction, beta - 1, beta,
                                 root, ply + 1);
                if (!search_aborted && score < beta)
                    score = paranoid(b, depth - 1, alpha, beta, root, ply + 1);
            }
        } else {
            score = paranoid(b, depth - 1, alpha, beta, root, ply + 1);
        }
        tt_unmake(b, moves[i], &u);
        if (search_aborted) return 0;
        if (maximising) {
            if (score > best) { best = score; best_move = moves[i]; }
            if (best > alpha) alpha = best;
        } else {
            if (score < best) { best = score; best_move = moves[i]; }
            if (best < beta) beta = best;
        }
        if (alpha >= beta) {
            if (use_ffa_killers && quiet) killer_store(ply, moves[i]);
            break;
        }
    }

    if (!legal) {
        /* Eliminated, and the game goes on. Depth is not spent: no move was
         * played, and each elimination strictly shrinks the alive mask, so
         * this can recurse at most three times. */
        FfaUndo eu;
        ffa_eliminate(b, me, &eu);
        best = paranoid(b, depth, alpha, beta, root, ply + 1);
        ffa_restore(b, me, &eu);
        /* Not stored: no move was played, so `depth` here does not mean what
         * it means at every other node, and the entry would be probed as
         * though it did. ponytail: rare path, not worth the care to get right.
         */
        return best;
    }

    if (slot) {
        int32_t s = best;
        if (s > MATE_SCORE - MAX_DEPTH) s += ply;
        else if (s < -(MATE_SCORE - MAX_DEPTH)) s -= ply;
        slot->key = probe;
        slot->score = s;
        slot->best = best_move;
        slot->depth = (int16_t)depth;
        /* Against the window this node was ENTERED with. Unlike negamax, both
         * ends move here: a max node raises alpha and a min node lowers beta,
         * so comparing against the live values would mislabel every cutoff. */
        slot->flag = (uint8_t)(best <= orig_alpha ? TT_UPPER
                               : (best >= orig_beta ? TT_LOWER : TT_EXACT));
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

/* The FFA root. Every child is scored in the root seat's terms, so unlike the
 * Teams root there is no negation here: paranoid returns a value already from
 * this seat's point of view, and the root simply takes the largest. */
static void tt_search_ffa(TtBoard *b, int depth, TtResult *out)
{
    uint32_t *moves, best_move = 0, ttmove = 0;
    int32_t *scores, best = -INF_SCORE;
    TtUndo u;
    int n, i, legal = 0, me = b->turn;

    nn_single_begin(b, me);
    moves = search_buf[0];
    scores = order_buf[0];
    n = tt_gen_pseudo(b, moves);
    if (tt_table && use_ffa_tt) {
        uint64_t probe = paranoid_key(b, me);
        TtEntry *slot = &tt_table[probe & tt_mask];
        if (slot->key == probe) ttmove = slot->best;
    }
    score_moves(b, moves, scores, n, ttmove, 0, 0);

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
        if (legal == 1) best_move = moves[i];
        score = paranoid(b, depth - 1, best, INF_SCORE, me, 1);
        tt_unmake(b, moves[i], &u);
        if (search_aborted) break;
        if (score > best) {
            best = score;
            best_move = moves[i];
        }
    }

    nn_single_end();
    out->nodes = search_nodes;
    out->score = legal ? best : -(MATE_SCORE);
    out->best = best_move;
    out->depth = depth;
    out->aborted = search_aborted;
}

/* One fixed-depth search. Iterative deepening and time management live in
 * Python at the root, per the architecture; this is the per-node loop only. */
void tt_search(TtBoard *b, int depth, uint64_t node_limit, TtResult *out)
{
    uint32_t *moves, best_move = 0;
    int32_t *scores, best = -INF_SCORE, alpha = -INF_SCORE;
    TtUndo u;
    Pin pins[8];
    uint64_t evade[4];
    int n, i, legal = 0, me = b->turn, king0, npins, in_chk;

    search_nodes = 0;
    search_makes = 0;
    search_evals = 0;
    search_gens = 0;
    search_limit = node_limit ? node_limit : (uint64_t)-1;
    search_aborted = 0;
    if (!lmr_built) lmr_build();
    /* Killers are per-search, not per-game: a stale table from another
     * position orders by moves that meant something somewhere else. */
    if (use_killers || use_ffa_killers) killers_clear();
    /* The root sits at ply 0 of the path, so children at ply 1 can see it. */
    rep_keys[rep_root] = b->key;

    if (b->mode == MODE_FFA) {
        tt_search_ffa(b, depth, out);
        return;
    }

    moves = search_buf[0];
    scores = order_buf[0];
    n = tt_gen_pseudo(b, moves);
    score_moves(b, moves, scores, n,
                (tt_table && tt_table[b->key & tt_mask].key == b->key)
                ? tt_table[b->key & tt_mask].best : 0, 0, 0);
    king0 = b->kings[me];
    in_chk = king0 >= 0 && tt_is_attacked(b, king0, me);
    npins = compute_pins(b, me, king0, pins);
    if (in_chk) compute_evasions(b, me, king0, evade);

    for (i = 0; i < n; i++) {
        int king, skip;
        int32_t score;
        pick_move(moves, scores, n, i);
        if (in_chk && surely_illegal(moves[i], king0, evade)) continue;
        skip = surely_legal(moves[i], king0, pins, npins, in_chk);
        tt_make(b, moves[i], &u);
        king = b->kings[me];
        if (!skip && king >= 0 && tt_is_attacked(b, king, me)) {
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
