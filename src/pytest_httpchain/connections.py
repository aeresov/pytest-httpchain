"""Connection stages' runtime: a protocol adapter's sessions, their readers
and inboxes, and the wait for a message.

A scenario's ``connections`` name adapter classes (``module:Class``) the user
writes: the plugin knows no protocol, only the adapter's four calls
(`ADAPTER_METHODS`, typed as `ProtocolAdapter`). Each open makes a fresh
`_Session`: the adapter instance, a reader thread calling ``receive()`` over and
over, and a bounded inbox of what it returned, in arrival order. A chain's
sessions live in its `Connections` registry, by name; the carrier
(``carrier.Carrier``) runs the connection step on them and ends them with the
chain (`close_all`).

What a match means is not decided here: ``response_steps.message_matches``
judges one message, and the carrier hands `_Session.expect` a predicate built on
it, so this module stays below ``response_steps``. Nothing it raises from the
reader thread escapes it: the run treats warnings as errors, and an exception
leaving a thread is reported as one.
"""

import contextvars
import copy
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, Protocol

import pytest
from pydantic import BaseModel

from pytest_httpchain.errors import RequestError, StageExecutionError, VerificationError
from pytest_httpchain.redaction import DEFAULT_REDACTION, Redaction
from pytest_httpchain.userfunc import UserFunctionError, import_adapter

logger = logging.getLogger(__name__)


class ProtocolAdapter(Protocol):
    """What a connection's adapter class provides, for type checkers.

    The plugin builds it with the connection's rendered ``params`` as keyword
    arguments, calls ``connect`` and ``send`` on the test's thread and
    ``receive`` on the connection's reader thread, at the same time, and
    ``close`` while a ``receive`` may be waiting, which it must make return.
    An optional ``debug_info() -> str`` adds text to failure reports.
    """

    def connect(self, timeout: float) -> dict[str, Any] | None:
        """Open the connection and do any handshake; the result is a stage's message."""
        ...

    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Send one message; a returned dict is the stage's ``sent``."""
        ...

    def receive(self, timeout: float) -> dict[str, Any] | None:
        """The next message, or None when none came within about ``timeout``;
        raise EOFError for a clean end of the connection."""
        ...

    def close(self) -> None:
        """Close the connection, making a ``receive()`` in progress return."""
        ...


# Each method an adapter must have, with the arguments the plugin passes it,
# positionally, in this order: for `validate --deep`, which checks an adapter
# class against them.
ADAPTER_METHODS: Mapping[str, tuple[str, ...]] = {
    "connect": ("timeout",),
    "send": ("message",),
    "receive": ("timeout",),
    "close": (),
}
# And those it may have.
OPTIONAL_ADAPTER_METHODS: Mapping[str, tuple[str, ...]] = {"debug_info": ()}

# What the reader passes `receive()`: how long a stop waits for it at most.
RECEIVE_TIMEOUT = 0.2
# The longest an `expect` sleeps between looks at its inbox: an interrupt
# (Ctrl-C) or the deadline is never further away than this.
WAIT_SLICE = 0.1
# How long a timed-out `expect` keeps watching, for the report's "late" messages.
LATE_WINDOW = 0.2
# How long closing or reopening a connection waits for its reader to stop.
JOIN_BUDGET = 2.0
# How long teardown waits for every reader of a chain to stop, all together.
TEARDOWN_BUDGET = 2.0
# Where `debug_info()`'s text is cut.
DEBUG_INFO_LIMIT = 4000
# Where a message is cut in a failure line.
_SHOWN_LIMIT = 200

# Raised by a call, these stop the run: never taken for a failure.
_INTERRUPTS = (KeyboardInterrupt, SystemExit, pytest.exit.Exception)


def describe_exception(e: BaseException) -> str:
    """``Type: message`` for an exception an adapter raised, whose ``__str__``
    may raise in turn."""
    try:
        text = str(e)
    except Exception:
        text = "<exception str() failed>"
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


class MessageValueError(ValueError):
    """A value an adapter returned that is no message: not a dict of str keys,
    holding JSON values or bytes at any depth. Its text names where."""


def _path(keys: tuple[str | int, ...]) -> str:
    """Where a value sits in a message: ``body.items[2]``, ``header["a-b"]``."""
    text = ""
    for key in keys:
        if isinstance(key, int):
            text += f"[{key}]"
        elif key.isidentifier():
            text += f".{key}" if text else key
        else:
            text += f"[{json.dumps(key)}]"
    return text


def _instead(value: Any) -> str:
    """What to return in place of ``value``, which no message holds."""
    if isinstance(value, tuple | set | frozenset):
        return "a list"
    if isinstance(value, bytearray | memoryview):
        return "bytes"
    if isinstance(value, Mapping):
        return "a dict"
    return "a JSON value or bytes"


def checked_message(value: Any, source: str) -> dict[str, Any]:
    """``value``, which ``source`` (``"receive()"``) returned, if it is a
    message: a dict with str keys, holding JSON values or bytes at any depth.
    Otherwise a `MessageValueError` naming the first value that is not, in
    document order: ``receive() returned tuple at body.items; return a list``.

    Iterative, so a deep message is no RecursionError. A container met again
    is not walked again: one shared, or one that holds itself, is checked once.
    """
    if not isinstance(value, dict):
        raise MessageValueError(f"{source} returned {type(value).__name__}; return a dict")
    seen: set[int] = set()
    pending: list[tuple[Any, tuple[str | int, ...]]] = [(value, ())]
    while pending:
        current, keys = pending.pop()
        if isinstance(current, dict | list):
            if id(current) in seen:
                continue
            seen.add(id(current))
            members: list[tuple[Any, Any]] = list(current.items()) if isinstance(current, dict) else list(enumerate(current))
            if isinstance(current, dict) and (key := next((key for key, _ in members if not isinstance(key, str)), None)) is not None:
                raise MessageValueError(f"{source} returned a key of type {type(key).__name__} at {_path(keys) or 'the top level'}; keys must be strings")
            # Last first, so the first comes off the stack first.
            pending.extend((member, (*keys, key)) for key, member in reversed(members))
        elif current is None or isinstance(current, str | bool | int | float | bytes):
            continue
        else:
            raise MessageValueError(f"{source} returned {type(current).__name__} at {_path(keys)}; return {_instead(current)}")
    return value


def adapter_error(connection: str, call: str, e: Exception) -> RequestError:
    """``e``, which the adapter of ``connection`` raised from ``call``, as the
    `RequestError` it fails the stage with: retryable when it is an OSError (a
    refused, reset or timed-out socket), or a stage failure the adapter marked
    retryable (``RequestError(..., retryable=True)``)."""
    retryable = isinstance(e, OSError) or (isinstance(e, StageExecutionError) and e.retryable)
    error = RequestError(f"Connection '{connection}': {call} failed: {describe_exception(e)}", retryable=retryable)
    error.__cause__ = e
    return error


def shown_message(message: Any) -> str:
    """A message as a failure line shows it: as JSON, bytes as hex, cut."""

    def default(value: Any) -> str:
        if isinstance(value, bytes):
            return f"0x{value[:64].hex()}" + ("..." if len(value) > 64 else "")
        return repr(value)

    try:
        text = json.dumps(message, ensure_ascii=False, default=default)
    except (TypeError, ValueError, RecursionError):
        text = "(a message too deep or too long to show)"
    return text if len(text) <= _SHOWN_LIMIT else f"{text[:_SHOWN_LIMIT]}... ({len(text)} characters)"


def declared_match(match: Any) -> str:
    """An ``expect.match`` as the scenario declares it, for a failure line:
    its templates as written, never their rendered values."""
    if match is None:
        return "any message"
    if isinstance(match, dict):
        match = {key: value.model_dump(mode="json", exclude_unset=True) if isinstance(value, BaseModel) else value for key, value in match.items()}
    return shown_message(match)


def debug_text(adapter: Any) -> str | None:
    """What the adapter's optional ``debug_info()`` says, cut at
    `DEBUG_INFO_LIMIT`, or None without one. Whatever it raises is its text
    instead: a report must not fail for it."""
    method = getattr(adapter, "debug_info", None)
    if method is None:
        return None
    try:
        text = method()
    except _INTERRUPTS:
        raise
    except Exception as e:
        return f"<debug_info() failed: {describe_exception(e)}>"
    if not isinstance(text, str):
        return f"<debug_info() returned {type(text).__name__}, not text>"
    return text if len(text) <= DEBUG_INFO_LIMIT else f"{text[:DEBUG_INFO_LIMIT]}... ({len(text)} characters)"


@dataclass(frozen=True, slots=True)
class ReaderEnd:
    """How a session's reader ended: ``error`` is what ``receive()`` raised
    (or why what it returned was refused), None for a clean end, when it
    raised EOFError or the session was stopped. ``after_stop`` is an end the
    plugin caused, by closing the connection: never reported."""

    error: BaseException | None
    after_stop: bool = False

    def describe(self) -> str:
        """Why the connection is not open, for a failure line."""
        return "closed by the other side" if self.error is None else f"failed: {describe_exception(self.error)}"

    def as_message(self) -> dict[str, Any]:
        """The message of an ``expect: {"closed": true}`` that saw this end."""
        if self.error is None:
            return {"error": None, "detail": None}
        try:
            detail = str(self.error)
        except Exception:
            detail = "<exception str() failed>"
        return {"error": type(self.error).__name__, "detail": detail}


@dataclass(frozen=True, slots=True)
class Miss:
    """Why a message does not match: the first expectation it fails, or,
    where ``error``, why the match cannot be evaluated against it (a
    ``length()`` of its null), which counts as no match."""

    reason: str
    error: bool = False


@dataclass(frozen=True, slots=True)
class Arrival:
    """A message as an `expect` saw it: its place in the session (``seq``,
    from 1), when it arrived, in seconds after the stage's send (or the wait's
    start, for a stage that sends nothing; negative for one queued before),
    and why it did not match (`Miss`), if it did not."""

    seq: int
    at: float
    message: dict[str, Any]
    miss: Miss | None = None


ExpectMode = Literal["message", "absent", "closed"]
# How an `expect` ended: it took a message; an absent one saw one match; it
# ran out of time; an absent one did not see one; the reader ended, which a
# closed one waits for.
ExpectResult = Literal["taken", "seen", "timeout", "absent", "closed", "ended"]


@dataclass(frozen=True, slots=True)
class ExpectOutcome:
    """How one `_Session.expect` ended (`ExpectResult`), with what it saw:
    the message it took, or that an absent one saw (``taken``); the messages
    it considered that did not match, oldest first; those that arrived within
    `LATE_WINDOW` after a timeout (``late``); how long it waited; how many
    queued messages it left out as older than the stage's send
    (``earlier``); and the reader's end, if it saw one."""

    result: ExpectResult
    waited: float
    taken: Arrival | None = None
    considered: tuple[Arrival, ...] = ()
    late: tuple[Arrival, ...] = ()
    earlier: int = 0
    end: ReaderEnd | None = None


# What a predicate says of a message: None, it matches; a `Miss`, why not.
type MatchPredicate = Callable[[dict[str, Any]], Miss | None]


class _Session:
    """One open of a connection: its adapter instance, its reader thread and
    its inbox. The reader appends what ``receive()`` returns, the test's
    thread takes and drains, both under one condition, which wakes a waiting
    `expect` on each arrival and on the reader's end."""

    class Opening(NamedTuple):
        """Which open a session is (from 1), its inbox_limit and its reader thread's name."""

        generation: int
        inbox_limit: int
        thread_name: str

    def __init__(self, name: str, adapter_name: str, adapter: Any, generation: int, inbox_limit: int, thread_name: str) -> None:
        self.name = name
        self.adapter_name = adapter_name
        self.adapter = adapter
        self.generation = generation
        self.opened_at = time.monotonic()
        self._inbox: deque[tuple[int, float, dict[str, Any]]] = deque(maxlen=inbox_limit)
        self._seq = 0
        self.dropped = 0
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._end: ReaderEnd | None = None
        # Whether a stage saw the reader's end, so teardown does not log it.
        self.observed = False
        # Run in a context of its own, empty: nothing a stage set (an
        # iteration's context managers) reaches the reader.
        self._thread = threading.Thread(target=contextvars.Context().run, args=(self._read,), name=thread_name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def end(self) -> ReaderEnd | None:
        with self._condition:
            return self._end

    @property
    def reader_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def age(self) -> float:
        return time.monotonic() - self.opened_at

    def _read(self) -> None:
        """The reader: ``receive()`` until it ends the connection or the
        session stops. Nothing it raises leaves the thread: it is the end."""
        error: BaseException | None = None
        try:
            while not self._stop.is_set():
                asked = time.monotonic()
                try:
                    received = self.adapter.receive(RECEIVE_TIMEOUT)
                except EOFError:
                    break
                except BaseException as e:
                    error = e
                    break
                if received is None:
                    # An adapter returning at once would spin the reader.
                    if time.monotonic() - asked < RECEIVE_TIMEOUT / 20:
                        self._stop.wait(0.01)
                    continue
                try:
                    message = checked_message(received, "receive()")
                except MessageValueError as e:
                    error = e
                    break
                self._append(message)
        except BaseException as e:
            error = e
        finally:
            with self._condition:
                self._end = ReaderEnd(error, after_stop=self._stop.is_set())
                self._condition.notify_all()

    def _append(self, message: dict[str, Any]) -> None:
        with self._condition:
            self._seq += 1
            if len(self._inbox) == self._inbox.maxlen:
                self.dropped += 1
            self._inbox.append((self._seq, time.monotonic(), message))
            self._condition.notify_all()

    def drain(self) -> int:
        """Discard every queued message; how many there were."""
        with self._condition:
            count = len(self._inbox)
            self._inbox.clear()
            return count

    def mark(self) -> int:
        """The sequence number of the last message to arrive: an `expect`
        after a send considers only those after it."""
        with self._condition:
            return self._seq

    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """``send()`` ``message``; what it returned, if anything, checked as a message."""
        try:
            returned = self.adapter.send(message)
        except Exception as e:
            raise adapter_error(self.name, "send()", e) from e
        if returned is None:
            return None
        try:
            return checked_message(returned, "send()")
        except MessageValueError as e:
            raise RequestError(f"Connection '{self.name}': {e}", retryable=False) from e

    def expect(self, predicate: MatchPredicate | None, timeout: float, *, after: int | None, since: float, mode: ExpectMode = "message") -> ExpectOutcome:
        """Wait up to ``timeout`` seconds, as ``mode`` says: for the oldest
        message ``predicate`` matches (None: any), which is taken out of the
        inbox; for none to match (``absent``); or for the reader to end
        (``closed``). With ``after`` (`mark`, before the stage's send), only
        the messages that arrived after it count. ``since`` is what arrival
        times are relative to.

        Each look evaluates only the messages that arrived since the last,
        copied under the lock and judged outside it. The wait sleeps in slices
        of at most `WAIT_SLICE`, against a monotonic deadline, and the reader
        ending wakes it: a wait for a message then ends at once, not at its
        timeout. Whatever ``predicate`` raises (the scenario's own error)
        ends the wait as it is.
        """
        started = time.monotonic()
        deadline = started + min(timeout, threading.TIMEOUT_MAX)
        seen = after if after is not None else 0
        with self._condition:
            earlier = sum(1 for seq, _, _ in self._inbox if seq <= seen) if after is not None else 0
        considered: list[Arrival] = []

        def ended(result: ExpectResult, **fields: Any) -> ExpectOutcome:
            return ExpectOutcome(result, waited=time.monotonic() - started, considered=tuple(considered), earlier=earlier, **fields)

        while True:
            with self._condition:
                fresh = [entry for entry in self._inbox if entry[0] > seen]
                end = self._end
                newest = self._seq
            if mode != "closed":
                for seq, arrived, message in fresh:
                    miss = predicate(message) if predicate is not None else None
                    arrival = Arrival(seq, arrived - since, message, miss)
                    if miss is None:
                        if mode == "message":
                            self._remove(seq)
                            return ended("taken", taken=arrival)
                        return ended("seen", taken=arrival)
                    considered.append(arrival)
            # What is not in the inbox past `seen` was dropped (inbox_limit).
            seen = newest
            if end is not None:
                self.observed = True
                return ended("closed" if mode == "closed" else "ended", end=end)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if mode == "absent":
                    return ended("absent")
                waited = time.monotonic() - started
                late = self._late(seen, since)
                return ExpectOutcome("timeout", waited=waited, considered=tuple(considered), late=late, earlier=earlier)
            with self._condition:
                if self._end is None and self._seq <= seen:
                    self._condition.wait(min(WAIT_SLICE, remaining))

    def _remove(self, seq: int) -> None:
        with self._condition:
            for entry in self._inbox:
                if entry[0] == seq:
                    self._inbox.remove(entry)
                    return

    def _late(self, seen: int, since: float) -> tuple[Arrival, ...]:
        """The messages that arrive within `LATE_WINDOW` after a timeout,
        for the report: they stay queued."""
        threading.Event().wait(LATE_WINDOW)
        with self._condition:
            return tuple(Arrival(seq, arrived - since, message) for seq, arrived, message in self._inbox if seq > seen)

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self, budget: float | None = None) -> str | None:
        """Stop the reader, ``close()`` the adapter and wait up to ``budget``
        seconds (`JOIN_BUDGET`) for the reader to end: why ``close()`` failed,
        or None. An interrupt it raises propagates, the reader stopped."""
        budget = JOIN_BUDGET if budget is None else budget
        self._stop.set()
        error = None
        try:
            self.adapter.close()
        except _INTERRUPTS:
            raise
        except BaseException as e:
            error = describe_exception(e)
        self._thread.join(budget)
        return error

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    def log_unobserved(self) -> None:
        """Log a reader failure no stage saw, at the session's end."""
        end = self.end
        if end is not None and end.error is not None and not end.after_stop and not self.observed:
            logger.error("Connection '%s' (open %d) failed and no stage saw it: %s", self.name, self.generation, describe_exception(end.error))


@dataclass(frozen=True, slots=True)
class ConnectionExchange:
    """What one connection stage did, for its report: an immutable
    snapshot the carrier takes once the step has run, before any close,
    never read from the live session. Its messages are copies.

    ``generation`` is which open of the connection the session is (from 1),
    and ``session_age`` how long it had been open, in seconds; both None when
    the stage got no session. ``opened`` is whether the stage opened it, and
    ``connected`` what ``connect()`` returned then. ``sent`` is the message
    sent and ``sent_return`` what ``send()`` returned. ``match`` is the declared
    match (templates as written), with its ``timeout`` and ``mode``, and the
    seconds ``waited``; ``result`` how the wait ended (`ExpectResult`).
    ``taken`` is the message the wait took, or an absent one saw;
    ``considered`` the messages that did not match, ``late`` those that
    arrived just after a timeout, ``earlier`` how many queued messages were
    older than the send, and ``dropped`` how many the session's inbox_limit
    discarded. ``end`` is how the session's reader ended, if it has, as the
    wait saw it or, without one, by the snapshot. ``debug_info`` is the
    adapter's, cut (`debug_text`).

    Its messages are as the adapter returned them: a report shows them through
    the scenario's `Redaction` (``report_formatter.format_connection_exchange``)."""

    connection: str
    adapter: str
    generation: int | None = None
    session_age: float | None = None
    opened: bool = False
    connected: dict[str, Any] | None = None
    sent: dict[str, Any] | None = None
    sent_return: dict[str, Any] | None = None
    match: Any = None
    timeout: float | None = None
    mode: ExpectMode | None = None
    waited: float | None = None
    result: ExpectResult | None = None
    taken: Arrival | None = None
    considered: tuple[Arrival, ...] = ()
    late: tuple[Arrival, ...] = ()
    earlier: int = 0
    dropped: int = 0
    end: ReaderEnd | None = None
    debug_info: str | None = None


@dataclass(slots=True)
class ExchangeRecord:
    """A `ConnectionExchange` being filled in as the step runs, then
    frozen (`freeze`), whether the step passed or failed."""

    connection: str
    adapter: str
    session: _Session | None = None
    opened: bool = False
    connected: dict[str, Any] | None = None
    sent: dict[str, Any] | None = None
    sent_return: dict[str, Any] | None = None
    match: Any = None
    timeout: float | None = None
    mode: ExpectMode | None = None
    outcome: ExpectOutcome | None = None

    def freeze(self) -> ConnectionExchange:
        """The snapshot, its messages deep-copied: a user function a later
        stage calls cannot change what the report shows. ``debug_info()`` is
        asked now, before any close."""
        session, outcome = self.session, self.outcome
        end = outcome.end if outcome is not None and outcome.end is not None else session.end if session is not None else None
        return ConnectionExchange(
            connection=self.connection,
            adapter=self.adapter,
            generation=session.generation if session is not None else None,
            session_age=session.age if session is not None else None,
            opened=self.opened,
            connected=_copied(self.connected),
            sent=_copied(self.sent),
            sent_return=_copied(self.sent_return),
            match=self.match,
            timeout=self.timeout,
            mode=self.mode,
            waited=outcome.waited if outcome is not None else None,
            result=outcome.result if outcome is not None else None,
            taken=_copied_arrival(outcome.taken) if outcome is not None and outcome.taken is not None else None,
            considered=tuple(map(_copied_arrival, outcome.considered)) if outcome is not None else (),
            late=tuple(map(_copied_arrival, outcome.late)) if outcome is not None else (),
            earlier=outcome.earlier if outcome is not None else 0,
            dropped=session.dropped if session is not None else 0,
            end=end,
            debug_info=debug_text(session.adapter) if session is not None else None,
        )


def _copied[T](value: T) -> T:
    """A deep copy of a message, or the value itself where one fails."""
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _copied_arrival(arrival: Arrival) -> Arrival:
    return Arrival(arrival.seq, arrival.at, _copied(arrival.message), arrival.miss)


def close_all(sessions: Iterable[_Session]) -> None:
    """End ``sessions`` at once, as a chain ends: stop every reader, then
    ``close()`` every adapter, each in its own guard, which must make a
    ``receive()`` in progress return, then wait for all the readers against one
    shared deadline of `TEARDOWN_BUDGET` seconds.

    No stage is left to fail, so what goes wrong is logged, never warned of
    (warnings are errors in a strict run): a ``close()`` that raised, a reader
    still running at the deadline (a daemon, left behind), and a reader's
    failure no stage saw. Its failures once stopped are the plugin's own
    doing, and not reported. An interrupt is re-raised once all are done.
    """
    sessions = list(sessions)
    interrupt: BaseException | None = None
    for session in sessions:
        session.stop()
    for session in sessions:
        try:
            session.adapter.close()
        except _INTERRUPTS as e:
            if interrupt is None:
                interrupt = e
        except BaseException as e:
            logger.error("Closing connection '%s' at the end of the chain failed: %s", session.name, describe_exception(e))
    deadline = time.monotonic() + TEARDOWN_BUDGET
    for session in sessions:
        session.join(max(0.0, deadline - time.monotonic()))
        if session.reader_alive:
            logger.error(
                "The reader of connection '%s' did not stop within %gs of the chain's end: its receive() does not return once close() is called",
                session.name,
                TEARDOWN_BUDGET,
            )
    for session in sessions:
        session.log_unobserved()
    if interrupt is not None:
        raise interrupt


@dataclass(slots=True)
class _Entry:
    """A connection's state in a chain: its open session, if any; how many
    times it was opened; and, once not open, why: the stage that closed it,
    or the failure of the open that replaced its session. ``stuck`` is a
    reader that did not stop, which bars reopening."""

    session: _Session | None = None
    generations: int = 0
    closed_by: str | None = None
    failure: str | None = None
    stuck: bool = False


class Connections:
    """A chain's connections, by name: each has never been opened, is open
    (a `_Session`, whose reader may have ended since), or was closed by a
    stage, or failed to reopen."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def __eq__(self, other: object) -> bool:
        """The same state: two fresh registries are equal (`carrier.fresh_chain_state`)."""
        return isinstance(other, Connections) and self._entries == other._entries

    __hash__ = None

    def session(self, name: str) -> _Session | None:
        entry = self._entries.get(name)
        return entry.session if entry is not None else None

    def never_opened(self, name: str) -> bool:
        entry = self._entries.get(name)
        return entry is None or entry.generations == 0

    def is_open(self, name: str) -> bool:
        """Whether ``name`` has a session whose reader has not ended."""
        session = self.session(name)
        return session is not None and session.end is None

    def not_open(self, name: str) -> str | None:
        """Why ``name``, opened before, has no session now: closed by a stage,
        or its reopening failed. None when it has one, or was never opened."""
        entry = self._entries.get(name)
        if entry is None or entry.generations == 0 or entry.session is not None:
            return None
        return f"closed by stage '{entry.closed_by}'" if entry.closed_by is not None else f"failed: {entry.failure}"

    def open(self, name: str, adapter_name: str, params: Mapping[str, Any], connect_timeout: float, inbox_limit: int, label: str) -> tuple[_Session, dict[str, Any] | None]:
        """Open ``name`` anew: end its session, if it has one, then import the
        adapter, build it with ``params``, ``connect()`` it and start its
        reader, registering the session only then. The result: the session
        and what ``connect()`` returned.

        An old session is detached before it is ended, so its reader can
        only reach its own inbox. A reader that does not stop within
        `JOIN_BUDGET` bars reopening: a new socket could reuse its file
        descriptor. A ``connect()`` that raises has ``close()`` called in its
        own guard, so a half-open connection is not left behind.
        """
        entry = self._entries.setdefault(name, _Entry())
        if entry.stuck:
            raise RequestError(f"Connection '{name}': the previous reader for '{name}' did not stop, so it cannot be opened again in this chain", retryable=False)
        old, entry.session = entry.session, None
        if old is not None:
            entry.closed_by, entry.failure = None, "reopening"
            error = old.shutdown()
            if old.reader_alive:
                entry.stuck = True
                raise RequestError(f"Connection '{name}': the previous reader for '{name}' did not stop within {JOIN_BUDGET:g}s of close()", retryable=False)
            if error is not None:
                logger.error("Closing connection '%s' to reopen it failed: %s", name, error)
        try:
            generation = entry.generations + 1
            opening = _Session.Opening(generation, inbox_limit, f"httpchain-connection-{label}-{name}-{generation}")
            session, result = self._connect(name, adapter_name, params, connect_timeout, opening)
        except BaseException as e:
            entry.failure = describe_exception(e.__cause__ if isinstance(e, RequestError) and e.__cause__ is not None else e)
            raise
        entry.session, entry.generations, entry.closed_by, entry.failure = session, session.generation, None, None
        return session, result

    @staticmethod
    def _connect(name: str, adapter_name: str, params: Mapping[str, Any], connect_timeout: float, opening: "_Session.Opening") -> tuple[_Session, dict[str, Any] | None]:
        try:
            adapter_class = import_adapter(adapter_name)
        except UserFunctionError as e:
            raise StageExecutionError(f"Connection '{name}': {e}", retryable=False) from e
        try:
            adapter = adapter_class(**params)
        except Exception as e:
            raise adapter_error(name, f"{adapter_name}(**params)", e) from e
        try:
            result = adapter.connect(connect_timeout)
            if result is not None:
                result = checked_message(result, "connect()")
            session = _Session(name, adapter_name, adapter, *opening)
            session.start()
        except BaseException as e:
            try:
                adapter.close()
            except _INTERRUPTS:
                raise
            except BaseException as close_error:
                logger.error("Closing connection '%s' after its connect() failed raised: %s", name, describe_exception(close_error))
            if isinstance(e, MessageValueError):
                raise RequestError(f"Connection '{name}': {e}", retryable=False) from e
            if isinstance(e, Exception):
                raise adapter_error(name, "connect()", e) from e
            raise
        return session, result

    def close(self, name: str, stage: str) -> str | None:
        """Close ``name`` for ``stage``: detach its session, stop its reader
        and ``close()`` its adapter. Why that failed, or None; a reader that
        does not stop is logged, and bars reopening."""
        entry = self._entries.get(name)
        if entry is None or entry.generations == 0:
            return None
        session, entry.session = entry.session, None
        entry.closed_by, entry.failure = stage, None
        if session is None:
            return None
        error = session.shutdown()
        if session.reader_alive:
            entry.stuck = True
            logger.error("The reader of connection '%s' did not stop within %gs of close()", name, JOIN_BUDGET)
        return f"Closing connection '{name}' failed: {error}" if error is not None else None

    def close_all(self) -> None:
        """End every session (`close_all`), and forget them all."""
        sessions = [entry.session for entry in self._entries.values() if entry.session is not None]
        self._entries.clear()
        close_all(sessions)


def not_open_failure(stage: str, connection: str, why: str) -> RequestError:
    """A stage using ``connection`` without ``open: true``, which is not open
    for ``why``: no attempt changes that."""
    return RequestError(f"Stage '{stage}': connection '{connection}' is not open ({why}); set \"open\": true to connect again", retryable=False)


def _count(n: int, what: str) -> str:
    return f"{n} {what}" if n == 1 else f"{n} {what}s"


def expect_failure(
    outcome: ExpectOutcome,
    *,
    stage: str,
    connection: str,
    match: Any,
    timeout: float,
    mode: ExpectMode,
    sent: bool,
    reopens: bool,
    ended_before: bool,
    redaction: Redaction = DEFAULT_REDACTION,
) -> StageExecutionError | None:
    """The stage failure ``outcome`` is, or None for one that passes.

    A timeout is a `VerificationError`, retryable: the next attempt sends
    again. An absent expect that saw its message is one no attempt changes.
    The reader ending under a wait for a message (an absent one's included:
    the connection ending is not "nothing arrived") is a `RequestError`, its
    error chained, retryable only on a stage that opens the connection again;
    one that ended before the stage began is the connection not being open.
    A message ``match`` is shown as declared (`declared_match`), and a
    message the failure quotes through ``redaction`` (`Redaction.message`);
    a miss reason it quotes is the predicate's, which redacts its own.
    """
    match outcome.result:
        case "taken" | "absent" | "closed":
            return None
        case "seen":
            assert outcome.taken is not None, "an absent expect that saw a message has it"
            shown = shown_message(redaction.message(outcome.taken.message))
            return VerificationError(
                f"Stage '{stage}': a message on connection '{connection}' matched {declared_match(match)} within {timeout:g} s, where none should: "
                f"message #{outcome.taken.seq}, {outcome.taken.at:.3f} s {'after the send' if sent else 'from the wait'}: {shown}",
                retryable=False,
            )
        case "ended":
            assert outcome.end is not None, "an ended expect saw the reader's end"
            if ended_before:
                error = not_open_failure(stage, connection, outcome.end.describe())
            else:
                waited_for = "no message to match " + declared_match(match) if mode == "absent" else declared_match(match)
                error = RequestError(f"Stage '{stage}': connection '{connection}' {outcome.end.describe()} while the stage waited for {waited_for}", retryable=reopens)
            error.__cause__ = outcome.end.error
            return error
        case "timeout" if mode == "closed":
            return VerificationError(f"Stage '{stage}': connection '{connection}' did not close within {timeout:g} s")
        case "timeout":
            arrived = len(outcome.considered)
            first_error = next((arrival.miss for arrival in outcome.considered if arrival.miss is not None and arrival.miss.error), None)
            evaluation = f"; the first that could not be evaluated: {first_error.reason}" if first_error is not None else ""
            return VerificationError(
                f"Stage '{stage}': no message on connection '{connection}' matched {declared_match(match)} within {timeout:g} s "
                f"({_count(arrived, 'message')} arrived {'after the send' if sent else 'while it waited'}){evaluation}"
            )
    raise RuntimeError(f"Unhandled expect result: {outcome.result}")
