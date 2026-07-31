#!/usr/bin/env python3
"""gui/app.py -- paste a PGN4, step through the game.

    python3 gui/app.py            # http://127.0.0.1:7442
    python3 gui/app.py --port 8080 --host 0.0.0.0

Default port is 7442, not 5000: macOS binds 5000 to ControlCenter (AirPlay).
$PORT is honoured as the default so harnesses that assign a port work; that is
deployment configuration, not a feature gate -- there are no hidden switches
here.

One Flask app, one page, one canvas. No framework, no build step, no npm.

The server does all the chess: it parses the PGN4, replays it, and hands the
client an array of frames, each carrying a FEN4 and the seat state. The client
only has to draw a FEN4 string, which is a twenty-line parser -- reimplementing
four-player rules in JavaScript would be a second engine to keep correct.

The C core keeps global state (the transposition table, the search buffers) and
is not thread-safe, so evaluation requests are serialised behind a lock.
"""

import argparse
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, render_template, request      # noqa: E402

from tetrarch.board import Board, MODE_FFA, MODE_TEAMS, SETUPS, move_str  # noqa: E402
from tetrarch import pgn4                                        # noqa: E402
from tetrarch import core                                        # noqa: E402
from tetrarch import movegen as gen                              # noqa: E402
from tetrarch.search import Limits, search                       # noqa: E402

app = Flask(__name__)
ENGINE_LOCK = threading.Lock()


@app.route("/")
def index():
    return render_template("index.html", setups=SETUPS)


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

    with ENGINE_LOCK:
        core.clear_hash()
        result = search(board, Limits(depth=depth))
    return jsonify({
        "score": result.score,
        "depth": result.depth,
        "nodes": result.nodes,
        "nps": result.nps,
        "best": move_str(result.best) if result.best else None,
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
