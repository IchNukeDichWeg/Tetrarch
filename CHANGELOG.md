# Changelog

One entry per release. A release is one **confirmed Elo gain** -- measured over
10,000 games against a certified null, not a commit and not a dormant toggle.
See [`docs/RELEASING.md`](docs/RELEASING.md) for the rules and
[`docs/AB.md`](docs/AB.md) for the full campaign record including rejections.

Distribution shown is the nine-bucket seat rotation (score sums 0, 0.5, 1 … 4),
never a pentanomial -- one opening here is four games, not two.

## v8 -- FFA

Free-for-all is playable. It was parsed, generated and perft-tested from the
start, but the search refused it: FFA is not zero-sum between any two seats, so
negamax does not apply. The C core now searches it paranoid -- every node scored
in the ROOT seat's terms, maximised at its own nodes and minimised at the other
three -- alongside the untouched Teams loop.

The engine plays FFA with **net-ffa1**, the first net trained on FFA self-play:
50,000 games at 7,500 nodes with the hand eval teaching, 13,222,513 positions,
epoch 2 of 8 selected on held-out loss.

```
Elo   | +106.05 +/- 5.55 (95%)
SPRT  | none -- a fixed-budget confirm, not a sequential test
Conf  | FIXED TIME movetime 100, hash 16, workers 96, book of 20,000 FFA
      | positions across all five setups, mode FFA
Games | 10,000 (2,500 openings x 4 rotations)   score 6,480.50 (64.8%)
Dist  | 0, 3, 21, 68, 121, 176, 334, 360, 421, 400, 304, 210, 82
      | THIRTEEN buckets -- an FFA rotation sums in thirds, not halves
Null  | NOT RUN at this instrument. Owed, and named here rather than implied.
Base  | the hand evaluation. There is no previous FFA version; v7 refused to
      | search the mode at all.
Bench | 93,846,865 nodes perft, 597,903 search
Pins  | unchanged on all five setups -- the Teams tree did not move
```

Per setup, ~2,000 games each: classic **+129.09 +/- 12.35**, rg **+109.04 +/-
12.90**, by **+102.73 +/- 12.45**, byg **+95.39 +/- 12.46**, modern **+94.33
+/- 11.84**. Strongest where it trained, 8-10 sigma everywhere, 35 Elo between
best and worst -- so one net serves all five and the two-net split the Teams
bundle needs does not repeat.

At fixed nodes 20,000 over 2,500 games it read **+67.35 +/- 10.38**. Two
instruments, both confirms, and the fixed-time number is the larger -- the
reverse of net v4 in Teams. No explanation is offered for that.

### What else changed

- **A transposition table for the paranoid search**, CONFIRMED at **+15.90 +/-
  7.28** over 2,500 games at fixed nodes. FFA had none; iterative deepening to
  depth 7 falls from 47.4M nodes to 24.3M with it.
- **movetime is enforced mid-depth.** The budget was checked only between
  iterative-deepening iterations, and one FFA ply costs ~17x the last, so a
  single iteration overran a 200ms budget by an order of magnitude on a slow
  machine. A fixed-time instrument that always reaches the same depth is a
  fixed-DEPTH instrument wearing the wrong label. The next iteration now gets a
  node cap of (observed nps x time remaining). Teams stays inside budget and
  its node counts are unchanged.
- **The FFA rules model**, all of RULES.md section 8: capture points, the +20
  for an elimination, and the multi-check bonuses. FFA is a points race and
  nothing was awarding points, so a game had no result at all.
- **Repetition detection, elimination and the game loop** in FFA, and `match.py`,
  `gen_data.py`, `book.py` and the net bundle all understand the mode.
- **uci.py replays only the new moves** of a `position` command. Rebuilding from
  move one is O(n^2) over a game -- invisible in Teams at ~100 plies, 3.9x on an
  FFA match at ~250.

### Rejected or void in this window

- **The FFA fixed-time readings before the movetime fix are VOID**: +18.30 +/-
  14.88 at movetime 200 and +75.71 +/- 17.21 at movetime 100, same engines and
  same book, 2.5 sigma apart. A clock that moves the answer 57 Elo when halved
  was not measuring a clock. Both are kept in docs/AB.md as the evidence.
- **net-v5 in FFA**, a Teams net handed to the mode, screened at **-124.50 +/-
  49.31**. FFA falls back to the hand eval rather than a Teams net, and says so.

### Known limits

- No null self-test at movetime 100 in FFA. The confirm is 19 sigma and a null
  will not move it, but it is owed.
- net-ffa1 trained on `classic` self-play alone. It generalises -- 35 Elo across
  the five -- but generation 2 should use `--setup all`.
- FFA training data gets no perspective augmentation: a paranoid score is in the
  mover's terms and does not convert to another seat, so a game yields a quarter
  the rows a Teams game does.
- Validation bottomed at epoch 2 and worsened for six consecutive epochs. The
  next FFA generation wants `--epochs 3`, not 8.
- Source release: `tetrarch-v8.tar.gz` is the committed tree at the tag,
  extracted and selftested before it was attached. No compiled binary --
  the C core is built per machine by `setup.sh`.

## v7 -- net v5

The engine plays with net v5. Generation 5: 125,000 self-play games at 7,500
nodes with net v4 teaching, 8 epochs at lambda 0.7, epoch 5 selected on
held-out loss.

Against net v4, 20,000 games at each instrument: **+7.78 +/- 4.61** at fixed
time and **+10.50 +/- 4.59** at fixed nodes. Both clear zero and agree.

A third compounding generation, and a much smaller step than the ones before
it -- net v4 had beaten the hand eval by +135.19 at fixed nodes. The returns
on repeating this loop unchanged look thin.

The engine also searches about 2.4x more nodes per second than v6 did, with
every move provably unchanged. Three changes, none of which can alter a
decision:

- Lazy accumulator perspectives, +59.8% NPS. Screened at fixed time over
  19,999 games: **+63.37 +/- 4.68**, eight times what net v5 was worth on the
  same instrument.
- The NNUE propagation as int8 SIMD, +23.5% NPS, bit-identical output.
- Pins computed once per node, so a move that cannot expose its own king skips
  the attack scan, +3.9% NPS.

And a rules gap closed: a repetition the search is walking into scores as the
draw it is.

## v6 -- NNUE evaluation

**+76.79 +/- 6.87** at fixed time and **+135.19 +/- 7.36** at fixed nodes,
10,000 games each. The first release to change the evaluation rather than the
search, and the first since v0 where a fresh clone plays differently.

Net v4 was trained on 12.31 M positions from 149,986 games that net v1 played.
Generation N+1 beating generation N is the mechanism the whole NNUE phase
rested on, and it had never once been demonstrated before this.

- The fixed-time instrument was certified with its own null first: -10.77 +/-
  13.96, inside noise. Every earlier fixed-time result rested on an instrument
  that had never been checked.
- Lambda closed at 0.7: the 0.30 and 0.15 arms lose by 94.51 and 124.60.
- Mobility in the hand eval rejected, at both instruments.
- `uci.py` loads `nets/net-v4.nnue` unless told otherwise; `Net=none` still
  selects the hand eval.

## v5 -- quiescence check evasions

**+106.78 ± 6.88** · 10,000 games · fixed nodes 20,000 · Dist 28, 6, 224, 30,
832, 40, 895, 22, 423

When the side to move is in check, quiescence searches every legal move instead
of standing pat, and reports mate when there are none. Previously quiescence
could not see a mate at all.

The largest single gain in the engine, on the one feature predicted to lose:
it costs nodes and wins nothing on node count. That prediction came from
two-player chess, where checks are rarer. A seat here can be checked by three
opponents.

- `search.py`'s reference quiescence mirrors it, so the minimax oracle still
  proves a theorem about the search that actually ships.
- `SEARCH_PINS` re-measured; classic depth 5 7,449 → 7,669.

## v4 -- late move pruning

**+36.09 ± 6.69** · 10,000 games · fixed nodes 20,000 · Dist 96, 6, 437, 36,
968, 33, 683, 16, 225

Drops the quiet tail outright once `4 + depth²` quiet moves have been tried
without a cutoff. Mean depth at the instrument 4.25 → 5.25, a full extra ply.

- `legal >= 1` guard: without it a node could prune every move and report a
  checkmate that is not there. 0 invented mates over 84 positions.
- `match.py` now suffixes a reused `--log` name instead of refusing to start.

## v3 -- lazy evaluation

**+42.88 ± 6.50** · 10,000 games · **fixed time** `movetime 200` · Dist 61, 13,
428, 44, 953, 46, 719, 17, 219

Skips the king-danger term when material alone settles the bound. That term was
54% of all search time. NPS 1,418,577 → 1,620,220 (+14.2%).

- Measured on fixed **time**: it changes speed and not the tree, so a
  fixed-nodes campaign would have reported exactly zero.

## v2 -- late move reductions

**+35.07 ± 6.51** · 10,000 games · fixed nodes 20,000 · Dist 88, 12, 415, 39,
1002, 45, 681, 16, 202

Quiet moves tried late are searched shallower with a null window, at full depth
only if one raises alpha. Mean depth 3.73 → 4.25.

- Rejected in the same window, kept dormant with their verdicts: **history
  heuristic** (dead at the depths this engine reaches) and **PVS** (actively
  negative -- 118.9% of baseline nodes at depth 4).

## v1 -- killer moves

**+50.42 ± 6.41** · 10,000 games · fixed nodes 20,000 · Dist 57, 8, 388, 31,
969, 47, 763, 21, 216

The search had no quiet-move ordering at all. Two killers per ply closed the
largest single ordering gap.

- First result on a harness whose null self-test passes. Two harness bugs had
  to be fixed first: a seat rotation that cancelled nothing (+36.26 Elo of
  phantom advantage) and a null test that collapsed both sides into one
  process.

## Before v1

Phases 0-3: the rules pinned against sources, a 14×14 board with two
independently written move generators agreeing over 10 M positions, perft exact
against Athena to depth 7, a C core, alpha-beta with a transposition table and
quiescence, PGN4, the viewer, and the match runner.

Net v0 was trained on 3.9 M self-play positions and **rejected** at
−40.13 ± 7.01. Phase 4's gate is still open.
