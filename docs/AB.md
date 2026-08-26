# Tetrarch -- A/B campaign log

Every measured result, kept so a rejected idea is never quietly re-enabled and
a confirmed one can be traced to the tree that produced it.

Report format is fixed: Elo with its error margin, games, the distribution, and
the instrument. Never a bare Elo. The distribution is score sums over the
four-game seat rotation -- **not** a pentanomial, which would assume two games
per opening.

Teams and FFA runs are reported the same way and **cannot be pooled**. Teams
pairs 2v2 and scores in half-points, so a rotation sums to one of NINE values
(0, 0.5, 1 … 4). FFA puts engine A on one seat against three of B and scores
its share of three pairwise contests, so it lands on thirds and sums to one of
THIRTEEN. Different pairing, different scale, different distribution.

Reference machine for these runs: 111-core Linux box, `--hash 256`.

---

## Harness validity

A fixed-nodes campaign is only worth running if a null self-test -- the same
engine on both sides -- comes back inside noise. Both entries below are harness
bugs found by exactly that.

| when | null result | verdict |
|---|---|---|
| before the rotation fix | **+36.26 ± 18.37** (2,000 games) | **BIASED** -- engine A received the original R+Y armies in all four rotations |
| after the rotation fix | −13.21 ± 14.63 (2,000 games) | inside noise at 0.9σ, but A and B collapsed to one subprocess |
| after the cache-key fix | **−2.64 ± 6.24** (10,000 games) | **PASSES** -- harness certified at this precision |

**Bug 1 -- the rotation cancelled nothing.** `rotate(b, k)` shifts every seat's
colour by +k, so rotated-frame team `t` holds the armies originally in team
`t ^ (k & 1)`. Assigning `a_team = rotation & 1` turned the board *and* the
team assignment together, handing engine A the first-moving team every game.
Fixed by choosing the original team first and deriving the seat index from it.

**Bug 2 -- the null test ran one process.** `get_engine` keyed its cache on
configuration only, so a null self-test (identical command, net and options)
returned the same subprocess for both sides: all four seats shared one
transposition table. Every real A/B differs in a net or an option and so got
two processes -- meaning the null was validating a setup no A/B ever used.
Fixed by keying on the side as well.

---

## Results

### Net v9 retrained with the split and E-01 fixes -- REJECTED

| | fixed nodes 20,000 | fixed time 200ms |
|---|---|---|
| Elo vs net-v5 | **-14.46 +/- 6.33** | **-31.77 +/- 6.40** |
| Games | 10,000 | 10,000 |
| Dist | 142, 29, 579, 109, 938, 110, 475, 26, 92 | 171, 26, 682, 79, 960, 52, 434, 11, 85 |

The same 250,000 games the original net v9 was trained on, retrained with the
two bugs that were live through all five failed generations now fixed: the
validation split leaked across games, so the epoch table chose on a number that
did not mean what it said, and E-01 had the extras arriving at the wrong scale.

**The fixes were not the cause.** The original v9 measured -71.36; this reads
-31.77 at fixed time. That is a large move and the fixes are worth having, but
generation 9 data still does not produce a net that beats net-v5, so whatever
stopped the loop compounding after v5 is something else.

**This is not a clean test, and the gap should not be quoted as the value of
the fixes.** The retrain ran 2 epochs where the original ran 8 -- the box died
during epoch 3 and 39 minutes an epoch made restarting a bad trade. Validation
was still improving from epoch 1 to 2 when it stopped, so the net is
undertrained rather than converged. A fair comparison needs the same 8 epochs
and is waiting on a GPU box.

What this does close: five generations were blamed on two bugs, and the bugs
are not enough. The next hypothesis has to be about the data or the recipe --
self-play from a strong teacher losing diversity, the lambda blend, or the
capacity of a 3840-256-32-32-1 net on this distribution -- not about the
trainer being broken.


### Net v0 -- REJECTED

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
to imitate a teacher it then has to beat -- and on that teacher's own metric it
scored 0.885 correlation against the hand eval's 0.947. Losing to the teacher
is a normal first-generation outcome, not a defect. The route forward is a
second generation (regenerate data with a net playing, retrain), not a tweak.

The net stays in `nets/` so the result is reproducible.

### Net v1 -- NOT promoted, but the pipeline works

| | |
|---|---|
| Mode / Setup | Teams / classic |
| Instrument | fixed nodes 20,000 |
| Games | 2,000 (500 openings × 4) -- screen only |
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
value is +13 or −18 -- do not promote, fix the speed -- so the games were spent
on the accumulator instead.

Training stopped improving at **epoch 5** (val 0.02690) while train loss kept
falling to 0.02323 by epoch 8. That is overfitting, not the underfitting v0
showed, so more epochs is the wrong lever and more data is the right one.

**Why parity was the expected ceiling, not a disappointment.** Both v0 and v1
were labelled by the *hand eval* playing, because `gen_data.py` had no way to
load a net until now. A student trained on a teacher's own search scores
converges on that teacher. Generation 2 is the first run where a net plays,
and it is the first real test of whether the loop compounds.

### Net v2 -- VOID, and so is the analysis that went with it

Rejected at -224.46 +/- 18.74 against the hand eval and -226.78 +/- 18.20
against net v1, and a long post-mortem here concluded that self-labelling was
"degenerative" and named two mechanisms for it.

**All of it is void.** The data was generated by an engine whose NNUE
accumulator was corrupted from the second move of every game (see the
accumulator entry below), so net v2 learned from labels a broken evaluator
produced. Net v4 repeats the method exactly, same lambda, same node budget, on
an engine that is not corrupting itself, and comes back **+93.95** instead of
-225.

The target-collapse statistics quoted at the time were real properties of the
files; the files were rubbish. The conclusion drawn from them was wrong in
every particular, and is kept here only because a campaign record that deletes
its mistakes is not a record.

`nets/net-v2.nnue` stays so the finding is checkable: its evaluations have
stdev 177 where every sound net sits near 380.

### Net v4 -- CONFIRMED, and the engine now plays with it

| | fixed time (movetime 200) | fixed nodes (20,000) |
|---|---|---|
| vs hand eval | **+76.79 +/- 6.87** | **+135.19 +/- 7.36** |
| games | 10,000 | 10,000 |
| screen was | +81.18 +/- 16.22 | +140.01 +/- 16.97 |

Both confirms landed inside their screens. Fixed TIME is the one that decides
what ships, and it is the smaller number for the obvious reason: the net costs
1.37x per node, so about 58 Elo of the fixed-node advantage is spent buying
back the depth it loses. It keeps well over half.

**The instrument was certified first.** Every fixed-time result in this file
rests on an instrument that had never had a null run on it. It has now:
**-10.77 +/- 13.96** over 2,000 games, 1.5 sigma, inside noise. Weaker than the
fixed-nodes null (-2.64 +/- 6.24) because it is a fifth of the games -- fixed
time costs 5.6x the wall clock per game. Worth noting the sign: the null
disadvantages engine A slightly, and every fixed-time result here is positive
FOR engine A, so if that bias is real it understates the gains rather than
inventing them.

Released as **v6**, the first release to change the evaluation rather than the
search, and the first since v0 where a fresh clone plays differently.

### Net v4 vs net v1 -- the loop compounds at both instruments

| | Elo | games |
|---|---|---|
| fixed nodes | +93.95 +/- 14.81 | 2,000 |
| fixed time | **+90.22 +/- 15.13** | 2,000 |

The generational gain is not a fixed-nodes artefact: it survives a clock
almost undiminished. That is what justifies spending server hours on
generation 5 with net v4 teaching.

### Lambda -- CLOSED at 0.7, and now closed from both sides

The original closure tested only arms BELOW 0.7 and called it settled. That was
not a closure, it was a default nobody had probed upward: every measurement
pointed the same way, which is exactly the shape that hides an optimum further
along. An 0.85 arm was later run on generation 6's data and lost -40.52 +/-
4.66 against v5, where the same data at 0.7 lost -30.29 +/- 4.65. Worse on
both sides, so 0.7 stands, now on evidence rather than on the absence of it.

The reasoning that motivated the 0.85 arm was wrong and worth writing down:
generation 6's labels come from an 18,000-node search rather than 7,500, so
the evaluation half of the target should be more trustworthy and deserve more
weight. It does not, or that is not what limits the blend.

One trap in reading those runs: losses at different lambda are not comparable.
0.85 reached a held-out loss of 0.01857 against 0.7's 0.02621 and played 10
Elo worse. Lambda changes the target itself, and sigmoid(eval) is easier to fit
than a game result, so a higher lambda buys a lower number mechanically.

Three arms trained from one cache, played head to head against 0.7:

| arm | Elo vs lambda 0.7 | games |
|---|---|---|
| lambda 0.30 | **-94.51 +/- 15.08** | 2,000 |
| lambda 0.15 | **-124.60 +/- 15.58** | 2,000 |

Monotone: the more weight on the game result, the worse. It tracks the
evaluation spread exactly -- stdev 390 at 0.7, 287 at 0.3, 227 at 0.15 -- and
the reason is that a game result is a 0/1 signal carrying no magnitude, so
leaning on it teaches a win-probability shape with less centipawn range.

This is the opposite of what the VOID net v2 post-mortem predicted, which is
the third of its conclusions to be overturned by a measurement. Lambda stays
0.7 and needs no further arms.

### VOID: the mobility rejection measured a margin, not a term

The rejection below stands as a record of what was run, but it did not measure
mobility. With LazyEval on -- the default since v3 -- every stand-pat the search
sees goes through `tt_eval_bounded`, whose full path returned
`material + hand_danger(b)`. `hand_mobility` was only ever added by `hand_eval`,
reachable solely from the MAX_DEPTH cap. So turning Mobility on changed exactly
one thing in the searched tree: `lazy_margin` widened from 384 to 1,344, making
the confirmed +42.88 lazy bail fire less often, while the term contributed
nothing.

Both arms of that campaign therefore measured a widened lazy margin. The term
now runs (`tt_eval_bounded` adds it when the toggle is on, verified: the tree
is byte-identical with Mobility off and moves with it on), so mobility has
never actually been screened and would need a fresh campaign to reject.

### Mobility in the hand eval -- REJECTED at both instruments (VOID, see above)

| | Elo | games |
|---|---|---|
| fixed nodes | -10.60 +/- 14.62 | 2,000 |
| fixed time | **-42.42 +/- 14.35** | 2,000 |

Split across both instruments on purpose, and the split is what makes the
verdict clean. At fixed nodes it is inside noise: **the term does not improve
the evaluation at all**, before any question of cost. At fixed time it loses
clearly, which is the cost arriving on top -- its own, plus the lazy-eval
margin widening from 384 to 1344.

So this is not "a good term that is too expensive", and tightening the bound
would not rescue it. The chess.com console dump that motivated it carries
mobility as its largest positional term; ours reproduces none of that value,
which says the useful thing is in HOW they compute it, not in the fact of
counting reachable squares.

Left in, default off, with this result attached.

### Net v4 -- the loop compounds

First generation trained on data a *net* produced, from an engine that was not
corrupting its own accumulator. 149,986 games played by net v1 at 20,000 nodes,
12.31 M positions, lambda 0.7, best held-out loss at epoch 6 of 24.

| opponent | Elo | games |
|---|---|---|
| net v1, its own teacher | **+93.95 +/- 14.81** | 2,000 |
| hand eval | **+140.01 +/- 16.97** | 2,000 |

About 6.3 and 8.3 sigma, against a null of -2.64 +/- 6.24.

**Phase 4's gate is met.** A net beats the evaluation it was written to
replace, after v0 at -40.13 and v1 at -2.26. More than that, generation N+1
beat generation N, which is the mechanism the whole NNUE phase rested on and
which had never once been demonstrated.

Both numbers are FIXED NODES. The net is 1.37x slower per node, so a
fixed-time result is what decides whether it ships, and these are screens at
+/-15 to +/-17 rather than confirms.

Transitivity does not close: v1 was -2.26 against the hand eval and v4 is
+93.95 against v1, predicting about +92, but v4 measured +140 against the hand
eval. Elo is not transitive across different opponents, so "+140" is
specifically versus the hand eval and not a rating.

Three lambda arms were trained from the same cache. Their evaluations are
progressively flatter as lambda falls -- stdev 390 at 0.7, 287 at 0.3, 227 at
0.15 -- which is the opposite of what the void v2 analysis predicted, since the
game result is a 0/1 signal carrying no magnitude. Only 0.7 has been played;
the others are unmeasured.

### Mobility in the hand eval -- built, NOT screened

`setoption`-free constant in `eval_hand.py`, C toggle default **off**.

The reference implementation's own evaluation, read out of its browser
console, carries mobility
as its largest positional term, larger than king safety. This had none:
material and a king-danger count were the whole eval. Counted per piece and
capped, pawns and kings excluded.

The cap is not an optimisation. It is what gives the term a provable maximum,
and lazy evaluation is sound only while the margin it bails on bounds
everything the cheap half omits. Turning mobility on widens that margin from
384 to 1344, so it bails far less often: **mobility has to pay for weakening a
confirmed +42.88 feature as well as for its own cost.**

The bound is loose. Observed swing over 30 midgames is +/-75 against a provable
maximum of 960, so tightening it is the obvious follow-up if a fixed-time
result comes out close.

Not yet played a game.

### Incremental NNUE accumulator -- infrastructure, no version number

Not an A/B: with no net loaded not a line of it runs, so the default build is
untouched and every pin is unchanged.

| | NNUE nps | vs hand eval |
|---|---:|---|
| full refresh per eval | 448,411 | 2.84× slower |
| incremental | 918,768 | **1.37× slower** |

A 2.07× speedup of the evaluation path. The 2.84× tax was unconditional, so
before this no net could ship on a fixed-time control however well it
evaluated.

Gated by `tt_nnue_acc_matches` against a rebuild -- perft and node pins cannot
see a wrong accumulator, because it changes evaluations rather than the shape
of the tree.

### Killers -- CONFIRMED, default on

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

The search previously had no quiet-move ordering at all -- TT move, MVV-LVA
captures, then generation order -- so this closed the largest single ordering
gap. Fixed-depth node counts fell to 28.0% of the unordered tree over five
setups to depth 5, and `SEARCH_PINS` in `selftest.py` was re-measured with it
on.

The toggle stays so it can be switched off for a future A/B.

### History heuristic -- built, NOT screened: the gate is structurally dead

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
**20,000 nodes the search reaches median depth 4** (range 3-5) -- one full round
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

### Principal variation search -- built, NOT screened: negative at this depth

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
depth 4 at 20,000 nodes** -- one round of four seats. Both are refinements that
assume depth and good ordering.

What limits depth here is the branching factor (~60 legal moves a seat) and the
cost of a node, of which the throwaway eval is 77% -- its king-danger term alone
is 54% of all search time. So the work that buys depth is pruning that engages
at shallow depth (LMP, LMR) and a cheaper eval, not window or ordering
refinements.

### Late move reductions -- CONFIRMED, default on

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

Half a ply of mean depth, and the tail reaches 6-7 where it previously never
passed 5. **LMR is not exact** -- it can miss a line the full search would find --
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

### Lazy evaluation -- CONFIRMED, default on

`setoption name LazyEval`, default **on**. Computes material first and skips
the king-danger term when material alone settles the bound by more than
`4 × 8 × king_danger = 384`, which is the most the danger term can move it.

**Not exact.** Every cutoff decision is identical -- the margin guarantees that
-- but a bail returns the material term rather than the true eval, and fail-soft
propagates that value. Measured node-identical anyway over 80 positions (20
start positions across five setups at depths 4-7, plus 60 random midgame
positions at depth 5), which suggests the difference is always absorbed by the
cutoff cascade that follows. Empirical, not a theorem; `selftest` watches it.

| | search nps at depth 7, classic |
|---|---:|
| off | 1,418,577 |
| on | **1,620,220** (+14.2%) |

**This one must be screened on fixed time, not fixed nodes.** It changes speed
and not the tree, so a fixed-nodes campaign would report exactly zero -- the
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
in **lower** -- as expected, because the confirm ran against the new LMR-on
baseline, and LMR had already bought some of the depth lazy eval was buying.
Confirming against the current default rather than the one a feature was
screened on is the point; the number moving down is not a regression.

`SEARCH_PINS` needed no change: the tree is unmoved, only the clock.

Still larger than +14% NPS would normally buy, and the likely reason is depth
granularity -- at a median of 4-5 plies, 14% more nodes is often the difference
between finishing an iteration and not.

### Late move pruning -- CONFIRMED, default on

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

A full extra ply, and the shallow tail (depth 2-3) disappears entirely. Larger
than LMR's gain (3.73 → 4.25) on the same measurement.

**It is a hard prune**, so it can miss things. Over 84 comparable positions it
missed a mate the full search finds 3 times, and **invented one 0 times** -- the
`legal >= 1` guard is what prevents a node pruning every move and then claiming
checkmate. selftest pins the never-invents property.

| | |
|---|---|
| Instrument | fixed nodes 20,000 |
| Games | 10,000 (2,500 openings × 4) |
| Elo | **+36.09 ± 6.69** |
| Dist | 96, 6, 437, 36, 968, 33, 683, 16, 225 |

Screened +36.62 ± 15.43 over 2,000 first -- the confirm landed almost exactly on
the screen. Released as **v4**.

### Legality fast path from pins -- +3.9% NPS, no A/B needed

Every move used to be made and then checked with a full attack scan on the
king. Pins are now computed once per node from a between/through table, and a
move that cannot expose its own king skips the scan entirely.

The predicate is one-sided: it answers "certainly legal" or "do not know",
never "illegal", so anything it declines falls through to the scan that was
always there. A pin it misses costs speed; only a pin it claims is absent when
present would be a bug.

| build | nps | |
|---|---:|---|
| scan every move | 1,732,406 | 1.000x |
| pins, scan only when unsure | 1,799,525 | **1.039x** |

Small, and worth saying why: computing the pins costs something at every node,
and it buys back only the scans on moves that were going to be legal anyway.
`tt_is_attacked` was 8.2% of a profile, so 3.9% is most of what was available.
This is not the shape of change that pays like the accumulator did.

Verified: perft is exact over all five setups and gates `tt_gen_legal`
directly; 120 full searches compared against a `-DNO_PIN_FASTPATH` build with
identical nodes, scores, best moves and evaluations; and the two answers are
compared on every move of 2,686 positions in selftest.py.

**One mutation of five could not be falsified by sampling.** Removing the
en-passant exclusion produced zero disagreements over 89,819 random positions,
834 of which had an en passant available. The case is real all the same: en
passant empties a square that is not the destination, so king, capturer, victim
and an enemy slider sharing a line means the capture opens it, and the pin scan
stops at our own pawn and never learns the square behind it is about to empty
too. Sampling could not reach it, so the position is constructed and pinned in
selftest.py instead. A test that cannot fail is not evidence, and the honest
response to one is to build the case by hand rather than to record a pass.

### Lazy accumulator perspectives -- +59.8% NPS, and +63.37 Elo for it

**Fixed time, movetime 200, classic, both sides net v5, 19,999 games:
+63.37 +/- 4.68.**

That is the number that says what speed is worth here. Net v5, a whole
generation of self-play, was +7.78 on the same instrument; this one change is
eight times it. The exchange rate is roughly +63 Elo for 1.45x on the machine
it was measured on, so a doubling is worth somewhere near +115 -- higher than
the +50 to +70 a two-player engine would expect, which is worth remembering
before dismissing a 10% speedup as not worth the week.

Measured against the v7 tree, which already had the int8 propagation, so the
int8 and pin gains are NOT in this figure.

The A/B was needed only because fixed time is where speed shows up. At fixed
nodes it would be exactly zero by construction, both sides playing identical
moves; that is a null self-test, not a measurement. The evaluation returns the
same integer either way, so the search takes the same path. Verified rather than asserted -- 120 full
searches over all five setups compared between the two builds, identical nodes,
scores, best moves and evaluations, plus the 14,662-pair accumulator
differential and the deep unwind that selftest.py already ran.

The engine maintained all four perspective accumulators on every make and
unmake. An evaluation reads exactly one of them, `nn_acc[b->turn]`, so three
quarters of that work was paid at nodes that never looked at it.

Toggles are now queued per perspective and applied when that perspective is
read. The queue cancels: pushing the exact inverse of the entry on top pops it
instead, so descending into a subtree and unwinding out again costs no row
arithmetic at all for a perspective nobody evaluated. Cancelling **only** an
exact inverse is what makes it safe -- such a pair is arithmetically a no-op
whatever order it arrived in, so a queue that fails to cancel is slower and
never wrong. That matters because make and unmake are not exact reverses for
castling, where the rook pair arrives in the same order both ways.

M2 Pro, depth 7, builds alternated in one session:

| build | nps | |
|---|---:|---|
| eager, all four perspectives | 1,157,392 | 1.000x |
| lazy, queued per perspective | 1,849,366 | **1.598x** |

**This was found by a sampling profile, and the micro-benchmark had it exactly
backwards.** `bench.py --profile` reported the evaluation at 26.7% of a node
and the accumulator at about 8%, described here as "cheap, not a hidden tax".
Sampling the running search said 3.1% and 23.3%. The micro-benchmark times a
component by running it on one position hundreds of thousands of times, which
kept the same few 512-byte rows of a 2 MB table resident; a real search scatters
across it. Component timings taken in isolation do not merely have wide error
bars, they can invert the ranking. Profile the real thing.

A first attempt to fix it by vectorising the accumulator's widening add was
**4% slower** and was dropped: the compiler already auto-vectorises that loop,
and the cost was never arithmetic. It was memory traffic, and the fix had to be
doing less of it rather than doing it faster.

### N-01, stopping the quiescence loop early -- +10.48 +/- 4.57 at fixed time

Quiescence searches captures only when not in check, but the loop ran to the
end of the generated list, and `pick_move` is a selection scan -- so every
iteration past the last capture cost O(n-i) comparisons to reach a `continue`.
Counting the captures first lets it stop there.

Node-identical, and checked as such on both architectures rather than argued:
85 rows compared between builds on the M2 (five setups at depths 1-7 plus 50
random midgames at depth 6) with nodes, score and best move equal on every one,
and the campaign box independently reporting 625,609 nodes from both builds.

| | |
|---|---|
| NPS, M2 Pro | 1,767,972 -> 1,896,929 (**1.073x**) |
| NPS, campaign box | 669,190 -> 698,367 (**1.044x**) |
| Elo, fixed time, movetime 200, 20,000 games | **+10.48 +/- 4.57** |

**The exchange rate, now measured twice.** 1.044x buying +10.48 implies about
+169 Elo per doubling of search speed; the lazy accumulator's 1.45x buying
+63.37 implied about +115. Both far above the +50 to +70 a two-player engine
expects, which is what a harder branching factor should do -- an extra ply is
worth more here. Treat the rate as somewhere in 115-170 rather than either
number alone, and note that a 4% speedup is worth 10 Elo, which is more than
generation 5 bought for hours of many-core time.

A speedup that cannot change a move still earns its A/B, because the question
was never whether it is correct -- it is what speed is worth.

### SEE pruning in quiescence -- CONFIRMED at both instruments, default on

`setoption name SEEPrune`, default **on**. A capture that loses material once
the exchange settles is skipped rather than given a subtree. Promotions are
exempt: SEE prices the pieces traded and not the piece gained, so it under-rates
them.

| | Elo | games |
|---|---|---|
| fixed nodes, 20,000 | **+16.32 +/- 4.58** | 20,000 |
| fixed time, movetime 200 | **+23.09 +/- 4.56** | 20,000 |

Same net both sides, so the toggle is the only difference.

**Winning by more on the clock is the result worth reading twice.** A feature
that costs time usually shows a smaller number at fixed time than at fixed
nodes -- net v4 lost about 58 Elo that way. This one gains. Pruning a losing
capture removes a whole subtree, so the search is cheaper as well as better
shaped, and SEE's own per-capture cost is more than repaid. The pinned trees
in selftest.py are three to four times smaller from depth 4 on.

Worth setting against the generational loop: net v5, a whole generation of
self-play, is +10.50 at fixed nodes. This toggle is +16.32.

### SEE in move ordering -- NOT screened, and probably will not be

`setoption name SEEOrder`, default **off**. Ordering captures by their settled
value instead of MVV-LVA searched **0.7% more nodes** in the first look, which
was enough to stop before spending an A/B on it. MVV-LVA leads with the most
valuable victim, which is a good cutoff bias even when the capture turns out to
be unsound; sorting by what the exchange actually yields throws that away.

The version worth trying is neither: keep MVV-LVA and use SEE only to sort
losing captures behind the quiets. That is a third thing this toggle does not
do.

### Where net v7's difference actually lives

The whole-book -0.82 is an average over five setups and hides the result. Split
by setup, from the same 20,000 games:

| setup | v7 vs v5 | |
|---|---|---|
| classic | **-23.68 +/- 10.21** | v5, and clears its interval |
| **modern** | **+22.15 +/- 10.81** | **v7, and clears its interval** |
| by | -3.17 +/- 10.63 | level |
| byg | +5.76 +/- 10.72 | level |
| rg | -2.56 +/- 10.71 | level |

Two setups separate, three do not. The earlier guess that v7 gained about +5
evenly across the four non-classic setups was arithmetic, not measurement, and
it was wrong: the gain is +22 on one setup and nothing on three.

**The setup v7 wins is `modern`, which is what chess.com runs.** It has been the
live default since 2022 (§3.5). So on the only variant most games are actually
played in, the net this file rejected twice is 22 Elo ahead of the one the
engine ships with, and the net the engine ships with is ahead only on `classic`,
which is the default here by an old decision and is played by nearly nobody.

That is a question about the goal rather than the measurement. If Tetrarch is
meant to play the game people play, v7 is already the better net and has been
since it was trained. If the target is the number this file has always quoted,
v5 stays. The two answers point in opposite directions and no further A/B
resolves it, because they disagree about what to measure rather than about what
the measurement says.

A per-setup bundle is the third answer and the data supports it: v5 on classic,
v7 on modern, either elsewhere. That is worth about 24 Elo on classic against
shipping v7 alone and about 22 on modern against shipping v5 alone, and
uci.py already knows the setup before it needs an evaluation.

Before building that, the cheaper experiment is more data. v7 saw 25,000 games
per setup against v5's 125,000 on classic. A 250,000-game generation across the
five gives each 50,000 and may beat v5 everywhere at once, which is a better
outcome than shipping two nets and a selector. Generation takes 29 minutes.

### Net v8 -- REJECTED, and it settles the teacher question

Generation 8: identical to generation 7 except the teacher, net **v4** instead
of v5. Same book, same 7,500 nodes, same lambda, 9,037,175 positions against
generation 7's 9,069,344.

| | vs net v5, 20,000 games |
|---|---|
| classic only | **-60.05 +/- 4.66** |
| across the book's five setups | **-32.30 +/- 4.72** |

Two things come out of it.

**v4 is much the worse teacher.** On the matched instrument v7 is -0.82 and v8
is -32.30. A 10.5 Elo gap between the two teachers produced a 31 Elo gap
between their students. Teaching from v5 stands; there is nothing to recover by
reaching back.

**The instrument correction reproduces.** v7 moved +25.04 going from classic to
the book, v8 moved +27.75. Two independent nets, near-identical shift, which is
what a real measurement artefact looks like rather than a lucky single result.

### RETRACTION: the node budget was not cleared

The generation 7 entry claimed the 18,000-node labels were exonerated, because
generation 7 at 7,500 nodes had landed near generation 6's -30.29. That rested
on generation 7 scoring -25.86, which was the mis-measured number.

On its own distribution generation 7 is **-0.82**. So generations 6 and 7 are
about 30 Elo apart, not level, and the two things separating them are the node
budget and the openings. The 18,000-node labels are a live suspect again.

A clean test exists and is cheap now that generation takes 29 minutes: the same
book at 18,000 nodes, teacher v5, screened on the book. One variable against
generation 7.

### The instrument has to match the training distribution

Net v7 measured **-25.86 +/- 4.57** on classic and **-0.82 +/- 4.67** across the
book's five setups. Same two nets, same node budget, same 20,000 games. Only the
positions they were asked to play changed, and the answer moved 25 Elo.

That is the whole gap between "rejected" and "level", and it was created here,
not discovered: v7 was the first net trained across all five setups, and it was
screened on classic alone because classic was what every earlier number used.
Holding the instrument fixed for comparability is right until the training
distribution moves, at which point the fixed instrument is measuring a fifth of
what the net learned.

The arithmetic locates the difference. Classic is a fifth of the book, so for
v5 to be +25.86 on classic and +0.82 over the whole book, v7 must be roughly
**+5 on each of the other four setups**. It traded 26 Elo of classic depth for
about 5 apiece across four setups it had never seen. Across the whole game,
a wash.

**No net before v7 has ever been measured on anything but classic.** v0 through
v6 were all trained on classic alone and screened on classic alone, so nothing
in this file before this entry says how any of them play `modern` -- which is
chess.com's live default and has been since 2022.

### Net v7 -- REJECTED on classic, LEVEL across the five setups

Generation 7: 125,000 games at 7,500 nodes with net v5 teaching, openings drawn
from a 150,000-position balanced book instead of a random walk. 10 epochs at
lambda 0.7, epoch 5 selected. 99.9% of the games are distinct.

**Fixed nodes, 20,000 games each: -25.86 +/- 4.57 on classic, -0.82 +/- 4.67
across the book's five setups.**

The first number was reported here as a rejection and that was wrong -- see the
entry above. v7 is not weaker than v5, it is spread differently: a fifth of its
training data is classic, against v5's whole.

It was also built as a diagnostic, and it worked as one. Generation 6 changed the
teacher and the node budget together and could not be attributed. This one went
back to 7,500 nodes and changed only the openings, and landed within noise of
generation 6:

| generation | teacher | nodes | openings | result |
|---|---|---:|---|---|
| v4 | v1 | 5,000 | random | **+135.19** |
| v5 | v4 | 7,500 | random | **+10.50** |
| v6 | v5 | 18,000 | random | **-30.29** |
| v7 | v5 | 7,500 | book | **-25.86** |

So the 18,000-node labels were not the cause, and the balanced book did not
rescue it. What v6 and v7 share, and v5 does not, is **net v5 as the teacher**.
Two generations reached the same result by different routes -- different node
budgets, different openings, one with SEEPrune on and one without -- which
points at the one thing held constant.

The book is not indicted by this. It fixed a real problem (openings that were
decided before either engine moved) and cost nothing: 9,069,344 positions
against generation 5's 8,737,466 from the same game count. It simply is not
what was wrong.

The cheap next test is to teach from **net v4** rather than v5. If that also
loses, something in the pipeline changed between generation 5 and 6 and the
teacher is innocent; if it wins, v5 is a bad teacher despite being the stronger
net, and the loop can continue from v4. Generation now takes 29 minutes rather
than hours, so this costs about 90 minutes end to end.

Worth stating plainly either way: three consecutive generations have lost, and
one afternoon of search work is +63.37 and +23.09. Self-play in this shape has
stopped paying, and the honest reading is that the loop was already exhausted
at v4 -- v5's +10.50 being the last flicker rather than a continuation.

### Net v6 -- REJECTED, and the loop has reversed

Generation 6: 125,000 self-play games at **18,000** nodes with net v5
teaching, 8 epochs at lambda 0.7, epoch 6 selected on held-out loss.

**Fixed nodes, 20,000, classic, 20,000 games: -30.29 +/- 4.65.**

Six times the error bar the wrong way. Net v5 stays the engine's evaluation.

Retrained from the same cache at lambda 0.85 to test whether the blend rather
than the data was at fault: **-40.52 +/- 4.66**, worse still. Two blends, both
losing, so this is the data and not a training hyperparameter. Those 125,000
games do not produce a better net at any setting tried.

The point of this generation was to raise label quality rather than teacher
strength: every previous one used 5,000 to 7,500 nodes per move, and this
one used 18,000, affordable only because the engine got 2.4x faster the same
day. Deeper labels made the net worse.

**It also reached a lower held-out loss than v5 did and played 30 Elo worse.**
The two losses are not strictly comparable, being measured on different
validation splits, but the direction is the point: the metric the trainer
optimises moved one way and playing strength moved the other. Every run prints
"Held-out loss is NOT Elo" and this is the first time that line has been
load-bearing.

What this does not tell us is which change did it. This generation moved the
teacher (v4 to v5) and the node budget (7,500 to 18,000) together, exactly the
confound flagged when generation 5 did the same thing. Generation 5 got away
with it by winning. This one did not, and the cost of that shortcut is that a
rejection carries no diagnosis.

The trend across the whole phase, all at fixed nodes:

| generation | teacher | nodes | result |
|---|---|---:|---|
| v4 | v1 | 5,000 | **+135.19** vs hand eval |
| v5 | v4 | 7,500 | **+10.50** vs v4 |
| v6 | v5 | 18,000 | **-30.29** vs v5 |

Set against +63.37 for one afternoon of profiling, self-play of this shape has
stopped paying and is now costing. A seventh generation in the same shape is
not obviously worth the machine time.

### Net v5 -- CONFIRMED at both instruments, and the engine now plays with it

Generation 5: 125,000 self-play games at 7,500 nodes with net v4 teaching,
trained 8 epochs at lambda 0.7. Epoch 5 won on held-out loss; epochs 6 to 8
did not improve on it, so the schedule was longer than the data needed.

| | fixed time (movetime 200) | fixed nodes (20,000) |
|---|---|---|
| v5 vs v4 | **+7.78 +/- 4.61** | **+10.50 +/- 4.59** |

20,000 games each. Both clear zero and the two agree, which is what promotion
requires: fixed time is the deciding instrument (see below), and a net winning
only at fixed nodes has not earned the default.

The loop compounds a third time. It also compounds much less: v4 beat the hand
eval by +135.19 at fixed nodes, and v5 beats v4 by +10.50 at the same
instrument. Another generation of the same shape looks like a thin return on
several hours of many-core time, and the honest read is that this approach is
near its ceiling rather than that one more turn of the crank will pay.

**A partial run of this A/B read -2.87 +/- 14.42 over 2,017 games and was
called a null result.** It was not: the full 20,000 games put the answer at
+10.50, comfortably inside that earlier interval. An interval spanning -18 to
+13 says the measurement cannot see the effect, which is not the same as the
effect being absent. Stopping an A/B early and reporting the point estimate is
how a real gain gets discarded.

### NNUE propagation in int8 SIMD -- +23.5% NPS, no A/B needed

Not an Elo measurement, and deliberately so. The evaluation returns the same
integer it always did, so the search takes the same path, visits the same nodes
and picks the same moves. `SEARCH_PINS` in selftest.py pins those node counts
and is unmoved. A change that cannot alter a decision does not need 2000 games
to authorise it; it needs a proof that it alters nothing, and a stopwatch.

The proof: `nn_crelu` clamps activations to [0,127] and the extras to
[-127,127], so every input to layers 2-4 already fits in int8. The products are
exact in int32 -- the widest partial sum is 263 x 127 x 127, three orders below
overflow -- and integer addition is associative, so the order the SIMD lanes
accumulate in cannot change the total. Bit-identical by construction rather
than by measurement, though it was measured too: 400 positions across all five
setups, zero mismatches, against both the vector build and `-DNN_SCALAR`.

The stopwatch, M2 Pro, depth 7, three builds alternated in one session so that
background load fell on all of them equally:

| build | nps | |
|---|---:|---|
| original, int32 arrays | 796,941 | 1.000x |
| int8 arrays, scalar dot | 884,978 | 1.110x |
| int8 arrays, SDOT | 984,357 | **1.235x** |

Worth reading that middle row before reaching for intrinsics elsewhere. Half
the gain is just narrowing the arrays from int32 to int8, which lets the
compiler vectorise the ordinary loop by itself. The hand-written SDOT adds
1.113x on top of that, not the whole 1.235x.

Per call the evaluation went from 547.6 ns to 157.4 ns, a 3.5x speedup, which
moves it from the largest identified component of a node to one of the
smallest. What that leaves behind is movegen and whatever is in the residual;
see the caveat in bench.py before trusting the residual's size.

**Measure builds by alternating them.** The first attempt compared a fresh
build against a number taken twenty minutes earlier and made this look like
+20%; the machine's load average had gone from 3.81 to 8.24 in between.
`TETRARCH_LIB` exists so that comparison can be interleaved instead.

### Repetition detection -- built, NOT a measured gain, default on

`setoption name Repetitions`, default **on**. A position the search is walking
into that has already occurred scores 0 instead of being evaluated as if it
were new.

Before this the search had no notion of a repetition at all. A side that was
winning could shuffle and read every repeat as a fresh position, and a side
that was losing could not steer into the draw the rule pays out on (§10.2).

Two things bound the scan. The last irreversible move, which `halfmove` counts:
nothing before a capture or a pawn advance can be repeated after it. And the
seat cycle: the side to move is part of the key, so only a position four plies
back can match, which the scan strides over. A dead seat shortens that cycle,
so with any seat dead the stride drops to one ply.

It scores the FIRST repeat rather than the third. Threefold is what the rule
pays out on, but a side that can repeat once can nearly always repeat again,
and learning the same thing the honest way costs two more plies of shuffling.

**Fixed nodes, 20k, classic, 2000 games, hand eval: -13.38 +/- 14.42.**
A later fixed-time screen, also on the hand eval, gave -8.37 +/- 26.68 over
552 games.

Both were run without `--net`, which match.py reads as `Net=none` rather than
as "use the engine's default" -- so these measured the search with the hand
evaluation, not with the net the engine plays. That is sound for this feature,
which is search-level and cannot care which evaluation is behind it, but the
instrument should be stated rather than assumed.

That interval spans zero, so this is not a measured improvement -- and the
point estimate being negative is not evidence of harm either. The reason is
visible in the same run: 98.45% of those games ended in checkmate, 1.45% were
adjudicated, and **not one reached the fifty-move rule**. Repetitions barely
arise at 20k nodes from random openings, so the instrument has almost nothing
to measure. It is kept on because scoring a draw as a draw is a rules gap
rather than a tuning choice, not because the harness endorsed it.

A fixed-time run is the one that would catch the scan's cost; it is not done.

### Quiescence check evasions -- CONFIRMED, default on

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

I predicted this might be *negative* -- it costs depth and wins nothing on node
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
overperformed. All three were misjudged in the same direction -- by assuming
chess proportions.

---

### FFA repetition detection -- KEPT ON NULL

| | |
|---|---|
| Instrument | fixed time, movetime 100 |
| Elo | **+0.96 +/- 3.78** |
| Games | 10,000 (2,500 complete rotations) |
| Dist | 2, 14, 43, 106, 222, 481, 767, 437, 262, 110, 37, 16, 3 |
| Placement | 2550/2463/2461/2526 |
| Per setup | by +7.90, modern +3.04, byg -1.00, classic -1.88, rg -3.33, all +/- ~8 |

`--opt-b Repetitions=false`, both sides the hand eval. Worth nothing at 0.25
sigma, and no setup moves. Kept on anyway, and for the reason the Teams version
was: without it the search cannot score a draw it is walking into, which is a
rules gap rather than a tuning choice. The toggle exists so the claim could be
measured like any other, and now it has been.

Note what this is NOT: a null. The toggle changes the tree -- 110,732 nodes
against 150,203 on a constructed eight-ply cycle -- so the two engines really
do differ. What it does show is that the FFA fixed-time harness carries no seat
bias at this scale: A placed 2550/2463/2461/2526 against 2,500 expected.

### Teams null self-test at fixed time, after the movetime fix -- PASSES

| | |
|---|---|
| Instrument | fixed time, movetime 200, classic, Teams |
| Elo | **-0.00 +/- 14.24** |
| Games | 2,000 (500 complete rotations), score 1000.00 |
| Dist | 23, 3, 116, 6, 205, 9, 109, 4, 25 |

Run because enforcing movetime mid-depth changed Teams play at fixed time --
Teams now spends about 0.87x of its budget where it spent all of it. Every
Teams fixed-time number in this file was taken before that, so the instrument
had to be re-certified rather than assumed intact. It is.

### FFA transposition table -- CONFIRMED, default on

| | |
|---|---|
| Instrument | fixed nodes 20,000 |
| Elo | **+15.90 +/- 7.28** |
| Games | 2,500 (625 complete rotations) |
| Dist | 1, 1, 4, 21, 50, 85, 211, 133, 70, 28, 14, 7, 0 |
| Placement | 718/599/573/610 |

`--opt-b FFATT=false` against the default, both sides the hand eval. The
paranoid search had no table at all until this; the Teams search has always had
one unconditionally, and the toggle exists so this could be measured rather
than because off is a configuration anyone should use.

Worth 1.95x on what the engine actually does -- iterative deepening to depth 7
falls from 47,367,089 nodes to 24,268,215 -- and 2.2 sigma of Elo at fixed
nodes. Both numbers matter: the node reduction is why an FFA A/B is affordable
at all, and the Elo is why it stays on.

### DEVIATION: the FFA confirm runs at movetime 100, not 200

Declared before the run, not after it.

The generation-1 numbers below were taken at movetime 200. The confirm --
10,000 games, ~2,000 per setup -- runs at **movetime 100** and therefore
cannot be pooled with them. It stands alone.

The reason is cost. 10,000 FFA games at movetime 200 is 4.5 hours on the box
that produced these numbers, because an FFA game is ~208 plies and that box
spends its whole budget per ply. Halving the clock roughly halves that.

What makes the trade cheap is the same thing that makes FFA expensive: a ply
costs about 17x the one before it, so the search depth is quantised and a
shorter clock buys almost nothing back. Measured on the reference Mac at
movetime 200 / 100 / 50 / 25, mean depth reached was 4.15 / 4.00 / 4.00 / 3.80.
Halving the clock costs 0.15 of a ply. In Teams, where a ply is 3.5x, the same
cut would cost real depth and this deviation would not be available.

That table is from a machine where the engine spends only 51ms of a 200ms
budget. The generation box spends all of it, so the curve is shifted there and
movetime 200 may genuinely reach deeper than 100 does. The deviation is
recorded rather than assumed harmless.

### FFA generation 1 -- CONFIRMED, +106.05 +/- 5.55

The first FFA self-play dataset and the first net trained on it. Recorded now
because the runs have ended; the Elo has not been measured yet and this entry
is not a result.

| | |
|---|---|
| Games | 50,000, `--mode ffa --nodes 7500` |
| Teacher | the throwaway hand eval -- no FFA net existed to label with |
| Positions | 13,222,513 (264 a game) |
| Training | 8 epochs, otherwise the defaults (lambda 0.7, batch 1024, val 0.02) |
| Net | `nets/ffa1/net-best.nnue` |

**FFA gets a quarter of the rows per position that Teams does.** The four-view
augmentation rests on a Teams score negating to the other team; a paranoid
score is in the MOVER's terms and converts to no other seat, so only the seat
that moved carries a label. Raw throughput is the same -- 164 positions per
core-second against Teams' 162, measured on 16 games at 7,500 nodes on an M2
Pro -- and the whole difference lands at cache time.

**Validation bottomed at epoch 2 and got worse for six consecutive epochs.**
`net-best.nnue` is byte-identical to `net-epoch02.nnue`, confirmed by md5, so
epochs 3 through 8 each moved away from it. 13.2M unaugmented rows is about a
third of what a Teams generation feeds the same net, and it overfits by epoch
3. **The next FFA generation should use `--epochs 3`**; 8 was six too many.

The per-epoch validation table was lost with the SSH session that owned the
run. The checkpoint md5s are the surviving evidence, and they are enough to
say which epoch won but not by how much.

**CONFIRM. net-ffa1 against the hand eval, movetime 100, 10,000 games** on the
committed 20,000-position FFA book, with movetime enforced mid-depth:

```
Elo   | +106.05 +/- 5.55   (2,500 complete rotations)
Dist  | 0, 3, 21, 68, 121, 176, 334, 360, 421, 400, 304, 210, 82
A pl. | 4349/2392/1607/1652  (1st/2nd/3rd/4th; 2,500 each if level)
Pts   | A 53.6 mean, B 39.5 per seat
Games | 255.3 plies mean, 66.9% last-seat-standing
Time  | 10m57s at 15.21 games/s on 96 cores
```

Per setup, ~2,000 games each:

```
classic  +129.09 +/- 12.35
rg       +109.04 +/- 12.90
by       +102.73 +/- 12.45
byg       +95.39 +/- 12.46
modern    +94.33 +/- 11.84
```

Strongest where it trained and 8-10 sigma everywhere, with 35 Elo between best
and worst. One net covers all five FFA setups; the Teams split that produced
the two-net bundle does not repeat here.

The run took 11 minutes where the same 10,000 games had been projected at four
hours. That is the mid-depth movetime fix, not the hardware: 15.21 games/s
against 0.7.

**Earlier, at fixed nodes 20,000 over 2,500 games: +67.35 +/- 10.38.** Two
different instruments, both confirms, and the fixed-time number is the larger
one -- the reverse of net v4 in Teams. Offered without an explanation: when the
clock genuinely binds, both engines are cut off mid-depth, and a hypothesis
that the net's evaluation is worth more per node under that cut is a hypothesis
and not a measurement.

**Superseded detail below.** Against it, on a 96-core box with a
book of 20,000 FFA positions:

```
              fixed nodes 20,000        fixed time 200ms
Elo         | +67.35 +/- 10.38        | +18.30 +/- 14.88
Games       | 2,500                   | 1,571
A placed    | 899/640/490/471         | 486/339/340/406
Points      | A 50.3, B 40.5          | A 39.7, B 39.1
Plies       | 250.8 mean              | 208.2 mean
Decisive    | 67.9%                   | 88.9%
```

Fixed nodes is a clear confirm at 6.5 sigma.

**The fixed-time column is VOID.** It was taken before movetime was enforced
mid-depth: the budget was checked only between iterative-deepening iterations,
and on the generation box a single FFA iteration cost about 660ms, so every
move ran to the same depth whatever the clock said. movetime 200 and movetime
100 produced identical searches there -- halving the clock changed wall time by
13%. A fixed-time instrument that always reaches the same depth is a fixed
DEPTH instrument wearing the wrong label, and +18.30 is not a fixed-time
number. Neither is the -26.60 above it.

Two readings taken through it, for the record and not to be quoted: +18.30
+/- 14.88 at movetime 200 over 1,571 games, and +75.71 +/- 17.21 at movetime
100 over 1,066. They differ by 2.5 sigma on the same engines and the same book,
which is itself the evidence that the instrument was not measuring a clock. Net v4 in Teams showed the same shape (+135.19 fixed
nodes against +76.79 fixed time) and cleared both. This one clears one.

**The fixed-time number moved 45 Elo when the time management was fixed, and
that is the more useful finding.** An earlier run of the same match read
**-26.60 +/- 17.44** over 1,099 games. That log has been discarded -- it is a
measurement taken through a broken instrument, and keeping it invites someone
to quote it. The number is recorded here only to size the effect of the fix. Nothing about either engine changed --
only FFA_NEXT_DEPTH_FRACTION, from the Teams value of 0.45 to 0.06. At 0.45 the
FFA search started plies costing 17x the time it had left, and the CHEAPER
engine overshot further because it reached more depths before the check. The
hand eval was quietly taking about 50% more thinking time per move than the
net. A fixed-time instrument that is not actually fixed measures the wrong
engine, and it read the sign backwards.

**Earlier partial screen, terminated at 229 games on throughput.** Fixed nodes
20,000, against the hand eval:

```
Elo   | +34.45 +/- 42.18   (43 complete rotations)
Dist  | 0, 0, 1, 2, 8, 7, 2, 6, 7, 5, 2, 3, 0
A pl. | 67/62/50/50  (1st/2nd/3rd/4th)
Pts   | A 46.2 mean, B 40.9 per seat
Games | 227.95 plies mean, 66.8% last-seat-standing
```

Inside noise at 0.8 sigma, so **not a result** -- but it is not a kill either,
and that is the thing worth knowing. The same screen run on net-v5, a Teams net
handed to FFA, read -124.50 +/- 49.31. This one leans positive on every
secondary reading: more firsts than the 57 a level engine expects, and 5.3
points a seat more than the hand eval. The net is not broken; it needs a real
sample.

The run was terminated because the harness, not the engine, is the bottleneck:
`uci.py` rebuilt the position from move one on every `position ... moves`, so a
300-ply FFA game costs O(n^2) in the Python movegen. Measured per engine move:
0.026s at ply 0, 0.160s at ply 100, 0.386s at ply 300. That is ~60 core-seconds
a game of pure replay against ~3 of actual search, and it is why 229 games took
an hour on a large box. Fixing that comes before the real screen.

Fixed nodes first as a kill filter -- an order-of-magnitude cheaper here, because FFA games
run ~300 plies and fixed time pays for every one of them -- and fixed time to
decide, since a net costs more per node than the hand eval and fixed nodes
hands it an advantage it will not have in a real game.

## A property of the harness worth knowing

Two identical `match.py` invocations return **byte-identical results** -- same
score, same distribution. Openings are seeded from `--seed` (default 1) and the
engines are deterministic at fixed nodes, so the same command replays the same
games.

That is good for reproducing a verdict and useless for adding confidence.
**Re-running is not an independent sample; change `--seed`.**
