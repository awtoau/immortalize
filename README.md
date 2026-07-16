# immortalize

**Opt shared read-only objects out of reference counting on free-threaded
CPython.** PEP 683 immortalization, packaged as a library:

```python
import immortalize

graph = build_big_shared_structure()   # fully built; read-only from here on
immortalize.immortalize_tree(graph)    # freeze point
start_worker_threads(graph)            # contention-free parallel reads
```

```
pip install immortalize
```

Pure Python, zero dependencies, ~200 lines. Extracted from
[pluribus](https://github.com/awtoau/pluribus), an FPGA reverse-engineering
engine whose parallel netlist-reachability stage hit the wall this package
removes.

---

## ⚠️ Read this first

This is an **expert tool** with sharp edges. Every one of these is
load-bearing:

1. **Immortalization is permanent and one-way.** An immortal object is
   *never freed* — `Py_DECREF` becomes a no-op, so its destructor never runs
   and its memory is held until the process exits. There is **no
   un-immortalize**, and there cannot be one: once refcount operations are
   skipped, the true reference count is unknowable, so any reversal would
   risk use-after-free. Permanence is forced by the design, not a choice.

2. **Only immortalize process-lifetime objects.** Immortalizing per-request,
   per-task, or per-iteration data is a memory leak, one object graph per
   iteration. If your code rebuilds the structure each run (caches, batch
   pipelines, servers), immortalize **only** the build that lives to the end
   of the process. When in doubt, don't.

3. **Immortality is NOT immutability.** An immortal dict can still be
   mutated, and concurrent mutation is still a data race that immortalization
   does nothing to fix. This tool is for structures that are strictly
   **read-only after a known freeze point**. If anything mutates the
   structure after workers start, you have a bug this package will happily
   make faster.

4. **It calls a private (but exported) CPython symbol.** The binding is a
   `ctypes` call to `_Py_SetImmortal` — the same routine CPython itself runs
   when it immortalizes objects at runtime (interned strings, deferred
   objects; `None`/`True`/small ints are *born* immortal at build time and
   never pass through it). The symbol has been present from CPython 3.12
   through 3.15 including all free-threaded builds we tested, but it is
   **not a public API** and could change in a future CPython — and not every
   distribution exports it (details below). `immortalize.available()` tells
   you whether the running interpreter exposes it; when it doesn't, every
   call in this package degrades to a documented no-op rather than an error.

5. **`immortalize_tree` walks what's reachable — audit your roots.** The
   deep walker follows dict keys/values, list/tuple/set/frozenset members,
   and instance `__dict__`/`__slots__`. If your "read-only graph" secretly
   holds a reference to a session object, a cache, or an open file, those
   become immortal too. Walk something you understand.

6. **Refcount-observing code sees sentinel values.** After immortalization,
   `sys.getrefcount(obj)` returns an enormous sentinel rather than a real
   count. Test frameworks or leak detectors that assert on refcounts will be
   confused by immortal objects (CPython's own test suite special-cases
   them).

## The problem this solves

On free-threaded CPython (PEP 703, `python3.13t`+), reference counting is
still there — it's just been made thread-safe. Almost every touch of a
shared object performs an `INCREF`/`DECREF` pair: indexing a dict,
iterating a list, binding a local. (3.14's stack references elide some of
these, but the hot container reads that dominate a graph traversal still
count.)

Free-threading uses **biased reference counting**: each object carries two
counters — `ob_ref_local` (cheap, non-atomic, owned by the thread that
created the object) and `ob_ref_shared` (**atomic**, used by every other
thread). A structure built by your main thread and then read by N workers
takes the atomic path in all N workers.

Those atomic read-modify-writes all land on the **same cache lines** — the
refcount fields of the hot objects. Under MESI cache coherence, an atomic
write needs exclusive ownership of the line, which invalidates it in every
other core. The line ping-pongs between cores; every operation stalls
waiting to reacquire it. The actual computation is often trivial next to
this coherence traffic.

The punchline: contention doesn't just cap your scaling, it **inverts** it.
More threads = more contenders for the same fixed set of cache lines = slower
than single-threaded.

**Immortalization** (PEP 683) sets the refcount to a permanent sentinel that
`INCREF`/`DECREF` check first and skip. No more refcount writes → the
object's cache lines become read-only-shared (which MESI serves to all cores
simultaneously, zero coherence traffic) → the wall disappears.

## Real-world numbers

From the workload this was extracted from
([pluribus](https://github.com/awtoau/pluribus) `reach.py`): all-pairs BFS
reachability over a recovered FPGA netlist graph (~3,000 nets, producing
1,021,839 reachable pairs), workers pulling nets from a shared queue and
walking a shared adjacency dict. 32-core Linux, CPython 3.15.0b3
free-threaded build. Output verified byte-identical with and without.

| threads | shared dict (refcounted) | after `immortalize_tree(graph)` |
|--------:|-------------------------:|--------------------------------:|
| 1       | 0.5 s                    | 0.4 s                            |
| 4       | 0.2 s                    | 0.2 s                            |
| 8       | 0.2 s                    | **0.1 s**                        |
| 16      | 0.8 s ← *regressing*     | **0.1 s**                        |
| 32      | 1.0 s ← *worse than 1 thread* | 0.2 s                       |

Note the left column: past 8 threads, adding workers made the refcounted run
*slower*, bottoming out worse than single-threaded at 32. That is refcount
cache-line contention, and no amount of code tuning inside the workers fixes
it — the fix is to stop refcounting the shared data.

The freeze itself is fast: immortalizing a 1.5-million-object routing graph
takes ~1 s, once, at startup.

## When to use it — and when not to

The decision axis is **lifetime first, size second**:

| your data is… | hot + shared? | right tool |
|---|---|---|
| process-lifetime, read-only | yes | **`immortalize_tree` at the freeze point** (size-independent: the call is O(graph), the "leak" is free because it lives forever anyway) |
| transient, small | yes | thread-local copies, or hoist hot attributes into locals before the loop |
| transient, big | yes | restructure — flat arrays / CSR indices (see below) — copies are too expensive and immortalizing would leak |
| mutable + shared | — | a lock. Immortalization is only for read-only data |

**The structural alternative:** if you can redesign, don't put hot shared
data in per-node Python objects at all. A graph as two flat integer arrays
(`offsets[]`/`neighbors[]`, CSR-style, via `array`/`numpy`/`memoryview`) has
*one* Python object to refcount instead of one per node — the contention
disappears structurally, and it's faster and smaller even on GIL builds.
`immortalize` is the ten-line retrofit; CSR is the "correct" HPC answer when
the refactor is worth it.

**On GIL builds this package still works** (PEP 683 landed in 3.12) — there's
no contention to fix, but immortalizing shared statics before `fork()` stops
refcount writes from dirtying copy-on-write pages, the original
Instagram-scale use case that motivated PEP 683.

## API

```python
immortalize.available() -> bool
    # Does this interpreter export the immortalization entry point?
    # When False, everything below is a documented no-op.

immortalize.is_free_threaded() -> bool
    # Free-threaded build with the GIL actually off?
    # (The contention motivation only exists when True.)

immortalize.immortalize(obj) -> obj
    # Immortalize one object. PERMANENT. Returns obj for chaining.

immortalize.immortalize_tree(root, *, limit=None) -> int
    # Immortalize root and everything reachable from it (cycle-safe).
    # Walks dicts, sequences/sets, instance __dict__ and __slots__.
    # Skips modules/functions/methods/classes. Returns the object count.
    # `limit` is a safety valve: stop after N objects (leaves the
    # structure partially immortalized).

immortalize.is_immortal(obj) -> bool
    # PEP 683 immortality check (PyUnstable_IsImmortal on 3.14+,
    # refcount-threshold heuristic on 3.12/3.13).
```

## Why not the official APIs?

Fair question — CPython has adjacent machinery, and none of it covers this
use case. Researched against CPython source and verified empirically:

- **`PyUnstable_SetImmortal`** (public, 3.15a6+): true immortalization, but
  it *refuses* any object that is not uniquely referenced by the calling
  thread, and refuses `str` outright. It's designed for immortalizing at
  creation time while you're the sole owner (e.g. in `tp_new`). Called on an
  already-built, already-shared graph — the retrofit case this package
  exists for — it returns 0 and does nothing.

- **`PyUnstable_Object_EnableDeferredRefcount`** (public, 3.14+): a
  different mechanism (deferred reference counting, PEP 703), not
  immortalization. It only applies to GC-tracked container types — it
  returns 0 for `bytes`, `bytearray`, `str`, `int`, so it cannot help a big
  shared buffer at all, and deferred objects are still eventually scanned
  and freed by the GC.

- **`gc.freeze()`** (stdlib): sounds right, isn't. It moves objects to a
  "permanent generation" the *cycle collector* never scans — but reference
  counting continues exactly as before. It solves GC-pause and fork/CoW-page
  problems, not refcount contention.

- **`_Py_SetImmortal`** (what this package calls): unconditional, complete
  (sets the thread-id word, both refcount fields, and untracks from the GC).
  It is what CPython itself runs on its own immortals. The trade-off is the
  private-API caveat above, plus one practical one: **symbol visibility
  varies by distribution**. Standard python.org, Linux-distro, and
  free-threaded builds we tested (3.14, 3.14t, 3.15t) export it; some
  standalone redistributions (e.g. `python-build-standalone`, which `uv`
  installs) do not expose it to `ctypes`. `available()` is the runtime
  truth, and everything no-ops cleanly when it's `False` — your code runs
  identically, just without the optimization.

Prior art: NumPy hit exactly this contention on two shared capsule objects
(~20% multithreaded regression) — that discussion produced
`PyUnstable_SetImmortal`. Instagram's fork-server immortalization motivated
PEP 683 itself. There was, at time of writing, no pip-installable package
offering `immortalize(obj)` — hence this one.

## Testing

```
python -m pytest tests/ -v
```

The behavioural tests run wherever `available()` is True (CPython 3.12+
including free-threaded builds) and skip gracefully elsewhere. CI runs
3.12, 3.13, 3.13t, 3.14 and 3.14t across Linux/Windows/macOS, plus
experimental 3.15/3.15t rows; the Linux jobs additionally *assert* the
symbol is exported, so a toolchain change cannot turn the suite into a
silent all-skip green.

## License

MIT.
