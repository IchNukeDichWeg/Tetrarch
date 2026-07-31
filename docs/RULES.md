# Tetrarch — Rules of Four-Player Chess

Normative rules reference for the engine. Target: **chess.com four-player chess**,
`modern` and `classic` setups, FFA and Teams.

Every rule below is one of:

* **[C]** — Confirmed against a cited source.
* **[D]** — Derived: proved from confirmed rules. The proof is given.
* **ASSUMPTION:** — Could not be confirmed. A choice is made and stated, with the
  cheapest experiment that would settle it.

Nothing in this document is from memory. Where sources disagree, both readings are
recorded and the conflict is called out.

---

## 0. Sources

| Key | Source |
|-----|--------|
| **[cc-terms]** | chess.com, *4 Player Chess* (Chess Terms) — <https://www.chess.com/terms/4-player-chess> |
| **[cc-help]** | chess.com Help Center, *4 Player Chess (4PC)*, article 668 — <https://support.chess.com/article/668-4-player-chess-4pc> |
| **[wb-play]** | Wikibooks, *Four-Player Chess / How to play?* — <https://en.wikibooks.org/wiki/Four-Player_Chess/How_to_play%3F> |
| **[wb-nota]** | Wikibooks, *Four-Player Chess / Notation* — <https://en.wikibooks.org/wiki/Four-Player_Chess/Notation> |
| **[wb-var]** | Wikibooks, *Four-Player Chess / Variants* — <https://en.wikibooks.org/wiki/Four-Player_Chess/Variants> |
| **[fen4]** | `TheThirdOne/fen4` — Rust parser/writer for chess.com's FEN4. Read at `src/types.rs`, `src/from_str.rs`, `src/display.rs`, `tests/basic.rs`. **Normative for the position format.** |
| **[athena]** | `arianahejazyan/Athena` — C++ 4PC engine. Read at `src/chess/{position.h,castle.h,square.h,movegen.cpp,position.cpp}`, `tests/data/perft.txt`. |
| **[4pc]** | `obryanlouis/4pchess` — C++ 4PC engine (Teams only). Read at `board.cc`, `board.h`. |
| **[cc-lobby]** | chess.com live 4PC lobby — the setup selector, its five presets, their in-app descriptions, and the rendered starting boards. Observed directly 2026-07-31. **Authoritative for starting positions**: it is the running game. |

Retrieved 2026-07-31. The two chess.com pages are the only *authoritative* sources;
the Wikibook is a good independent cross-check; the two engines are implementations
and are treated as evidence, not authority — where they disagree with each other,
that is recorded explicitly (§5.4, §6.4).

---

## 1. Board and coordinates

**[C]** 14×14 grid with the four 3×3 corners removed → **160 playable squares**.
[cc-terms] describes it as "160 squares because three extra ranks are added to each
side."

Files **a–n** (left to right), ranks **1–14** (bottom to top). Square names are
standard algebraic: `a1` … `n14`. [fen4] `Position::from_str` accepts exactly
`[a-n]` followed by `1`–`14`, 2–3 characters.

Removed corners (never playable):

```
a1-c1  a2-c2  a3-c3       l1-n1  l2-n2  l3-n3
a12-c12 a13-c13 a14-c14   l12-n12 l13-n13 l14-n14
```

Internal representation (per the project brief): 16-wide padded mailbox,
`sq = rank*16 + file`, `VALID[256]` byte table, `COMPACT[256] → 0..159`.
[athena] uses the identical scheme (`Square::rank() = id >> 4`, `file() = id & 15`,
`Square::compress()` → 0..159), which is a useful independent confirmation that the
padding width is sufficient: with files 14 and 15 permanently invalid, every knight
and king delta from a real file (0..13) lands either on a real file or on the
padding columns, so no wraparound is possible. Rank underflow *can* go negative and
must be bounds-checked before indexing.

---

## 2. Seats, turn order, modes

**[C]** Four armies: **Red, Blue, Yellow, Green**. "The game always starts with Red
and follows in a clockwise order." [cc-terms]. [fen4] `TurnColor::next()` encodes
exactly `Red → Blue → Yellow → Green → Red`.

Seat index used throughout Tetrarch: `R=0, B=1, Y=2, G=3`. This matches the order of
every 4-array in FEN4 [fen4] ("All of the arrays are information about the players
with the leftmost data about Red and proceeding clockwise").

Seat geometry: **Red bottom, Blue left, Yellow top, Green right** [4pc] `board.h`
conventions comment.

**[C]** Two modes:

* **FFA** — four independent players, points race. [cc-terms]
* **Teams** — "two players each, who are always across the board from each other.
  Players cannot capture their teammate's pieces." [cc-terms] Standard pairing is
  **R+Y vs B+G**.

**[C]** [wb-var] notes chess.com also offers `R&B vs Y&G` and `R&G vs B&Y` team
pairings. Tetrarch v0 supports **R+Y vs B+G only**; the other two are recorded here
so the team-assignment is a parameter rather than a hardcoded assumption.

---

## 3. Starting positions

**[C]** chess.com offers **five** setups, not two: `Classic`, `Modern`, `BY`, `BYG`,
`RG`. All five are selectable in both FFA and Teams. [cc-lobby]

[athena]'s `Setup` option knows only `modern` and `classic`; that is a limitation of
Athena, not of the game. Tetrarch supports all five.

### 3.1 The only thing that varies

All five setups are identical except for the **king/queen placement of each seat**.
Pawns, rooks, knights and bishops never move between setups. Each seat's king sits on
one of the two central squares of its home row, with the queen on the other.

Home rows and their two central squares:

| Seat | Home row | Central squares | Rook squares |
|------|----------|-----------------|--------------|
| Red | rank 1, files d–k | g1, h1 | d1, k1 |
| Blue | file a, ranks 4–11 | a7, a8 | a4, a11 |
| Yellow | rank 14, files d–k | g14, h14 | d14, k14 |
| Green | file n, ranks 4–11 | n7, n8 | n4, n11 |

### 3.2 [D] The naming scheme

The preset names decode against **Classic as the baseline**: the seats named in the
label have their king and queen **swapped relative to Classic**.

| Setup | Seats swapped vs Classic |
|-------|--------------------------|
| `classic` | — (the baseline) |
| `modern` | Blue, Green (i.e. implicitly "BG") |
| `by` | Blue, Yellow |
| `byg` | Blue, Yellow, Green |
| `rg` | Red, Green |

This rule reproduces all five rendered boards in [cc-lobby] exactly, which is the
check that the readings below are right rather than a transcription slip.

> **Design consequence:** the setup is a 4-bit parameter, not five 400-character FEN
> strings. One base position plus a swap set. Adding a sixth arrangement later costs
> one line.

### 3.3 King and queen squares, all five setups

| Setup | Red K/Q | Blue K/Q | Yellow K/Q | Green K/Q |
|-------|---------|----------|------------|-----------|
| `classic` | **h1**/g1 | **a8**/a7 | **g14**/h14 | **n7**/n8 |
| `modern` | **h1**/g1 | **a7**/a8 | **g14**/h14 | **n8**/n7 |
| `by` | **h1**/g1 | **a7**/a8 | **h14**/g14 | **n7**/n8 |
| `byg` | **h1**/g1 | **a7**/a8 | **h14**/g14 | **n8**/n7 |
| `rg` | **g1**/h1 | **a8**/a7 | **g14**/h14 | **n8**/n7 |

Equivalently, by whether each seat's king sits on its **own right hand** (facing the
centre):

| Setup | Red | Blue | Yellow | Green | 180°-symmetric? |
|-------|-----|------|--------|-------|-----------------|
| `classic` | right | left | right | left | **yes** |
| `modern` | right | right | right | right | **yes** (90° too) |
| `by` | right | right | left | left | no |
| `byg` | right | right | left | right | no |
| `rg` | left | left | right | right | no |

Only `classic` and `modern` are symmetric under 180° rotation — i.e. only in those two
do Teams partners (R+Y, B+G) start with identical shapes. This is worth knowing before
running a Teams A/B on `by`, `byg` or `rg`: the seat-assignment bias those setups carry
is structural, not noise.

`modern` is additionally symmetric under 90°, which is what [cc-lobby] means by "simple
symmetry and basic defense lines that are the same for all colors."

### 3.4 `classic` — the base position and Tetrarch's default

```
R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-0-
3,yR,yN,yB,yK,yQ,yB,yN,yR,3/
3,yP,yP,yP,yP,yP,yP,yP,yP,3/
14/
bR,bP,10,gP,gR/
bN,bP,10,gP,gN/
bB,bP,10,gP,gB/
bK,bP,10,gP,gQ/
bQ,bP,10,gP,gK/
bB,bP,10,gP,gB/
bN,bP,10,gP,gN/
bR,bP,10,gP,gR/
14/
3,rP,rP,rP,rP,rP,rP,rP,rP,3/
3,rR,rN,rB,rQ,rK,rB,rN,rR,3
```

Kings: **rK h1, bK a8, yK g14, gK n7**.

Independently corroborated: this is byte-for-byte [fen4]'s `Board::default()`, and it
is the FEN4 [wb-nota] publishes as "the standard starting position". It is also
[athena]'s `STARTPOS[1]`.

[cc-lobby] describes it as "The initial setup of 4PC that has a huge theory developed
and is the best explored setup among others, loved by most streamers and high-rated
players. It has a lot of varied and asymmetric openings, yet has balance issues."

**Tetrarch's default is `classic`** — project owner's decision, on the grounds that it
is where the theory and the strong opposition are. All five setups are implemented and
tested; `classic` is what strength work targets.

### 3.5 `modern` — chess.com's own default

```
R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-0-
3,yR,yN,yB,yK,yQ,yB,yN,yR,3/
3,yP,yP,yP,yP,yP,yP,yP,yP,3/
14/
bR,bP,10,gP,gR/
bN,bP,10,gP,gN/
bB,bP,10,gP,gB/
bQ,bP,10,gP,gK/
bK,bP,10,gP,gQ/
bB,bP,10,gP,gB/
bN,bP,10,gP,gN/
bR,bP,10,gP,gR/
14/
3,rP,rP,rP,rP,rP,rP,rP,rP,3/
3,rR,rN,rB,rQ,rK,rB,rN,rR,3
```

Kings: **rK h1, bK a7, yK g14, gK n8**. Matches [athena]'s `STARTPOS[0]` and the
fixtures throughout [4pc]'s `board_test.cc`.

**[C] `modern` is chess.com's default, and has been since 2022** — [cc-lobby] states
verbatim: "currently the default since 2022". This closes what was Open Item 1: the
`fen4` crate and the Wikibook notation page simply predate the 2022 change and describe
the older default. Both were right when written; neither is right now.

Tetrarch must therefore be correct on `modern` even though it optimises for `classic` —
it is what a random chess.com opponent will be playing.

### 3.6 Deriving the other three

`by`, `byg` and `rg` are §3.4's base FEN with the K/Q characters exchanged on the seats
named in §3.2. No separate literals; `selftest.py` asserts each generated FEN4 against
the §3.3 table.

---

## 4. Piece movement

**[C]** Knight, bishop, rook, queen and king move exactly as in chess. [wb-play]
Sliding pieces stop at the board edge and at the removed corners — in the mailbox
representation this is `sq += delta while VALID[sq]`, with no special-casing.

### 4.1 Pawn direction

**[C]** Each seat's pawns move toward the opposite side. [4pc] `GetPawnMoves2`:

| Seat | Forward | Home rank/file (double push allowed) | Capture squares |
|------|---------|--------------------------------------|-----------------|
| Red | +rank (north) | rank **2** | rank+1, file±1 |
| Blue | +file (east) | file **b** | file+1, rank±1 |
| Yellow | −rank (south) | rank **13** | rank−1, file±1 |
| Green | −file (west) | file **m** | file−1, rank±1 |

Home ranks confirmed independently by [fen4]'s round-trip test fixture, whose
`enPassant` field reads `('i3:i4','c6:d6','f12:f11','l9:k9')` — a Red double push
i2→i4, a Blue b6→d6, a Yellow f13→f11, a Green m9→k9.

**[C]** Double push is available only from the home rank/file. [fen4] exposes this as
a tunable, `'pawnsBaseRank':8` (default `2`, `0` meaning pawns never move two).
Tetrarch honours the field on input; v0 emits only the default.

### 4.2 Promotion

**[C]** Promotion happens on the **8th rank counted from the promoting seat**, not on
a board edge:

* **FFA:** "a pawn is automatically promoted to a queen" [cc-terms]; "pawns promote to
  1-point queens on the 8th row from each player's perspective" [wb-play];
  "Pawns promote on your 8th rank" [cc-help]. **No underpromotion in FFA.**
* **Teams:** "pawns promote on the 11th rank. For standard Teams matches,
  underpromotion is also possible." [cc-terms], [cc-help], [wb-play].

Concrete squares:

| Seat | FFA promotion (8th) | Teams promotion (11th) |
|------|---------------------|------------------------|
| Red | rank 8 | rank 11 |
| Blue | file h | file k |
| Yellow | rank 7 | rank 4 |
| Green | file g | file d |

[athena] hardcodes `PROMOTES = 11` (`constants.h`) and applies it via
`Square::promotes()` — i.e. Athena implements the Teams rank unconditionally.

**[C]** A queen produced by promotion in FFA is a **"1-point queen"**: it is worth
**+1** when captured, not +9. [cc-terms] "+1 for a pawn or promoted queen";
[cc-help] "Pawns +1, 1-point Queens +1". This is a real, load-bearing rule — it means
the board carries two distinguishable queen types and the NNUE feature set and the
FFA points model must both know which is which.

> **Design consequence:** `promoted` is a per-piece bit, part of the position, and
> must be Zobrist-hashed. It is not derivable from the piece array.

---

## 5. En passant

This is the rule most likely to hide bugs, and the brief is right that it needs a
lifetime rather than a boolean.

### 5.1 Lifetime — the square belongs to the seat that pushed

**[C]** The en-passant square is **per seat**, and is cleared when **that same seat
moves again** — not when the next player moves.

Two independent implementations agree:

* [athena] `position.h` holds `std::array<Square, COLOR_NB> enpass_`. `position.cpp`
  `make_move` saves and then clears **only the moving seat's own** entry
  (`set_enpass(Square::offboard(), turn_.id())`) before optionally setting it to
  `Square::middle(source, target)` on a double push.
* [4pc] `board.cc` computes `n_turns = (4 + mover.color - victim.color) % 4` and
  reaches back `n_turns` plies in the move history to find the victim's double push.

**[D] Lifetime is therefore three plies.** After seat *X* double-pushes, each of the
three other seats gets exactly one turn during which the capture is available; the
opportunity ends when *X* is to move again.

**[C]** FEN4 stores this as a four-element array, one entry per seat:
`{'enPassant':('i3:i4','','','')}`. [fen4] `types.rs`.

> **Design consequence:** model it as `ep[4]`, each entry either empty or a
> `(target, victim)` square pair. Elimination of a seat mid-round changes who actually
> gets a turn, so a naive "decrement a counter each ply" model is wrong; key off the
> owning seat's next move, exactly as both reference engines do. All four entries are
> Zobrist-hashed.

### 5.2 Geometry — two squares, not one

**[C]** FEN4 records **both** squares as a colon-joined pair: "The first position is
where a pawn can capture and the second is where the passing pawn should be removed
in the event of a capture. This is necessary notation because some types of fairy
pawns move diagonally. Without the extra information it could be ambiguous."
[fen4] `types.rs`.

So the pair is `M:T` where `M` is the square the pusher skipped and `T` is where the
pushed pawn now stands. Verified against the [fen4] test fixture: Red `i3:i4` means
"capture onto i3, remove the pawn on i4".

**Tetrarch stores both squares.** Deriving `T` from `M` and the victim's seat works
for orthodox pawns but throws away information the format deliberately carries.

### 5.3 [D] Head-on en passant is unreachable

A capture en passant requires an enemy pawn's *skipped* square to be attacked by one
of your pawns. Combined with §4.2's promotion ranks, that is **impossible between
seats facing each other** (Red↔Yellow, Blue↔Green), in both modes:

| Pusher | Skipped square lands on | Opposing pawn would need to be on | Max/min reachable by that seat | Possible? |
|--------|------------------------|-----------------------------------|--------------------------------|-----------|
| Yellow (13→11) | rank 12 | Red pawn on rank 11 | Red pawn max rank 7 (FFA) / 10 (Teams) | no |
| Red (2→4) | rank 3 | Yellow pawn on rank 4 | Yellow pawn min rank 8 (FFA) / 5 (Teams) | no |
| Green (m→k) | file l | Blue pawn on file l | Blue pawn max file g (FFA) / j (Teams) | no |
| Blue (b→d) | file c | Green pawn on file c | Green pawn min file h (FFA) / e (Teams) | no |

**Every en passant in 4PC is between perpendicular seats** — R↔B, R↔G, Y↔B, Y↔G.
A pawn is captured en passant by a pawn moving on a *different axis*.

This is worth writing into `selftest.py` as a positive assertion, not just a comment:
a movegen that ever emits a head-on en passant has a bug somewhere else.

### 5.4 Divergence: the second flanking pawn

The two reference engines disagree, and at least one is wrong.

A double push from `S` through `M` to `T` is attackable by **up to two** enemy pawns —
the two that attack `M`.

* [athena] generates both (`generate_enpass_moves` builds `source = target - take(color, i)`
  for `i ∈ {0,1}`) — correct generation. But its *make-move* removes
  `source + push(color)`, which is only the pushed pawn for one of the two flanks.
* [4pc] only generates the flank where the pushed pawn sits directly in front of the
  capturing pawn (its EP branch requires `GetPiece(from + forward)` to be the enemy
  pawn) — it misses the other flanking pawn entirely.

**Tetrarch's rule (from first principles, and consistent with FEN4's `M:T` pair):**
*any* pawn that attacks `M` may capture; the pawn removed is always the one recorded
at `T`. Both flanks generated, correct pawn removed in both cases.

> **Consequence for the Phase 2 gate:** Tetrarch will diverge from [athena]'s perft
> at the first depth containing a two-flank en passant. See §12 — it does not affect
> the depths currently pinned, but the divergence is expected, not a regression.

### 5.5 [C] An en passant capture can take two pieces

The capturing pawn lands on `M`. In 4PC `M` may be occupied by a **third player's**
piece, since `M` is a normal empty-or-occupied square from every other seat's point of
view. [4pc] handles this explicitly — "there may be both en-passant and piece capture
in the same move" — permitting the move when `M` holds a non-teammate piece and
recording both captures.

So a single pawn move can remove two enemy pieces and, in FFA, score for both.
Move encoding must carry two captured pieces.

---

## 6. Castling

**[C]** As in chess: king moves two squares toward a rook, rook jumps to the square
the king crossed. Rights are per seat and per side, and FEN4 carries them as two
separate 4-arrays (§9). [wb-play] states castling is "the same" as regular chess.

**[C]** Conditions, from [athena] `movegen.cpp` `generate_castle_moves` (the stricter
and more standard of the two implementations):

1. The right is still held for that seat and side.
2. All squares strictly between king and rook are empty.
3. The king's **origin, transit and destination** squares are all unattacked.

[4pc] omits the destination-square check in generation and relies on the later
legality filter; equivalent outcome, and Tetrarch follows [athena].

### 6.1 Squares are derived, never tabulated

With five setups (§3) the king's home square varies per seat *and* per setup, so
castling squares are **computed from the king's actual square**, not looked up. The
derivation, for a seat whose home row runs between two rook squares:

* The **short** side is the rook 3 squares away (2 squares between); the **long** side
  is the rook 4 squares away (3 between).
* The king always starts adjacent-but-one to the short rook, so: **short side is
  whichever side the king starts nearer.** For a king-right seat that is the seat's
  right; for a king-left seat, its left.
* King moves 2 toward the chosen rook; rook lands on the square the king crossed.
* Empty required: every square strictly between king and rook.
* Unattacked required: king's origin, transit and destination.

### 6.2 Worked table for `classic` (Tetrarch's default)

| Seat | Side | King | Rook | Must be empty | Must be unattacked |
|------|------|------|------|---------------|--------------------|
| Red | short | h1 → j1 | k1 → i1 | i1, j1 | h1, i1, j1 |
| Red | long | h1 → f1 | d1 → g1 | e1, f1, g1 | h1, g1, f1 |
| Blue | short | a8 → a10 | a11 → a9 | a9, a10 | a8, a9, a10 |
| Blue | long | a8 → a6 | a4 → a7 | a5, a6, a7 | a8, a7, a6 |
| Yellow | short | g14 → e14 | d14 → f14 | e14, f14 | g14, f14, e14 |
| Yellow | long | g14 → i14 | k14 → h14 | h14, i14, j14 | g14, h14, i14 |
| Green | short | n7 → n5 | n4 → n6 | n5, n6 | n7, n6, n5 |
| Green | long | n7 → n9 | n11 → n8 | n8, n9, n10 | n7, n8, n9 |

In `modern`, Blue and Green have the king on the other central square, so both of their
rows mirror: Blue short becomes a7 → a5 (rook a4 → a6), Green short becomes n8 → n10
(rook n11 → n9), and the long sides likewise.

### 6.3 ASSUMPTION: which FEN4 array is "kingside"

> **ASSUMPTION:** FEN4 field 3 ("kingside") is the **short** side and field 4
> ("queenside") is the **long** side, for every seat and every setup.
>
> This is the standard-chess meaning — White's king starts nearer the h-rook, and the
> h-side is kingside. Carried over literally, "kingside" is the side the king starts
> nearer. For the king-left seats (Blue and Green in `classic`; Red and Blue in `rg`)
> that means "kingside" points toward the seat's **left**, which reads oddly but is
> the only definition that is setup-independent.
>
> The alternative — "kingside" fixed to a compass direction per seat regardless of
> where the king stands — would make the two arrays swap meaning between `classic` and
> `modern` for Blue and Green. No source states which chess.com uses.
>
> **Cheapest experiment:** from a `classic` start, move Blue's a11 rook, export the
> FEN4, and see which of the two arrays drops Blue's bit. If it is field 3, this
> assumption holds. Thirty seconds, and it only matters for interop with chess.com's
> own FEN4 — Tetrarch is self-consistent either way.

### 6.4 Known defect in the reference implementation

[athena]'s `Castle::table_` and its `STARTPOS` array **disagree for Blue and Green**:
the `modern` half of the castle table lists Blue's king on a8 and Green's on n7, which
are the `classic` squares (§3.3). Its `generate_castle_moves` also never verifies that
a king is actually on `king_source`. In a `modern` game this lets Blue and Green
generate a castling move from an empty square once the path clears.

This is precisely the failure mode a hardcoded 128-entry square table invites, and it
is why §6.1 derives instead. With five setups rather than Athena's two, a table would
need 320 entries and the same class of bug would be five times as likely.

It does not affect the perft numbers in §12 (castling needs 2–3 pieces developed off
the back rank, far beyond depth 7), but it is the reason those numbers are treated as
a cross-check rather than an oracle.

---

## 7. Check, checkmate, stalemate

**[C]** A player is in check when their king is attacked. In FFA a player can be in
check from **up to three different players simultaneously**, and chess.com scores that
explicitly — see §8. Legality is unchanged: you may not leave your own king in check,
regardless of how many opponents attack it.

**[C] Checkmate eliminates that player; the game continues.** "The game ends when
three players are eliminated." [cc-help]

**[C] Stalemate eliminates the stalemated player.** "When a player is checkmated **or
stalemated**, all of their pieces become inactive and are grayed out." [cc-terms]

> **Correction to the brief.** The brief says "A stalemated player is skipped, not
> drawn." The "not drawn" half is right — there is no game-wide draw. But the
> stalemated player is not merely skipped: they are **eliminated**, their pieces go
> dead, and in FFA they are awarded **+20** for it. See §8.

**[C]** In **Teams**, stalemate is a draw, and "occur[s] only on the affected player's
turn" [cc-help].

**[C]** In Teams the win condition is reduced: "The first team to checkmate one of the
opposing team's players wins the game." [wb-play] Teams is therefore genuinely
two-player zero-sum, which is why the brief builds it first.

---

## 8. Scoring (FFA only)

Confirmed against **both** chess.com pages, which agree exactly.

### 8.1 Captures

| Captured piece | Points |
|----------------|--------|
| Pawn | **+1** |
| Knight | **+3** |
| Bishop | **+5** |
| Rook | **+5** |
| Queen | **+9** |
| **Promoted ("1-point") queen** | **+1** |
| King (a live "dead king walking", §9.2) | **+20** |
| Spare king | **+3** |

[cc-terms]: "+1 for a pawn or promoted queen, +3 for a knight, +5 for a bishop, +5 for
a rook, and +9 for a queen". [cc-help]: "Pawns +1, 1-point Queens +1 … Knights +3,
Bishops +5, Rooks +5, Queens +9, Kings +20, Spare kings +3".

The brief's numbers were correct, including **bishop = 5** (not 3 — the diagonals are
long on a 14×14 board). The two additions the brief did not have are the 1-point queen
and the king/spare-king values.

> The Wikibook does not publish a per-piece table, so these five values rest on two
> chess.com pages only. They agree verbatim, which is the strongest confirmation
> available short of measuring a live game.

### 8.2 Eliminations

| Event | Points |
|-------|--------|
| Checkmating an opponent | **+20** to the mating player |
| Being stalemated (stalemating oneself) | **+20** to the **stalemated** player |
| Stalemating a dead-king-walking (§9.2) | **+10** to *each* remaining active player |
| Draw — threefold repetition, insufficient material, or 50-move | **+10** to each active player |

[cc-terms]: "By checkmating an opponent (+20). By stalemating oneself (+20). By
stalemating an opponent (+10 for each player still in the game)". [cc-help] resolves
the apparent contradiction between rows 2 and 3: for a resigned/timed-out player,
"Checkmating a king yields 20 points" and "stalemating it awards 10 points to each
remaining active player." So the +20 goes to a *live* player who stalemates himself;
the +10-each applies to stalemating a zombie king, where no one individual earns it.

[wb-var] adds that checkmate is **+40** when the promotion setting is *not* "1pt Q".
Standard chess.com FFA uses 1-point queens, so **+20** is the standard value. The
coupling is recorded so the two constants stay linked.

### 8.3 Multi-check bonuses

| Simultaneous checks | With a queen | With any other piece |
|---------------------|--------------|----------------------|
| Two kings | **+1** | **+5** |
| Three kings | **+5** | **+20** |

[cc-terms] and [cc-help] agree. [wb-play] independently confirms the non-queen values
(+5 / +20). The brief did not mention these; they are worth real material and the eval
must see them.

### 8.4 Variant knobs (recorded, not implemented in v0)

[wb-var]: FFA capture points may be multiplied by 1 (standard), 2, 3 or 4; promotion
may be set to Any / Queen / Rook / Bishop / Knight / 1pt Q. Tetrarch v0 implements
multiplier 1 and 1pt-Q promotion. These are named as constants, not scattered
literals, so the other settings stay reachable.

---

## 9. Elimination and dead pieces

### 9.1 Dead pieces score nothing

**[C]** "When a player is checkmated or stalemated, all of their pieces become
inactive and are grayed out. **Capturing those pieces does not provide any points.**"
[cc-terms]. [cc-help] and [wb-play] both agree.

> **Correction to the brief.** The brief anticipates "rules for capturing an
> already-dead player's pieces" with some point value. There is no such value: dead
> pieces are worth **zero**. They remain on the board as obstacles and are capturable,
> but scoring for them is nil.

**[C]** Dead pieces are first-class in the position format: FEN4 encodes them with a
`d` prefix, optionally retaining the original seat — `dP`, `dK`, or `drP`, `dbN`,
`dyB`, `dgR`. [fen4] `from_str.rs`. Tetrarch keeps the originating seat, because the
NNUE feature index and any future scoring variant both need it.

The brief's framing is right in the part that matters: **movegen skips dead seats,
eval must not.** A board with 40 dead pieces on it is a different game from an empty
one.

### 9.2 Dead King Walking

**[C]** Resignation and timeout behave differently from checkmate: "When a player
resigns or times out, their army becomes 'dead,' but their **King remains 'live'** and
moves randomly until checkmated or stalemated." [cc-help] Capturing that king is worth
+20 (§8.1); stalemating it gives +10 to each remaining active player (§8.2).

[fen4] carries the state for this in `'resigned':(…)` and `'flagged':(…)`, and notes
both are needed for the "DeadKingWalking" feature.

> **Scope decision:** Tetrarch v0 parses and preserves `resigned` and `flagged` on FEN4
> round-trip, but does **not** implement zombie-king movement. Engine-vs-engine play
> never resigns mid-game, so the mechanic is unreachable in our own match runner. It
> is reachable against chess.com. Flagged for Phase 6+.

### 9.3 Other zombie variants

**[C]** [fen4] also carries `'zombieImmune':(…)` (dead pieces cannot be captured) and
`'zombieType':('','','','muncher')` (muncher / comfuter / checker / ranter …).
[wb-var] describes a "DeadWall" variant where dead pieces become impassable walls.
Parsed and preserved; not implemented.

---

## 10. Draws and per-seat counters

### 10.1 Fifty-move rule

**[C]** A 50-move-rule draw exists and awards +10 to each active player [cc-terms].
FEN4 carries a **single global** half-move counter, reset on any capture or pawn
advance by any seat — [fen4] calls the field "ply since last pawn move or capture";
[athena] calls it `fifty_move_clock` and increments it once per ply.

> **ASSUMPTION:** the threshold is **50 moves per seat = 200 plies**.
>
> Neither chess.com page states the ply count, and neither reference engine enforces
> a threshold at all — [athena] tracks the clock but never tests it, [4pc] has no
> 50-move logic. The alternative reading is 50 plies (12.5 rounds), which is
> implausibly short for a 14×14 board. 200 is chosen because chess.com's move
> numbering advances once per full round, so "50 moves" most naturally means 50 of
> your own.
>
> **Cheapest experiment:** shuffle two kings in a live chess.com 4PC game and count.
> Low priority — it changes nothing about play strength, only about when a long
> endgame is scored.

### 10.2 Threefold repetition

**[C]** Exists, awards +10 to each active player [cc-terms].

> **ASSUMPTION:** a position repeats when the full Zobrist key matches — piece
> placement, side to move, all four castling-right pairs, all four en-passant entries,
> **and the alive mask**. Points are deliberately excluded: points are monotonic and
> including them would make repetition unreachable.
>
> No source defines repetition for 4PC. Including the alive mask is not optional — a
> position with a seat eliminated is a different game, as the brief says.

### 10.3 Insufficient material

**[C]** Exists, awards +10 to each active player [cc-terms].

> **ASSUMPTION:** v0 detects only the trivially safe case — every remaining live seat
> has king only, and no dead pieces remain that could be promoted or captured for
> value. The chess two-player table (K+B, K+N, same-colour bishops) does **not**
> transfer: three kings and a dead rook on the board is not a draw, because there is
> nothing to force. Under-detecting a draw is safe; over-detecting throws games.

---

## 11. FEN4 — the position format

**Normative reference: [fen4].** Field order, delimiters and edge cases below are read
from its parser and writer, not invented. Tetrarch matches it exactly, including the
quirks.

### 11.1 Field order

```
turn - dead - castleKingside - castleQueenside - points - halfmoveClock [ - {extra} ] - board
```

Six or seven `-`-separated sections before the board. Note this is **not** FEN's
order: castling rights come near the *front*, and the board comes **last**, not first.

| # | Field | Form | Notes |
|---|-------|------|-------|
| 1 | Turn | `R` \| `B` \| `Y` \| `G` | Uppercase only |
| 2 | Dead | `0,0,0,0` | Exactly 4, each strictly `0` or `1`, order R,B,Y,G |
| 3 | Castle kingside | `1,1,1,1` | Same constraints |
| 4 | Castle queenside | `1,1,1,1` | Same constraints |
| 5 | Points | `0,0,0,0` | 4 unsigned integers (`u16` in [fen4]) |
| 6 | Halfmove clock | `0` | Single unsigned integer |
| 7 | Extra (optional) | `{'key':value,…}` | Omitted entirely when empty — **not** written as `{}` |
| 8 | Board | 14 `/`-separated ranks | See §11.2 |

### 11.2 Board section

* **Rank 14 first, descending to rank 1.** Within a rank, file a → file n.
* Pieces are `<colour><shape>`: colour ∈ `r b y g`, shape is the piece letter
  (`P N B R Q K`). [fen4] accepts *any* character as the shape, which is how it
  supports fairy pieces.
* Dead pieces prefix `d`: `dP` (origin unknown) or `drP`/`dbP`/`dyP`/`dgP`
  (origin retained).
* `X` — capital — is a **wall** (permanently blocked square), used by custom boards.
* Runs of empty squares are written as a decimal count `1`–`14`.
* **The removed 3×3 corners are written as ordinary empty squares**, i.e. `3,…,3`
  and `14` for the two fully empty ranks — *not* as walls. [fen4] `types.rs`:
  "the 3x3 corners of the 14x14 board are counted as empty squares".

### 11.3 Quirks that will bite

1. **The board is found by the *last* `-` in the whole string** ([fen4]
   `Board::from_str` uses `rfind('-')`), not by counting fields. A `-` inside the
   extra-options block would break parsing. Tetrarch reproduces this behaviour so
   that any string it accepts, `fen4` accepts.
2. **Lowercase `x` is not valid.** [athena] and [4pc] both write the corners as
   `x,x,x,…` and neither is parseable by [fen4], which only accepts capital `X` and
   only as a wall. Tetrarch **reads** lowercase `x` as an empty corner square for
   interoperability with those two engines, and **writes** the [fen4]-canonical `3`
   / `14` runs. This asymmetry is deliberate and is asserted in `selftest.py`.
3. **Canonical output contains newlines** — one after the metadata, one after each
   rank. [fen4]'s round-trip test asserts byte equality including them. Input is
   whitespace-tolerant (each comma-segment is `trim()`ed).
4. Empty-run counts may span the corners (`bR,bP,10,gP,gR` crosses the middle) and a
   whole empty rank is just `14`.
5. `dead[]` and dead pieces on the board are independent fields — [fen4]'s own test
   fixture has `0,0,0,0` for `dead` while carrying `dK`/`dQ` pieces.

### 11.4 Extra options

Written in [fen4]'s struct-field order, comma-separated, inside one `{}`:

`royal`, `lives`, `resigned`, `flagged`, `stalemated`, `gameOver`, `zombieImmune`,
`zombieType`, `enPassant`, `pawnsBaseRank`, `uniquify`, `std2pc`.

Value types: `'string'`, bare number, `true`/`false`, and `(r,b,y,g)` tuples.
`'kingSquares'` is accepted as a deprecated alias for `'royal'`. `null` is accepted
where a boolean is expected and means `false`.

Tetrarch v0 **uses** `enPassant` and `pawnsBaseRank`, **preserves** the rest across a
round trip, and **rejects unknown tags** rather than silently dropping them — matching
[fen4]'s `UnknownTag` error.

### 11.5 PGN4

**[C]** Tag pairs in brackets (Variant, player names/ratings, TimeControl, Date, Site,
Result, Termination), then movetext in long algebraic: move number, period, moves
separated by `..`, variations in parentheses, comments in braces. [wb-nota]

v0 needs PGN4 only for reading opening books and writing match logs. Full parity is
not a Phase 1 requirement.

---

## 12. Perft cross-check targets

[athena] ships `tests/data/perft.txt` with node counts from the `modern` start
position:

| Depth | Nodes |
|-------|-------|
| 1 | 20 |
| 2 | 395 |
| 3 | 7,800 |
| 4 | 152,050 |
| 5 | 3,452,310 |
| 6 | 77,430,383 |
| 7 | 1,735,784,286 |

Depth 1 = 20 is a sanity anchor: Red has 8 pawns × 2 pushes + 2 knights × 2 moves.
That holds for **all five setups** — knights start on e1 and j1 in every one of them,
and only the king and queen ever move between setups (§3.1). Any setup returning
something other than 20 at depth 1 is broken before anything else is worth checking.

Since Tetrarch defaults to `classic` but Athena's table is `modern`, the Phase 2 gate
runs `modern` explicitly to compare against these numbers, and pins `classic` (and the
other three) against Tetrarch's own two independent generators.

**These are a cross-check, not an oracle.** Three caveats, all established above:

* Promotion rank cannot matter at these depths — a Red pawn needs 6 of its own moves
  to reach rank 8, i.e. ≥21 plies. FFA and Teams give identical counts to depth 7.
* Castling cannot occur by depth 7, so [athena]'s castle-table defect (§6.4) does not
  contaminate them.
* En passant **does** occur — first at depth 4 — and Tetrarch's two-flank rule (§5.4)
  differs from [athena]'s single-flank make-move. If Tetrarch's depth 4–7 counts
  differ from the table, the two-flank case is the **first** thing to check, and the
  divergence should be reproduced by hand on a specific position before either
  number is trusted.

Tetrarch's own perft numbers for both setups, both modes, to depth 5, are recorded in
`docs/PERFT.md` at the Phase 1 gate, pinned to a named machine.

---

## 13. Summary of corrections to the project brief

| Brief said | Actual | § |
|-----------|--------|---|
| pawn 1, knight 3, bishop 5, rook 5, queen 9 | Confirmed, all five | 8.1 |
| "a bounty for delivering checkmate" | +20 (or +40 if 1pt-Q promotion is off) | 8.2 |
| "rules for capturing an already-dead player's pieces" | They are worth **0** | 9.1 |
| "A stalemated player is skipped, not drawn" | **Eliminated**, and awarded +20 | 7 |
| (not mentioned) | Promoted queens are worth **+1**, not +9 | 4.2 |
| (not mentioned) | Double/triple-check bonuses: +1/+5 queen, +5/+20 other | 8.3 |
| (not mentioned) | Live king +20, spare king +3 | 8.1 |
| (not mentioned) | Dead King Walking on resign/timeout | 9.2 |
| "En passant has a multi-turn lifetime … decaying square" | Correct. Precisely: per-seat, cleared on that seat's next move, 3-ply window | 5.1 |
| (not mentioned) | Head-on en passant is provably unreachable | 5.3 |
| (not mentioned) | One en-passant capture can take **two** pieces | 5.5 |
| "each promotes on its own 8th rank" | 8th in FFA, **11th** in Teams | 4.2 |
| "both setups (`modern` / `classic`)" | **Five** setups: classic, modern, by, byg, rg | 3 |

## 14. Open items

| # | Item | Status | § |
|---|------|--------|---|
| ~~1~~ | ~~Which setup is chess.com's live default~~ | **CLOSED** — `modern`, "default since 2022" per [cc-lobby]. Tetrarch nonetheless defaults to `classic` by decision, and supports all five | 3.4, 3.5 |
| 2 | 50-move threshold in plies | **ASSUMPTION: 200** | 10.1 |
| 3 | Repetition key definition | **ASSUMPTION: full Zobrist incl. alive mask, excl. points** | 10.2 |
| 4 | Insufficient material in 4PC | **ASSUMPTION: bare-kings only** | 10.3 |
| 5 | Two-flank en passant: which engine is right | Tetrarch derives from first principles; expect perft divergence | 5.4 |
| 6 | Per-piece capture values rest on chess.com only | Two pages agree verbatim; Wikibook has no table | 8.1 |
| 7 | Which FEN4 castling array is "kingside" for a king-left seat | **ASSUMPTION: short side.** Only affects chess.com interop | 6.3 |

Items 2–4 are the ones that could silently corrupt results, and each can wait for the
phase that touches it. Item 7 is a thirty-second check whenever you are next in a game.
