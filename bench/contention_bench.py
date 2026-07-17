#!/usr/bin/env python3
"""Fixed-work multithreaded read workload over ONE shared graph — the perf
target for proving the cache-line-contention claims in the README.

The workload is a BFS sweep: an adjacency dict {int: tuple(int, ...)} built
once by the main thread, then T worker threads each run full BFS from a
statically-partitioned set of roots.  Workers share NOTHING except the graph
(no shared queue, no shared results) so any cross-core traffic is attributable
to the graph objects themselves.

Modes:
  shared    workers read the one graph (biased refcounting: every read is an
            atomic RMW on ob_ref_shared of shared objects)
  immortal  same single graph, immortalize_tree() at the freeze point first
            (INCREF/DECREF skip; zero refcount writes)
  local     each worker deep-copies the graph before the measured window
            (thread-local ownership: refcounts use the non-atomic local path)

If contention on the shared objects' refcount words is the true cause,
`immortal` and `local` should both restore scaling while `shared` degrades
with thread count.  `local` triangulates: it removes sharing without removing
refcounting.

Usage:
  python3.13t+ bench/contention_bench.py MODE NTHREADS [NODES] [DEGREE]

Output: one JSON line on stdout with the measured wall time; hot-object
cache-line addresses on stderr (for correlating perf c2c report lines).
"""
import copy
import json
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "src"))
import immortalize as im  # noqa: E402


def build_graph(nodes, degree, seed=42):
    rng = random.Random(seed)
    graph = {}
    for n in range(nodes):
        # ring edge keeps it connected; the rest random
        nbrs = {(n + 1) % nodes}
        while len(nbrs) < degree:
            nbrs.add(rng.randrange(nodes))
        graph[n] = tuple(sorted(nbrs))
    return graph


def bfs_sweep(graph, roots):
    touched = 0
    for root in roots:
        seen = {root}
        frontier = [root]
        while frontier:
            nxt = []
            for node in frontier:
                for nbr in graph[node]:
                    if nbr not in seen:
                        seen.add(nbr)
                        nxt.append(nbr)
            touched += len(frontier)
            frontier = nxt
    return touched


def main():
    mode = sys.argv[1]
    nthreads = int(sys.argv[2])
    nodes = int(sys.argv[3]) if len(sys.argv) > 3 else 3000
    degree = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    assert mode in ("shared", "immortal", "local"), mode

    graph = build_graph(nodes, degree)

    # stderr: cache-line addresses of the hottest shared objects, so a
    # `perf c2c report` cacheline column can be matched to actual objects.
    hot = [graph, graph[0], graph[1], graph[nodes // 2]]
    for o in hot:
        sys.stderr.write(
            f"hotobj {type(o).__name__:5s} id=0x{id(o):x} "
            f"cacheline=0x{id(o) & ~63:x}\n")

    # Optionally dump EVERY graph object's cache line so perf c2c's contended
    # lines can be set-joined against the graph: the refcount words live at
    # offsets 12/16 of each object, i.e. in the object's first cache line.
    dump = os.environ.get("BENCH_DUMP_LINES")
    if dump:
        with open(dump, "w") as fh:
            fh.write(f"0x{id(graph) & ~63:x} dict graph\n")
            for k, v in graph.items():
                fh.write(f"0x{id(v) & ~63:x} tuple node{k}\n")
                fh.write(f"0x{id(k) & ~63:x} int key{k}\n")

    n_imm = 0
    if mode == "immortal":
        n_imm = im.immortalize_tree(graph)

    # static partition: every root swept exactly once across all threads
    all_roots = list(range(nodes))
    chunks = [all_roots[i::nthreads] for i in range(nthreads)]

    locals_ = {}
    if mode == "local":
        for i in range(nthreads):
            locals_[i] = copy.deepcopy(graph)   # outside the measured window

    start = threading.Barrier(nthreads + 1)
    results = [0] * nthreads

    def worker(i):
        g = locals_[i] if mode == "local" else graph
        roots = chunks[i]
        start.wait()
        results[i] = bfs_sweep(g, roots)

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(nthreads)]
    for t in threads:
        t.start()
    start.wait()
    t0 = time.perf_counter()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    print(json.dumps({
        "mode": mode, "threads": nthreads, "nodes": nodes, "degree": degree,
        "wall_s": round(wall, 4), "immortalized": n_imm,
        "touched": sum(results),
        "free_threaded": im.is_free_threaded(),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
