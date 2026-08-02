#!/usr/bin/env python3
"""gui/app.py -- replay a PGN4, or play against the engine.

    python3 gui/app.py            # http://127.0.0.1:7442
    python3 gui/app.py --port 8080 --host 0.0.0.0

gui/viewer.html works on its own -- open it from disk, no server needed. This
adds the one thing a static file cannot do: run the engine. The page detects
that and shows its Evaluate panel only when served.

Default port is 7442, not 5000: macOS binds 5000 to ControlCenter (AirPlay).
$PORT is honoured as the default so harnesses that assign a port work; that is
deployment configuration, not a feature gate -- there are no hidden switches
here.

One Flask app, one page, one canvas. No framework, no build step, no npm.

The page replays PGN4 itself, in the browser. That is a second implementation
of "apply this move", so tests/js_replay_check.js compares it frame-for-frame
against tetrarch/pgn4.py and selftest.py runs that whenever node is present.
It generates no moves and tests no legality; /api/parse remains here for
callers that want the engine's own replay, including its legality errors.

The C core keeps global state (the transposition table, the search buffers) and
is not thread-safe, so evaluation requests are serialised behind a lock.
"""

import argparse
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

from tetrarch.board import (Board, MODE_FFA, MODE_TEAMS, SETUPS,  # noqa: E402
                            DEFAULT_SETUP, SEAT_NAMES, TYPE_CHARS,
                            PC_COLOR, PC_TYPE, VALID, DEAD_UNKNOWN,
                            move_str, sq_of, start_board)
from tetrarch import pgn4                                        # noqa: E402
from tetrarch import core                                        # noqa: E402
from tetrarch import movegen as gen                              # noqa: E402
from tetrarch.search import Limits, search, search_multi         # noqa: E402

app = Flask(__name__)
ENGINE_LOCK = threading.Lock()

HERE = os.path.dirname(os.path.abspath(__file__))
NETS_DIR = os.path.join(os.path.dirname(HERE), "nets")
#: What both pages evaluate with unless told otherwise. Net v4 beats the hand
#: eval by +140.01 +/- 16.97 at fixed nodes, so it is the strongest evaluation
#: here and the sensible thing to show a human. Pass "none" for the hand eval.
DEFAULT_NET = "net-v4.nnue"


@app.route("/")
def index():
    """Serve the same file that works standalone.

    gui/viewer.html opens straight off disk with no server. Served here it is
    byte-identical and simply notices that an /api/eval endpoint answers, so
    there is one page to keep correct rather than two.
    """
    return send_from_directory(HERE, "viewer.html")


@app.route("/play")
def play_page():
    """The interactive board. Needs the server -- it is the engine that plays,
    and the rules live in Python. viewer.html stays standalone and untouched."""
    return send_from_directory(HERE, "play.html")


# --- interactive play ------------------------------------------------------
#
# The client holds a FEN4 and nothing else. Every reply carries the whole
# render state, so the page never applies a rule of its own: no move
# generation, no legality, no promotion logic, no elimination. viewer.html
# replays PGN4 in JavaScript and needs a differential gate for exactly that
# reason (tests/js_replay_check.js); this page cannot drift because it never
# decides anything.

def _render_state(board):
    grid = []
    for rank in range(14):
        row = []
        for file in range(14):
            sq = sq_of(file, rank)
            piece = board.sq[sq] if VALID[sq] else 0
            if not piece:
                row.append(None)
                continue
            colour = PC_COLOR[piece]
            row.append({
                "c": "d" if colour == DEAD_UNKNOWN
                     else SEAT_NAMES[colour].lower(),
                "t": TYPE_CHARS[PC_TYPE[piece]],
                "dead": colour == DEAD_UNKNOWN,
            })
        grid.append(row)

    legal = [move_str(m) for m in gen.gen_legal(board)]
    in_check = gen.in_check(board, board.turn)
    if legal:
        status = "check" if in_check else "playing"
    else:
        status = "checkmate" if in_check else "stalemate"
    return {
        "fen4": board.to_fen4().replace("\n", ""),
        "grid": grid,
        "turn": SEAT_NAMES[board.turn],
        "alive": [bool(a) for a in board.alive],
        "points": [int(p) for p in board.points],
        "legal": legal,
        "status": status,
    }


def _board_from(payload):
    mode = MODE_FFA if payload.get("mode") == "ffa" else MODE_TEAMS
    return Board.from_fen4(payload.get("fen4", ""), mode), mode


@app.route("/api/play/new", methods=["POST"])
def api_play_new():
    payload = request.get_json(silent=True) or {}
    setup = payload.get("setup", DEFAULT_SETUP)
    if setup not in SETUPS:
        return jsonify({"error": "unknown setup %r" % setup}), 400
    mode = MODE_FFA if payload.get("mode") == "ffa" else MODE_TEAMS
    return jsonify(_render_state(start_board(setup, mode)))


@app.route("/api/play/move", methods=["POST"])
def api_play_move():
    payload = request.get_json(silent=True) or {}
    try:
        board, _ = _board_from(payload)
    except Exception as exc:                                    # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    token = payload.get("move", "")
    for m in gen.gen_legal(board):
        if move_str(m) == token:
            board.make(m)
            return jsonify(_render_state(board))
    return jsonify({"error": "illegal move %r" % token}), 400


@app.route("/api/play/engine", methods=["POST"])
def api_play_engine():
    """Search, play the move, return the new state.

    One endpoint rather than eval-then-move: two calls would let the position
    change between them, and the client would have to know how to apply a move
    to do anything with the answer.

    With "apply": false the move is searched and reported but not played --
    that is the hint button, and it is the same code path so a hint can never
    disagree with what the engine would actually do.
    """
    payload = request.get_json(silent=True) or {}
    try:
        board, mode = _board_from(payload)
    except Exception as exc:                                    # noqa: BLE001
        return jsonify({"error": str(exc)}), 400

    if not gen.gen_legal(board):
        return jsonify(_render_state(board))
    if mode == MODE_FFA or not all(board.alive):
        # The search is genuine two-player negamax on the team split, which
        # FFA is not. Saying so beats returning a Teams score for a position
        # Teams cannot represent. Phase 5.
        return jsonify({"unsupported":
                        "the engine plays Teams only -- FFA needs the "
                        "paranoid search from Phase 5"}), 409

    movetime = max(50, min(int(payload.get("movetime", 1000)), 30000))
    want_net = payload.get("net")
    want_net = DEFAULT_NET if want_net is None else want_net.strip()
    with ENGINE_LOCK:
        note = _select_net(want_net)
        core.clear_hash()
        result = search(board, Limits(movetime=movetime))
    if not result.best:
        return jsonify({"error": "no move found"}), 500
    played = move_str(result.best)
    if payload.get("apply", True):
        for m in gen.gen_legal(board):
            if move_str(m) == played:
                board.make(m)
                break
    state = _render_state(board)
    state.update({"move": played, "score": result.score, "depth": result.depth,
                  "nodes": result.nodes, "nps": result.nps, "note": note})
    return jsonify(state)


#: Which net the engine is currently playing with; None means the hand eval.
_LOADED_NET = None


def _select_net(name):
    """Load a net from nets/ by bare filename, or the hand eval for "".

    Only a basename that already exists in nets/ is accepted -- the value comes
    from the browser, so it never reaches the filesystem as a path.
    """
    global _LOADED_NET
    if name and (os.path.basename(name) != name
                 or not name.endswith(".nnue")
                 or not os.path.exists(os.path.join(NETS_DIR, name))):
        return "unknown net %r; using the hand eval" % name
    name = name or None
    if name == _LOADED_NET:
        return None
    if name is None:
        core.unload_net()
    else:
        from tetrarch import nnue
        core.load_net(nnue.Net.load(os.path.join(NETS_DIR, name)))
    _LOADED_NET = name
    return None


@app.route("/api/play/pgn", methods=["POST"])
def api_play_pgn():
    """The game so far as PGN4, replayed from the start and the move list.

    pgn4.write already exists and is gated against pgn4.parse, so this replays
    rather than formatting anything itself -- a second move formatter is the
    thing that drifts.

    Tags follow the standard 4PC export, including the named StartFen4, so
    the result loads in other 4PC viewers and not only in ours.
    """
    payload = request.get_json(silent=True) or {}
    setup = payload.get("setup", DEFAULT_SETUP)
    if setup not in SETUPS:
        return jsonify({"error": "unknown setup %r" % setup}), 400
    mode = MODE_FFA if payload.get("mode") == "ffa" else MODE_TEAMS
    board = start_board(setup, mode)
    moves = []
    for token in payload.get("moves", []):
        found = next((m for m in gen.gen_legal(board)
                      if move_str(m) == token), None)
        if found is None:
            return jsonify({"error": "illegal move %r at ply %d"
                            % (token, len(moves))}), 400
        moves.append(found)
        board.make(found)
    # Only CurrentMove: pgn4.write supplies Variant, RuleVariants and the
    # named StartFen4 itself, and it knows the FFA rule list differs.
    text = pgn4.write(start_board(setup, mode), moves,
                      tags={"CurrentMove": str(len(moves))})
    return jsonify({"pgn4": text})


#: What each net measured against the one before it. Kept here rather than in
#: the page so there is one place where a result is written down, and it is the
#: same place the file lives. Every figure traces to docs/AB.md.
#:
#: Only v0 and v1 were measured against the hand eval directly, so v1-vs-v0 is
#: a difference of two results rather than a head-to-head, and says so. v2 was
#: measured under the accumulator bug and is void -- reporting -226 next to two
#: honest numbers would be worse than reporting nothing.
NET_ELO = [
    {"file": "net-v0.nnue", "label": "v0", "vs": "hand eval",
     "elo": -40.13, "err": 7.01},
    {"file": "net-v1.nnue", "label": "v1", "vs": "v0",
     "elo": 37.87, "err": None,
     "note": "difference of two results against the hand eval, "
             "not a head-to-head A/B"},
    {"file": "net-v2.nnue", "label": "v2", "vs": "v1", "elo": None,
     "note": "measured under the accumulator bug -- the number does not stand"},
    {"file": "net-v4.nnue", "label": "v4", "vs": "v1",
     "elo": 90.22, "err": 15.13, "default": True,
     "note": "Shipped in v6: the engine's evaluation. Against the hand eval "
             "+76.79 \u00b1 6.87 on a clock and +135.19 \u00b1 7.36 at fixed "
             "nodes, 10,000 games each. The figure shown is against its own "
             "teacher, net v1, at fixed time -- generation N+1 beating "
             "generation N is what the whole NNUE phase rested on. "
             "There is no v3: that generation's data was discarded."},
]


@app.route("/api/version")
def api_version():
    """So a page can say which engine it is talking to rather than assume."""
    import uci
    return jsonify({"version": uci.VERSION, "name": uci.NAME})


@app.route("/api/play/nets")
def api_play_nets():
    """Nets on disk, each with what it measured against the previous one.

    Net v4 is the engine's evaluation as of v6 and the default here. v0 and v1
    lost; v2 is void, having been trained on data a broken engine produced.
    """
    try:
        found = set(n for n in os.listdir(NETS_DIR) if n.endswith(".nnue"))
    except OSError:
        found = set()
    known = {e["file"]: e for e in NET_ELO}
    order = {e["file"]: i for i, e in enumerate(NET_ELO)}
    nets = []
    for name in sorted(found, key=lambda n: (order.get(n, 999), n)):
        entry = dict(known.get(name, {"file": name,
                                      "label": name[:-5], "vs": None,
                                      "elo": None, "err": None,
                                      "note": "not measured"}))
        entry["file"] = name
        nets.append(entry)
    return jsonify({"nets": nets})


@app.route("/api/parse", methods=["POST"])
def api_parse():
    text = (request.get_json(silent=True) or {}).get("pgn4", "")
    if not text.strip():
        return jsonify({"error": "nothing to parse"}), 400
    try:
        game = pgn4.parse(text)
    except Exception as exc:                                    # noqa: BLE001
        return jsonify({"error": "could not read the PGN4: %s" % exc}), 400

    try:
        frames, terminations = pgn4.replay(game)
        error = None
    except pgn4.Pgn4Error as exc:
        # Replay as far as it got, so a game that breaks at move 40 still shows
        # the first 39. Silently truncating would be worse than saying so.
        frames, terminations = pgn4.replay(game, limit=_last_good(game))
        error = str(exc)
    except Exception as exc:                                    # noqa: BLE001
        return jsonify({"error": "replay failed: %s" % exc}), 400

    return jsonify({
        "tags": game.tags,
        "mode": "teams" if game.mode == MODE_TEAMS else "ffa",
        "setup": game.setup,
        "frames": frames,
        "terminations": terminations,
        "tokens": game.tokens,
        "error": error,
    })


def _last_good(game):
    """How many tokens replay cleanly, for partial display after a bad move."""
    board = game.start.copy()
    for i, token in enumerate(game.tokens):
        stripped = token.rstrip("+#")
        try:
            if stripped in pgn4.TERMINATORS:
                board.alive[board.turn] = False
                board.turn = board.next_turn()
                continue
            board.make(pgn4.resolve(board, token))
        except Exception:                                       # noqa: BLE001
            return i
    return len(game.tokens)


@app.route("/api/eval", methods=["POST"])
def api_eval():
    payload = request.get_json(silent=True) or {}
    fen4 = payload.get("fen4", "")
    depth = max(1, min(int(payload.get("depth", 6)), 12))
    mode = MODE_FFA if payload.get("mode") == "ffa" else MODE_TEAMS
    try:
        board = Board.from_fen4(fen4, mode)
    except Exception as exc:                                    # noqa: BLE001
        return jsonify({"error": str(exc)}), 400

    if not gen.gen_legal(board):
        state = "checkmate" if gen.in_check(board, board.turn) else "stalemate"
        return jsonify({"terminal": state})
    if mode == MODE_FFA or not all(board.alive):
        # Paranoid search is Phase 5; saying so beats reporting a Teams score
        # for a position Teams cannot represent.
        return jsonify({"unsupported":
                        "FFA and eliminated-seat search arrive in Phase 5"})

    # Above one line the root gives up its cutoffs so every line has a real
    # score rather than a bound; that costs several times the nodes, which is
    # why it is asked for rather than always on.
    lines = max(1, min(int(payload.get("lines", 1)), 8))
    want = payload.get("net", DEFAULT_NET)
    with ENGINE_LOCK:
        # Selected inside the lock: the loaded net is global state, so without
        # this the viewer would analyse with whatever the play page left behind.
        note = _select_net("" if want in (None, "none") else want)
        core.clear_hash()
        results = search_multi(board, Limits(depth=depth), lines)
    if not results:
        return jsonify({"error": "no move found"}), 500
    top = results[0]
    return jsonify({
        "score": top.score,
        "depth": top.depth,
        "nodes": top.nodes,
        "nps": top.nps,
        "best": move_str(top.best) if top.best else None,
        "lines": [{"move": move_str(r.best), "score": r.score,
                   "depth": r.depth, "nodes": r.nodes,
                   "pv": [move_str(m) for m in
                          (r.pv or core.pv(board, r.best))]}
                  for r in results],
        "net": _LOADED_NET or "hand eval",
        "note": note,
    })


@app.route("/api/legal", methods=["POST"])
def api_legal():
    """Legal moves for the position, so the board can highlight them."""
    payload = request.get_json(silent=True) or {}
    mode = MODE_FFA if payload.get("mode") == "ffa" else MODE_TEAMS
    try:
        board = Board.from_fen4(payload.get("fen4", ""), mode)
    except Exception as exc:                                    # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify({"moves": [move_str(m) for m in gen.gen_legal(board)]})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7442)))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    print("Tetrarch viewer on http://%s:%d" % (args.host, args.port))
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
