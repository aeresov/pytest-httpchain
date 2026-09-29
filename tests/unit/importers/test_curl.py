"""Unit tests for importers/curl.py: curl commands read as a shell and curl
would, and the round trip of the curl command a report shows."""

import base64
import dataclasses
import json
import re
import shlex
from typing import Any

import httpx
import pytest

from pytest_httpchain.importers import (
    FileData,
    ImportSourceError,
    Part,
    PartsData,
    RecordedRequest,
    ShellCommand,
    Stdin,
    TextData,
    build_scenario,
    parse_curl,
    parse_curl_words,
    split_commands,
)
from pytest_httpchain.models import Request
from pytest_httpchain.redaction import NO_REDACTION
from pytest_httpchain.report_formatter import format_curl
from pytest_httpchain.request_builder import build_request_kwargs
from tests.unit.importers.helpers import comparable, sent_requests

# What a command without options sends: curl follows no redirect unless told.
_DEFAULTS = RecordedRequest(method="", url="", follow_redirects=False)


def _fields(request: RecordedRequest) -> dict[str, Any]:
    """The fields ``request`` sets beyond what a command without options sends."""
    values = {field.name: getattr(request, field.name) for field in dataclasses.fields(request)}
    return {name: value for name, value in values.items() if name in ("method", "url") or value != getattr(_DEFAULTS, name)}


def _one(text: str) -> tuple[dict[str, Any], list[str]]:
    requests, warnings = parse_curl(text)
    assert len(requests) == 1
    return _fields(requests[0]), warnings


# --- the shell's part: quoting, continuations, comments, several commands ---


def _shape(pipelines: list[list[ShellCommand]]) -> list[list[Any]]:
    """Each pipeline's commands: a command's text, with its redirections and
    standard input beside it where it has them."""
    return [
        [command.text if not command.redirections and command.stdin is None else (command.text, command.redirections, command.stdin) for command in pipeline]
        for pipeline in pipelines
    ]


@pytest.mark.parametrize(
    ("text", "pipelines", "warnings"),
    [
        pytest.param("curl https://x.test/a", [["curl https://x.test/a"]], [], id="one-command"),
        # A backslash before a line break continues the line, as docs paste it.
        pytest.param("curl \\\n  -X POST \\\r\n  https://x.test/a", [["curl   -X POST   https://x.test/a"]], [], id="continuations"),
        # ... but not inside single quotes, where it is text.
        pytest.param("curl -d 'a\\\nb' https://x.test/a", [["curl -d 'a\\\nb' https://x.test/a"]], [], id="continuation-in-single-quotes"),
        pytest.param('curl -d "a\\\nb" https://x.test/a', [['curl -d "ab" https://x.test/a']], [], id="continuation-in-double-quotes"),
        # A comment starts a word; a `#` inside one is text (a URL's fragment).
        pytest.param("# note\ncurl https://x.test/a#top # trailing", [["curl https://x.test/a#top"]], [], id="comments"),
        pytest.param("curl https://x.test/a\ncurl https://x.test/b", [["curl https://x.test/a"], ["curl https://x.test/b"]], [], id="one-per-line"),
        pytest.param(
            "curl https://x.test/a; curl https://x.test/b && curl https://x.test/c || curl https://x.test/d",
            [["curl https://x.test/a"], ["curl https://x.test/b"], ["curl https://x.test/c"], ["curl https://x.test/d"]],
            [],
            id="separators",
        ),
        pytest.param("curl 'https://x.test/a;b&c|d>e'", [["curl 'https://x.test/a;b&c|d>e'"]], [], id="separators-quoted"),
        # bash's $'...' decoded, for shlex, which has no such quoting.
        pytest.param("curl -d $'it\\'s\\n\\x41\\u00e9' https://x.test/a", [["curl -d 'it'\"'\"'s\n" + "Aé' https://x.test/a"]], [], id="ansi-c-quoting"),
        pytest.param(r"curl -d $'\101\cA\e\q' https://x.test/a", [["curl -d 'A\x01\x1b\\q' https://x.test/a"]], [], id="ansi-c-octal-control-unknown"),
        pytest.param('curl -d $"x" https://x.test/a', [['curl -d "x" https://x.test/a']], [], id="locale-quoting"),
        # A CRLF after the backslash too (a command pasted from Windows),
        # inside double quotes as outside them.
        pytest.param('curl \\\r\n  -d "a\\\r\nb" https://x.test/a', [['curl   -d "ab" https://x.test/a']], [], id="continuations-crlf"),
        # A pipeline's commands, each with its own redirections; a line
        # break after a `|` continues it.
        pytest.param(
            "cat body.json | curl -d @- https://x.test/a | jq '.a; .b' > out.json",
            [["cat body.json", "curl -d @- https://x.test/a", ("jq '.a; .b'", ["> out.json"], None)]],
            [],
            id="pipeline",
        ),
        pytest.param("curl https://x.test/a |\n  jq . |& tee log", [["curl https://x.test/a", "jq .", "tee log"]], [], id="pipeline-continued"),
        # A redirection takes one word, wherever it is; its descriptor is
        # the digits before it; `&` in one is no background operator.
        pytest.param(
            "curl >out https://x.test/a 2>&1 -s; curl https://x.test/b &> log",
            [[("curl  https://x.test/a  -s", [">out", "2>&1"], None)], [("curl https://x.test/b", ["&> log"], None)]],
            [],
            id="redirections",
        ),
        pytest.param("curl https://x.test/a>out", [[("curl https://x.test/a", [">out"], None)]], [], id="redirection-attached"),
        # Standard input: a file, a here-string, a here-document (its body
        # the lines up to its delimiter; with <<- less their leading tabs).
        pytest.param(
            "curl -d @- https://x.test/a < 'my body.json'",
            [[("curl -d @- https://x.test/a", [], Stdin("the redirection < 'my body.json'", path="my body.json"))]],
            [],
            id="stdin-file",
        ),
        pytest.param("curl -d @- https://x.test/a <<< $'a=1\\nb'", [[("curl -d @- https://x.test/a", [], Stdin("the here-string", text="a=1\nb\n"))]], [], id="here-string"),
        pytest.param(
            'curl -d @- https://x.test/a <<EOF; curl https://x.test/b\n{"a": "$HOME \\$x"}\n\tEOF\nEOF\ncurl https://x.test/c',
            [
                [("curl -d @- https://x.test/a", [], Stdin("the here-document", text='{"a": "$HOME $x"}\n\tEOF\n'))],
                ["curl https://x.test/b"],
                ["curl https://x.test/c"],
            ],
            ["The curl command uses shell expansions, which are not expanded: $HOME is imported as written"],
            id="here-document",
        ),
        pytest.param(
            "curl -d @- https://x.test/a <<-'EOF'\r\n\t$HOME\r\n\tEOF\r\n",
            [[("curl -d @- https://x.test/a", [], Stdin("the here-document", text="$HOME\n"))]],
            [],
            id="here-document-quoted-tabs-stripped",
        ),
        pytest.param("curl https://x.test/a 3<<EOF\nx\nEOF", [[("curl https://x.test/a", ["3<<EOF"], None)]], [], id="here-document-other-descriptor"),
        pytest.param(
            'curl -H "Authorization: Bearer $TOKEN" "$BASE/a"',
            [['curl -H "Authorization: Bearer $TOKEN" "$BASE/a"']],
            ["The curl command uses shell expansions, which are not expanded: $TOKEN, $BASE is imported as written"],
            id="expansions",
        ),
        pytest.param('curl "https://x.test/\\$a"', [['curl "https://x.test/$a"']], [], id="escaped-dollar"),
        pytest.param(
            "curl https://x.test/`date`",
            [["curl https://x.test/`date`"]],
            ["The curl command uses shell expansions, which are not expanded: `...` is imported as written"],
            id="backticks",
        ),
        pytest.param("", [], [], id="empty"),
    ],
)
def test_split_commands(text, pipelines, warnings):
    found, found_warnings = split_commands(text)
    assert (_shape(found), found_warnings) == (pipelines, warnings)


def test_split_commands_keeps_each_command_as_written():
    """What a warning names: each command as written, a redirection and its
    word as written too."""
    [[cat, curl, jq]], _ = split_commands("cat  body.json |  curl -d @- \\\n  https://x.test/a 2> /dev/null | jq .")
    assert (cat.written, curl.written, jq.written, curl.redirections) == ("cat  body.json", "curl -d @- \\\n  https://x.test/a 2> /dev/null", "jq .", ["2> /dev/null"])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("curl -d 'x https://x.test", "The curl command has a single quote (') that is never closed", id="single"),
        pytest.param('curl -d "x https://x.test', 'The curl command has a double quote (") that is never closed', id="double"),
        pytest.param("curl -d $'x https://x.test", "The curl command has a $'...' string that is never closed", id="ansi-c"),
        pytest.param("", "There is no curl command to import", id="nothing"),
        pytest.param("curl https://x.test/a\necho done", "Command 2 is not a curl command: 'echo done'", id="not-curl"),
        pytest.param("curl -X", "The curl option -X needs a value", id="missing-value"),
        pytest.param("curl https://x.test/a \\", "Cannot read the curl command: No escaped character", id="trailing-backslash"),
        pytest.param("curl -sS", "The curl command has no URL", id="no-url"),
        pytest.param("curl ftp://x.test/a", "The curl command's URL 'ftp://x.test/a' is not http or https", id="not-http"),
        # A command without the name curl starts with an option or a URL
        # with its scheme: anything else is some other command, whose words
        # would be taken for URLs.
        pytest.param("wget https://x.test/a", "The command is not a curl command: 'wget https://x.test/a'", id="wget"),
        pytest.param("http POST https://x.test/a", "The command is not a curl command: 'http POST https://x.test/a'", id="httpie"),
        pytest.param("x.test/a -X POST", "The command is not a curl command: 'x.test/a -X POST'", id="bare-host-first"),
        pytest.param("cat o.json | wget https://x.test/a", "The command is not a curl command: 'cat o.json | wget https://x.test/a'", id="pipeline-without-curl"),
        pytest.param("# only a note", "There is no curl command to import", id="only-a-comment"),
        # A URL no URL parser reads, a file name no file has, a redirection
        # without its file: a clean error, never a traceback.
        pytest.param("curl -g 'http://[::1/x'", "The curl command's URL 'http://[::1/x' is no URL: Invalid IPv6 URL", id="malformed-url"),
        pytest.param(
            "curl -d $'@a\\x00b' https://x.test/", "The curl command reads the file 'a\\x00b', which cannot be: no file name holds a NUL character", id="nul-in-data-file"
        ),
        pytest.param(
            "curl --data-binary $'@a\\x00b' https://x.test/",
            "The curl command reads the file 'a\\x00b', which cannot be: no file name holds a NUL character",
            id="nul-in-binary-file",
        ),
        pytest.param(
            "curl -F $'f=@a\\x00b' https://x.test/", "The curl command reads the file 'a\\x00b', which cannot be: no file name holds a NUL character", id="nul-in-form-file"
        ),
        pytest.param(
            "curl -d @- https://x.test/ < $'a\\x00b'",
            "The curl command reads the file 'a\\x00b', which cannot be: no file name holds a NUL character",
            id="nul-in-stdin-file",
        ),
        pytest.param("curl https://x.test/a >", "The curl command's redirection '>' has nothing after it", id="redirection-without-file"),
    ],
)
def test_unreadable_commands_fail_cleanly(text, message):
    with pytest.raises(ImportSourceError, match=f"^{re.escape(message)}$"):
        parse_curl(text)


# --- the options ---

_FORM = [("Content-Type", "application/x-www-form-urlencoded")]
_JSON = [("Content-Type", "application/json"), ("Accept", "application/json")]


@pytest.mark.parametrize(
    ("command", "fields"),
    [
        pytest.param("curl https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, id="url"),
        pytest.param("curl --url https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, id="--url"),
        # curl's default scheme, and the fragment curl never sends.
        pytest.param("curl x.test/a#frag", {"method": "GET", "url": "http://x.test/a"}, id="bare-host"),
        # A single command may leave out its name.
        pytest.param("-X DELETE https://x.test/a", {"method": "DELETE", "url": "https://x.test/a"}, id="without-curl"),
        pytest.param("curl -X patch https://x.test/a", {"method": "PATCH", "url": "https://x.test/a"}, id="-X"),
        pytest.param("curl -XPUT https://x.test/a", {"method": "PUT", "url": "https://x.test/a"}, id="-X-attached"),
        pytest.param("curl --request OPTIONS https://x.test/a", {"method": "OPTIONS", "url": "https://x.test/a"}, id="--request"),
        pytest.param("curl -I https://x.test/a", {"method": "HEAD", "url": "https://x.test/a"}, id="-I"),
        pytest.param("curl --head https://x.test/a", {"method": "HEAD", "url": "https://x.test/a"}, id="--head"),
        pytest.param(
            "curl -H 'Accept: text/html' -H 'X-Empty;' --header 'X-Two:  b ' https://x.test/a",
            {"method": "GET", "url": "https://x.test/a", "headers": [("Accept", "text/html"), ("X-Empty", ""), ("X-Two", "b")]},
            id="-H",
        ),
        pytest.param("curl -d 'a=1' -d 'b=2' https://x.test/a", {"method": "POST", "url": "https://x.test/a", "headers": _FORM, "body": TextData("a=1&b=2")}, id="-d"),
        pytest.param("curl --data a=1 https://x.test/a", {"method": "POST", "url": "https://x.test/a", "headers": _FORM, "body": TextData("a=1")}, id="--data"),
        pytest.param("curl --data-ascii a=1 https://x.test/a", {"method": "POST", "url": "https://x.test/a", "headers": _FORM, "body": TextData("a=1")}, id="--data-ascii"),
        pytest.param("curl --data-raw @literal https://x.test/a", {"method": "POST", "url": "https://x.test/a", "headers": _FORM, "body": TextData("@literal")}, id="--data-raw"),
        pytest.param(
            "curl --data-binary @body.bin https://x.test/a", {"method": "POST", "url": "https://x.test/a", "headers": _FORM, "body": FileData("body.bin")}, id="--data-binary-file"
        ),
        # curl's encoding: the content encoded, `+` for a space, the name as given.
        pytest.param(
            "curl --data-urlencode 'q=a b&c' --data-urlencode '=x y' --data-urlencode 'plain text' https://x.test/a",
            {"method": "POST", "url": "https://x.test/a", "headers": _FORM, "body": TextData("q=a+b%26c&x+y&plain+text")},
            id="--data-urlencode",
        ),
        pytest.param(
            "curl --json '{\"a\":1}' --json '{\"b\":2}' https://x.test/a",
            {"method": "POST", "url": "https://x.test/a", "headers": _JSON, "body": TextData('{"a":1}{"b":2}')},
            id="--json",
        ),
        # A header the command gives wins over curl's own.
        pytest.param(
            "curl -H 'Content-Type: text/plain' -d hi https://x.test/a",
            {"method": "POST", "url": "https://x.test/a", "headers": [("Content-Type", "text/plain")], "body": TextData("hi")},
            id="-d-typed",
        ),
        # `Name:` removes a header curl writes itself.
        pytest.param("curl -H 'Content-Type:' -d hi https://x.test/a", {"method": "POST", "url": "https://x.test/a", "body": TextData("hi")}, id="-d-untyped"),
        pytest.param(
            "curl -F 'title=Hi' -F 'doc=@a.txt;type=text/plain;filename=b.txt' -F 'note=<n.txt' -F 'kind=x;type=text/x' -F 'q=\"a;\\\"b\"' --form-string 'raw=@x' https://x.test/a",
            {
                "method": "POST",
                "url": "https://x.test/a",
                "body": PartsData(
                    (
                        Part("title", value="Hi"),
                        Part("doc", path="a.txt", filename="b.txt", content_type="text/plain"),
                        Part("note", path="n.txt", filename=""),
                        Part("kind", value="x", content_type="text/x"),
                        Part("q", value='a;"b'),
                        Part("raw", value="@x"),
                    )
                ),
            },
            id="-F",
        ),
        pytest.param("curl -G -d a=1 --data-urlencode 'b=c d' 'https://x.test/a?z=0'", {"method": "GET", "url": "https://x.test/a?z=0&a=1&b=c+d"}, id="-G"),
        pytest.param("curl --get -d a=1 https://x.test/a", {"method": "GET", "url": "https://x.test/a?a=1"}, id="--get"),
        pytest.param(
            "curl --url-query 'q=1 2' --url-query '+raw=%41' --url-query plain https://x.test/a", {"method": "GET", "url": "https://x.test/a?q=1+2&raw=%41&plain"}, id="--url-query"
        ),
        pytest.param("curl -u 'ann:pw:1' https://x.test/a", {"method": "GET", "url": "https://x.test/a", "user": ("ann", "pw:1")}, id="-u"),
        # Without a password curl asks for one.
        pytest.param("curl --user ann https://x.test/a", {"method": "GET", "url": "https://x.test/a", "user": ("ann", None)}, id="--user-no-password"),
        pytest.param("curl --digest -u ann:pw https://x.test/a", {"method": "GET", "url": "https://x.test/a", "user": ("ann", "pw"), "digest": True}, id="--digest"),
        pytest.param("curl --digest --basic -u ann:pw https://x.test/a", {"method": "GET", "url": "https://x.test/a", "user": ("ann", "pw")}, id="--basic"),
        pytest.param("curl --oauth2-bearer tok https://x.test/a", {"method": "GET", "url": "https://x.test/a", "bearer": "tok"}, id="--oauth2-bearer"),
        pytest.param("curl -A 'Agent/1' https://x.test/a", {"method": "GET", "url": "https://x.test/a", "headers": [("User-Agent", "Agent/1")]}, id="-A"),
        pytest.param("curl --user-agent A -H 'User-Agent: B' https://x.test/a", {"method": "GET", "url": "https://x.test/a", "headers": [("User-Agent", "B")]}, id="-A-overridden"),
        # An empty one removes curl's own, which is not written anyway.
        pytest.param("curl -A A -A '' https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, id="-A-empty"),
        pytest.param("curl -e 'https://ref.test/;auto' https://x.test/a", {"method": "GET", "url": "https://x.test/a", "headers": [("Referer", "https://ref.test/")]}, id="-e"),
        pytest.param("curl --referer r https://x.test/a", {"method": "GET", "url": "https://x.test/a", "headers": [("Referer", "r")]}, id="--referer"),
        pytest.param("curl -b 'a=1; b=2' --cookie c=3 https://x.test/a", {"method": "GET", "url": "https://x.test/a", "cookies": [("a", "1"), ("b", "2"), ("c", "3")]}, id="-b"),
        pytest.param("curl -L https://x.test/a", {"method": "GET", "url": "https://x.test/a", "follow_redirects": True}, id="-L"),
        pytest.param("curl --location https://x.test/a", {"method": "GET", "url": "https://x.test/a", "follow_redirects": True}, id="--location"),
        pytest.param("curl -k https://x.test/a", {"method": "GET", "url": "https://x.test/a", "verify_tls": False}, id="-k"),
        pytest.param("curl --insecure https://x.test/a", {"method": "GET", "url": "https://x.test/a", "verify_tls": False}, id="--insecure"),
        pytest.param("curl -m 2.5 https://x.test/a", {"method": "GET", "url": "https://x.test/a", "timeout": 2.5}, id="-m"),
        pytest.param("curl --max-time 3 https://x.test/a", {"method": "GET", "url": "https://x.test/a", "timeout": 3.0}, id="--max-time"),
        # Clustered short options, the last one taking a value.
        pytest.param("curl -sSLkXPOST https://x.test/a", {"method": "POST", "url": "https://x.test/a", "follow_redirects": True, "verify_tls": False}, id="cluster"),
        # Output, progress and protocol options change nothing a scenario says.
        pytest.param(
            "curl -s -S -v -i -f -# -N -g -0 --compressed --http2 --http1.1 --globoff --fail-with-body --no-progress-meter -O -w '%{http_code}' -D h.txt --trace t.txt https://x.test/a",
            {"method": "GET", "url": "https://x.test/a"},
            id="ignored",
        ),
    ],
)
def test_options(command, fields):
    assert _one(command) == (fields, [])


def test_one_request_per_url_and_per_next():
    """curl sends each URL with the same options, and after ``--next`` starts
    over with options of its own."""
    requests, warnings = parse_curl("curl -H 'X: 1' https://x.test/a https://x.test/b --next -d a=1 https://x.test/c")
    assert [(request.method, request.url, request.headers) for request in requests] == [
        ("GET", "https://x.test/a", [("X", "1")]),
        ("GET", "https://x.test/b", [("X", "1")]),
        ("POST", "https://x.test/c", _FORM),
    ]
    assert warnings == []


@pytest.mark.parametrize(
    ("command", "fields", "warnings"),
    [
        # Named, never silently dropped: a curl option the import does not
        # map (its value skipped as curl reads it, not taken for a URL) and
        # what curl does not have.
        pytest.param(
            "curl --retry 3 -x http://proxy:3128 --cacert ca.pem --ntlm -4 -z yesterday --frobnicate -Wq https://x.test/a",
            {"method": "GET", "url": "https://x.test/a"},
            ["Ignored the curl options the import does not map: --retry, -x, --cacert, --ntlm, -4, -z", "Ignored what is not a curl option: --frobnicate, -Wq"],
            id="unmapped-and-unknown",
        ),
        # httpx collapses a path's dot segments, which --path-as-is keeps.
        pytest.param(
            "curl --path-as-is https://x.test/a/../b",
            {"method": "GET", "url": "https://x.test/a/../b"},
            ["Ignored the curl options the import does not map: --path-as-is"],
            id="path-as-is",
        ),
        # curl sends a Cookie header given with -H in place of -b's cookies
        # (and none when -H removes it).
        pytest.param(
            "curl -b a=1 -H 'Cookie: c=3' https://x.test/a",
            {"method": "GET", "url": "https://x.test/a", "headers": [("Cookie", "c=3")]},
            ["Left out the -b cookies: curl sends the Cookie header given with -H in their place"],
            id="cookie-header-over-b",
        ),
        pytest.param(
            "curl -b a=1 -H 'Cookie:' https://x.test/a",
            {"method": "GET", "url": "https://x.test/a"},
            ["Left out the -b cookies: curl sends the Cookie header given with -H in their place"],
            id="cookie-header-removed",
        ),
        # curl never takes `--name=value`.
        pytest.param("curl --request=POST https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Ignored what is not a curl option: --request=POST"], id="equals-form"),
        pytest.param(
            "curl -b cookies.txt https://x.test/a",
            {"method": "GET", "url": "https://x.test/a"},
            ["Left out -b 'cookies.txt', a cookie file: add its cookies to the scenario"],
            id="cookie-file",
        ),
        pytest.param(
            "curl -H @headers.txt https://x.test/a",
            {"method": "GET", "url": "https://x.test/a"},
            ["Left out -H '@headers.txt', which reads headers from a file: add them to the scenario"],
            id="header-file",
        ),
        pytest.param(
            "curl -H 'not a header' https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Left out -H 'not a header', which is no header"], id="not-a-header"
        ),
        pytest.param(
            "curl --data-urlencode 'a@file.txt' -d b=1 https://x.test/a",
            {"method": "POST", "url": "https://x.test/a", "headers": _FORM, "body": TextData("b=1")},
            ["Left out --data-urlencode 'a@file.txt', which reads a file: add its content to the scenario"],
            id="urlencode-file",
        ),
        pytest.param(
            "curl -d @a.txt -d b=1 https://x.test/a",
            {"method": "POST", "url": "https://x.test/a", "headers": _FORM, "body": TextData("b=1")},
            ["Left out the data read from a.txt: add it to the scenario"],
            id="file-beside-data",
        ),
        pytest.param(
            "curl -d @- https://x.test/a",
            {"method": "POST", "url": "https://x.test/a"},
            ["Left out the data read from standard input (@-): add it to the scenario"],
            id="stdin-data",
        ),
        pytest.param(
            "curl -F noequals https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Left out the form part 'noequals', which is no name=content"], id="form-no-name"
        ),
        pytest.param(
            "curl -F 'f=@a.txt;encoder=base64' https://x.test/a",
            {"method": "POST", "url": "https://x.test/a", "body": PartsData((Part("f", path="a.txt"),))},
            ["Ignored the ;encoder= of form part 'f'"],
            id="form-param",
        ),
        pytest.param(
            "curl -F a=1 -d b=2 https://x.test/a",
            {"method": "POST", "url": "https://x.test/a", "body": PartsData((Part("a", value="1"),))},
            ["Left out the -d data beside the -F form parts, which curl refuses to send together"],
            id="form-and-data",
        ),
        pytest.param(
            "curl -m soon https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Ignored --max-time 'soon', which is no number of seconds"], id="bad-max-time"
        ),
        # No timeout a client can have: infinity (which JSON cannot hold), a
        # negative number, and curl's 0, no limit, for which it has no setting.
        pytest.param("curl -m inf https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Ignored --max-time 'inf', which is no number of seconds"], id="-m-inf"),
        pytest.param("curl -m -1 https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Ignored --max-time '-1', which is no number of seconds"], id="-m-negative"),
        pytest.param(
            "curl -m 0 https://x.test/a",
            {"method": "GET", "url": "https://x.test/a"},
            ["Ignored --max-time 0, curl's no limit, which a scenario has no setting for: the client's timeout applies (30 seconds by default)"],
            id="-m-0",
        ),
        # curl's -o, which reads as import's own given after the command.
        pytest.param(
            "curl -o out.json --output o2.json -so o3.json https://x.test/a",
            {"method": "GET", "url": "https://x.test/a"},
            [
                "Ignored -o 'out.json', the file curl writes its answer to: to write the scenario to a file, give import's -o before the command",
                "Ignored --output 'o2.json', the file curl writes its answer to: to write the scenario to a file, give import's -o before the command",
                "Ignored -o 'o3.json', the file curl writes its answer to: to write the scenario to a file, give import's -o before the command",
            ],
            id="-o",
        ),
        # What comes before curl: a prompt, sudo, a variable for curl's
        # environment, what runs curl with options of its own. Not taken for
        # URLs (curl's default scheme would make them http://$/ and
        # http://sudo/), nor its options for curl's (sudo's -E would take
        # the word curl for curl's -E value).
        pytest.param("$ curl https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Ignored what comes before curl: $"], id="prompt"),
        pytest.param(
            "TOKEN=abc sudo /usr/bin/curl https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Ignored what comes before curl: TOKEN=abc sudo"], id="prefix-words"
        ),
        pytest.param("sudo -E curl https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Ignored what comes before curl: sudo -E"], id="runner-option"),
        pytest.param("watch -n 1 curl https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Ignored what comes before curl: watch -n 1"], id="runner-option-value"),
        pytest.param("time -p ./curl https://x.test/a", {"method": "GET", "url": "https://x.test/a"}, ["Ignored what comes before curl: time -p"], id="runner-curl-path"),
        # Without its name, a command starting with an option or a URL is
        # curl's: a curl among its words is a value, and a URL ending in
        # /curl a URL.
        pytest.param("-A curl https://x.test/a", {"method": "GET", "url": "https://x.test/a", "headers": [("User-Agent", "curl")]}, [], id="nameless-curl-value"),
        pytest.param("https://x.test/bin/curl -I", {"method": "HEAD", "url": "https://x.test/bin/curl"}, [], id="nameless-url-first"),
        # Read here without a directory to read it from, -d @file is the
        # file's, sent as it is, where curl strips its line breaks.
        pytest.param(
            "curl -d @data.txt https://x.test/a",
            {"method": "POST", "url": "https://x.test/a", "headers": _FORM, "body": FileData("data.txt")},
            ["curl sends data.txt with its line breaks removed, but it is not read here: the scenario sends the file as it is"],
            id="-d-file",
        ),
    ],
)
def test_what_is_not_imported_is_named(command, fields, warnings):
    assert _one(command) == (fields, warnings)


def test_a_word_that_looks_like_no_url_is_named():
    """curl takes a word that is no option for a URL, http:// its default
    scheme: one without a dot or a port in its host, but localhost, is most
    likely no URL (an option's value the import does not know of, a word
    of another command), and a warning names it."""
    requests, warnings = parse_curl("curl -n 1 https://x.test/a x.test/b localhost/c localhost:8080/d '[::1]/e' api/f")
    assert [request.url for request in requests] == [
        "http://1",
        "https://x.test/a",
        "http://x.test/b",
        "http://localhost/c",
        "http://localhost:8080/d",
        "http://[::1]/e",
        "http://api/f",
    ]
    assert warnings == [
        "Ignored the curl options the import does not map: -n",
        "Imported '1' as the URL 'http://1', as curl reads a word that is no option: if it is no URL, remove it",
        "Imported 'api/f' as the URL 'http://api/f', as curl reads a word that is no option: if it is no URL, remove it",
    ]


def test_prompts_before_several_commands():
    """Each of several commands may follow a prompt; a line that is no curl
    command, after one or not, is still refused."""
    requests, warnings = parse_curl("$ curl https://x.test/a\n$ curl https://x.test/b")
    assert [request.url for request in requests] == ["https://x.test/a", "https://x.test/b"]
    assert warnings == ["Ignored what comes before curl: $", "Ignored what comes before curl: $"]
    with pytest.raises(ImportSourceError, match=re.escape("Command 2 is not a curl command: '$ echo done'")):
        parse_curl("$ curl https://x.test/a\n$ echo done")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        # A curl with nothing after it is a word of another command, which
        # is then refused by its number, not read as curl without a URL.
        pytest.param("sudo apt-get install -y curl\ncurl https://x.test/a", "Command 1 is not a curl command: 'sudo apt-get install -y curl'", id="install-line"),
        pytest.param("which curl\ncurl https://x.test/a", "Command 1 is not a curl command: 'which curl'", id="which-line"),
        # A curl command among several that is refused is named by its number.
        pytest.param("curl https://x.test/a\ncurl -sS", "Command 2 ('curl -sS'): The curl command has no URL", id="numbered-error"),
    ],
)
def test_several_commands_name_the_one_refused(text, message):
    with pytest.raises(ImportSourceError) as excinfo:
        parse_curl(text)
    assert str(excinfo.value) == message


def test_data_file_is_read_as_curl_sends_it(tmp_path):
    """curl strips the carriage returns, line feeds and NUL bytes of a file
    ``-d @file`` (``--data-ascii @file``) sends, which a body read from the
    file as the stage runs would keep: the file is read now, from where the
    command runs, and its content is the body, joined with other data as
    curl joins it. ``--data-binary`` and ``--json`` send a file as it is:
    that stays the file's."""
    (tmp_path / "form.txt").write_bytes(b"a=1&\r\nb=2\x00\n")
    (tmp_path / "raw.bin").write_bytes(b"x\ny")
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9")

    def body(command: str) -> tuple[Any, list[str]]:
        [request], warnings = parse_curl(command, data_dir=tmp_path)
        return request.body, warnings

    assert body("curl -d @form.txt https://x.test/a") == (TextData("a=1&b=2"), [])
    assert body("curl --data-ascii @form.txt -d c=3 https://x.test/a") == (TextData("a=1&b=2&c=3"), [])
    assert body("curl --data-binary @raw.bin https://x.test/a") == (FileData("raw.bin"), [])
    assert body("curl --json @raw.bin https://x.test/a") == (FileData("raw.bin"), [])
    assert body("curl -d @missing.txt https://x.test/a") == (
        FileData("missing.txt"),
        ["curl sends missing.txt with its line breaks removed, but No such file or directory: the scenario sends the file as it is"],
    )
    assert body("curl -d @latin.txt https://x.test/a") == (
        FileData("latin.txt"),
        ["curl sends latin.txt with its line breaks removed, but it is not UTF-8 text: the scenario sends the file as it is"],
    )
    [request], _ = parse_curl("curl -G -d @form.txt https://x.test/a", data_dir=tmp_path)
    assert request.url == "https://x.test/a?a=1&b=2"


@pytest.mark.parametrize(
    ("command", "body", "warnings"),
    [
        # What a plain cat or echo writes into the pipe is what @- reads: a
        # file, read as -d @file reads it, or text.
        pytest.param("cat order.json | curl -X POST https://x.test/o -d @-", TextData('{"a": 1}'), [], id="cat-file"),
        pytest.param("cat order.json | curl --data-binary @- https://x.test/o", FileData("order.json"), [], id="cat-file-binary"),
        pytest.param("echo '{\"a\": 1}' | curl --json @- https://x.test/o", TextData('{"a": 1}\n'), [], id="echo"),
        pytest.param("echo -n a b | curl -d @- https://x.test/o", TextData("a b"), [], id="echo-n"),
        # cat alone writes its own standard input; a prompt may come first.
        pytest.param("cat <<'EOF' | curl -d @- https://x.test/o\na=1\nEOF", TextData("a=1"), [], id="cat-here-document"),
        pytest.param("$ cat order.json | curl -d @- https://x.test/o", TextData('{"a": 1}'), [], id="prompt-before-cat"),
        # Any other command's output is not known: named, and @- left out.
        pytest.param(
            "jq -c . order.json | curl -d @- https://x.test/o",
            None,
            ["Ignored the command piped into curl: jq -c . order.json", "Left out the data read from standard input (@-): add it to the scenario"],
            id="other-command",
        ),
        pytest.param(
            "cat a b | curl -d @- https://x.test/o",
            None,
            ["Ignored the command piped into curl: cat a b", "Left out the data read from standard input (@-): add it to the scenario"],
            id="cat-two-files",
        ),
        # Nor is it read when curl reads no standard input, or when its own
        # redirection gives it one.
        pytest.param(
            "cat order.json | curl https://x.test/o",
            None,
            ["Ignored the command piped into curl (cat order.json): the curl command reads no data from standard input (@-)"],
            id="cat-unread",
        ),
        pytest.param(
            "cat other.json | curl -d @- https://x.test/o < order.json", TextData('{"a": 1}'), ["Ignored the command piped into curl: cat other.json"], id="redirection-wins"
        ),
        # A here-document, a here-string: -d without their line breaks, as
        # curl sends it; --data-urlencode encodes the text whole.
        pytest.param("curl -d @- https://x.test/o <<'EOF'\n{\n  \"a\": 1\n}\nEOF", TextData('{  "a": 1}'), [], id="here-document"),
        pytest.param("curl --data-urlencode q@- https://x.test/o <<< 'a b'", TextData("q=a+b%0A"), [], id="here-string-urlencoded"),
        pytest.param(
            "curl https://x.test/o <<EOF\nx\nEOF",
            None,
            ["Ignored the here-document: the curl command reads no data from standard input (@-)"],
            id="here-document-unread",
        ),
        pytest.param(
            "curl -F f=@- https://x.test/o <<< x",
            None,
            ["Left out the form part 'f', which reads standard input: add it to the scenario"],
            id="form-part-from-stdin",
        ),
        # What curl's output goes to is not the request's.
        pytest.param(
            "curl https://x.test/o 2>/dev/null | jq . > out.json",
            None,
            ["Ignored the redirections of the curl command: 2>/dev/null", "Ignored what curl's output is piped to: jq . > out.json"],
            id="output",
        ),
    ],
)
def test_standard_input_and_pipelines(command, body, warnings, tmp_path):
    """The curl command of a pipeline is the one named curl; what ``@-``
    reads is its standard input, where the text says what that is."""
    (tmp_path / "order.json").write_text('{"a": 1}\n')
    [request], found = parse_curl(command, data_dir=tmp_path)
    assert (request.body, found) == (body, warnings)


def test_root_prompt():
    """A root shell's prompt, ``#``, reads as a comment: text with no curl
    command but such a comment is read as a command after that prompt. A
    comment beside a command stays one."""
    requests, warnings = parse_curl("# curl -X POST https://x.test/a \\\n  -H 'X: 1'")
    assert [(request.method, request.url, request.headers) for request in requests] == [("POST", "https://x.test/a", [("X", "1")])]
    assert warnings == ["Read the # before curl as a root shell's prompt, not as the start of a comment"]
    requests, warnings = parse_curl("# curl https://x.test/old\ncurl https://x.test/new")
    assert ([request.url for request in requests], warnings) == (["https://x.test/new"], [])


@pytest.mark.parametrize(
    ("command", "urls"),
    [
        pytest.param("curl 'https://x.test/[1-3]'", ["https://x.test/1", "https://x.test/2", "https://x.test/3"], id="range"),
        pytest.param("curl 'https://x.test/[08-10:2]'", ["https://x.test/08", "https://x.test/10"], id="range-padded-stepped"),
        pytest.param("curl 'https://x.test/[a-c]'", ["https://x.test/a", "https://x.test/b", "https://x.test/c"], id="letters"),
        # The last place varies fastest; a set's item may be empty.
        pytest.param("curl 'https://{a,b}.test/{x,}y'", ["https://a.test/xy", "https://a.test/y", "https://b.test/xy", "https://b.test/y"], id="sets"),
        # A backslash makes a brace or bracket text, in a set too.
        pytest.param("curl 'https://x.test/\\{a\\}/{b\\,c,d}'", ["https://x.test/{a}/b,c", "https://x.test/{a}/d"], id="escapes"),
    ],
)
def test_url_globbing(command, urls):
    """curl makes several requests of a URL with sets and ranges (unless
    -g): so does the import, a stage each, saying so."""
    requests, warnings = parse_curl(command)
    assert [request.url for request in requests] == urls
    assert len(warnings) == 1
    assert warnings[0].endswith(": curl reads {a,b} and [1-3] in a URL as sets and ranges, and -g sends the URL as written")


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("curl -g 'https://x.test/[1-3]/{a,b}'", id="-g"),
        pytest.param("curl --globoff 'https://x.test/[1-3]/{a,b}'", id="--globoff"),
        # An IPv6 address's brackets, and [] (a PHP-style array's).
        pytest.param("curl 'http://[::1]:8080/a?x[]=1'", id="ipv6-and-empty-brackets"),
    ],
)
def test_url_without_globbing(command):
    [request], warnings = parse_curl(command)
    assert request.url == shlex.split(command)[-1]
    assert warnings == []


@pytest.mark.parametrize(
    ("url", "why"),
    [
        pytest.param("https://x.test/{}", "{} is an empty set", id="empty-set"),
        pytest.param("https://x.test/{a,b", "a { is never closed", id="unclosed-set"),
        pytest.param("https://x.test/{a,[1-2]}", "a set cannot hold another set or a range", id="nested"),
        pytest.param("https://x.test/{a]}", "a ] closes no [", id="bracket-in-set"),
        pytest.param("https://x.test/a}", "a } closes nothing", id="unopened"),
        pytest.param("https://x.test/[1-", "[ is no range", id="unclosed-range"),
        pytest.param("https://x.test/[x]", "[x] is no range", id="no-range"),
        pytest.param("https://x.test/[3-1]", "[3-1] is no range", id="backwards"),
        pytest.param("https://x.test/[1-3:0]", "[1-3:0] is no range", id="zero-step"),
        pytest.param("https://x.test/[1-3:5]", "[1-3:5] is no range", id="step-past-the-end"),
        pytest.param("https://x.test/[a-Z]", "[a-Z] is no range", id="letters-backwards"),
        pytest.param("https://x.test/[A-z]", "[A-z] is no range", id="letters-across-cases"),
    ],
)
def test_url_glob_curl_refuses(url, why):
    with pytest.raises(ImportSourceError, match=f"^{re.escape(f'curl refuses the URL {url!r} ({why}): ')}"):
        parse_curl(f"curl '{url}'")


def test_url_globbing_makes_at_most_a_hundred_requests():
    with pytest.raises(ImportSourceError, match=re.escape("curl makes 200 requests of the URL 'https://x.test/[1-100]/{a,b}', more than 100 stages: ")):
        parse_curl("curl 'https://x.test/[1-100]/{a,b}'")


def test_words_split_by_the_shell():
    """Several arguments are the command's words already: not split again,
    so a value with spaces stays one."""
    requests, warnings = parse_curl_words(["curl", "-d", '{"a": 1}', "-H", "Content-Type: application/json", "https://x.test/a"])
    assert _fields(requests[0]) == {"method": "POST", "url": "https://x.test/a", "headers": [("Content-Type", "application/json")], "body": TextData('{"a": 1}')}
    assert warnings == []


# --- the curl command a report shows, imported again ---

_CLIENT = httpx.Client()


def _built(**kwargs: Any) -> httpx.Request:
    """A request as the plugin's client builds one, with httpx's own headers."""
    return _CLIENT.build_request(**kwargs)


def _multipart() -> httpx.Request:
    """A multipart request as the plugin sends one: bytes with a boundary."""
    body = {"multipart": {"fields": {"title": "Hi", "tags": ["a", "b"]}, "files": {"doc": {"content": "hello\n", "filename": "a.txt", "content_type": "text/plain"}}}}
    return _built(**build_request_kwargs(Request.model_validate({"url": "https://api.test/upload", "method": "POST", "body": body})))


# The secrets the requests below send, as the environment variables their
# placeholders read.
_SECRETS = {"API_TOKEN": "tok-123", "API_PASSWORD": "pw:1", "COOKIE": "session=abc; theme=dark", "API_KEY": "K1", "X_API_KEY": "k-2"}

ROUND_TRIPS = [
    pytest.param(lambda: _built(method="GET", url="https://api.test/items", params=[("a", "1"), ("b", "x y"), ("a", "2")], headers={"X-Trace": "t1"}), id="query"),
    pytest.param(lambda: _built(method="POST", url="https://api.test/users", json={"name": "Ann", "tags": ["x"], "n": 1.5}), id="json"),
    pytest.param(lambda: _built(method="POST", url="https://api.test/v", content='{"a": 1}', headers={"Content-Type": "application/vnd.api+json"}), id="json-vendor-type"),
    pytest.param(lambda: _built(method="POST", url="https://api.test/login", data={"user": "ann", "roles": ["a", "b"]}), id="form"),
    pytest.param(lambda: _built(method="PUT", url="https://api.test/doc", content="hello", headers={"Content-Type": "text/plain"}), id="text"),
    # format_curl removes the Content-Type curl would add to a body without one.
    pytest.param(lambda: _built(method="POST", url="https://api.test/raw", content=b"raw data"), id="text-untyped"),
    pytest.param(lambda: _built(method="POST", url="https://api.test/bin", content=bytes(range(256))), id="binary-from-file"),
    pytest.param(_multipart, id="multipart"),
    pytest.param(lambda: _built(method="GET", url="https://api.test/me", headers={"Authorization": "Bearer tok-123"}), id="bearer"),
    pytest.param(lambda: _built(method="GET", url="https://api.test/me", headers={"Authorization": "Basic " + base64.b64encode(b"ann:pw:1").decode()}), id="basic"),
    pytest.param(lambda: _built(method="GET", url="https://api.test/me", headers={"Cookie": "session=abc; theme=dark"}), id="cookies"),
    pytest.param(lambda: _built(method="GET", url="https://api.test/items?api_key=K1&x=1", headers={"X-API-Key": "k-2"}), id="redacted-names"),
    # Brackets, which the command's --globoff keeps curl from globbing.
    pytest.param(lambda: _built(method="GET", url="https://api.test/a[1]/items?filter[id]=1"), id="brackets"),
    pytest.param(lambda: _built(method="HEAD", url="https://api.test/ok"), id="head"),
    pytest.param(lambda: _built(method="POST", url="https://api.test/ping"), id="bodyless-post"),
    pytest.param(lambda: _built(method="GET", url="http://127.0.0.1:8080/x", headers={"Host": "vhost.test"}), id="virtual-host"),
    # Template syntax in what was sent is sent as it was.
    pytest.param(lambda: _built(method="POST", url="https://api.test/tpl", headers={"X-Tpl": "{{ name }}"}, json={"t": "\\{{ x }}"}), id="template-syntax"),
]


@pytest.mark.parametrize("make", ROUND_TRIPS)
def test_format_curl_round_trips(make, tmp_path, monkeypatch):
    """The curl command a failing stage's report shows (F06's
    ``format_curl``) imports as a scenario that sends the same request again:
    the same method, URL and query, headers (httpx writes its transport
    headers for both) and body. Its secrets are placeholders, which the
    environment fills in; a binary body is the file the command reads it
    from, saved where the report's note says."""
    original = make()
    command = format_curl(original, NO_REDACTION)
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "body.bin").write_bytes(original.read())

    requests, warnings = parse_curl(command)
    result = build_scenario(requests, description="round trip")
    [sent] = sent_requests(result.scenario, tmp_path)

    assert warnings == []
    assert comparable(sent) == comparable(original)
    for value in _SECRETS.values():
        assert value not in json.dumps(result.scenario)


def test_redacted_curl_command_imports_placeholders():
    """With the report's redaction, what it hides is ``[REDACTED]``, which
    becomes a placeholder like any secret: never written as a value."""
    original = _built(method="GET", url="https://api.test/items?token=t1", headers={"Authorization": "Bearer tok", "Cookie": "sid=1", "X-Auth-Token": "a"})
    requests, _ = parse_curl(format_curl(original))
    result = build_scenario(requests, description="redacted")
    assert "[REDACTED]" not in json.dumps(result.scenario)
    assert [(entry.var, entry.what) for entry in result.placeholders] == [
        ("token", "query parameter 'token'"),
        ("x_auth_token", "the x-auth-token header"),
        ("authorization", "the Authorization header"),
        ("cookie", "the cookies sid"),
    ]


def test_binary_body_is_the_file_the_command_names():
    """A report's command reads a binary body from a file, ``body.bin``; the
    scenario reads that file, relative to its own directory, which the
    import lists for the user to put there."""
    requests, _ = parse_curl(format_curl(_built(method="POST", url="https://api.test/bin", content=bytes(range(256)))))
    result = build_scenario(requests, description="binary")
    assert result.files == ["body.bin"]
    assert result.scenario["stages"][0]["request"]["body"] == {"binary": "body.bin"}
