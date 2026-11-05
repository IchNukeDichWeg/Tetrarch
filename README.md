# Tetrarch

A four-player chess engine built to play the strongest 4PC it can -- 14x14,
four seats, Teams and FFA.

[![release](https://img.shields.io/github/v/release/IchNukeDichWeg/Tetrarch?label=release)](https://github.com/IchNukeDichWeg/Tetrarch/releases)
[![license](https://img.shields.io/github/license/IchNukeDichWeg/Tetrarch)](LICENSE)

Python owns the root -- iterative deepening, time management, protocol, tooling.
A C shared library owns the per-node loop -- board, movegen, ordering, transposition
table, pruning, quiescence, NNUE inference. A pure-Python reference engine is the
source of truth, and `selftest.py` asserts the C core agrees with it bit for bit.

## Quick start

```bash
git clone https://github.com/IchNukeDichWeg/Tetrarch.git
cd Tetrarch
./setup.sh
```

That is the whole of a fresh-machine setup: it installs a compiler and numpy/flask
if they are missing, builds the C core, and then runs the full test ladder -- a setup
that reports success and leaves a broken build costs a whole campaign to discover.
numpy is required. flask is only the GUI's: if it will not install, setup warns,
the engine and the ladder carry on without it, and the GUI checks are skipped.

No virtualenv. Everything is a plain `python3 something.py`.

```bash
python3 uci.py                 # the engine, speaking the protocol
python3 selftest.py            # the full ladder: 750 checks, ~20s on an 18-core M5 Pro
python3 gui/app.py             # viewer + play, http://127.0.0.1:7442
```

Two pages. `/` replays a PGN4 you paste -- `gui/viewer.html` also opens straight
off disk with no server at all. `/play` is an interactive board: set each of the
four seats to Human or Engine and play, in Teams or free-for-all -- the engine
plays both, FFA since v8. Both pages pick the net the way the engine does, by
mode and setup (`nets/bundle.txt`).

## Strength

Every release below is confirmed over at least 10,000 games with a full four-game
seat rotation, against a null self-test on the same harness. The fixed-nodes
harness is certified at −0.31 ± 6.58 after an orientation fix; the fixed-time
harness at −3.56 ± 9.91 in Teams and −3.62 ± 7.61 in FFA
([`docs/AB.md`](docs/AB.md), Harness validity).

**v9 against v8**, fixed time, 10,000 games each: **Teams +300.58 ± 9.39**
(movetime 200) and **FFA +168.75 ± 5.47** (movetime 100). Rows below that list
two instruments give fixed nodes / fixed time.

| release | feature | Elo | instrument |
|---|---|---|---|
| [v9](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v9) | reverse futility pruning | **+133.95 ± 7.00** / **+155.54 ± 11.10** | both |
| [v9](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v9) | FFA late move pruning | **+121.23 ± 5.36** / **+104.33 ± 7.58** | both |
| [v9](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v9) | FFA late move reductions | **+46.67 ± 4.99** / **+14.28 ± 7.83** | both |
| [v9](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v9) | null-move pruning | **+20.28 ± 6.54** / **+27.33 ± 10.09** | both |
| [v9](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v9) | SEE king-recapture fix | **+17.70 ± 6.41** / **+12.77 ± 10.26** | both |
| [v8](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v8) | FFA: paranoid search + net-ffa1 | **+106.05 ± 5.55** | fixed time |
| [v8](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v8) | FFA transposition table | **+15.90 ± 7.28** | fixed nodes |
| [v7](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v7) | lazy accumulator perspectives | **+63.37 ± 4.68** | fixed time |
| [v7](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v7) | NNUE net v5 | **+7.78 ± 4.61** | fixed time |
| [v6](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v6) | NNUE evaluation (net v4) | **+76.79 ± 6.87** | fixed time |
| [v5](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v5) | quiescence check evasions | **+106.78 ± 6.88** | fixed nodes |
| [v4](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v4) | late move pruning | +36.09 ± 6.69 | fixed nodes |
| [v3](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v3) | lazy evaluation | +42.88 ± 6.50 | fixed time |
| [v2](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v2) | late move reductions | +35.07 ± 6.51 | fixed nodes |
| [v1](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v1) | killer moves | +50.42 ± 6.41 | fixed nodes |

Since v0 the search tree at classic depth 5 has gone from 228,628 nodes to 3,227.
Mean depth at a 20,000-node budget went from 3.73 to 5.25 before v9 and has not been
re-measured since. v9 also made the core +70% faster on x86 (2x EPYC 7443) and
+18% on Apple (M5 Pro), node for node identical.

The largest gain is the one that was predicted to be negative. Chess intuition
about a feature's value does not survive the move to four seats -- see
[`docs/AB.md`](docs/AB.md).

Rejections are recorded too -- see [`docs/AB.md`](docs/AB.md). Two features were
closed without spending a single game because a cheap measurement showed their
gate was structurally dead.

## The game

4PC has five starting setups -- `classic`, `modern`, `by`, `byg`, `rg` --
differing only in each seat's king/queen placement. All five are supported.
**Tetrarch defaults to `classic`**, where the theory and the strong opposition are;
`modern` has been the live default since 2022.

[`docs/RULES.md`](docs/RULES.md) is the normative rules reference. Every rule carries
its source; anything unconfirmed is an explicit `ASSUMPTION:` with the cheapest
experiment that would settle it. Read it before touching engine code -- four-player
en passant, per-seat castling and dead-seat scoring are all places where the obvious
guess is wrong.

Move generation is verified two ways: two independently written generators agreeing
over 10,000,000 random positions, and perft exact against
[Athena](https://github.com/arianahejazyan/Athena) to depth 7 (1,735,784,286 nodes).

## Documentation

| | |
|---|---|
| [`docs/RULES.md`](docs/RULES.md) | the normative rules, with sources and assumptions |
| [`docs/AB.md`](docs/AB.md) | every measured result, rejection and harness bug |
| [`docs/PERFT.md`](docs/PERFT.md) | pinned node counts and the reference machine |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | how the protocol diverges from UCI, and why |
| [`docs/RELEASING.md`](docs/RELEASING.md) | what earns a version number |
| [`CHANGELOG.md`](CHANGELOG.md) | one entry per confirmed gain |

## Development

```bash
make            # build the C core
make test       # the selftest ladder
make quick      # build + the movegen/C-core sections only: the inner loop, not the gate
make bench      # the bench signature
make dist       # source tarball
```

`selftest.py` runs before every commit, including pure-Python ones. It pins perft,
cross-checks both move generators, asserts the C core and the Python reference agree
on eval and Zobrist keys bit for bit, and checks that every search toggle sits at
the default its A/B gave it -- a toggle that silently flipped would make every
later A/B a comparison against something nobody measured.

Measuring a change:

```bash
python3 match.py 500 --log screen.jsonl --opt-a LMP=true --nodes 20000 --workers 0
```

The positional argument is **opening positions, not games** -- each is played four
times as a full seat rotation, because seat identity is worth more Elo than most
engine changes. Results are reported as Elo with its error margin plus the
nine-bucket rotation distribution, never a bare Elo and never a pentanomial.

## Layout

```
uci.py              the protocol            match.py     engine-vs-engine, seat rotation
selftest.py         the test ladder         bench.py     node/nps benchmark
gen_data.py         self-play positions     train.py     NNUE trainer

tetrarch/           board, movegen x2, search root, eval, NNUE, PGN4, C binding
src/c/tetrarch.c    board, movegen, perft, eval, transposition table, search
nets/               quantised nets the engine loads
gui/                viewer.html (standalone), play.html, app.py (the engine behind both)
docs/               rules, results, perft, protocol, releasing
```

## Status

**Phase 4 is done.** Net v5 is the engine's evaluation as of v7 on `classic`, and
net v7 on the other four setups, where it measured level or better
(`nets/bundle.txt`). Net v5 beat net v4 by
**+7.78 ± 4.61** on a clock and **+10.50 ± 4.59** at fixed nodes over 20,000 games
each. Net v4, which it replaced, had beaten the hand eval by
**+76.79 ± 6.87** on a clock and **+135.19 ± 7.36** at fixed nodes, 10,000
games each. It also beats its own teacher, net v1, by **+90.22 ± 15.13** -- so
generation N+1 beating generation N, the mechanism the whole NNUE phase rested
on, holds on a clock and not just at equal nodes.

The fixed-time instrument was certified with its own null before any of this
was believed: −10.77 ± 13.96, inside noise.

Net v2 and the analysis published with it are void: its training data came from
an engine whose NNUE accumulator was corrupted from the second move of every
game. Same method on a sound engine gives +94 where v2 gave −225. Both the
result and the retraction are in [`docs/AB.md`](docs/AB.md).

**Phase 5 is done.** Free-for-all plays, as of v8. The C core searches it
paranoid -- every node in the root seat's terms, since FFA is not zero-sum
between any two seats and negamax does not apply -- and the engine evaluates it
with net-ffa1, the first net trained on FFA self-play. Against the hand eval
FFA played before: **+106.05 ± 5.55** over 10,000 games on a clock, and
**+67.35 ± 10.38** at fixed nodes. All five setups are 8-10 sigma positive and
one net covers them.

**v9: the search prunes.** Reverse futility and null-move pruning in Teams, late
move pruning and reductions in the paranoid FFA search, all confirmed at both
instruments and default on. At 20,000 nodes the old FFA search reached 3.11
plies, under one round, so the root seat never saw its own next move; LMP and
LMR take it to five. FFA killers confirmed at fixed nodes and were NULL on the
clock, so they stay off. Both fixed-time nulls have now been run.

Not yet built: multithreading, full bitboards, and quiescence in FFA. The FFA
clock still uses about a third of movetime, so every FFA number above is on
that clock. An opening book landed for both modes; the FFA one is committed at
`books/`.

## License and credit

MIT -- see [LICENSE](LICENSE).

No third-party code is vendored or copied into this repository. Everything here
was written for it. What the projects below contributed was knowledge: what the
rules of 4PC actually are, how the position format is spelled, and node counts
to check a move generator against. Rules of a game and perft counts are facts
rather than authorship, so nothing here is a derivative work -- but they saved
a great deal of time and are worth reading, so they are named wherever they were
used rather than only here.

- [`arianahejazyan/Athena`](https://github.com/arianahejazyan/Athena) (MIT) --
  C++ 4PC engine. Its `tests/data/perft.txt` is the external cross-check for
  the `modern` move generator ([`docs/PERFT.md`](docs/PERFT.md)), and its
  `Setup` option and `position <setup>` form are the protocol conventions this
  engine matches ([`docs/PROTOCOL.md`](docs/PROTOCOL.md)). Castling geometry,
  en passant and promotion behaviour were read from it and cited throughout
  [`docs/RULES.md`](docs/RULES.md) as `[athena]`.
- [`obryanlouis/4pchess`](https://github.com/obryanlouis/4pchess) -- C++ 4PC
  engine, Teams only. Cited as `[4pc]` where it settles a rule question.
- [`TheThirdOne/fen4`](https://github.com/TheThirdOne/fen4) -- Rust parser and
  writer for the FEN4 position format, and normative for it here. Cited as
  `[fen4]`.

The full source list, including chess.com's own documentation and the live
lobby, is the table at the top of [`docs/RULES.md`](docs/RULES.md); every rule
in that file names where it came from and flags where it is an assumption
instead.

`numpy` and `flask` (the GUI's only) are runtime dependencies, installed rather
than vendored, and are not redistributed here.
