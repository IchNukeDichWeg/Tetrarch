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

### Principal variation search — built, NOT screened: negative at this depth

`setoption name PVS`, default **off**. Correct (returns the same score as plain
alpha-beta, pinned in selftest) and off is byte-identical to the pinned tree.

Node counts at fixed depth, classic + modern:

| depth | off | on | |
|---:|---:|---:|---|
| 4 | 19,318 | 22,977 | **118.9%** |
| 5 | 143,774 | 165,817 | **115.3%** |
| 6 | 513,485 | 540,697 | 105.3% |
| 7 | 2,704,547 | 2,546,724 | 94.2% |

It makes the tree **bigger** at the depths this engine actually searches. That
is not a defect: PVS pays for a re-search whenever a later move beats alpha, so
it only wins when the ordering is good enough that the first move usually is
best. At a branching factor near 60 with only two killers ordering the quiets,
the re-searches cost more than the null windows save.

Not screened. It would lose, and losing would tell us about the ordering rather
than about PVS. Revisit after the ordering improves or the search reaches
depth 7.

### The pattern in these two

History and PVS are both dead for the same reason: **the search reaches median
depth 4 at 20,000 nodes** — one round of four seats. Both are refinements that
assume depth and good ordering.

What limits depth here is the branching factor (~60 legal moves a seat) and the
cost of a node, of which the throwaway eval is 77% — its king-danger term alone
is 54% of all search time. So the work that buys depth is pruning that engages
at shallow depth (LMP, LMR) and a cheaper eval, not window or ordering
refinements.

### Late move reductions — CONFIRMED, default on

`setoption name LMR`, default **off**, with `LMRMinDepth` (3) and `LMRMinMove`
(3). Reduction table is `0.75 + log(depth)·log(move)/2.25`.

Node counts at fixed depth, classic + modern:

| depth | off | on | |
|---:|---:|---:|---|
| 4 | 19,318 | 13,451 | 69.6% |
| 5 | 143,774 | 31,359 | **21.8%** |
| 6 | 513,485 | 154,319 | 30.1% |
| 7 | 2,704,547 | 397,384 | **14.7%** |

Unlike history and PVS this engages at depth 4, because it attacks the
branching factor rather than assuming the ordering is already good.

The number that decides whether it is worth screening is not the node count but
whether it converts into depth at a fixed budget. Over 40 positions at 20,000
nodes:

| | median depth | mean depth | distribution |
|---|---:|---:|---|
| off | 4 | 3.73 | 2:1, 3:11, 4:26, 5:2 |
| on | 4 | **4.25** | 2:1, 3:8, 4:15, 5:13, 6:2, 7:1 |

Half a ply of mean depth, and the tail reaches 6–7 where it previously never
passed 5. **LMR is not exact** — it can miss a line the full search would find —
so the reduction is not automatically Elo, and this one genuinely needs the
games.

| | |
|---|---|
| Instrument | fixed nodes 20,000 |
| Games | 10,000 (2,500 openings × 4) |
| Elo | **+35.07 ± 6.51** |
| Dist | 88, 12, 415, 39, 1002, 45, 681, 16, 202 |

Screened +24.53 ± 14.19 over 2,000 first, confirmed at ~5.4σ. `SEARCH_PINS`
re-measured with it on.

The minimax oracle in `selftest.py` turns LMR off for its comparison: the
reductions are inexact, so the oracle would otherwise be checking alpha-beta
against a search that is deliberately allowed to differ.

### Lazy evaluation — built, needs a FIXED-TIME screen

`setoption name LazyEval`, default **off**. Computes material first and skips
the king-danger term when material alone settles the bound by more than
`4 × 8 × king_danger = 384`, which is the most the danger term can move it.

**Not exact.** Every cutoff decision is identical — the margin guarantees that
— but a bail returns the material term rather than the true eval, and fail-soft
propagates that value. Measured node-identical anyway over 80 positions (20
start positions across five setups at depths 4–7, plus 60 random midgame
positions at depth 5), which suggests the difference is always absorbed by the
cutoff cascade that follows. Empirical, not a theorem; `selftest` watches it.

| | search nps at depth 7, classic |
|---|---:|
| off | 1,418,577 |
| on | **1,620,220** (+14.2%) |

**This one must be screened on fixed time, not fixed nodes.** It changes speed
and not the tree, so a fixed-nodes campaign would report exactly zero — the
instrument would under-credit the entire gain.

And a fixed-time run needs **far fewer workers than cores**. Two engine
subprocesses per worker means 111 workers on a 111-core box gives each engine
about half a core, and the timing becomes noise. Around 40 workers keeps every
engine on its own core.

**Screened +64.66 ± 15.12** over 2,000 games at `movetime 200`, 40 workers
(Dist: 13, 1, 64, 10, 187, 22, 133, 7, 63). About 4.3σ.

Larger than +14% NPS would usually buy, and the likely reason is depth
granularity: at a median of 4 plies, 14% more nodes is often the difference
between finishing an iteration and not, and a whole extra ply is worth far more
than 14%. Awaiting a 10,000-game confirm — which will run against the new
LMR-on baseline, not the one it was screened on.

---

## A property of the harness worth knowing

Two identical `match.py` invocations return **byte-identical results** — same
score, same distribution. Openings are seeded from `--seed` (default 1) and the
engines are deterministic at fixed nodes, so the same command replays the same
games.

That is good for reproducing a verdict and useless for adding confidence.
**Re-running is not an independent sample; change `--seed`.**
