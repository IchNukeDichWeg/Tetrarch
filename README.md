# Tetrarch

A four-player chess engine for chess.com 4PC — 14×14, four seats, FFA and Teams.

Python owns the root: iterative deepening, time management, protocol, tooling.
A C shared library owns the per-node loop: board, movegen, ordering, TT, pruning,
quiescence, NNUE inference. A pure-Python reference engine is the source of truth and
stays correct forever; `selftest.py` asserts the C core agrees with it.

Status: **Phase 2** — C core ported and agreeing with the Python reference
node-for-node; perft exact against Athena to depth 7. No search and no eval yet.

```bash
python3 selftest.py
```

Bench signature (depth 5, five frozen positions, `bench.py`):

```
93846865 nodes 27492167 nps
```

The node count is exact and machine-independent. The nps is an Apple M2 Pro
figure — always report the machine with an NPS claim, and use
`bench.py --rounds 9` for anything under 1 %.

## Setup

```bash
./setup.sh
```

Re-runnable. Verifies the toolchain, creates `.venv` with numpy and flask, and builds
every `src/c/*.c` into `build/lib<name>.so` with `-O3 -march=native -shared -fPIC`.

## Rules

[`docs/RULES.md`](docs/RULES.md) is the normative rules reference. Every rule carries
its source; anything unconfirmed is an explicit `ASSUMPTION:` with the cheapest
experiment that would settle it. Read it before touching engine code.

chess.com offers five starting setups — `classic`, `modern`, `by`, `byg`, `rg` — which
differ only in each seat's king/queen placement. All five are supported. **Tetrarch
defaults to `classic`**, where the theory and the strong opposition are; `modern` is
chess.com's own default since 2022 and must stay correct for real-opponent play.

## Layout

Proposed. Only the Phase 0 files exist today; the rest arrive with the phase that
needs them, not before.

```
setup.sh                  build the .so's; re-runnable
Makefile                  distributable binary                      (Phase 3)
selftest.py               the ladder — runs before every commit     done
bench.py                  fixed-position node/nps benchmark         done
match.py                  headless engine-vs-engine, seat rotation  (Phase 3)
sprt.py                   GSPRT, pentanomial, opt-in                (Phase 4)
gen_data.py               self-play position generation             (Phase 4)
train.py                  NNUE trainer                              (Phase 4)
tune.py                   Texel-style tuner for non-net scalars     (Phase 7)
uci.py                    the protocol                              (Phase 3)

tetrarch/                 the Python engine + reference
    board.py              14×14 padded mailbox, FEN4 I/O            done
    movegen.py            fast generator                            done
    movegen_slow.py       independent slow-obvious generator        done
    search.py             root: iterative deepening, time mgmt      (Phase 3)
    eval_hand.py          throwaway — deleted at Phase 4            (Phase 3)
    nnue.py               feature extraction, inference reference   (Phase 4)
    core.py               ctypes binding to the C library           done

src/c/                    the accelerator
    tetrarch.c            board, movegen, perft; TT/search/NNUE later  done

gui/                      Flask + one page + one canvas             (Phase 6)

docs/
    RULES.md              normative rules, with sources             done
    PERFT.md              pinned perft numbers, named machine       done
    PROTOCOL.md           UCI divergences                           (Phase 3)

tests/data/               positions, opening seeds
```

## Doctrine

One logical change per commit. `python3 selftest.py` before every commit, including
pure-Python ones. One search feature at a time, behind a toggle, A/B'd before the next.
Every output path in every tool is a CLI argument with no repo default.
