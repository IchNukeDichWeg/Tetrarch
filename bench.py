#!/usr/bin/env python3
"""bench.py -- fixed-position node/nps benchmark.

    python3 bench.py                 # the signature run
    python3 bench.py --depth 6
    python3 bench.py --rounds 9      # for an NPS comparison

    python3 bench.py --perft         # the old Phase 2 signature
    python3 bench.py --profile       # where a node's time actually goes

The default benches the **search**, which is what governs how long a data
generation run takes and what a fixed-time A/B is really measuring. `--perft`
keeps the Phase 2 number -- legal move generation plus make/unmake and nothing
else -- which is a movegen measurement and cannot see the evaluation at all.
The two are not comparable and the signature quoted in commits is the search
one.

`--profile` times the components separately inside C, because reaching them
from Python rebuilds the whole board per call and that costs more than any of
them. Each cost is then weighted by how often a node actually calls it, which
is counted rather than assumed -- assuming one evaluation per node overstates
its share by nearly two times, because interior nodes recurse instead of
evaluating and only the quiescence leaves pay for it.

**The per-call figures are a floor, and "unaccounted" is therefore a ceiling.**
Each component is measured by running it on one position hundreds of thousands
of times, so every table and every cache line it touches is warm. A real search
calls it on a position it has never seen. Do not read "unaccounted" as a
measurement of move ordering and the transposition table: it is those plus the
cache misses this method cannot reproduce.

The number to trust for the evaluation's total cost is not in this table at
all -- it is the difference between `bench.py` and `bench.py --net none`, which
measures the whole engine both ways and needs no attribution to be true.

The positions are frozen as literal FEN4 rather than generated from a seed. A
seeded generator would silently re-pick its positions the first time move
ordering changed, which would move the signature for a reason that has nothing
to do with speed.

Two things about reading the output, both learned expensively:

* **A shallow single-depth bench cannot resolve sub-1% NPS.** For any claim
  under 1%, use `--rounds 9` or more, take the median, and run on an idle
  machine. Round 1 is discarded automatically -- it pays the page-fault and
  cache-warming cost that later rounds do not.
* **NPS gains are architecture-specific.** The same node-identical change can
  be +5% on one microarchitecture and a wash on another. Always report the
  machine, which is why the header prints it.
"""

import argparse
import platform
import statistics
import subprocess
import sys
import time

from tetrarch.board import Board, MODE_FFA, MODE_TEAMS
from tetrarch import core

#: (label, mode, fen4). Frozen -- see the module docstring.
POSITIONS = [
    ("classic-start", MODE_TEAMS,
     "R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-0-"
     "3,yR,yN,yB,yK,yQ,yB,yN,yR,3/3,yP,yP,yP,yP,yP,yP,yP,yP,3/14/"
     "bR,bP,10,gP,gR/bN,bP,10,gP,gN/bB,bP,10,gP,gB/bK,bP,10,gP,gQ/"
     "bQ,bP,10,gP,gK/bB,bP,10,gP,gB/bN,bP,10,gP,gN/bR,bP,10,gP,gR/14/"
     "3,rP,rP,rP,rP,rP,rP,rP,rP,3/3,rR,rN,rB,rQ,rK,rB,rN,rR,3"),
    ("classic-10ply", MODE_TEAMS,
     "Y-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-1-"
     "3,yR,yN,yB,yK,yQ,yB,yN,yR,3/3,yP,yP,1,yP,yP,yP,1,yP,3/5,yP,3,yP,4/"
     "bR,bP,10,gP,gR/bN,bP,10,gP,gN/bB,1,bP,9,gP,1/bK,bP,8,gP,2,gQ/"
     "bQ,bP,9,gB,gP,gK/bB,bP,10,gP,gB/bN,bP,5,rP,4,gP,gN/bR,bP,7,rQ,2,gP,gR/14/"
     "3,rP,rP,rP,rP,1,rP,rP,rP,3/3,rR,rN,rB,1,rK,rB,rN,rR,3"),
    ("modern-16ply", MODE_TEAMS,
     "R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-1-{'enPassant':('','','i12:i11','')}-"
     "3,yR,yN,1,yK,yQ,yB,yN,yR,3/3,yP,1,yP,yP,yP,1,yP,4/4,yP,5,yP,3/"
     "bR,bP,yB,5,yP,3,gP,gR/bN,bP,10,gP,1/bB,1,bP,8,gN,gP,1/bQ,bP,8,gP,2,gK/"
     "bK,bP,10,gP,gQ/bB,2,bP,6,gB,gP,1,gB/bN,bP,10,gP,gN/"
     "bR,1,bP,5,rP,3,gP,gR/9,rP,4/3,rP,rP,rP,rP,rP,2,rP,3/"
     "3,rR,rN,rB,rQ,rK,rB,rN,rR,3"),
    ("classic-24ply-ffa", MODE_FFA,
     "R-0,0,0,0-0,1,1,1-0,1,1,1-0,0,0,0-2-{'enPassant':('','c8:d8','','')}-"
     "3,yR,yN,yB,yK,yQ,1,yN,yR,3/4,yP,yP,yP,1,yP,yP,4/3,yP,3,yP,2,yP,3/"
     "bR,2,bP,8,gP,gR/bN,1,bP,6,gN,2,gP,1/bB,bP,11,gB/bK,2,bP,8,gP,gQ/"
     "bQ,bN,1,bP,8,gP,gK/bB,bP,6,yB,1,gP,2,gB/2,bP,10,gN/"
     "bR,bP,2,rP,4,rP,gP,1,gP,gR/10,rP,3/3,rP,1,rP,rP,rP,rP,5/"
     "3,rR,rN,rB,rQ,1,rK,rN,rR,3"),
    ("by-20ply", MODE_TEAMS,
     "R-0,0,0,0-1,1,1,1-1,1,1,1-0,0,0,0-0-{'enPassant':('','','','l7:k7')}-"
     "3,yR,yN,yB,yQ,yK,yB,1,yR,3/3,yP,2,yP,yP,2,yP,3/4,yP,4,yP,yN,3/"
     "bR,1,bB,bP,1,yP,2,yP,3,gP,gR/bN,2,bP,7,gP,1,gN/1,bP,8,gP,2,gB/"
     "bQ,bP,10,gP,gQ/bK,bP,8,gP,2,gK/bB,bP,8,gP,2,gB/"
     "bN,bP,3,rP,4,rQ,gP,1,gN/bR,1,bP,4,rP,rP,3,gP,gR/14/"
     "3,rP,rP,1,rP,2,rP,rP,3/3,rR,rN,rB,1,rK,rB,rN,rR,3"),
]


def machine():
    bits = [platform.machine(), platform.system()]
    try:
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True, timeout=2)
        if chip.returncode == 0 and chip.stdout.strip():
            bits.insert(0, chip.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return " / ".join(bits)


def search_round(depth):
    """One search per position. Nodes are the search's, not perft's."""
    nodes = 0
    per_position = []
    started = time.perf_counter()
    for label, mode, fen in POSITIONS:
        b = Board.from_fen4(fen, mode)
        core.clear_hash()
        t = time.perf_counter()
        r = core.search(b, depth)
        per_position.append((label, r.nodes, time.perf_counter() - t))
        nodes += r.nodes
    return nodes, time.perf_counter() - started, per_position


def profile(depth, iters):
    """Per-call cost of each component, weighted by how often a node calls it.

    A per-call cost on its own says nothing about where the time goes -- the
    cheapest component can dominate if it runs often enough. `gen_pseudo` runs
    once per interior node and `evaluate` at most once, both close enough to 1
    to use directly. Makes are counted for real, because that rate is the one
    that is not obvious.
    """
    nodes = makes = evals = 0
    secs = 0.0
    for _, mode, fen in POSITIONS:
        b = Board.from_fen4(fen, mode)
        core.clear_hash()
        t = time.perf_counter()
        r = core.search(b, depth)
        secs += time.perf_counter() - t
        nodes += r.nodes
        makes += core.search_makes()
        evals += core.search_evals()
    ns_node = secs / nodes * 1e9
    per_node = {"gen_pseudo": 1.0, "make+unmake": makes / nodes,
                "evaluate": evals / nodes, "gen_legal": 0.0}

    rows = []
    for what, label in (("gen", "gen_pseudo"), ("makeunmake", "make+unmake"),
                        ("eval", "evaluate"), ("legal", "gen_legal")):
        total = 0.0
        for _, mode, fen in POSITIONS:
            b = Board.from_fen4(fen, mode)
            total += core.bench_component(b, what, iters)
        ns = total / (iters * len(POSITIONS)) * 1e9
        rows.append((label, ns, per_node[label], ns * per_node[label]))
    return ns_node, nodes, makes / nodes, rows


def one_round(depth):
    nodes = 0
    per_position = []
    started = time.perf_counter()
    for label, mode, fen in POSITIONS:
        b = Board.from_fen4(fen, mode)
        t = time.perf_counter()
        n = core.perft(b, depth)
        per_position.append((label, n, time.perf_counter() - t))
        nodes += n
    return nodes, time.perf_counter() - started, per_position


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--depth", type=int, default=7,
                    help="search depth; --perft overrides the default to 5")
    ap.add_argument("--perft", action="store_true",
                    help="bench movegen only, the Phase 2 signature")
    ap.add_argument("--profile", action="store_true",
                    help="per-component cost against the search's ns/node")
    ap.add_argument("--iters", type=int, default=200000,
                    help="calls per component under --profile")
    # The engine plays with a net, so benching without one measures a path it
    # never takes -- and the evaluation is exactly what a profile is looking
    # for. "none" selects the hand eval.
    ap.add_argument("--net", default="default", metavar="PATH",
                    help="net to bench with; 'default' is the one the engine "
                         "plays with, 'none' is the hand eval")
    ap.add_argument("--rounds", type=int, default=1, metavar="N",
                    help="repeat N times and report the median; round 1 is "
                         "discarded when N > 1")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not core.available():
        print("bench.py needs the C core: run ./setup.sh", file=sys.stderr)
        return 1

    if args.net != "none":
        path = args.net
        if path == "default":
            import uci
            path = uci.DEFAULT_NET
        try:
            from tetrarch import nnue
            core.load_net(nnue.Net.load(path))
        except Exception as exc:                                # noqa: BLE001
            print("could not load net %s: %s" % (path, exc), file=sys.stderr)
            return 1

    print("machine: %s" % machine())
    print("net:     %s" % ("loaded" if core.net_loaded() else "none (hand eval)"))

    if args.profile:
        depth = args.depth
        ns_node, nodes, mpn, rows = profile(depth, args.iters)
        print("depth:   %d over %d positions" % (depth, len(POSITIONS)))
        print("search:  %d nodes, %.1f ns/node, %.2f makes/node"
              % (nodes, ns_node, mpn))
        print("component            ns/call  per node   ns/node   share")
        for label, ns, rate, weighted in rows:
            if rate == 0.0:
                print("  %-18s %8.1f       --         --      --" % (label, ns))
            else:
                print("  %-18s %8.1f %9.2f %9.1f %6.1f%%"
                      % (label, ns, rate, weighted, weighted / ns_node * 100.0))
        accounted = sum(w for _, _, r, w in rows if r)
        print("  %-18s %8s %9s %9.1f %6.1f%%"
              % ("unaccounted", "", "", ns_node - accounted,
                 (ns_node - accounted) / ns_node * 100.0))
        print("\ngen_legal is not on the search's path -- it generates and then\n"
              "filters every move, where the search filters one move at a time.\n"
              "It is here to price the movegen the rest of the tooling calls.")
        return 0

    if args.perft and args.depth == 7:
        args.depth = 5
    print("depth:   %d over %d positions (%s)"
          % (args.depth, len(POSITIONS), "perft" if args.perft else "search"))

    rates = []
    nodes = 0
    for r in range(args.rounds):
        nodes, secs, per_position = (one_round(args.depth) if args.perft
                                     else search_round(args.depth))
        rate = nodes / secs
        rates.append(rate)
        if not args.quiet and r == 0:
            for label, n, t in per_position:
                print("  %-18s %12d %9.0f nps" % (label, n, n / max(t, 1e-12)))
        if args.rounds > 1:
            print("  round %d/%d  %.0f nps%s"
                  % (r + 1, args.rounds, rate, "  (discarded)" if r == 0 else ""))

    if args.rounds > 1:
        # Round 1 pays page-fault and cache-warming costs the rest do not.
        kept = rates[1:]
        rate = statistics.median(kept)
        spread = (max(kept) - min(kept)) / rate * 100.0
        print("median of %d rounds, spread %.2f%%" % (len(kept), spread))
    else:
        rate = rates[0]

    print("%d nodes %d nps" % (nodes, int(rate)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
