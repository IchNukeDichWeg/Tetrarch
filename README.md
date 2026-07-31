# Tetrarch

A four-player chess engine for chess.com 4PC — 14×14, four seats, FFA and Teams.

Python owns the root: iterative deepening, time management, protocol, tooling.
A C shared library owns the per-node loop: board, movegen, ordering, TT, pruning,
quiescence, NNUE inference. A pure-Python reference engine is the source of truth and
stays correct forever; `selftest.py` asserts the C core agrees with it.

Status: **Phase 4** — NNUE inference in the C core, bit-exact with the Python
reference; net v0 trained on 3.9M self-play positions and shipped in `nets/`.
The A/B against the hand-eval build is the outstanding gate.

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

Re-runnable, and it is the whole of a fresh-box setup: it installs a compiler and
numpy/flask if they are missing, builds every `src/c/*.c` into
`build/lib<name>.so` with `-O3 -march=native -shared -fPIC`, then runs the whole
`selftest.py` ladder — a setup that reports success and leaves a
broken `.so` costs a whole campaign to discover, and on a new architecture the
ladder is the only thing that proves the C core still agrees with the Python
reference. `--no-install` skips touching the system; `--no-test` skips the
ladder for a fast rebuild loop.

No virtualenv: everything is a plain `python3 something.py`.

## Rules

[`docs/RULES.md`](docs/RULES.md) is the normative rules reference. Every rule carries
its source; anything unconfirmed is an explicit `ASSUMPTION:` with the cheapest
experiment that would settle it. Read it before touching engine code.

chess.com offers five starting setups — `classic`, `modern`, `by`, `byg`, `rg` — which
differ only in each seat's king/queen placement. All five are supported. **Tetrarch
defaults to `classic`**, where the theory and the strong opposition are; `modern` is
chess.com's own default since 2022 and must stay correct for real-opponent play.

## Layout

Items marked `done` exist; the rest arrive with the phase that needs them, not
before.

```
setup.sh                  build the .so's; re-runnable
Makefile                  distributable binary                      (Phase 3)
selftest.py               the ladder — runs before every commit     done
bench.py                  fixed-position node/nps benchmark         done
match.py                  headless engine-vs-engine, seat rotation  done
sprt.py                   GSPRT, nine-bucket, opt-in                (Phase 4)
gen_data.py               self-play position generation             done
train.py                  NNUE trainer                              done
tune.py                   Texel-style tuner for non-net scalars     (Phase 7)
uci.py                    the protocol                              done

tetrarch/                 the Python engine + reference
    board.py              14×14 padded mailbox, FEN4 I/O            done
    movegen.py            fast generator                            done
    movegen_slow.py       independent slow-obvious generator        done
    search.py             root: iterative deepening, time mgmt      done
    eval_hand.py          throwaway — deleted at Phase 4            done
    nnue.py               features, net format, reference forward   done
    pgn4.py               PGN4 read/write                           done
    core.py               ctypes binding to the C library           done

src/c/                    the accelerator
    tetrarch.c            board, movegen, perft, eval, TT, search   done

nets/                     quantised nets the engine loads
    net-v0.nnue           first net; bootstrap off the throwaway eval  done

gui/                      paste PGN4, step a game                   done
    viewer.html           standalone: open it from disk, no server
    app.py                serves the same file plus an engine endpoint

tests/js_replay_check.js  viewer replay vs pgn4.py, run by selftest  done

docs/
    RULES.md              normative rules, with sources             done
    PERFT.md              pinned perft numbers, named machine       done
    PROTOCOL.md           UCI divergences                           done

tests/data/               positions, opening seeds
```

## Doctrine

One logical change per commit. `python3 selftest.py` before every commit, including
pure-Python ones. One search feature at a time, behind a toggle, A/B'd before the next.
Every output path in every tool is a CLI argument with no repo default.
