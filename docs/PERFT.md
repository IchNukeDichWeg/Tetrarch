# Tetrarch — perft record

Phase 1 gate numbers. Section references (§n) are to `RULES.md`.

**Perft counts are exact integer arithmetic and are not machine-dependent.**
They are pinned unconditionally in `selftest.py` and must never be re-pinned to
match a machine that "disagrees" — a disagreement is a bug, not a rounding
difference. Only the *timings* below belong to a particular box. The
float-sensitive node pins arrive with the search in Phase 3; those get a named
reference machine and this warning applies to them for real.

## Reference machine (timings only)

| | |
|---|---|
| Chip | Apple M2 Pro, 10 cores (6 performance / 4 efficiency) |
| OS | macOS 26.5.2, Darwin 25.5.0, arm64 |
| Python | CPython 3.14.6 |
| Engine | pure Python (`tetrarch/movegen.py`); no C core yet |

## Node counts from the starting position

All five setups (§3), Teams mode.

| depth | classic | modern | by | byg | rg |
|------:|--------:|-------:|---:|----:|---:|
| 1 | 20 | 20 | 20 | 20 | 20 |
| 2 | 399 | 395 | 395 | 395 | 395 |
| 3 | 7,960 | 7,800 | 7,880 | 7,880 | 7,880 |
| 4 | 158,402 | 152,050 | 155,226 | 155,210 | 155,226 |
| 5 | 3,730,168 | 3,452,310 | 3,593,432 | 3,525,566 | 3,587,766 |

Depth 5 wall time: 30–35 s per setup, single-threaded.

### FFA and Teams agree at these depths

Measured identical for every setup through depth 4, and for `classic` and
`modern` at depth 5. This is expected rather than lucky: the two modes differ
only in promotion rank and in whether teammates may be captured (§4.2, §2). A
pawn needs six of its own moves to reach any promotion rank, which is 21+ plies;
and no piece can reach a teammate's position within five plies. Neither
difference can bite this shallow.

## External cross-check: Athena

`modern` is the only setup with an outside oracle —
`arianahejazyan/Athena`, `tests/data/perft.txt` (§12).

| depth | Athena | Tetrarch | |
|------:|-------:|---------:|---|
| 1 | 20 | 20 | match |
| 2 | 395 | 395 | match |
| 3 | 7,800 | 7,800 | match |
| 4 | 152,050 | 152,050 | match |
| 5 | 3,452,310 | 3,452,310 | match |
| 6 | 77,430,383 | 77,430,383 | match |
| 7 | 1,735,784,286 | 1,735,784,286 | match |

**Exact through depth 7.** Depths 6 and 7 were run through the C core (3.0 s and
68.7 s); pure Python would have taken ~12 min and ~4.5 h.

En passant first occurs at depth 4, so this confirms the en-passant lifetime
model (§5.1) and the capture geometry against an independent implementation
across 1.7 billion nodes.

### The two-flank divergence is not testable by perft

§5.4 predicted Tetrarch would diverge from Athena once two of your own pawns
attack the same skipped square. It did not appear at depth 6 or 7, and it never
can: the case needs, for example, Blue to double-push b6–d6 while Red has pawns
on **both** b5 and d5. Red's pawns start on files d–k, so putting one on file b
costs two captures plus a push — six Red moves at minimum, which is ply 21 or
later. Every other seat pairing is worse.

So **no reachable perft can distinguish the two implementations here**, and
Athena's numbers matching is not evidence either way. The differential gate
against the slow reference generator, which reaches these positions by
scattering rather than by playing to them, is the only check that covers it.

## Why the setups differ

They differ from depth 2 onward, and the reason is worth recording because it
looks like a bug and is not.

Only king/queen placement varies between setups (§3.1). That still changes node
counts, because a queen's reach past the cut corners depends on which of the two
central squares it stands on, and because it changes which enemy pieces are
pinned.

The sharpest case: **`classic` and `rg` place Blue identically** (bK a8, bQ a7)
yet differ by exactly 4 at depth 2. The whole difference sits on Red's two `g2`
moves, `g2g3` and `g2g4`, which give Blue 20 replies in `classic` and 18 in `rg`:

* In `rg`, Red's **queen** is on h1. Once the g2 pawn steps off, the queen's
  h1–a8 diagonal runs g2, f3, e4, d5, c6, b7 — stopping on Blue's b7 pawn, which
  stands directly in front of Blue's king on a8. The b7 pawn is therefore
  **pinned**, and its two moves (`b7c7`, `b7d7`) become illegal.
* In `classic`, Red's queen is on g1 instead, which is not on that diagonal, and
  no pin exists.

2 lost replies × 2 Red moves = 4. 399 − 4 = 395.

`by` and `rg` agree through depth 4 and then separate at depth 5
(3,593,432 vs 3,587,766), which is the same class of effect one ply deeper.

## Cross-generator agreement (§ Phase 1 gate)

`tetrarch/movegen.py` (padded-mailbox deltas, ray-cast attack detection) and
`tetrarch/movegen_slow.py` ((file, rank) pairs, its own bounds test, castling
geometry rebuilt from scratch, brute-force attack enumeration) are compared
move-for-move, not just by leaf count.

* Full perft(3) tree agreement for all five setups — in `selftest.py`, every run.
* Random-position cross-check, both generators' complete legal move lists sorted
  and compared.

Throughput on the reference machine: **183–283 positions/s** single-process,
**2,871 positions/s** sustained across all 10 cores over the full gate run. The
slow generator is the bottleneck by design; speeding it up would defeat its
purpose.

(Short parallel runs report a lower rate — progress only advances as whole
chunks finish, so the first few ticks under-count and the ETA overstates. 2,871
is the sustained figure from the 10 M run, not an extrapolation.)

Positions are drawn from two sources, because neither covers the rule surface
alone: 35 % are playouts of 0–45 random legal moves from a random setup and mode
(these reach castling and en-passant sequences), 65 % are scattered positions
with seats deliberately placed at home, en-passant offers planted together with a
pawn positioned to accept them, dead seats, and pieces on promotion ranks.

The run asserts afterwards that it actually saw each hard rule; a cross-check
that never generated an en-passant capture would pass vacuously.

| run | positions | disagreements | ep | promo | castle | check | dead |
|---|---:|---:|---:|---:|---:|---:|---:|
| pre-commit default | 3,000 | 0 | 334 | 614 | 393 | 678 | 1,166 |
| recorded | 20,000 | 0 | 2,019 | 3,920 | 2,700 | 4,499 | 8,008 |
| **gate** | **10,000,000** | **0** | 983,351 | 1,992,104 | 1,371,517 | 2,257,281 | 4,053,962 |

## Phase 1 gate: PASSED

```
selftest.py --crosscheck 10000000 --workers 0
```

10,000,000 positions, **0 disagreements**, 3,482.9 s (58 min) at 2,871 positions/s
on the reference machine, seed 0. 243,138 positions (2.4 %) were terminal — no
legal move for the side to move — which is the expected rate for scattered
positions and confirms the sample reaches checkmate and stalemate shapes rather
than only quiet middlegames.

Coverage over the run: 983,351 en-passant captures, 1,992,104 promotions,
1,371,517 castles, 2,257,281 positions with the side to move in check, 4,053,962
with at least one seat eliminated. Every one of those is asserted non-zero — a
cross-check that never generated an en-passant capture would pass vacuously.

Both Phase 1 gate conditions are therefore met: the two independently written
generators agree over 10 M random positions, and perft to depth 5 is recorded
above for all five setups with `modern` matching Athena exactly.

## Phase 2 gate: PASSED

The C core (`src/c/tetrarch.c`, reached through `tetrarch/core.py`) declares no
chess constants of its own — every table is pushed in from `board.py` at
startup, and the struct layouts are asserted against the C `sizeof` at load
time.

* **Node-for-node with Python**: identical perft at every depth 1–5, for all
  five setups, in both modes. 50 comparisons, 0 differences.
* **Depths 6 and 7 against Athena**: exact, as tabulated above.
* **Zobrist keys agree bit-for-bit** between C and Python over random positions.
* **`is_attacked` agrees** between C and Python over random positions and squares.
* **Move lists agree** — the cross-check is now three-way (fast Python, slow
  Python, C), so a C-only bug is caught by the same gate.

### Key and unmake integrity

Perft is blind to two failures that matter later: a wrong *incremental* Zobrist
key still counts the right number of nodes, and it only surfaces once the
transposition table starts trusting it. `tt_key_check` walks the whole legal
tree checking, after every make, that the incremental key equals a full
recompute, and after every unmake that the piece array is restored exactly.

Depth 3, all five setups, both modes: **0 mismatches** in all ten runs.

### Speed

| | nodes/s |
|---|---:|
| Python reference (`tetrarch/movegen.py`) | ~105,000 |
| C core (`src/c/tetrarch.c`) | **~27,500,000** |

About 260×. Athena reports ~120 Mnps with 256-bit bitboards; Tetrarch is mailbox
by design (see the project brief) and porting to bitboards stays a later option
behind the same interface, if profiling ever says so.

Bench signature at depth 5 over the five frozen positions in `bench.py`:

```
93846865 nodes 27492167 nps
```

The node count is exact and machine-independent; the nps is the M2 Pro figure
above. `bench.py --rounds 9` discards round 1 and reports the median with its
spread — use that, not the single-round number, for any NPS claim under 1 %.
