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
| after the cache-key fix | *pending* | |

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

### Killers — screened positive, confirmation pending

| | |
|---|---|
| Mode / Setup | Teams / classic |
| Instrument | fixed nodes 20,000 |
| Games | 2,000 (500 openings × 4) |
| Elo | **+40.66 ± 14.50** |
| Dist | 15, 0, 90, 5, 185, 12, 153, 0, 40 |

Two killer moves per ply, `setoption name Killers`. Default **off** pending
confirmation. Fixed-depth node counts fall to 28.0% of baseline over five
setups to depth 5.

Screened on the harness carrying the process-sharing flaw above. The flaw
affected the null test rather than this pairing (A and B differed by an option,
so they were already two processes), but the ±14.50 leaves room for the ~13 Elo
the null was reading, so this needs a 10,000-game confirm before it goes
default-on.
