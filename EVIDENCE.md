# Evidence

The README makes specific claims about CPython internals and CPU
microarchitecture. This page proves each one with real tools — ctypes header
inspection and Linux `perf` hardware counters — and ships the scripts so you
can reproduce every number.

**Test machine:** Intel i9-14900K (8 P-cores + 16 E-cores, 24 cores / 32
threads, 64 B cache lines), Linux 7.1, CPython 3.15.0b3 free-threaded,
machine quiesced during measurement. `perf stat` runs need no privileges
(`perf_event_paranoid=2`); the `perf c2c` step needs
`kernel.perf_event_paranoid=-1` temporarily.

Reproduce:

```
python3.15t bench/prove_biased_refcount.py          # claims 1, 2, 5 — no perf needed
python3.15t bench/contention_bench.py MODE T        # perf workload (MODE: shared|immortal|local)
```

---

## Claim 1 — "reference counting is still there; reads write to the refcount"

**Tool:** ctypes reads of the object header while controlled reads happen
(`bench/prove_biased_refcount.py`, proofs B–D).

- 5,000 dict-index reads (`d["k"]`) executed by a non-owner thread, results
  held: the value object's `ob_ref_shared` count rose by **exactly 5,000**.
- A thread binding-and-dropping `x = d["k"]` in a loop, sampled from another
  thread: the shared refcount was caught mid-write in **200,000 of 200,000
  samples**. A pure *read* workload keeps the refcount word permanently hot.

## Claim 2 — "biased reference counting: two counters, owner non-atomic, foreign atomic"

**Tool:** same header inspection (proofs A–C), against the free-threaded
object layout (`ob_tid` @0, `ob_ref_local` u32 @12, `ob_ref_shared` i64 @16):

- Two objects created by the main thread carry the same `ob_tid` owner tag; an
  object created in a worker carries a different one. **(ownership)**
- 1,000 refs held by the owner: `ob_ref_local` +1000, `ob_ref_shared`
  untouched. **(owner path, plain increments)**
- 5,000 refs held by a foreign thread: `ob_ref_shared` +5000 (count is stored
  `<< 2`; the low state bits latch on first foreign contact), `ob_ref_local`
  untouched, and the count returns to baseline on release. **(foreign path)**

The atomicity of the foreign path is architectural (`lock`-prefixed RMW);
its *cost* is what Claim 3 measures.

## Claim 3 — "atomic RMWs on the same cache lines; MESI ping-pong; computation trivial next to coherence traffic"

**Tool:** `perf stat` with cross-core snoop counters, and `perf c2c` for the
line-level view. Workload: `bench/contention_bench.py` — one adjacency dict
(3,000 nodes, degree 4) built by the main thread, 16 worker threads doing a
fixed 9,000,000-node-visit BFS sweep, pinned to P-cores, sharing *nothing*
except the graph.

`perf stat -e cycles:u,instructions:u,cpu_core/mem_load_l3_hit_retired.xsnp_hitm/u,cpu_core/ocr.demand_rfo.l3_hit.snoop_hitm/u`:

| counter (16 threads, fixed work) | shared | immortalized | ratio |
|---|---:|---:|---:|
| wall time | 3.65 s | 0.10 s | **36×** |
| instructions | 25.3 G | 19.1 G | 1.3× (same work) |
| cycles | 46.8 G | 8.9 G | 5.2× |
| **IPC** | **0.54** | **2.14** | 4× |
| `xsnp_hitm` (load hit modified data in another core) | **57,513,431** | 35,300 | **1,629×** |
| `rfo.snoop_hitm` (atomic stole a modified line from another core) | **26,618,578** | 8,733 | **3,048×** |

Same instructions, one-quarter the IPC, tens of millions of cross-core
modified-line transfers: the machine is not computing, it is shipping cache
lines between cores.

**Line-level smoking gun** (`perf c2c record` / `report`, 235,046 samples):
the workload dumps the cache-line address of every graph object
(`BENCH_DUMP_LINES`), and joining those against perf's contended-lines table
shows **the #1 most-contended cache line in the entire process is the graph
dict object itself** — 9.69% of all HITMs on the line holding its refcount
words (offsets 12/16). The runner-up lines are one per worker thread (the
interpreter's per-thread biased-refcount state) and CPython's parking-lot
mutex `buckets` (`Python/lock.c`) — the locking fallback for contended
objects. c2c summary, shared vs immortalized run:

| `perf c2c` (16 threads) | shared | immortalized |
|---|---:|---:|
| Load local HITM | 11,151 | **1** |
| Load hits on shared lines | 81,901 | **1** |
| **Locked (atomic) accesses on shared lines** | **19,682** | **0** |
| Store hits on shared lines | 44,391 | **0** |

## Claim 4 — "contention doesn't cap scaling, it inverts it"

**Tool:** the same fixed-work sweep across thread counts (wall time of the
measured BFS window only; 1–16 threads pinned to P-cores, 24/32 unpinned):

| threads | shared | immortalized | shared speedup vs 1T | immortal speedup |
|---:|---:|---:|---:|---:|
| 1 | 1.41 s | 1.02 s | 1.0× | 1.0× |
| 2 | 1.21 s | 0.54 s | 1.17× | 1.9× |
| 4 | 1.07 s | 0.35 s | 1.32× | 2.9× |
| 8 | 1.49 s | 0.19 s | 0.95× ← *inverting* | 5.3× |
| 16 | 3.74 s | 0.13 s | **0.38×** | 7.8× |
| 24 | 6.05 s | 0.076 s | 0.23× | 13.5× |
| 32 | 6.10 s | 0.085 s | **0.23× — 4.3× slower than 1 thread** | 12.0× |

Shared peaks at 1.32× at 4 threads and then *loses* ground; at 32 threads it
is 4.3× slower than single-threaded and **72× slower than the immortalized
run of the same work**. Two footnotes the table also proves:

- Even at **1 thread** immortalization wins (1.41 → 1.02 s): the lone worker
  is a *foreign* thread (main built the graph), so every refcount op takes
  the atomic path — uncontended atomics aren't free either.
- A third mode, `local` (each thread deep-copies the graph first), lands at
  0.23 s @16T — 16× better than shared but still 2.3× behind immortal, with
  18.7 M residual HITMs. `deepcopy` does not copy immutable atoms, so the
  ~3,000 heap `int` objects are still shared across all "private" copies and
  their refcounts still ping-pong. Thread-local copies cannot fully de-share
  Python data; stopping the refcount writes can.

## Claim 5 — "immortalization sets a sentinel; INCREF/DECREF skip; the line becomes read-only-shared"

**Tool:** header inspection (proof E) + the counters above.

- After `immortalize_tree`: `ob_ref_local == 0xFFFFFFFF` (the PEP 683
  sentinel), `ob_tid` cleared to unowned.
- **8,000,000 reads from 4 foreign threads** while sampling the header
  100,000 times: the counters were observed in **exactly one state** — they
  never moved. Zero refcount writes.
- The MESI consequence is the c2c table above: stores and locked accesses on
  shared lines drop to literal zero, and read-only-shared lines replicate
  into every core's cache (HITM 11,151 → 1).

---

*Numbers are from one quiesced run each; re-runs vary by ~10% but every
ratio above is orders-of-magnitude stable. On a different CPU the event
names differ (these are Intel Raptor Lake P-core events; AMD exposes the
equivalent via IBS) but the shape reproduces anywhere biased refcounting
meets MESI.*
