# Tetrarch

A four-player chess engine for [chess.com 4PC](https://www.chess.com/4-player-chess) — 14×14, four seats, Teams and FFA.

[![release](https://img.shields.io/github/v/release/IchNukeDichWeg/Tetrarch?label=release)](https://github.com/IchNukeDichWeg/Tetrarch/releases)
[![license](https://img.shields.io/github/license/IchNukeDichWeg/Tetrarch)](LICENSE)

Python owns the root — iterative deepening, time management, protocol, tooling.
A C shared library owns the per-node loop — board, movegen, ordering, transposition
table, pruning, quiescence, NNUE inference. A pure-Python reference engine is the
source of truth, and `selftest.py` asserts the C core agrees with it bit for bit.

## Quick start

```bash
git clone https://github.com/IchNukeDichWeg/Tetrarch.git
cd Tetrarch
./setup.sh
```

That is the whole of a fresh-machine setup: it installs a compiler and numpy/flask
if they are missing, builds the C core, and then runs the full test ladder — a setup
that reports success and leaves a broken build costs a whole campaign to discover.

No virtualenv. Everything is a plain `python3 something.py`.

```bash
python3 uci.py                 # the engine, speaking the protocol
python3 selftest.py            # 427 checks, ~13s
python3 gui/app.py             # the viewer, http://127.0.0.1:7442
```

`gui/viewer.html` also opens straight off disk with no server at all.

## Strength

Every gain below is measured over 10,000 games with a full four-game seat rotation,
against a null self-test of −2.64 ± 6.24 on the same harness.

| release | feature | Elo | instrument |
|---|---|---|---|
| [v4](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v4) | late move pruning | +36.09 ± 6.69 | fixed nodes |
| [v3](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v3) | lazy evaluation | +42.88 ± 6.50 | fixed time |
| [v2](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v2) | late move reductions | +35.07 ± 6.51 | fixed nodes |
| [v1](https://github.com/IchNukeDichWeg/Tetrarch/releases/tag/v1) | killer moves | +50.42 ± 6.41 | fixed nodes |

Since v0 the search tree at classic depth 5 has gone from 228,628 nodes to 7,449,
and mean depth at a 20,000-node budget from 3.73 to 5.25.

Rejections are recorded too — see [`docs/AB.md`](docs/AB.md). Two features were
closed without spending a single game because a cheap measurement showed their
gate was structurally dead.

## The game

chess.com offers five starting setups — `classic`, `modern`, `by`, `byg`, `rg` —
differing only in each seat's king/queen placement. All five are supported.
**Tetrarch defaults to `classic`**, where the theory and the strong opposition are;
`modern` has been chess.com's own default since 2022.

[`docs/RULES.md`](docs/RULES.md) is the normative rules reference. Every rule carries
its source; anything unconfirmed is an explicit `ASSUMPTION:` with the cheapest
experiment that would settle it. Read it before touching engine code — four-player
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
dormant — a toggle that silently flipped would make every later A/B a comparison
against something nobody measured.

Measuring a change:

```bash
python3 match.py 500 --log screen.jsonl --opt-a LMP=true --nodes 20000 --workers 0
```

The positional argument is **opening positions, not games** — each is played four
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

**Phase 4 is open.** The engine still plays on a hand-written evaluation that is
marked for deletion: net v0 was trained on 3.9 M self-play positions and lost its
A/B at −40.13 ± 7.01. Four confirmed search gains since then have made the engine a
substantially stronger teacher, so the next step is regenerating data with it and
retraining.

Not yet built: FFA paranoid search, repetition detection, an opening book, and
multithreading.

## License

MIT — see [LICENSE](LICENSE).

Prior art read before designing this, and worth reading: `arianahejazyan/Athena`,
`obryanlouis/4pchess`, `TheThirdOne/fen4`.
