"""What an imported scenario sends, for the importers' tests to compare with
the requests it was imported from::

    from tests.unit.importers.helpers import comparable, sent_requests
"""

import email.parser
import email.policy
import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx

from pytest_httpchain.importers import scenario_text
from pytest_httpchain.request_builder import build_client_kwargs, build_request_kwargs
from pytest_httpchain.templates import walk
from pytest_httpchain.utils import process_substitutions
from pytest_httpchain.validation import load_scenario


def load_written(scenario: dict[str, Any], directory: Path | None = None) -> Any:
    """``scenario`` written as an import writes it, into ``directory`` (a
    directory of its own when None), and loaded back as collection loads a
    file: through the reference resolver, then the model."""
    if directory is None:
        with tempfile.TemporaryDirectory() as temporary:
            return load_written(scenario, Path(temporary))
    path = directory / "test_imported.http.json"
    path.write_text(scenario_text(scenario), encoding="utf-8")
    model, _ = load_scenario(path, root_path=directory)
    return model


def sent_requests(scenario: dict[str, Any], scenario_dir: Path | None = None) -> list[httpx.Request]:
    """The requests ``scenario``'s stages send, in order, built as the
    carrier builds them from the file an import writes (loaded as
    collection loads it, its substitutions, client, auth and each stage's
    request rendered, then `build_client_kwargs` and `build_request_kwargs`)
    and caught by a transport that answers each with a 200."""
    model = load_written(scenario, scenario_dir)
    context = process_substitutions(model.substitutions)
    client_config = walk(model.client, context)
    auth = walk(model.auth, context) if model.auth is not None else None
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        request.read()
        sent.append(request)
        return httpx.Response(200)

    kwargs = build_client_kwargs(client_config, walk(model.ssl, context), auth, scenario_dir)
    with httpx.Client(**kwargs, transport=httpx.MockTransport(record)) as client:
        for stage in model.stages:
            client.request(**build_request_kwargs(walk(stage.request, context), scenario_dir, client_config))
    return sent


def _parts(content: bytes, content_type: str) -> list[tuple[Any, ...]]:
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + content)
    return [(part.get_param("name", header="content-disposition"), part.get_filename(), part.get("content-type"), part.get_payload(decode=True)) for part in message.iter_parts()]


def comparable(request: httpx.Request) -> tuple[Any, ...]:
    """What makes two requests equivalent: method, URL with its query as
    pairs, in order, headers but the body's length, and the body: a
    multipart one part by part (its boundary is each request's own), a JSON
    one as the value it encodes."""
    content = request.read()
    content_type = request.headers.get("content-type", "")
    headers = sorted((name.lower(), value) for name, value in request.headers.multi_items() if name.lower() != "content-length")
    body: Any = content
    if content_type.startswith("multipart/form-data"):
        body = _parts(content, content_type)
        headers = [(name, "multipart/form-data" if name == "content-type" else value) for name, value in headers]
    elif "json" in content_type:
        body = json.loads(content)
    query = parse_qsl(request.url.query.decode(), keep_blank_values=True)
    return request.method, str(request.url.copy_with(query=None)), query, headers, body
