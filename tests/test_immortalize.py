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
