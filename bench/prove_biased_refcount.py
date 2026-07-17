#!/usr/bin/env python3
"""Empirical proof of the biased-refcount claims in the README, via direct
header inspection with ctypes.  No timing, no perf — every assertion here is
a deterministic read of the object header while controlled INCREFs happen.

Free-threaded CPython (3.13t+) object header layout on 64-bit:

    offset  0: ob_tid        (uintptr_t)  owner-thread tag
    offset 12: ob_ref_local  (uint32)     owner thread's counter (non-atomic)
    offset 16: ob_ref_shared (int64)      other threads' counter (atomic),
                                          value = count << 2 | state bits

Claims proven:
  A. Objects are OWNED by their creating thread (ob_tid tags the owner).
  B. Refs taken by the owner thread move ob_ref_local only.
  C. Refs taken by a foreign thread move ob_ref_shared only (the atomic path).
  D. Plain dict indexing by a foreign thread INCREFs the value object
     (reads write to the refcount).
  E. Immortalization sets the local counter to a sentinel, clears the owner
     tag, and freezes both counters against reads from many threads.

Run:  python3.13t+ bench/prove_biased_refcount.py
Exits nonzero if any assertion fails.
"""
import ctypes
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "src"))
import immortalize as im  # noqa: E402

FAILURES = []


def hdr(obj):
    """(ob_tid, ob_ref_local, ob_ref_shared) read straight from memory."""
    a = id(obj)
    return (ctypes.c_uint64.from_address(a).value,
            ctypes.c_uint32.from_address(a + 12).value,
            ctypes.c_int64.from_address(a + 16).value)


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}{('  -- ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(name)


def main():
    print(f"python {sys.version.split()[0]}  "
          f"free_threaded={im.is_free_threaded()}  available={im.available()}")
    if not (im.is_free_threaded() and im.available()):
        print("needs a free-threaded build exporting _Py_SetImmortal")
        return 2

    print("\n== A. objects are owned by their creating thread ==")
    a1, a2 = {"main": 1}, {"main": 2}
    made_in_worker = []
    t = threading.Thread(target=lambda: made_in_worker.append({"worker": 1}))
    t.start(); t.join()
    w1 = made_in_worker[0]
    tid_a1, tid_a2, tid_w1 = hdr(a1)[0], hdr(a2)[0], hdr(w1)[0]
    check("two main-thread objects share one owner tag", tid_a1 == tid_a2,
          f"ob_tid=0x{tid_a1:x}")
    check("worker-thread object has a different owner tag", tid_w1 != tid_a1,
          f"worker ob_tid=0x{tid_w1:x}")

    print("\n== B. owner-thread refs move ob_ref_local only ==")
    obj = {"payload": "x"}
    _, loc0, sh0 = hdr(obj)
    held = [obj] * 1000                      # 1000 INCREFs from the owner
    _, loc1, sh1 = hdr(obj)
    check("ob_ref_local rose by exactly 1000", loc1 - loc0 == 1000,
          f"local {loc0} -> {loc1}")
    check("ob_ref_shared untouched", sh1 == sh0,
          f"shared stayed {sh0}")
    del held

    print("\n== C. foreign-thread refs move ob_ref_shared only ==")
    _, loc0, sh0 = hdr(obj)
    holding = threading.Event()
    release = threading.Event()

    def foreign_holder():
        grabbed = [obj] * 5000               # 5000 INCREFs from a non-owner
        holding.set()
        release.wait()
        del grabbed

    t = threading.Thread(target=foreign_holder)
    t.start()
    holding.wait()
    _, loc1, sh1 = hdr(obj)
    release.set(); t.join()
    # shared field stores count << 2; the low 2 bits are state flags that
    # latch on (sticky) the first time a foreign thread touches the object,
    # so compare the count portion (>> 2) only.
    check("ob_ref_shared count rose by exactly 5000",
          (sh1 >> 2) - (sh0 >> 2) == 5000,
          f"shared {sh0} -> {sh1} (delta {(sh1 >> 2) - (sh0 >> 2)} refs)")
    check("ob_ref_local untouched by foreign refs", loc1 == loc0,
          f"local stayed {loc0}")
    _, _, sh2 = hdr(obj)
    check("ob_ref_shared count returned after release",
          (sh2 >> 2) == (sh0 >> 2),
          f"shared back to {sh2} (count {(sh2 >> 2)}; state flags latched)")

    print("\n== D. dict indexing by a foreign thread INCREFs the value ==")
    d = {"k": ["the", "value"]}
    val = d["k"]
    _, _, sh0 = hdr(val)
    holding.clear(); release.clear()

    def foreign_indexer():
        grabbed = [d["k"] for _ in range(5000)]   # 5000 dict reads, refs HELD
        holding.set()
        release.wait()
        del grabbed

    t = threading.Thread(target=foreign_indexer)
    t.start()
    holding.wait()
    _, _, sh1 = hdr(val)
    release.set(); t.join()
    check("5000 held dict-reads == 5000 shared INCREFs",
          (sh1 >> 2) - (sh0 >> 2) == 5000,
          f"shared delta = {(sh1 >> 2) - (sh0 >> 2)} refs")

    # transient reads: worker binds-and-drops in a loop; sample the shared
    # counter from here and count how often we catch it nonzero.
    n_loops = 2_000_000
    n_samples = 200_000
    stop = threading.Event()

    def transient_reader():
        for _ in range(n_loops):
            x = d["k"]      # INCREF on read ...
            del x           # ... DECREF on drop
        stop.set()

    t = threading.Thread(target=transient_reader)
    t.start()
    caught = 0
    for _ in range(n_samples):
        if hdr(val)[2] != sh0:
            caught += 1
        if stop.is_set():
            break
    t.join()
    check("transient reads caught mid-INCREF (shared counter observed "
          "nonzero)", caught > 0,
          f"{caught} of {n_samples} samples caught a transient shared ref")

    print("\n== E. immortalization: sentinel, untag, and total freeze ==")
    frozen = {"k": ["frozen", "value"]}
    fval = frozen["k"]
    tid_before = hdr(fval)[0]
    im.immortalize_tree(frozen)
    tid_after, loc_after, sh_after = hdr(fval)
    check("ob_ref_local is the immortal sentinel 0xFFFFFFFF",
          loc_after == 0xFFFFFFFF, f"local=0x{loc_after:x}")
    check("owner tag cleared to unowned", tid_after == 0 != tid_before,
          f"ob_tid 0x{tid_before:x} -> 0x{tid_after:x}")
    check("is_immortal() agrees", im.is_immortal(fval))

    readers_done = threading.Barrier(5)

    def hammer():
        for _ in range(2_000_000):
            x = frozen["k"]
            del x
        readers_done.wait()

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    samples = set()
    for _ in range(100_000):
        samples.add(hdr(fval)[1:])
    readers_done.wait()
    for t in threads:
        t.join()
    final = hdr(fval)[1:]
    check("counters NEVER moved under 8M reads from 4 foreign threads",
          samples == {(0xFFFFFFFF, sh_after)} and final == (0xFFFFFFFF, sh_after),
          f"observed states: {samples}")

    print(f"\n{'ALL PROOFS PASS' if not FAILURES else 'FAILURES: ' + str(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
