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

No virtualenv. Everything is a plain `python3 something.py`.

```bash
python3 uci.py                 # the engine, speaking the protocol
python3 selftest.py            # 427 checks, ~13s
python3 gui/app.py             # viewer + play, http://127.0.0.1:7442
```

Two pages. `/` replays a PGN4 you paste -- `gui/viewer.html` also opens straight
off disk with no server at all. `/play` is an interactive board: set each of the
four seats to Human or Engine and play. The engine plays Teams only; in
free-for-all every seat has to be human, because the paranoid search is Phase 5.

## Strength

Every gain below is measured over 10,000 games with a full four-game seat rotation,
against a null self-test of −2.64 ± 6.24 on the same harness.

| release | feature | Elo | instrument |
|---|---|---|---|
| [v6](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v6) | NNUE evaluation (net v4) | **+76.79 ± 6.87** | fixed time |
| [v5](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v5) | quiescence check evasions | **+106.78 ± 6.88** | fixed nodes |
| [v4](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v4) | late move pruning | +36.09 ± 6.69 | fixed nodes |
| [v3](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v3) | lazy evaluation | +42.88 ± 6.50 | fixed time |
| [v2](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v2) | late move reductions | +35.07 ± 6.51 | fixed nodes |
| [v1](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v1) | killer moves | +50.42 ± 6.41 | fixed nodes |

Since v0 the search tree at classic depth 5 has gone from 228,628 nodes to 7,669,
and mean depth at a 20,000-node budget from 3.73 to 5.25.

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
make bench      # the bench signature
make dist       # source tarball
```

`selftest.py` runs before every commit, including pure-Python ones. It pins perft,
cross-checks both move generators, asserts the C core and the Python reference agree
on eval and Zobrist keys bit for bit, and checks that every dormant toggle is really
dormant -- a toggle that silently flipped would make every later A/B a comparison
against something nobody measured.

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
gui/                viewer.html (standalone) + app.py (adds an engine endpoint)
docs/               rules, results, perft, protocol, releasing
```

## Status

**Phase 4 is done.** Net v4 is the engine's evaluation as of v6, confirmed at
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

Not yet built: FFA paranoid search, repetition detection, an opening book,
multithreading, and int8 SIMD for the hidden layers.

## License

MIT -- see [LICENSE](LICENSE).

Prior art read before designing this, and worth reading: `arianahejazyan/Athena`,
`obryanlouis/4pchess`, `TheThirdOne/fen4`.
