# Releasing

One release per **confirmed Elo gain**. Not per commit, not per dormant toggle,
not per tooling change -- a version number here means a measured improvement in
playing strength that survived a 10,000-game confirm.

Versions are plain incrementing integers: `v1`, `v2`, `v3`. `VERSION` in
`uci.py` carries the current number and is bumped in the same commit that
promotes the feature to its default.

## What earns a release

A feature that has:

1. **Screened** at ~2,000 games and not been clearly negative.
2. **Confirmed** at ~10,000 games.
3. Been promoted to its default, with `SEARCH_PINS` re-measured if the tree moved.
4. Had its verdict written into `docs/AB.md` and next to the code.

A dormant toggle does not get a release, however interesting. Nor does a
correctness fix that costs Elo, nor tooling, nor documentation.

## What a release note contains

A one-paragraph headline saying what the thing is and what it measured, then a
fixed stats block, then the detail. The block:

```
Elo   | +NN.NN +- N.NN (95%)
SPRT  | GSPRT bounds and LLR, or "none" and why
Conf  | the instrument: FIXED NODES n / FIXED TIME movetime, hash, workers, setup, mode
Games | N: total (openings x 4 rotations)   score S (S%)
Dist  | the NINE buckets -- score sums 0, 0.5, 1 ... 4 over the rotation
Null  | the null self-test on the same harness
Base  | which version this was measured against
Bench | the perft bench signature
Pins  | search node counts, if the tree moved
```

**`Dist` is nine buckets in Teams, never a pentanomial.** A pentanomial assumes
two games per opening; here one opening is a four-game seat rotation.

**In FFA it is THIRTEEN.** The pairing there is one engine against three of the
other, and a game scores the mover's share of its three pairwise contests, so
sums land on thirds rather than halves. A Teams and an FFA campaign cannot be
pooled and their distributions do not compare.

**`Conf` must name the instrument** and it must be the right one. A change that
moves nodes gets fixed nodes; a change that moves only speed gets fixed time,
because fixed nodes would report exactly zero. Mixing instruments inside one
campaign invalidates it.

**`Base` is the previous version**, not the version a feature was screened
against. The exception is a mode that did not exist before: the first FFA net
has no previous FFA version to measure against, so its base is the hand
evaluation FFA actually played, and the note says so. A confirm run against the current default is the point -- gains are
not additive, and a number that drops between screen and confirm is usually
the previous release having already banked some of it.

## Rejections belong in the notes too

A release lists what was rejected in the same window and why, with its numbers.
A verdict that is not written down gets rediscovered, and `docs/AB.md` is the
long-form record.

## Assets

`make dist REF=vN` writes `tetrarch-vN.tar.gz` and it gets attached to the
release. It is `git archive` at the tag, so the tarball is exactly the
committed tree -- no build output, no `.venv`, nothing uncommitted -- with the
nets and the FFA book in it.

**Extract it, build it and run the selftest before attaching.** An asset nobody
has run is worse than no asset: it is the one thing a stranger tries first.

Still a SOURCE release. There is no compiled binary because the engine is
Python plus a C shared library that `setup.sh` builds per machine, and shipping
a `.so` for one architecture would be worse than shipping none. A real binary
asset means choosing which platforms to build for, and that is a project
rather than a step.
