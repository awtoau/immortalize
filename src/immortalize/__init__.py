"""immortalize — opt shared read-only objects out of reference counting.

In free-threaded CPython (PEP 703, python3.13t+), every thread that touches a
shared object performs an INCREF/DECREF on it.  For a large structure read by
many worker threads, those refcount writes are atomic read-modify-writes that
all land on the same cache lines, which then ping-pong between cores.  The
contention does not merely cap scaling -- it can invert it, making more
threads *slower* than one.

Immortalizing an object (PEP 683) sets its refcount to a permanent sentinel:
INCREF/DECREF become no-ops, the object's cache lines become read-only-shared,
and the contention disappears.

THIS IS AN EXPERT TOOL.  Read the safety notes before use:

  * Immortalization is PERMANENT and ONE-WAY.  An immortal object is never
    freed.  There is no un-immortalize -- after skipping refcounts, the true
    count is unknowable, so reversal cannot exist safely.
  * Only immortalize PROCESS-LIFETIME objects.  Immortalizing per-request /
    per-iteration data is a memory leak, one object graph at a time.
  * Immortality is NOT immutability.  An immortal dict can still be mutated,
    and concurrent mutation is still a data race.  This tool is for structures
    that are read-only after a known "freeze point".
  * The binding uses CPython's exported-but-private `_Py_SetImmortal` symbol
    (the same routine CPython itself runs when it immortalizes objects at
    runtime, e.g. interned strings; `None`/`True`/small ints are born immortal
    at build time).  It has been present from CPython 3.12 through 3.15 on
    standard builds, but it is not a public API, and some redistributions
    (e.g. python-build-standalone) do not export it.  `available()` reports
    whether the running interpreter exposes it; everything degrades to a no-op
    when it does not.

Typical usage -- the freeze-point idiom::

    graph = build_big_shared_graph()      # fully built, read-only from here on

    import immortalize
    immortalize.immortalize_tree(graph)   # freeze point: before workers start

    start_worker_threads(graph)           # refcount-contention-free reads

Extracted from the pluribus FPGA reverse-engineering engine
(https://github.com/awtoau/pluribus), where it removed a refcount-contention
wall in a parallel BFS (see the README for numbers).
"""
from __future__ import annotations

import ctypes
import sys
import types
import weakref
from typing import Any

__version__ = "0.1.0"

__all__ = [
    "available",
    "is_free_threaded",
    "is_immortal",
    "immortalize",
    "immortalize_tree",
]

_SET = None
_IS = None
try:
    _SET = ctypes.pythonapi._Py_SetImmortal
    _SET.argtypes = [ctypes.py_object]
    _SET.restype = None
except AttributeError:  # pragma: no cover - depends on the build
    _SET = None
try:
    # Public since 3.14; on older interpreters is_immortal() falls back to a
    # refcount-threshold heuristic.
    _IS = ctypes.pythonapi.PyUnstable_IsImmortal
    _IS.argtypes = [ctypes.py_object]
    _IS.restype = ctypes.c_int
except AttributeError:  # pragma: no cover - depends on the build
    _IS = None

# Never immortalize these: modules/functions/classes are shared machinery
# (some already handled by deferred refcounting), and freezing them adds
# nothing but surprise.  Weakref proxies are skipped because merely touching
# a dead proxy's type protocol raises ReferenceError.
_SKIP = (types.ModuleType, types.FunctionType, types.BuiltinFunctionType,
         types.MethodType, type,
         weakref.ProxyType, weakref.CallableProxyType, weakref.ref)
_EXACT_CONTAINERS = (dict, list, tuple, set, frozenset)
# Leaves: immortalize the object itself but do not walk inside it.
_LEAF = (str, bytes, bytearray, memoryview, int, float, complex, bool,
         type(None), range)

# PEP 683 immortal refcounts are enormous on every implementation (2**32-ish
# on 64-bit builds, saturated high values on 32-bit).  Anything above this
# threshold is not a countable reference total from real code.
_IMMORTAL_REFCNT_FLOOR = 2 ** 28


def available() -> bool:
    """True if this interpreter exposes the immortalization entry point.

    When False, :func:`immortalize` and :func:`immortalize_tree` are no-ops
    and :func:`is_immortal` may under-report.
    """
    return _SET is not None


def is_free_threaded() -> bool:
    """True on a free-threaded (PEP 703) build with the GIL actually off.

    Immortalization still *works* on GIL builds (PEP 683 landed in 3.12) and
    is useful there to stop refcount writes from dirtying copy-on-write pages
    in fork-based servers -- but the refcount-*contention* motivation only
    exists when this returns True.
    """
    fn = getattr(sys, "_is_gil_enabled", None)
    return not fn() if fn is not None else False


def is_immortal(obj: Any) -> bool:
    """True if `obj` is immortal (PEP 683)."""
    if _IS is not None:
        return bool(_IS(obj))
    return sys.getrefcount(obj) > _IMMORTAL_REFCNT_FLOOR


def immortalize(obj: Any) -> Any:
    """Immortalize a single object.  Returns `obj` for chaining.

    PERMANENT: the object will never be freed.  Only call this on objects
    that live for the remainder of the process.  No-op when
    :func:`available` is False.
    """
    if _SET is not None:
        _SET(obj)
    return obj


def immortalize_tree(root: Any, *, limit: int | None = None) -> int:
    """Immortalize `root` and everything reachable from it.  Returns the count.

    Walks dicts (keys and values), lists/tuples/sets/frozensets, and plain
    objects via ``__dict__`` and ``__slots__``.  Leaf types (str, bytes,
    numbers, ...) are immortalized but not walked into.  Modules, functions,
    methods and classes are skipped entirely.  Cycle-safe.

    PERMANENT and one-way: every visited object will never be freed.  Only
    call this at a freeze point on a fully built, read-only,
    process-lifetime structure.  `limit` is a hard cap: no more than `limit`
    objects are immortalized (a safety valve for unexpectedly huge graphs);
    when the cap is reached the walk stops and the structure is only
    partially immortalized.

    An object that misbehaves during the walk (a raising ``__dict__``
    property, exotic proxies, ...) is skipped rather than aborting the walk
    half-way through an irreversible operation.

    Returns 0 (and does nothing) when :func:`available` is False.
    """
    if _SET is None:
        return 0
    seen = set()
    stack = [root]
    n = 0
    while stack:
        o = stack.pop()
        oid = id(o)
        if oid in seen:
            continue
        seen.add(oid)
        try:
            if isinstance(o, _SKIP):
                continue
            if limit is not None and n >= limit:
                break
            _SET(o)
            n += 1
            if isinstance(o, _LEAF):
                continue
            if isinstance(o, dict):
                for k, v in o.items():
                    stack.append(k)
                    stack.append(v)
            elif isinstance(o, (list, tuple, set, frozenset)):
                stack.extend(o)
            # Subclasses of the builtin containers can also carry instance
            # attributes, so they fall through to the attribute walk too.
            if type(o) not in _EXACT_CONTAINERS:
                d = getattr(o, "__dict__", None)
                if isinstance(d, dict):
                    stack.append(d)
                for klass in type(o).__mro__:
                    slots = getattr(klass, "__slots__", ()) or ()
                    if isinstance(slots, str):
                        slots = (slots,)
                    for slot in slots:
                        try:
                            stack.append(getattr(o, slot))
                        except AttributeError:
                            pass
        except Exception:
            continue
    return n
