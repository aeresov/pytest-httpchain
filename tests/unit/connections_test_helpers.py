"""Importable connection adapters for the ``connections`` and carrier tests:
in-process, no sockets.

A plain module for the reason ``response_steps_test_helpers`` is one: an
adapter is named by a dotted import string (``tests.unit.connections_test_helpers:FakeAdapter``),
and one naming the test module would load a second copy of it under
``--import-mode=importlib``.

A `FakeAdapter` is built with the name of a `Peer` (its ``peer`` param),
which a test registers in `PEERS` and drives: what the other side pushes,
answers and fails with, and what it was sent.
"""

import queue
import threading
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from pytest_httpchain.errors import RequestError

# The peers adapters connect to, by name: a test registers its own (`peer`).
PEERS: dict[str, "Peer"] = {}


class _Closed:
    """What `FakeAdapter.close` puts in its inbox, to wake a waiting receive()."""


class _End:
    """The other side ending the connection: receive() raises ``error``, or EOFError."""

    def __init__(self, error: BaseException | None) -> None:
        self.error = error


class Peer:
    """The other side of a fake connection. ``greeting`` is what connect()
    returns; ``reply`` maps a message sent to the messages pushed in answer;
    ``send_return`` is what send() returns (a callable gets the message);
    the ``*_error`` raise from their call."""

    def __init__(
        self,
        *,
        greeting: Any = None,
        reply: Callable[[dict[str, Any]], list[Any]] | None = None,
        send_return: Any = None,
        connect_error: BaseException | None = None,
        send_error: BaseException | None = None,
        close_error: BaseException | None = None,
        init_error: BaseException | None = None,
        debug: Any = None,
    ) -> None:
        self.greeting = greeting
        self.reply = reply
        self.send_return = send_return
        self.connect_error = connect_error
        self.send_error = send_error
        self.close_error = close_error
        self.init_error = init_error
        self.debug = debug
        self.sent: list[dict[str, Any]] = []
        self.adapters: list[FakeAdapter] = []
        self.connects = 0
        self.closes = 0

    @property
    def current(self) -> "FakeAdapter":
        return self.adapters[-1]

    def push(self, *messages: Any) -> None:
        """Messages the other side sends, to the latest connection."""
        for message in messages:
            self.current.inbox.put(message)

    def end(self, error: BaseException | None = None) -> None:
        """The other side ends the latest connection: receive() raises
        ``error``, or EOFError for a clean end."""
        self.current.inbox.put(_End(error))


def peer(name: str, **settings: Any) -> Peer:
    """Register a `Peer` under ``name`` and return it."""
    PEERS[name] = created = Peer(**settings)
    return created


class FakeAdapter:
    """An adapter whose other side is a `Peer`, in the same process."""

    def __init__(self, peer: str, **params: Any) -> None:
        self.peer = PEERS[peer]
        if self.peer.init_error is not None:
            raise self.peer.init_error
        self.params = params
        self.inbox: queue.Queue[Any] = queue.Queue()
        self.connected = False
        self.closed = False
        self.receive_threads: set[str] = set()
        self.peer.adapters.append(self)

    def connect(self, timeout: float) -> Any:
        self.peer.connects += 1
        self.timeout = timeout
        if self.peer.connect_error is not None:
            raise self.peer.connect_error
        self.connected = True
        return self.peer.greeting

    def send(self, message: dict[str, Any]) -> Any:
        if self.peer.send_error is not None:
            raise self.peer.send_error
        self.peer.sent.append(message)
        if self.peer.reply is not None:
            for answer in self.peer.reply(message):
                self.inbox.put(answer)
        return self.peer.send_return(message) if callable(self.peer.send_return) else self.peer.send_return

    def receive(self, timeout: float) -> Any:
        self.receive_threads.add(threading.current_thread().name)
        try:
            item = self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(item, _Closed):
            raise OSError("connection closed")
        if isinstance(item, _End):
            raise item.error if item.error is not None else EOFError()
        return item

    def close(self) -> None:
        self.peer.closes += 1
        self.closed = True
        self.inbox.put(_Closed())
        if self.peer.close_error is not None:
            raise self.peer.close_error

    def debug_info(self) -> Any:
        if isinstance(self.peer.debug, BaseException):
            raise self.peer.debug
        return self.peer.debug


# Set by a test, to see whether the reader thread runs in its context.
PROBE: ContextVar[str] = ContextVar("connections_probe", default="unset")


class ContextAdapter(FakeAdapter):
    """A `FakeAdapter` noting, on its peer, what `PROBE` reads in receive()."""

    def receive(self, timeout: float) -> Any:
        self.peer.seen_probe = PROBE.get()
        return super().receive(timeout)


class MinimalAdapter(FakeAdapter):
    """A `FakeAdapter` without debug_info()."""

    debug_info = None


# Released by a test once it is done with a `StuckAdapter`, so its reader
# thread does end.
STUCK_RELEASE = threading.Event()


class StuckAdapter(FakeAdapter):
    """An adapter whose receive() ignores close(): it returns only once
    `STUCK_RELEASE` is set."""

    def receive(self, timeout: float) -> Any:
        STUCK_RELEASE.wait()
        return None


class RetryableAdapter(FakeAdapter):
    """An adapter that opts its own transport errors into retry."""

    def connect(self, timeout: float) -> Any:
        self.peer.connects += 1
        if self.peer.connects < 2:
            raise RequestError("handshake not ready", retryable=True)
        return {"ready": True}


def is_pong(message: dict[str, Any]) -> bool:
    """A verify function a connection stage calls with message=."""
    return message.get("op") == "pong"


def saves_message(message: dict[str, Any]) -> dict[str, Any]:
    """A save function returning the reserved name ``message``."""
    return {"message": message}


def not_a_class(**params: Any) -> None:
    """A function where an adapter class belongs."""


NOT_AN_ADAPTER = 42
