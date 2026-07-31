// Differential gate for the standalone viewer's JavaScript replayer.
//
//   node tests/js_replay_check.js cases.json
//
// The viewer replays PGN4 in the browser with no server behind it, which means
// a second implementation of "apply this move" exists. It generates no moves
// and tests no legality, but castling, en passant, promotion and seat
// elimination are all real rules -- and a second implementation of a rule is
// exactly the thing that drifts.
//
// This extracts the pure functions out of gui/viewer.html and replays the same
// games tetrarch/pgn4.py replayed, comparing every frame. selftest.py runs it
// when node is available and skips it otherwise; node is not a dependency of
// the engine.

const fs = require("fs");
const path = require("path");

const root = path.dirname(__dirname);
const html = fs.readFileSync(path.join(root, "gui", "viewer.html"), "utf8");

const script = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));

// Everything from the rendering section down touches the DOM. The replayer
// above it is pure, which is the whole point of the split.
const pure = script.slice(0, script.indexOf("// --- rendering"));

const sandbox = {};
new Function("exports", pure + "\n" +
  "exports.parsePgn4 = parsePgn4; exports.replay = replay;" +
  "exports.startPosition = startPosition; exports.parseFen4 = parseFen4;" +
  "exports.SEATS = SEATS;")(sandbox);

function frameKey(frame, SEATS) {
  const rows = [];
  for (let r = 13; r >= 0; r--) {
    const cells = [];
    let empty = 0;
    for (let f = 0; f < 14; f++) {
      const p = frame.grid[r][f];
      if (!p) { empty++; continue; }
      if (empty) { cells.push(String(empty)); empty = 0; }
      cells.push((p.dead ? "d" : "") + p.c + p.t);
    }
    if (empty) cells.push(String(empty));
    rows.push(cells.join(","));
  }
  const dead = frame.alive.map(a => (a ? "0" : "1")).join(",");
  return `${SEATS[frame.turn]}|${dead}|${rows.join("/")}`;
}

const cases = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
let compared = 0;
const failures = [];

for (const c of cases) {
  const game = sandbox.parsePgn4(c.pgn4);
  const out = sandbox.replay(game);
  if (out.error) {
    failures.push(`${c.setup}: replay error: ${out.error}`);
    continue;
  }
  if (out.frames.length !== c.frames.length) {
    failures.push(`${c.setup}: ${out.frames.length} frames, expected ${c.frames.length}`);
    continue;
  }
  for (let i = 0; i < out.frames.length; i++) {
    compared++;
    const got = frameKey(out.frames[i], sandbox.SEATS);
    if (got !== c.frames[i]) {
      failures.push(`${c.setup} ply ${i}:\n  js  ${got}\n  py  ${c.frames[i]}`);
      break;
    }
  }
}

console.log(JSON.stringify({compared, failures}));
process.exit(failures.length ? 1 : 0);
