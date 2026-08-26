# Tetrarch -- engine protocol

UCI is a two-player protocol. It has one clock per side, one `startpos`, and a
move format that assumes 64 squares. None of that survives contact with a
four-seat 14×14 game, so Tetrarch extends it -- minimally, and every divergence
is listed here.

Where `arianahejazyan/Athena` already made a choice, Tetrarch matches it: a
`Setup` option, and `position <setup>` / `position fen <FEN4>`.

Section references (§n) are to `RULES.md`.

## Divergences from UCI, in full

| # | UCI | Tetrarch | Why |
|---|-----|----------|-----|
| 1 | `position startpos` | `position startpos [setup]`, `position <setup>`, `position fen4 <FEN4>`, `position fen <FEN4>` | There are five start positions, not one (§3) |
| 2 | `wtime`/`btime`, `winc`/`binc` | `rtime`/`btime`/`ytime`/`gtime`, `rinc`/`binc`/`yinc`/`ginc` | Four clocks. Note `btime` means **Blue**, not black |
| 3 | moves are 4-5 chars over 64 squares | 4-6 chars over 196 (`a1`-`n14`), e.g. `h2h3`, `b7c8q` | 14×14 file and rank names |
| 4 | `score mate N` counts moves | counts the mating **team's** moves | The rotation gives each team two plies in four; a ply count would read double |
| 5 | `stop` interrupts the search | accepted and ignored | Single-threaded: `go` has already returned. No multithreading until single-thread is fast and correct |
| 6 | -- | `option name Setup` combo: `classic`, `modern`, `by`, `byg`, `rg` | Athena's shape, extended to all five (§3) |
| 7 | -- | `option name Mode` combo: `teams`, `ffa` | Two different games (§2) |
| 8 | -- | `bench`, `perft <depth>`, `print` / `d` | Debug commands; `bench` prints the commit signature |
| 9 | `uciok`/`readyok`/`bestmove` | unchanged | |

## Commands

### `position`

```
position startpos [setup] [moves <m1> <m2> ...]
position <setup>            [moves ...]        # Athena compatible
position fen4 <FEN4>        [moves ...]
position fen  <FEN4>        [moves ...]        # Athena compatible
```

`<setup>` is one of `classic` (the default), `modern`, `by`, `byg`, `rg`.

The FEN4 is a **single token** -- the format contains no spaces once newlines
are stripped, and canonical FEN4 newlines must be removed before sending.
Format details, including the field order and the quirks, are in §11.

### `go`

```
go depth <n>
go nodes <n>
go movetime <ms>
go rtime <ms> btime <ms> ytime <ms> gtime <ms> [rinc ...] [binc ...] ...
go infinite
```

Only the clock belonging to the seat to move is read; the other three are
accepted and ignored. **`btime` is Blue's clock, not black's** -- the collision
with standard UCI is unavoidable and is the reason this table exists.

Pick exactly one instrument. `match.py` refuses a mixture, because mixing
fixed-nodes and fixed-time inside one campaign invalidates it.

### `setoption`

```
setoption name Setup value classic|modern|by|byg|rg
setoption name Mode  value teams|ffa
setoption name Hash  value <mb>
```

Changing `Setup` or `Mode` resets the board.

### `info` lines

```
info depth <d> score cp <n> nodes <n> nps <n> time <ms> pv <move>
info depth <d> score mate <n> ...
```

`score` is from the perspective of the side to move's **team**; in FFA (not
zero-sum) it is the ROOT seat's own paranoid value -- no negation relates one
seat's outlook to another's. In Teams the
seat rotation alternates team every ply, so this is a well-defined two-player
score (§2). `mate n` counts the mating team's own moves.

The `pv` is the whole variation, walked out of the transposition table by
`core.pv`. With `MultiPV > 1` each line carries a `multipv` rank, best first,
and every line has an exact score rather than a bound -- the root gives up its
cutoffs to make that true, which costs several times the nodes. Mate scores are
counted in TEAM moves, not plies, since a team moves once per two seats.

## Not implemented yet

* **MultiPV in FFA** -- the paranoid search prices ONE line whatever MultiPV
  asks for (ranking every root move for one fixed root needs a C entry that
  takes the root explicitly); the single line is streamed under the multi
  contract. FFA itself searches since v8.
* **`stop` / `ponder`** -- single-threaded (divergence 5).
* **Repetition and the 50-move rule inside the search** -- `match.py` adjudicates
  the 50-move draw at the game level. Item 2 and 3 in §14 are still open, and
  wiring an assumed rule into the search before it is settled would bake in the
  wrong answer.
* **1-point queens across a FEN4 boundary** -- FEN4 cannot represent them
  (§11.2), so a position sent as FEN4 loses the distinction. Send `position
  fen4 <start> moves ...` rather than the current position when it matters;
  `match.py` does.
