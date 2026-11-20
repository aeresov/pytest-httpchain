"""Payloads shared by the unit tests of more than one module.

A plain helpers module (not conftest.py, which pytest treats as a plugin file,
not an import target)::

    from tests.unit.helpers import NOT_FOUND, TOO_DEEP_TO_PARSE, TOO_DEEP_TO_WALK, on_bounded_stack
"""

import threading
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Protocol, SupportsIndex

import pytest

# Beyond what CPython's JSON decoder parses on an 8 MiB stack, by a wide margin:
# 3.13 counts to a fixed 10,000 levels (3,000 on Windows), and 3.14 measures
# the stack itself and gives out near 65,000 levels on 8 MiB. The decoder raises
# RecursionError, which is not a ValueError. Parse it only under
# `on_bounded_stack`: on 3.14 a larger stack parses it fine.
TOO_DEEP_TO_PARSE = b"[" * 1_000_000 + b"]" * 1_000_000

# Parses everywhere, but Python code that walks it (pprint, jsonschema) spends
# at least one frame per level, so it overflows the default recursion limit of
# 1,000 on its own, whatever the stack size.
TOO_DEEP_TO_WALK = b"[" * 1_000 + b"]" * 1_000

# Far past the default recursion limit of 1,000: only a walker that spends no
# stack frame per level of nesting gets through a value this deep.
BEYOND_RECURSION_LIMIT = 5_000

# Deep enough that a walker spending two frames per level overflows, shallow
# enough to load: the reference resolver itself spends one frame per level.
LOADABLE_BUT_DEEP = 700

_BOUNDED_STACK_SIZE = 8 * 1024 * 1024

# How the OS says a file is missing, as a regex: Windows' os.stat() words it
# its own way (`[WinError 2] The system cannot find the file specified`).
NOT_FOUND = r"\[(?:Errno|WinError) 2\] (?:No such file or directory|The system cannot find the file specified)"


def nested(leaf: Any, depth: int) -> Any:
    """``leaf`` under ``depth`` levels of alternating single-key dicts and
    single-item lists, so a walker's dict and list branches both recur."""
    value = leaf
    for level in range(depth):
        value = [value] if level % 2 else {"k": value}
    return value


def on_bounded_stack[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call ``fn`` on a thread with an 8 MiB stack, returning or raising what it does.

    On 3.14 the decoder's depth limit is set by the parsing thread's stack, and
    a runner's main thread can have far more than 8 MiB (a GitHub-hosted one
    parsed 100,000 levels), so decoder overflow only reproduces on a stack of
    known size.
    """
    previous = threading.stack_size(_BOUNDED_STACK_SIZE)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(fn, *args, **kwargs)
    finally:
        threading.stack_size(previous)
    return future.result()


class _StrictMarkersConfig:
    """What `pytest.mark` reads of a session's config, for a session run with
    strict markers that registers pytest's own marks only: pytest 9.0 and 9.1
    read the ``markers`` ini lines, 9.2 ``_iter_registered_markers()``."""

    MARKERS = ("skip", "skipif", "xfail", "parametrize", "usefixtures", "filterwarnings")

    def getini(self, name: str) -> Any:
        return {"markers": list(self.MARKERS), "strict_markers": True}.get(name)

    def _iter_registered_markers(self) -> Iterator[SimpleNamespace]:
        return (SimpleNamespace(name=name) for name in self.MARKERS)


@contextmanager
def strict_markers() -> Generator[None]:
    """`pytest.mark` as collection under strict markers finds it: a mark the
    session does not register fails with pytest's Failed, a BaseException.
    The unit run's own config, and the marks it has found registered, are set
    aside meanwhile, and put back."""
    mark = pytest.mark
    saved = mark._config, mark._markers
    mark._config = _StrictMarkersConfig()  # ty: ignore[invalid-assignment]
    mark._markers = set()
    try:
        yield
    finally:
        mark._config, mark._markers = saved


class Proxy:
    """``value`` as a proxy object presents it (wrapt's ``ObjectProxy``,
    lazy-object-proxy's ``Proxy``): its ``__class__`` is ``value``'s, so
    ``isinstance`` takes it for one, and it forwards attributes, ``str()``,
    ``len()``, iterating, indexing, ``hash()``, ``==`` and truth to
    ``value``, as those do. Its type is its own, which what goes by an
    object's real type reads: ``json.dumps`` refuses it, and so does a
    ``str`` method given it as an argument. ``unittest.mock.Mock(spec=...)``
    passes ``isinstance`` the same way and forwards none of these."""

    def __init__(self, value: Any) -> None:
        self._value = value

    @property
    def __class__(self) -> type:
        return type(self._value)

    def __getattr__(self, name: str) -> Any:
        if name == "_value":  # not set yet: no value to forward to
            raise AttributeError(name)
        return getattr(self._value, name)

    def __str__(self) -> str:
        return str(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._value)

    def __getitem__(self, key: Any) -> Any:
        return self._value[key]

    def __hash__(self) -> int:
        return hash(self._value)

    def __eq__(self, other: object) -> bool:
        return self._value == other

    def __bool__(self) -> bool:
        return bool(self._value)


def posing_subclass(base: type, claimed: type) -> type:
    """A subclass of ``base`` whose instances' ``__class__`` is ``claimed``:
    a real ``base``, which isinstance takes for a ``claimed`` as well. No
    ordinary class does this; one that does passes a check of its real
    type, and code that goes by isinstance reads it as what it claims."""
    return type(f"{base.__name__.title()}As{claimed.__name__.title()}", (base,), {"__class__": property(lambda self: claimed)})


IntAsStr = posing_subclass(int, str)
StrAsBytes = posing_subclass(str, bytes)
StrAsInt = posing_subclass(str, int)
BytesAsStr = posing_subclass(bytes, str)
ListAsDict = posing_subclass(list, dict)
DictAsList = posing_subclass(dict, list)
# A subclass claiming its own base says nothing untrue: isinstance reads it
# as what it is.
StrAsStr = posing_subclass(str, str)
IntAsInt = posing_subclass(int, int)
IntAsBool = posing_subclass(int, bool)
FloatAsInt = posing_subclass(float, int)
FloatAsStr = posing_subclass(float, str)


class Named(Protocol):
    """A Protocol, which no class check takes unless runtime_checkable:
    ``issubclass(str, Named)`` raises TypeError."""

    name: str


# Claims a class whose check raises: whether the claim is true cannot be told.
StrAsNamed = posing_subclass(str, Named)
# Claims no class at all.
StrClaimsText = type("StrClaimsText", (str,), {"__class__": property(lambda self: "str")})


def counting_reads(base: type) -> tuple[type, list[int]]:
    """A class whose instances count the reads of their ``__class__`` (a
    proxy's runs its own code): ``base`` itself for ``object``, a stand-in
    proxy, else a subclass of ``base`` claiming ``base``, an honest one."""
    reads = [0]

    def claimed(self: Any) -> type:
        reads[0] += 1
        return str if base is object else base

    return type(f"Counting{base.__name__.title()}", (base,), {"__class__": property(claimed)}), reads


def exiting_claim() -> Any:
    """A str whose ``__class__``, read, calls pytest.exit()."""

    def claimed(self: Any) -> type:
        pytest.exit("exit while reading __class__")

    return type("ExitingClaim", (str,), {"__class__": property(claimed)})("x")


class UpperHexBytes(bytes):
    """Bytes whose slices keep the class and write upper-case hex: the line
    and the redaction must write it the same way, whatever its methods."""

    def __getitem__(self, key: Any) -> Any:
        item = super().__getitem__(key)
        return UpperHexBytes(item) if isinstance(key, slice) else item

    def hex(self, sep: str | bytes = "", bytes_per_sep: SupportsIndex = 1) -> str:
        return (super().hex(sep, bytes_per_sep) if sep else super().hex()).upper()


class ShiftedBytes(bytes):
    """Bytes whose slices start one byte late."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            return bytes.__getitem__(self, slice((key.start or 0) + 1, None if key.stop is None else key.stop + 1, key.step))
        return super().__getitem__(key)


class LowerKey(str):
    """A key whose ``lower()`` is its own: listed or not is str's to say."""

    def lower(self) -> str:
        return "not-a-listed-key"


class Unreadable:
    """A proxy whose wrapped value cannot be had, as lazy-object-proxy's
    ``Proxy`` of a factory that raises: reading its ``__class__`` runs the
    factory."""

    @property
    def __class__(self) -> type:
        raise RuntimeError("factory failed")


class StrUnreadable(str):
    """A real str whose ``__class__`` cannot be read."""

    @property
    def __class__(self) -> type:
        raise RuntimeError("factory failed")
