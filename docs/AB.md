# Tetrarch — A/B campaign log

Every measured result, kept so a rejected idea is never quietly re-enabled and
a confirmed one can be traced to the tree that produced it.

Report format is fixed: Elo with its error margin, games, the nine-bucket
distribution, and the instrument. Never a bare Elo. The distribution is score
sums over the four-game seat rotation (0, 0.5, 1 … 4) — **not** a pentanomial,
which would assume two games per opening.

Reference machine for these runs: 111-core Linux box, `--hash 256`.

---

## Harness validity

A fixed-nodes campaign is only worth running if a null self-test — the same
engine on both sides — comes back inside noise. Both entries below are harness
bugs found by exactly that.

| when | null result | verdict |
|---|---|---|
| before the rotation fix | **+36.26 ± 18.37** (2,000 games) | **BIASED** — engine A received the original R+Y armies in all four rotations |
| after the rotation fix | −13.21 ± 14.63 (2,000 games) | inside noise at 0.9σ, but A and B collapsed to one subprocess |
| after the cache-key fix | **−2.64 ± 6.24** (10,000 games) | **PASSES** — harness certified at this precision |

**Bug 1 — the rotation cancelled nothing.** `rotate(b, k)` shifts every seat's
colour by +k, so rotated-frame team `t` holds the armies originally in team
`t ^ (k & 1)`. Assigning `a_team = rotation & 1` turned the board *and* the
team assignment together, handing engine A the first-moving team every game.
Fixed by choosing the original team first and deriving the seat index from it.

**Bug 2 — the null test ran one process.** `get_engine` keyed its cache on
configuration only, so a null self-test (identical command, net and options)
returned the same subprocess for both sides: all four seats shared one
transposition table. Every real A/B differs in a net or an option and so got
two processes — meaning the null was validating a setup no A/B ever used.
Fixed by keying on the side as well.

---

## Results

### Net v0 — REJECTED

| | |
|---|---|
| Mode / Setup | Teams / classic |
| Instrument | fixed nodes 20,000 |
| Games | 10,000 (2,500 openings × 4) |
| Elo | **−40.13 ± 7.01** |
| Dist | 253, 11, 731, 48, 857, 30, 449, 7, 114 |

Measured against the throwaway hand eval, one `setoption` apart on an identical
binary. Rerun after the rotation fix; an earlier run against the biased harness
read −14.74 ± 8.25 and is void.

**Not promoted.** Net v0 was bootstrapped off the hand eval, so it was trained
to imitate a teacher it then has to beat — and on that teacher's own metric it
scored 0.885 correlation against the hand eval's 0.947. Losing to the teacher
is a normal first-generation outcome, not a defect. The route forward is a
second generation (regenerate data with a net playing, retrain), not a tweak.

The net stays in `nets/` so the result is reproducible.

### Killers — CONFIRMED, default on

| | |
|---|---|
| Mode / Setup | Teams / classic |
| Instrument | fixed nodes 20,000 |
| Games | 10,000 (2,500 openings × 4) |
| Elo | **+50.42 ± 6.41** |
| Dist | 57, 8, 388, 31, 969, 47, 763, 21, 216 |
| Null on the same harness | −2.64 ± 6.24 |

Two killer moves per ply, `setoption name Killers`. About 8σ; screened at
+40.66 ± 14.50 over 2,000 games first, and the confirm came in higher rather
than regressing to zero.

The search previously had no quiet-move ordering at all — TT move, MVV-LVA
captures, then generation order — so this closed the largest single ordering
gap. Fixed-depth node counts fell to 28.0% of the unordered tree over five
setups to depth 5, and `SEARCH_PINS` in `selftest.py` was re-measured with it
on.

The toggle stays so it can be switched off for a future A/B.

### History heuristic — built, NOT screened: the gate is structurally dead

`setoption name History`, default **off**. Correct and verified (off is
byte-identical to the pinned tree, the toggle reaches the search, the dormant
code costs −0.1% NPS, inside noise). It has not been A/B'd, deliberately.

Node reduction at fixed depth, classic + modern:

| depth | off | on | |
|---:|---:|---:|---|
| 5 | 143,774 | 137,715 | 95.8% |
| 6 | 513,485 | 515,979 | 100.5% |
| 7 | 2,704,547 | 2,271,372 | 84.0% |
| 8 | 19,447,589 | 16,318,517 | 83.9% |

History only starts paying at depth 7. But at the campaign instrument of
**20,000 nodes the search reaches median depth 4** (range 3–5) — one full round
of four seats. In that regime the feature does nothing, so a screen there would
measure noise and return a confident null about a feature that was never
engaged.

Depth against budget, classic, 16 positions:

| nodes | median depth | s/move |
|---:|---:|---:|
| 20,000 | 4 | 0.02 |
| 100,000 | 5 | 0.07 |
| 300,000 | 5 | 0.22 |
| 1,000,000 | 6 | 0.71 |

Reaching depth 7 needs roughly a million nodes a move, which is ~35× the
current cost per game. Worth spending once there is a reason to believe the
engine plays there; not worth spending to confirm a null.

Left dormant with this note attached, per the doctrine on closing a
structurally dead gate before spending A/B time.
