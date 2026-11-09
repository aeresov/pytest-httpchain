# Connections

Not every service a test talks to speaks HTTP. A device may hold one long-lived TCP connection to its backend, frame its messages in its own binary format, answer a challenge before it is let in, and get commands pushed to it at any moment. A `connection` stage drives such a protocol through an *adapter*: a small Python class you write, which knows how to open the connection, encode and send one message, and decode the next one. The plugin knows nothing about the protocol. It gives each connection a reader that queues what arrives, waits for the message a stage expects, and hands that message to the same `verify` and `save` steps an HTTP stage uses.

```json
{
    "connections": {
        "device": {
            "adapter": "adapters:LineJsonAdapter",
            "params": {"host": "127.0.0.1", "port": "{{ env('DEVICE_PORT') }}"}
        }
    },
    "stages": [
        {
            "name": "hello",
            "connection": {"name": "device", "open": true},
            "response": [{"verify": {"jmespath": {"greeting": "hello"}}}]
        },
        {
            "name": "ping",
            "connection": {
                "name": "device",
                "send": {"op": "ping", "id": 1},
                "expect": {"match": {"op": "pong", "reply_to": "{{ sent.seq }}"}, "timeout": 2}
            },
            "response": [{"save": {"jmespath": {"server_time": "time"}}}]
        },
        {
            "name": "bye",
            "connection": {"name": "device", "send": {"op": "bye"}, "expect": {"match": "op == 'bye'"}, "close": true}
        }
    ]
}
```

A stage has either a `request` or a `connection`, never both. HTTP and connection stages mix freely in one scenario: they run in order, as one chain, and read what the stages before them saved.

## Declaring a connection

`connections` maps a name to the adapter that serves it:

| Option | Type | Default | Description |
|---|---|---|---|
| `adapter` | string | required | The adapter class, as `module:Class`, imported the way user functions are |
| `params` | object | `{}` | Keyword arguments for the adapter's constructor; templates allowed |
| `connect_timeout` | number or template | `10` | Seconds, passed to the adapter's `connect()` |
| `inbox_limit` | integer | `1000` | Most messages kept queued; beyond it the oldest are dropped, and counted in reports |
| `description` | string | | Free text |

`params` and `connect_timeout` are rendered each time the connection opens. They can read scenario substitutions, scenario-level `fixtures`, and any value a stage before the opening one saved, such as a device id returned by a registration call. They cannot read the opening stage's own fixtures, parameters or substitutions: the connection outlives that stage. For the same reason a fixture `params` read must be class-scoped or wider; a function-scoped one fails the opening stage. `validate` checks these names against every stage that may open the connection.

## The connection step

| Option | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | A key of `connections` |
| `open` | boolean | `false` | Connect now, closing the connection first if it is open. Without an `expect`, what `connect()` returned is the stage's message |
| `drain` | boolean | `false` | Discard every message already queued |
| `send` | object or template | | One message for the adapter to send; templates allowed |
| `expect` | object | | Wait for a message (below) |
| `close` | boolean | `false` | Close the connection once the stage has run, whether or not its checks pass |

A stage sets at least one of these, and they run in this order: open, drain, send, expect, the response steps, close. A stage that is skipped does none of them, `close` included.

### Opening and closing

The first stage of the chain that names a connection opens it, so `open` is needed only to check the handshake result or to connect again. A stage that opens the connection this way takes no message from it: only `open: true` makes the result of `connect()` a message to check.

Each open is a new session, with its own adapter instance, reader and queue: messages queued on the old one are discarded with it.

Once a stage has closed a connection, a stage that uses it again must say `open: true`. Without it the stage fails, and no retry changes that:

```text
Stage 'status': connection 'device' is not open (closed by stage 'bye'); set "open": true to connect again
```

`pytest-httpchain validate` reports this before anything runs (`HTTPCHAIN044`), when the stage that closes the connection cannot be skipped.

When the other side ends the connection, or the adapter's `receive()` fails, what was queued before the end stays queued, and until a stage opens the connection again:

- a stage that sends on it fails at once, as not open: `connection 'device' is not open (closed by the other side)`, or `(failed: ConnectionResetError: ...)`;
- an `expect` still takes a matching message that arrived before the end, and fails the same way, at once, when none is left;
- `expect: {"closed": true}` passes at once.

### Waiting for a message

Everything the connection receives is queued in arrival order, whether or not a stage is waiting for it. `expect` takes the oldest queued message that matches, out of the queue, waiting up to `timeout` seconds for one. A stage that sends looks only at messages that arrived after its send, so an old push or a late reply to an earlier attempt cannot be mistaken for the answer. A stage that sends nothing looks at the whole queue. Messages that do not match stay queued for later stages; `drain` discards them.

| Option | Type | Default | Description |
|---|---|---|---|
| `match` | string or object | any message | Which message to take |
| `timeout` | number or template | `5` | Seconds to wait |
| `absent` | boolean | `false` | Pass if no message matches within `timeout`; fail on the first one that does |
| `closed` | boolean | `false` | Wait for the other side to end the connection; takes no `match` and no `absent` |

`match` comes in two forms:

- **A JMESPath expression.** A message is taken when the expression's result is truthy as JMESPath has it: anything but `false`, `null` or an empty string, list or object. For example, ``"header.action == 'DoCommand' && body.seq > `3`"``. The expression is never rendered, so a `{{` in it is an error.
- **An object of expectations.** These are written as in [`verify.jmespath`](responses.md#jmespath-assertions), and all of them must hold. Values are rendered, with `sent` in scope, so a saved value compares as a value: `{"header.request_id": "{{ request_id }}", "body.items": {"length": 2}}`. A value that does not render fails the stage at once.

A message the match cannot be evaluated against (a `length()` of its null field) does not match. When nothing matches in time the stage fails:

```text
Stage 'ping': no message on connection 'device' matched {"op": "pong", "id": 1} within 2 s (3 messages arrived after the send)
```

The failure names the first message the match could not be evaluated against, if any. The [report](#reports) lists the messages, and for each one the first expectation it did not meet.

**Making sure something does not happen.** `absent: true` turns the wait around. It is how a test checks that a rejected call pushed nothing, or that an acknowledged command is not sent again. It passes at the deadline, and fails at once on the first message that matches, which no retry changes:

```json
{"name": "flush", "connection": {"name": "spk1", "drain": true}},
{"name": "rejected call", "request": {"url": "...", "method": "POST"}, "response": [{"verify": {"status": 403}}]},
{"name": "nothing pushed", "connection": {"name": "spk1", "expect": {"match": "header.forward.msg.action.name == 'DoCommand'", "absent": true, "timeout": 3}}}
```

```text
Stage 'nothing pushed': a message on connection 'spk1' matched "header.forward.msg.action.name == 'DoCommand'" within 3 s, where none should: message #4, 0.512 s from the wait: {"header": ...}
```

The connection ending is not "nothing arrived": an `absent` wait fails if the connection ends before its deadline.

**Waiting for the other side to hang up.** `closed: true` passes when the connection ends, at once if it already has. The stage's message then says how: `{"error": "ConnectionResetError", "detail": "..."}` when `receive()` failed, or `{"error": null, "detail": null}` for a clean end. A connection still open at the deadline fails the stage: `Stage 'kicked': connection 'device' did not close within 10 s`. Any other wait on a connection that ends fails as soon as it ends, without waiting out its timeout:

```text
Stage 'ping': connection 'device' closed by the other side while the stage waited for {"op": "pong", "id": 1}
```

**Push tests.** Open the connection and drain it before the HTTP stage that triggers the push, not in the stage that expects it: a `drain` there would discard the very message the HTTP stage caused. The stage that expects the push sends nothing, so it looks at the whole queue.

## Checking the message

The message a stage took is what its response steps work on: the `expect` match, the end a `closed: true` wait saw, or what `connect()` returned on an `open: true` stage without an `expect`.

- `verify.jmespath`, `save.jmespath` and `verify.body.schema` run on it as on a decoded JSON body.
- Templates in the response steps read it as `message`, for example `{{ message.body.state == 'playing' }}`. A stage that sends also reads what `send()` returned as `sent` (`null` when it returned nothing), in its response steps and in its `expect.match` values. A name of your own called `message` or `sent` is shadowed there (`HTTPCHAIN045`).
- User functions are called with `message=`, the message as a dict, in place of `response=`:

```python
# checks.py
def ack_accepted(message: dict) -> bool:
    return message["body"]["status"] == "ACCEPTED"
```

HTTP-only settings have nothing to work on here and are refused on a connection stage (`HTTPCHAIN041`): `verify.status`, `verify.headers`, the body text checks (`verify.body.contains`, `not_contains`, `matches`, `not_matches`), `save.regex`, `response_codec`, `parallel` and reading `response`. A stage without a message (no `expect` and no `open: true`, or an `absent` expect) cannot run the checks that need one (`HTTPCHAIN043`).

## Retries

[`retry`](../advanced/retry.md) works on connection stages. Each attempt runs the stage again from the top: it reconnects if the stage has `open: true`, and drains, sends and waits anew. A `close: true` closes once, after the last attempt.

- **An `expect` that timed out** is a `verify` failure, and so is a `closed: true` wait whose connection did not end.
- **An adapter that fails to build, connect or send** is a `request` failure. It is retried when the adapter raised an `OSError` (a refused or reset socket, a timeout) or a `RequestError` it marked `retryable=True` (`from pytest_httpchain.errors import RequestError`).
- **The connection ending** while the stage waits is a `request` failure, retried only on a stage with `open: true`, which connects again.
- **Not retried:**
    - using a connection that is not open, without `open: true`, since no attempt can change that;
    - an `absent` expect that saw its message;
    - a `connect()` or `send()` result that is not a message, and an adapter that cannot be imported.
- **Reconnect polling:** use `open: true` together with `retry`.

## Writing an adapter

This adapter speaks newline-delimited JSON over TCP. It numbers the frames it sends, and the server it talks to answers each one with that number as `reply_to`:

```python
# adapters.py
import json
import select
import socket
import threading
import time


class LineJsonAdapter:
    """Newline-delimited JSON over TCP."""

    def __init__(self, host: str, port: int):
        self.address = (host, int(port))
        self.sock: socket.socket | None = None
        self.buffer = b""
        self.seq = 0
        self.write_lock = threading.Lock()

    def connect(self, timeout: float) -> dict | None:
        self.sock = socket.create_connection(self.address, timeout=timeout)
        self.sock.settimeout(None)  # blocking from here on: receive() waits with select
        return self.receive(timeout)  # the greeting is the handshake result

    def send(self, message: dict) -> dict:
        assert self.sock is not None
        with self.write_lock:
            self.seq += 1
            frame = {"seq": self.seq, **message}  # the server's reply carries it back as reply_to
            self.sock.sendall(json.dumps(frame).encode() + b"\n")
            return {"seq": self.seq}  # the stage reads it as `sent`

    def receive(self, timeout: float) -> dict | None:
        assert self.sock is not None
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.sock], [], [], remaining)[0]:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("connection closed by peer")  # a clean end
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)  # wakes a receive() in progress
            except OSError:
                pass
            self.sock.close()

    def debug_info(self) -> str:
        return f"unread bytes: {self.buffer[:200].hex()}"
```

The plugin calls the adapter like this:

- **`__init__(**params)`** with the rendered `params`.
- **`connect(timeout)`** on the test's thread, with `connect_timeout`. It opens the connection, performs any handshake, and returns a dict for the stage to check, or `None`. A handshake the server refuses at protocol level is best returned as a dict, such as `{"result": "rejected", "reason": "..."}`, so a stage can verify it. Raise only when the transport fails. If `connect()` raises, `close()` is still called, so it must cope with a connection that was only half opened.
- **`send(message)`** on the test's thread, with one rendered `send` object. Values may be bytes, so a binary protocol can carry raw fields. If it returns a dict, such as the frame header it assigned, the stage reads that as `sent`, for example `"match": {"reply_to": "{{ sent.seq }}"}`.
- **`receive(timeout)`** on the connection's reader thread, over and over. It returns one decoded message as a dict, or `None` if nothing arrived within `timeout`. It must return within about `timeout`; the plugin passes 0.2 seconds. Frames the adapter deals with itself, such as heartbeats and automatic acknowledgements, should return `None` and are then not queued. To report a clean end of the connection, raise `EOFError`; any other exception it raises ends the connection as failed, with that exception.
- **`close()`** when a stage closes the connection, before it is reopened, after a failed `connect()`, and when the scenario ends, including after a failure. It may run while `receive()` is still waiting, and it must make that `receive()` return. For a socket, `shutdown()` before `close()` does this.
- **`debug_info()`** is optional. It returns text for failure reports, such as the bytes the adapter holds undecoded, and may run while `receive()` does. It is cut at 4000 characters, and what it raises is shown in its place.

**Threads.** `send()` runs on the test's thread at the same time as `receive()` runs on the reader's. If the adapter also writes from `receive()` or from threads of its own, which heartbeats and acknowledgements often do, it must guard every write with one lock, as `write_lock` does above. Otherwise two frames can interleave. `receive()` must also not change state that `send()` relies on: setting the socket timeout in `receive()` would apply to `sendall()` too, which is why the adapter above keeps the socket blocking and waits with `select`. An adapter that starts threads of its own stops them in `close()`.

**Message values.** A message, and what `connect()` and `send()` return, must be a dict with string keys, holding JSON values (objects, lists, strings, numbers, booleans, null) or bytes, at any depth. A message `receive()` returns that is not one ends the connection, and a `connect()` or `send()` result that is not one fails the stage. The error names the path of the value:

```text
receive() returned tuple at body.items; return a list
```

Protobuf's `MessageToDict` turns 64-bit integers into strings and bytes into base64. Convert them back, or compare against strings.

`pytest-httpchain validate --deep` imports each adapter and checks three things: that it has these methods, that each method takes these arguments, and that its constructor accepts the declared `params` (`HTTPCHAIN046`). For type checking, `pytest_httpchain.ProtocolAdapter` is the matching `typing.Protocol`.

## Lifetime and teardown

Connections belong to the scenario's chain of stages. A class-scoped parametrized fixture runs the scenario once per value, and each run has its own connections.

**After a failure.** The stages after a failing one are skipped as usual, but connections stay open, so an `always_run` stage can still send a farewell message. An `always_run` stage whose connection is not open at that point is skipped, with `Flow aborted, and connection 'device' is not open`: it never connects.

**When the scenario ends.** Every connection is closed: the plugin stops its reader, calls `close()`, and waits up to 2 seconds in all for the readers to finish. No stage is left to fail, so what goes wrong is logged: a `close()` that raised, and a reader still running at the deadline, as errors; a connection whose reader failed after the last stage that used it, as a warning:

```text
Connection 'device' (open 1) failed and no stage saw it: ConnectionResetError: [Errno 104] Connection reset by peer
```

The scenario's connections are closed after its class-scoped fixtures are torn down. A fixture that serves the other side, a local test server say, should be module-scoped or wider so it outlives them; a class-scoped one goes first, and the warning above is then expected for an adapter that does not raise `EOFError` on a clean end.

A reader that does not stop within 2 seconds of its connection being closed or reopened bars opening that connection again in the chain: `the previous reader for 'device' did not stop`.

## Reports

A failing connection stage, or any connection stage under `-rP`, gets a **Connection Exchange** section in its report, titled with the attempt it shows when the stage was [retried](../advanced/retry.md#reports-har-and-logs) (`Connection Exchange (attempt 3 of 3)`):

```text
Stage 'ping': no message on connection 'device' matched {"op": "pong", "id": 2} within 0.5 s (1 message arrived after the send)
----------------------------- Connection Exchange ------------------------------
Connection 'device' (adapter adapters:LineJsonAdapter), open 1, 0.703 s old
Sent:
{
  "op": "ping",
  "id": 1
}
send() returned:
{
  "seq": 2
}
Expect:
{
  "op": "pong",
  "id": 2
}
Timeout: 0.5 s, waited 0.500 s
Result: timed out, no message matched
Considered, not matching (1 message):
#4 at +0.000 s after the send: JMESPath 'id' doesn't match: expected 2, got 1
{
  "op": "pong",
  "id": 1,
  "time": 1700000000,
  "reply_to": 2
}
Not considered: 2 messages queued before the send, 0 dropped by inbox_limit
debug_info():
unread bytes:
```

It shows:

- the connection and its adapter, which open of the connection the stage used, and how long it had been open;
- what `connect()` returned, when the stage opened the connection;
- the message sent, and what `send()` returned;
- the match as written (its templates, not their values), the timeout, and how long the stage waited;
- how the wait ended, and the message taken, or the message an `absent` wait saw;
- when nothing matched, the messages that were considered (the last 20), each with its arrival time and the first expectation it did not meet, and those that arrived within 0.2 seconds after the deadline, marked as late and left queued;
- how many queued messages were left out as older than the send, and how many `inbox_limit` dropped;
- how the connection's reader ended, if it has;
- the adapter's `debug_info()`, taken when the stage's attempt ended, before any close.

Messages are shown as JSON, cut at the report's body limit, bytes as hex.

Values under keys listed in `httpchain_redact_message_keys` are shown as `[REDACTED]` at any depth, in this section and in the failure messages that quote a message: an `absent` wait's hit, and the reasons a message did not match, a rendered expectation on a listed key included. Keys are matched case-insensitively, and are separated by whitespace or commas. The default list is the [redacted header and query parameter names](../getting-started.md#secrets-in-reports), lowercased: `authorization`, `token`, `password`, and so on. Setting the option replaces the default, and an empty value disables it. `verify` and `save` still see the real values, and their failure messages are not redacted, as an HTTP stage's body checks are not. The match is shown as written, and `debug_info()` exactly as the adapter returns it.

Connection stages add nothing to HAR files.
