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

### Net v1 — NOT promoted, but the pipeline works

| | |
|---|---|
| Mode / Setup | Teams / classic |
| Instrument | fixed nodes 20,000 |
| Games | 2,000 (500 openings × 4) — screen only |
| Elo | **−2.26 ± 15.58** |
| Dist | 42, 2, 102, 3, 196, 7, 115, 1, 32 |

| | net v0 | net v1 |
|---|---|---|
| Elo vs hand eval | −40.13 ± 7.01 | **−2.26 ± 15.58** |
| teacher | v0 engine | v5 engine |
| budget | 5,000 nodes | 20,000 nodes |
| teacher mean depth | ~3.7 | 5.09 |
| teacher quiescence | stood pat through checks | sees evasions and mates |
| positions | 3.9 M | 9.19 M |

**Roughly +38 Elo of net quality**, and the net now evaluates about as well as
the hand eval per node. Not promoted: at the time it was also **2.84× slower**
per node, which loses at any real time control regardless of the fixed-nodes
result.

No 10,000-game confirm was run. Every decision is the same whether the true
value is +13 or −18 — do not promote, fix the speed — so the games were spent
on the accumulator instead.

Training stopped improving at **epoch 5** (val 0.02690) while train loss kept
falling to 0.02323 by epoch 8. That is overfitting, not the underfitting v0
showed, so more epochs is the wrong lever and more data is the right one.

**Why parity was the expected ceiling, not a disappointment.** Both v0 and v1
were labelled by the *hand eval* playing, because `gen_data.py` had no way to
load a net until now. A student trained on a teacher's own search scores
converges on that teacher. Generation 2 is the first run where a net plays,
and it is the first real test of whether the loop compounds.

### Net v2 — REJECTED, and the self-labelling loop is degenerative

First generation where a net labelled the data: 149,986 games played by net v1
at 20,000 nodes, 11.0 M positions, best held-out loss 0.03557 at epoch 6.

| opponent | Elo | games |
|---|---|---|
| hand eval | **−224.46 ± 18.74** | 2,000 |
| net v1 | **−226.78 ± 18.20** | 2,000 |

Losing by the same margin to two opponents that are themselves at parity
(v1 vs hand = −2.26) is internally consistent: v2 is simply far weaker. This
is not "the loop failed to compound", it is the loop running **backwards**.

**Mechanism 1 — the targets collapsed.** The trained net's evaluations have
stdev 177 against v1's 379, and almost never go negative (min −55, mean +203).
It learned its targets faithfully; the targets were the problem:

| | v1 data | v2 data |
|---|---|---|
| target stdev | 0.3317 | **0.2420** |
| target mean | 0.5068 | **0.5347** |
| saturated (<0.01 or >0.99) | 13.72% | 5.59% |

The blend is `0.7 · sigmoid(cp/400) + 0.3 · result`, so 70% of the target comes
from the teacher's search score. Net v1's scores cluster nearer zero than the
hand eval's (median cp −5 against +88), so the signal shrinks. Feeding that
back produces a flatter net, whose scores cluster harder still. At λ=1.0 the
v2 spread is 0.219 against v1's 0.328; the collapse is entirely in the score
term, and the game-result term is what holds the target apart.

**Mechanism 2 — the labels are far less learnable, and λ cannot fix it.**
Against a constant predictor:

| | best val | constant predictor | variance explained |
|---|---|---|---|
| v1 | 0.02690 | 0.11002 | **76%** |
| v2 | 0.03557 | 0.05856 | **39%** |

The hand eval is a simple deterministic function -- material plus a king-danger
count -- and a net fits it easily. A net's own search scores are a complicated,
partly arbitrary function, and 61% of their variance is not recoverable from
the position at all. That is the deeper failure, and lowering λ does not touch
it.

**Why the premise did not hold.** Bootstrapping needs search(net) to be
appreciably better than net. At 20,000 nodes the search reaches ~2.5 moves per
seat against a branching factor near 60, so the margin is thin -- and net v1
was only at parity with the hand eval to begin with. A teacher no better than
the last teacher cannot compound.

Ruled out before blaming the data: the trainer rewrite reproduces v1's original
training on v1's cache (0.02926 / 0.02798 / 0.02733 against the server's
0.02937 / 0.02801 / 0.02737, the gap being BLAS summation order), and the v2
labels predict their game result 100% of the time.

**Data provenance, unresolved.** Games at index ≥1368 do not reproduce under
any engine, net, node budget or depth tried, while indices 0-1367 reproduce
bit-exactly with net v1 at 20,000 nodes. The generating run was interrupted and
resumed, and 80 games in 1184-1367 appear twice with different content. So the
dataset is known to be mixed, and how much of the −225 belongs to that rather
than to the two mechanisms above is not established.

### Incremental NNUE accumulator — infrastructure, no version number

Not an A/B: with no net loaded not a line of it runs, so the default build is
untouched and every pin is unchanged.

| | NNUE nps | vs hand eval |
|---|---:|---|
| full refresh per eval | 448,411 | 2.84× slower |
| incremental | 918,768 | **1.37× slower** |

A 2.07× speedup of the evaluation path. The 2.84× tax was unconditional, so
before this no net could ship on a fixed-time control however well it
evaluated.

Gated by `tt_nnue_acc_matches` against a rebuild — perft and node pins cannot
see a wrong accumulator, because it changes evaluations rather than the shape
of the tree.

### Killers — CONFIRMED, default on

| | |
|---|---|
| Mode / Setup | Teams / classic |
| Instrument | fixed nodes 20,000 |
| Games | 10,000 (2,500 openings × 4) |
| Elo | **+50.42 ± 6.41** |
| Dist | 57, 8, 388, 31, 969, 47, 763, 21, 216 |
| Null on the same harness | −2.64 ± 6.24 |

Two killer moves per ply, `setoption name Killers`, default **on**. About 8σ; screened at
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

`setoption name LMR`, default **on**, with `LMRMinDepth` (3) and `LMRMinMove`
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

### Lazy evaluation — CONFIRMED, default on

`setoption name LazyEval`, default **on**. Computes material first and skips
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

| | |
|---|---|
| Instrument | **fixed time**, `movetime 200`, 100 workers |
| Games | 10,000 (2,500 openings × 4) |
| Elo | **+42.88 ± 6.50** |
| Dist | 61, 13, 428, 44, 953, 46, 719, 17, 219 |

About 6.6σ. Screened +64.66 ± 15.12 over 2,000 games first, and the confirm came
in **lower** — as expected, because the confirm ran against the new LMR-on
baseline, and LMR had already bought some of the depth lazy eval was buying.
Confirming against the current default rather than the one a feature was
screened on is the point; the number moving down is not a regression.

`SEARCH_PINS` needed no change: the tree is unmoved, only the clock.

Still larger than +14% NPS would normally buy, and the likely reason is depth
granularity — at a median of 4–5 plies, 14% more nodes is often the difference
between finishing an iteration and not.

### Late move pruning — CONFIRMED, default on

`setoption name LMP`, default **on**, with `LMPMaxDepth` (3) and `LMPBase` (4).
Drops quiet moves outright once `LMPBase + depth²` of them have been tried
without a cutoff, at shallow depth, not in check.

Nodes at fixed depth, classic + modern:

| depth | off | on | |
|---:|---:|---:|---|
| 4 | 13,451 | 5,744 | **42.7%** |
| 5 | 31,359 | 12,178 | **38.8%** |
| 6 | 154,319 | 71,296 | 46.2% |
| 7 | 397,384 | 224,196 | 56.4% |

Depth at 20,000 nodes over 40 positions:

| | median | mean | distribution |
|---|---:|---:|---|
| off | 4 | 4.25 | 2:1, 3:8, 4:15, 5:13, 6:2, 7:1 |
| on | **5** | **5.25** | 4:5, 5:21, 6:13, 7:1 |

A full extra ply, and the shallow tail (depth 2–3) disappears entirely. Larger
than LMR's gain (3.73 → 4.25) on the same measurement.

**It is a hard prune**, so it can miss things. Over 84 comparable positions it
missed a mate the full search finds 3 times, and **invented one 0 times** — the
`legal >= 1` guard is what prevents a node pruning every move and then claiming
checkmate. selftest pins the never-invents property.

| | |
|---|---|
| Instrument | fixed nodes 20,000 |
| Games | 10,000 (2,500 openings × 4) |
| Elo | **+36.09 ± 6.69** |
| Dist | 96, 6, 437, 36, 968, 33, 683, 16, 225 |

Screened +36.62 ± 15.43 over 2,000 first — the confirm landed almost exactly on
the screen. Released as **v4**.

### Quiescence check evasions — CONFIRMED, default on

`setoption name QSEvasions`, default **on**. When the side to move is in check
quiescence searches every legal move instead of captures only, does not stand
pat, and reports mate when there are none.

Standing pat means "I could just do nothing here", which is precisely what a
side in check may not do. Without this, quiescence scores lost positions as
quiet and **cannot see a mate at all**.

What it fixes, over 65 random positions with a seat in check, searched to
depth 2: **10 mates (15%) are visible only with evasions on.**

What it costs:

| depth | off | on | |
|---:|---:|---:|---|
| 4 | 13,451 | 13,678 | 101.7% |
| 5 | 31,359 | 32,214 | 102.7% |
| 6 | 154,319 | 188,136 | 121.9% |
| 7 | 397,384 | 502,636 | 126.5% |

Mean depth at 20,000 nodes falls 4.25 → 4.08.

So it is a genuine correctness fix that costs about a sixth of a ply.

**Screened +104.68 ± 15.67** over 2,000 games (Dist: 7, 2, 45, 7, 167, 6, 176,
2, 88). About 6.7σ, and the largest single result measured on this engine.

I predicted this might be *negative* — it costs depth and wins nothing on node
count. That was reasoning from two-player chess, and it was wrong for a reason
specific to this game: **in 4PC you can be checked by three opponents rather
than one**, so checks are far more frequent, and a quiescence that mis-handles
check is proportionally more broken. Standard chess intuition understates this
feature here.

| | |
|---|---|
| Instrument | fixed nodes 20,000 |
| Games | 10,000 (2,500 openings × 4) |
| Elo | **+106.78 ± 6.88** |
| Dist | 28, 6, 224, 30, 832, 40, 895, 22, 423 |

About 15.5σ, and the confirm came in **above** the screen. The largest single
gain in the engine, on the one feature predicted to lose. Released as **v5**.

The general lesson is worth keeping: **chess intuition about the relative value
of a feature does not transfer to a four-seat game.** History and PVS both
underperformed their chess reputations here; check evasions massively
overperformed. All three were misjudged in the same direction — by assuming
chess proportions.

---

## A property of the harness worth knowing

Two identical `match.py` invocations return **byte-identical results** — same
score, same distribution. Openings are seeded from `--seed` (default 1) and the
engines are deterministic at fixed nodes, so the same command replays the same
games.

That is good for reproducing a verdict and useless for adding confidence.
**Re-running is not an independent sample; change `--seed`.**
