# Changelog

One entry per release. A release is one **confirmed Elo gain** — measured over
10,000 games against a certified null, not a commit and not a dormant toggle.
See [`docs/RELEASING.md`](docs/RELEASING.md) for the rules and
[`docs/AB.md`](docs/AB.md) for the full campaign record including rejections.

Distribution shown is the nine-bucket seat rotation (score sums 0, 0.5, 1 … 4),
never a pentanomial — one opening here is four games, not two.

## v5 — quiescence check evasions

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

## v4 — late move pruning

**+36.09 ± 6.69** · 10,000 games · fixed nodes 20,000 · Dist 96, 6, 437, 36,
968, 33, 683, 16, 225

Drops the quiet tail outright once `4 + depth²` quiet moves have been tried
without a cutoff. Mean depth at the instrument 4.25 → 5.25, a full extra ply.

- `legal >= 1` guard: without it a node could prune every move and report a
  checkmate that is not there. 0 invented mates over 84 positions.
- `match.py` now suffixes a reused `--log` name instead of refusing to start.

## v3 — lazy evaluation

**+42.88 ± 6.50** · 10,000 games · **fixed time** `movetime 200` · Dist 61, 13,
428, 44, 953, 46, 719, 17, 219

Skips the king-danger term when material alone settles the bound. That term was
54% of all search time. NPS 1,418,577 → 1,620,220 (+14.2%).

- Measured on fixed **time**: it changes speed and not the tree, so a
  fixed-nodes campaign would have reported exactly zero.

## v2 — late move reductions

**+35.07 ± 6.51** · 10,000 games · fixed nodes 20,000 · Dist 88, 12, 415, 39,
1002, 45, 681, 16, 202

Quiet moves tried late are searched shallower with a null window, at full depth
only if one raises alpha. Mean depth 3.73 → 4.25.

- Rejected in the same window, kept dormant with their verdicts: **history
  heuristic** (dead at the depths this engine reaches) and **PVS** (actively
  negative — 118.9% of baseline nodes at depth 4).

## v1 — killer moves

**+50.42 ± 6.41** · 10,000 games · fixed nodes 20,000 · Dist 57, 8, 388, 31,
969, 47, 763, 21, 216

The search had no quiet-move ordering at all. Two killers per ply closed the
largest single ordering gap.

- First result on a harness whose null self-test passes. Two harness bugs had
  to be fixed first: a seat rotation that cancelled nothing (+36.26 Elo of
  phantom advantage) and a null test that collapsed both sides into one
  process.

## Before v1

Phases 0–3: the rules pinned against sources, a 14×14 board with two
independently written move generators agreeing over 10 M positions, perft exact
against Athena to depth 7, a C core, alpha-beta with a transposition table and
quiescence, PGN4, the viewer, and the match runner.

Net v0 was trained on 3.9 M self-play positions and **rejected** at
−40.13 ± 7.01. Phase 4's gate is still open.
