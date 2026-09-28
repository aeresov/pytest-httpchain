"""One `httpx.Client` per scenario, shared by every stage, and the scenario's
``client`` block that configures it.

Documented in docs/usage/scenarios.md as a guarantee, so it needs a pin: the
cookie jar is the observable half, and nothing else in the suite exercises it.
What each ``client`` setting means for one request is pinned in
tests/unit/test_request_builder.py; here the settings meet a real server:
the example app's, which speaks HTTP/1.1 only, and, for what HTTP/2 does to
concurrency, a small HTTP/2 one defined at the end of this module.
"""

import asyncio
import concurrent.futures
import ssl
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from urllib.parse import parse_qs, urlsplit

import h2.config
import h2.connection
import h2.events
import h2.settings
import h11
import pytest

# Imported in the outer session, as tests/integration/test_ssl.py explains.
import trustme

from tests.integration.helpers import stage


def test_cookie_jar_is_shared_across_stages(run_scenario):
    """A Set-Cookie from an earlier stage is sent by a later one with no save
    step — the stages share one client, not one per request."""
    result = run_scenario("client/test_shared_cookie_jar.http.json", args=())

    result.assert_outcomes(passed=2)


def test_client_defaults_reach_the_server(run_scenario):
    """Relative URLs land under ``base_url`` (a template over the environment,
    resolved once per scenario); the client's headers and params arrive with
    every request, and a stage's own header, param or URL query wins."""
    result = run_scenario("client/test_client_defaults.http.json")

    result.assert_outcomes(passed=6)


def test_proxy_carries_every_request(run_scenario):
    """Without the proxy the host would not even resolve."""
    result = run_scenario("client/test_client_proxy.http.json")

    result.assert_outcomes(passed=1)


def test_form_and_multipart_bodies_keep_their_content_type(run_scenario):
    """A scenario-wide ``Content-Type: application/json`` went onto the form
    body as well, and replaced the multipart type with its boundary: the
    server parsed neither. Each is sent as the type it is encoded in."""
    form = stage(
        "form",
        "/echo/form",
        request={"method": "POST", "body": {"form": {"a": "1"}}},
        response=[{"save": {"jmespath": {"a": "form.a"}}}, {"verify": {"status": 200, "expressions": ["{{ a == '1' }}"]}}],
    )
    upload = stage(
        "upload",
        "/echo/multipart",
        request={"method": "POST", "body": {"files": {"upload": "upload_a.txt"}}},
        response=[{"save": {"jmespath": {"size": "fields.upload.size"}}}, {"verify": {"status": 200, "expressions": ["{{ size == 12 }}"]}}],
    )
    multipart = stage(
        "multipart",
        "/echo/multipart",
        request={"method": "POST", "body": {"multipart": {"fields": {"a": "1"}}}},
        response=[{"verify": {"status": 200, "jmespath": {"form.a": ["1"]}}}],
    )
    result = run_scenario({"client": {"headers": {"Content-Type": "application/json"}}, "stages": [form, upload, multipart]}, "body_types/upload_a.txt")

    result.assert_outcomes(passed=3)


def test_relative_url_rendered_without_base_url_fails_the_stage(run_scenario):
    """The validator only sees a URL relative before its first template
    (HTTPCHAIN034); one a template renders relative fails its stage cleanly,
    before anything is sent, instead of httpx's "missing protocol"."""
    scenario = {
        "substitutions": [{"vars": {"path": "/ok"}}],
        "stages": [{"name": "relative", "request": {"url": "{{ path }}"}, "response": [{"verify": {"status": 200}}]}],
    }
    result = run_scenario(scenario)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Request URL '/ok' is relative, but the scenario sets no client.base_url to resolve it against*"])


def test_parallel_stage_is_not_capped_by_the_connection_pool(run_scenario):
    """httpx's default pool of 100 connections silently capped
    ``max_concurrency``: 150 concurrent requests of a second took over two.
    The barrier answers only once all 120 are in flight together, so a cap
    below that fails the stage with 504s rather than slowing it down."""
    burst = stage("burst", "/barrier/120", parallel={"repeat": 120, "max_concurrency": 120})
    result = run_scenario({"stages": [burst]})

    result.assert_outcomes(passed=1)


@pytest.mark.slow
def test_max_connections_caps_concurrency(run_scenario):
    """The negative control for the test above, and proof ``max_connections``
    reaches the pool: with two connections the three requests never meet."""
    burst = stage("burst", "/barrier/3?timeout=0.3", parallel={"repeat": 3, "max_concurrency": 3})
    result = run_scenario({"client": {"max_connections": 2}, "stages": [burst]})

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*expected 200, got 504*"])


# The HTTP/2 streams the server below allows a connection at once.
STREAM_LIMIT = 2


class _Barrier:
    """The example app's ``/barrier/<parties>`` for the server below: each
    request is answered 200 once ``parties`` are in flight together, or 504
    after ``?timeout=`` seconds (5 by default)."""

    def __init__(self) -> None:
        self.arrived = 0
        self.met = asyncio.Event()

    async def answer(self, target: str) -> tuple[int, bytes]:
        url = urlsplit(target)
        parties = int(url.path.rpartition("/")[2])
        self.arrived += 1
        if self.arrived >= parties:
            self.met.set()
        try:
            await asyncio.wait_for(self.met.wait(), float(parse_qs(url.query).get("timeout", ["5"])[0]))
        except TimeoutError:
            return 504, b"{}"
        return 200, b"{}"


class _BarrierConnection(asyncio.Protocol):
    """One TLS connection to the barrier, in HTTP/2 or HTTP/1.1, whichever
    the client's ALPN offer settles on."""

    def __init__(self, barrier: _Barrier) -> None:
        self.barrier = barrier
        self.pending: set[asyncio.Task] = set()
        self.target = ""

    def connection_made(self, transport) -> None:
        self.transport = transport
        self.h2: h2.connection.H2Connection | None = None
        if transport.get_extra_info("ssl_object").selected_alpn_protocol() == "h2":
            self.h2 = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
            # Sent in the connection's first SETTINGS frame, in place of h2's
            # default of 100. From a second one (update_settings()), httpcore
            # would first open up 100 streams, then take them back, and a
            # thread that took one in between could leave the reading thread
            # waiting on it with the read lock held.
            self.h2.local_settings = h2.settings.Settings(client=False, initial_values={h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: STREAM_LIMIT})
            self.h2.initiate_connection()
            transport.write(self.h2.data_to_send())
        else:
            self.h11 = h11.Connection(h11.SERVER)

    def data_received(self, data: bytes) -> None:
        if self.h2 is not None:
            for event in self.h2.receive_data(data):
                if isinstance(event, h2.events.RequestReceived):
                    self._answer(dict(event.headers)[":path"], partial(self._send_h2, event.stream_id))
            self.transport.write(self.h2.data_to_send())
        else:
            self.h11.receive_data(data)
            self._next_h11()

    def _next_h11(self) -> None:
        while True:
            event = self.h11.next_event()
            if isinstance(event, h11.Request):
                self.target = event.target.decode()
            elif isinstance(event, h11.EndOfMessage):
                self._answer(self.target, self._send_h11)
            elif not isinstance(event, h11.Data):
                return  # NEED_DATA, PAUSED until answered, or closed

    def _answer(self, target: str, send: Callable[[int, bytes], None]) -> None:
        task = asyncio.get_running_loop().create_task(self._respond(target, send))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _respond(self, target: str, send: Callable[[int, bytes], None]) -> None:
        status, body = await self.barrier.answer(target)
        if not self.transport.is_closing():
            send(status, body)

    def _send_h2(self, stream_id: int, status: int, body: bytes) -> None:
        assert self.h2 is not None
        self.h2.send_headers(stream_id, [(":status", str(status)), ("content-type", "application/json"), ("content-length", str(len(body)))])
        self.h2.send_data(stream_id, body, end_stream=True)
        self.transport.write(self.h2.data_to_send())

    def _send_h11(self, status: int, body: bytes) -> None:
        headers = [("content-type", "application/json"), ("content-length", str(len(body)))]
        for event in (h11.Response(status_code=status, headers=headers), h11.Data(data=body), h11.EndOfMessage()):
            self.transport.write(self.h11.send(event))
        self.h11.start_next_cycle()
        self._next_h11()


@contextmanager
def _serve_barrier(context: ssl.SSLContext) -> Iterator[str]:
    """Serve the barrier over TLS from an event loop in a background thread."""
    started: concurrent.futures.Future = concurrent.futures.Future()

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        stop = loop.create_future()
        barrier = _Barrier()
        server = await loop.create_server(lambda: _BarrierConnection(barrier), "127.0.0.1", 0, ssl=context)
        async with server:
            started.set_result((server.sockets[0].getsockname()[1], loop, stop))
            await stop
            server.close_clients()

    thread = threading.Thread(target=asyncio.run, args=(serve(),), name="barrier-tls-server")
    thread.start()
    port, loop, stop = started.result(timeout=10)
    try:
        yield f"https://127.0.0.1:{port}"
    finally:
        loop.call_soon_threadsafe(stop.set_result, None)
        thread.join()


@pytest.fixture
def h2_server(pytester):
    """The barrier over TLS, in HTTP/2 to a client that offers it and HTTP/1.1
    otherwise, as an HTTPS API commonly is (the example app's server speaks
    HTTP/1.1 only), with the issuing CA's bundle at ``ca.pem``."""
    ca = trustme.CA()
    ca.cert_pem.write_to_path(str(pytester.path / "ca.pem"))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("127.0.0.1").configure_cert(context)
    context.set_alpn_protocols(["h2", "http/1.1"])
    with _serve_barrier(context) as url:
        yield url


@pytest.mark.parametrize(
    ("client", "parties", "outcome"),
    [
        # httpcore sends every request to one origin down a single HTTP/2
        # connection, and keeps as many streams open on it as the server
        # allows (up to its own 100), however large the pool.
        pytest.param({}, STREAM_LIMIT, "passed", id="http2-carries-the-stream-limit"),
        pytest.param({}, STREAM_LIMIT + 1, "failed", id="http2-holds-back-one-more", marks=pytest.mark.slow),
        # The documented way past it: HTTP/1.1, one connection per request.
        pytest.param({"http2": False}, STREAM_LIMIT + 1, "passed", id="http1-lifts-the-cap"),
    ],
)
def test_http2_caps_requests_in_flight_at_the_stream_limit(run_scenario, h2_server, client, parties, outcome):
    """The unbounded pool does not help under HTTP/2, the default, against a
    server that negotiates it: docs/advanced/parallel.md says so and names
    ``client.http2: false`` as the way out, and this keeps that true.

    The server allows two streams, a limit the docs name as well, rather than
    leaving httpcore's own 100 in force: it is the same cap, and a hundred
    threads opening streams at once on httpcore's one sync HTTP/2 connection
    race each other (two took the same stream ID, or sent theirs out of
    order, and the server reset the stream), failing a run now and then
    whatever it tests.
    With two streams they open one at a time, as httpcore allows a single
    stream until the server's SETTINGS frame arrives."""
    timeout = 5 if outcome == "passed" else 0.5
    burst = {
        "name": "burst",
        "request": {"url": f"/barrier/{parties}?timeout={timeout}"},
        "parallel": {"repeat": parties, "max_concurrency": parties},
        "response": [{"verify": {"status": 200}}],
    }
    result = run_scenario({"ssl": {"verify": "ca.pem"}, "client": {"base_url": h2_server, **client}, "stages": [burst]})

    result.assert_outcomes(**{outcome: 1})
    if outcome == "failed":
        # Held back, not refused: the first two waited out the barrier together.
        result.stdout.fnmatch_lines(["*expected 200, got 504*"])
