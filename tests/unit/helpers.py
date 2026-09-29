"""Payloads shared by the unit tests of more than one module.

A plain helpers module (not conftest.py, which pytest treats as a plugin file,
not an import target)::

    from tests.unit.helpers import NOT_FOUND, TOO_DEEP_TO_PARSE, TOO_DEEP_TO_WALK, on_bounded_stack
"""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

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
