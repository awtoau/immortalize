"""Tests for immortalize.

Everything that actually immortalizes leaks those objects by design; a test
process is the one place that's harmless.  Behavioural tests are gated on
``available()`` so the suite also passes on interpreters without the symbol
(where the package degrades to a documented no-op).
"""
import gc
import sys
import weakref

import pytest

import immortalize as im


def test_api_surface():
    assert isinstance(im.available(), bool)
    assert isinstance(im.is_free_threaded(), bool)
    assert im.__version__


def test_noop_when_unavailable(monkeypatch):
    monkeypatch.setattr(im, "_SET", None)
    obj = {"a": 1}
    assert im.immortalize(obj) is obj
    assert im.immortalize_tree(obj) == 0


needs_symbol = pytest.mark.skipif(
    not im.available(), reason="interpreter does not export _Py_SetImmortal")


@needs_symbol
def test_immortalize_single():
    obj = {"k": "v"}
    assert not im.is_immortal(obj)
    assert im.immortalize(obj) is obj
    assert im.is_immortal(obj)


@needs_symbol
def test_immortalize_is_idempotent():
    obj = [1, 2, 3]
    im.immortalize(obj)
    im.immortalize(obj)
    assert im.is_immortal(obj)


@needs_symbol
def test_tree_walks_containers():
    inner_list = [10.5, "deep"]
    inner = {"nested": inner_list}
    tup = ("t1", "t2")
    fs = frozenset({"fs-member"})
    root = {"a": inner, "b": tup, "c": {7, 8}, "d": fs, "e": bytearray(b"buf")}
    n = im.immortalize_tree(root)
    assert n > 0
    for o in (root, inner, inner_list, tup, fs,
              root["e"], inner_list[1], tup[0]):
        assert im.is_immortal(o), repr(o)


@needs_symbol
def test_tree_handles_cycles():
    a = []
    a.append(a)
    b = {"self": None}
    b["self"] = b
    assert im.immortalize_tree([a, b]) >= 3
    assert im.is_immortal(a)
    assert im.is_immortal(b)


@needs_symbol
def test_tree_walks_instance_dict_and_slots():
    class WithDict:
        def __init__(self):
            self.payload = ["dict-held"]

    class WithSlots:
        __slots__ = ("x", "y")

        def __init__(self):
            self.x = ["slot-held"]
            # y deliberately unset: walker must tolerate AttributeError

    d, s = WithDict(), WithSlots()
    im.immortalize_tree([d, s])
    assert im.is_immortal(d)
    assert im.is_immortal(d.payload)
    assert im.is_immortal(s)
    assert im.is_immortal(s.x)


@needs_symbol
def test_tree_skips_functions_modules_classes():
    def fn():
        pass

    class Klass:
        pass

    root = {"f": fn, "m": sys, "c": Klass, "data": ["kept"]}
    im.immortalize_tree(root)
    assert im.is_immortal(root)
    assert im.is_immortal(root["data"])
    # 3.13 free-threaded immortalizes all deferred-refcount objects
    # (functions, modules) as soon as any extra thread starts (gh-117783,
    # removed in 3.14), so the negative asserts only hold from 3.14 on.
    if not (im.is_free_threaded() and sys.version_info < (3, 14)):
        assert not im.is_immortal(fn)
        assert not im.is_immortal(sys)
    # NB: classes are often already immortal/deferred on some builds; the
    # contract is only that the walker never *calls* immortalize on them, so
    # no assertion on Klass's state -- just that the walk completed.


@needs_symbol
def test_tree_limit_stops_early():
    root = [[i, str(i)] for i in range(100)]
    n = im.immortalize_tree(root, limit=5)
    assert n == 5


@needs_symbol
def test_tree_limit_zero_immortalizes_nothing():
    obj = {"payload": ["untouched"]}
    assert im.immortalize_tree(obj, limit=0) == 0
    assert not im.is_immortal(obj)
    assert not im.is_immortal(obj["payload"])


@needs_symbol
def test_tree_survives_dead_weakref_proxy():
    class T:
        pass

    t = T()
    proxy = weakref.proxy(t)
    del t
    gc.collect()
    root = {"corpse": proxy, "data": ["still-frozen"]}
    im.immortalize_tree(root)  # must not raise ReferenceError
    assert im.is_immortal(root)
    assert im.is_immortal(root["data"])


@needs_symbol
def test_tree_survives_raising_dict_property():
    class Hostile:
        @property
        def __dict__(self):
            raise RuntimeError("no introspection for you")

    root = [Hostile(), ["sibling"]]
    im.immortalize_tree(root)  # must not abort the walk
    assert im.is_immortal(root)
    assert im.is_immortal(root[1])


@needs_symbol
def test_tree_walks_container_subclass_attributes():
    class AttrDict(dict):
        pass

    d = AttrDict(k=["value"])
    d.attr = ["attribute-held"]
    im.immortalize_tree(d)
    assert im.is_immortal(d)
    assert im.is_immortal(d["k"])
    assert im.is_immortal(d.attr)


@needs_symbol
def test_tree_handles_single_string_slots():
    class OneSlot:
        __slots__ = "x"

        def __init__(self):
            self.x = ["slot-value"]

    s = OneSlot()
    im.immortalize_tree(s)
    assert im.is_immortal(s)
    assert im.is_immortal(s.x)


@needs_symbol
def test_leaf_types_not_walked_but_immortalized():
    s = "leaf-string-%d" % id(object())
    n = im.immortalize_tree(s)
    assert n == 1
    assert im.is_immortal(s)


@needs_symbol
def test_immortal_survives_del_and_gc():
    obj = {"payload": list(range(50))}
    im.immortalize_tree(obj)
    ref = id(obj)
    del obj
    gc.collect()
    # Nothing to assert directly on the freed-or-not memory without unsafe
    # tricks; the meaningful invariant is that del+collect neither crashed
    # nor raised.  (Immortals are never deallocated by design.)
    assert isinstance(ref, int)


# ---- v0.2 diagnostics & guardrails ------------------------------------------

needs_ft = pytest.mark.skipif(
    not (im.available() and im.is_free_threaded()),
    reason="needs a free-threaded build exporting _Py_SetImmortal")


def test_probe_unsupported_reports_cleanly(monkeypatch):
    if im.is_free_threaded():
        pytest.skip("only meaningful on non-free-threaded builds")
    r = im.probe({"a": 1})
    assert r["supported"] is False
    assert "reason" in r


@needs_ft
def test_probe_cold_then_foreign_touched():
    import threading
    # runtime-built strings and LARGE ints only: compile-time constants,
    # single-char strings, and the static small-int cache (which reaches 1024
    # on 3.15t) are pre-immortalized and would pollute the "cold" baseline.
    cold = {"key%d" % i: ["val%d" % i, i + 10_000] for i in range(50)}
    r0 = im.probe(cold)
    assert r0["supported"] and r0["objects_sampled"] > 0
    assert r0["shared_evidence"] == 0
    assert r0["immortal"] == 0

    hold, release = threading.Event(), threading.Event()

    def toucher():
        grabbed = [v for v in cold.values()]
        hold.set()
        release.wait()
        del grabbed

    t = threading.Thread(target=toucher)
    t.start(); hold.wait()
    r1 = im.probe(cold)
    release.set(); t.join()
    assert r1["shared_evidence"] > 0
    assert r1["shared_evidence_fraction"] > 0

    im.immortalize_tree(cold)
    r2 = im.probe(cold)
    assert r2["immortal"] == r2["objects_sampled"]
    assert r2["immortal_fraction"] == 1.0


@needs_symbol
def test_strict_refuses_generator_and_freezes_nothing():
    gen = (x for x in range(3))
    root = {"gen": gen, "data": ["untouched"]}
    with pytest.raises(im.UnsafeToImmortalize) as exc:
        im.immortalize_tree(root, strict=True)
    assert "generator" in str(exc.value)
    assert not im.is_immortal(root)
    assert not im.is_immortal(root["data"])


@needs_symbol
def test_strict_refuses_open_file(tmp_path):
    f = open(tmp_path / "x.txt", "w")
    try:
        root = [f, ["sibling"]]
        with pytest.raises(im.UnsafeToImmortalize):
            im.immortalize_tree(root, strict=True)
        assert not im.is_immortal(root)
    finally:
        f.close()


@needs_symbol
def test_strict_refuses_lock_and_del():
    import threading

    class WithDel:
        def __del__(self):
            pass

    with pytest.raises(im.UnsafeToImmortalize) as exc:
        im.immortalize_tree({"l": threading.Lock()}, strict=True)
    assert "lock" in str(exc.value)
    with pytest.raises(im.UnsafeToImmortalize) as exc:
        im.immortalize_tree({"d": WithDel()}, strict=True)
    assert "__del__" in str(exc.value)


@needs_symbol
def test_strict_passes_clean_structure():
    root = {"a": [1, 2.5, "s"], "b": ("t",)}
    n = im.immortalize_tree(root, strict=True)
    assert n > 0
    assert im.is_immortal(root)


@needs_symbol
def test_stats_counts_calls_and_objects():
    before = im.stats()
    im.immortalize_tree(["counted", 1])
    after = im.stats()
    assert after["tree_calls"] == before["tree_calls"] + 1
    assert after["objects_frozen"] > before["objects_frozen"]
    assert any("test_immortalize" in site for site in after["call_sites"])


@needs_symbol
def test_repeat_call_site_warns():
    saved = im.repeat_call_warning
    im.repeat_call_warning = 3
    try:
        def freeze_once():
            im.immortalize_tree(["leak", "leak"])   # one call site
        freeze_once()
        freeze_once()
        with pytest.warns(ResourceWarning, match="per-iteration"):
            freeze_once()
    finally:
        im.repeat_call_warning = saved
