"""curl commands as `RecordedRequest`s, for ``import curl``.

A command is text as pasted from a shell or docs, read with POSIX shell
quoting (`shlex`), or words a shell already split. The text may hold several
commands, one per line or after a ``;``, ``&&`` or ``||``, as a browser's
"copy all as cURL" writes them. Before `shlex` splits a command into words,
`split_commands` does what shlex does not do as a shell would:

- a ``\\`` before a line break continues the line (outside single quotes);
- a ``#`` starting a word begins a comment (shlex also takes one inside a
  word, as in a URL's ``#fragment``);
- ``$'...'`` (bash's ANSI-C quoting, which browsers write for a value holding
  a quote or a line break) is decoded, and requoted for shlex;
- a pipe (``|``) separates the commands of a pipeline, and a redirection is
  taken off the command's words: one of its output (``> out.json``,
  ``2>&1``) is left out, and one of its input (``< body.json``, a
  here-document ``<<EOF``, a here-string ``<<<``) is what ``@-`` reads.

The curl command of a pipeline is the one named ``curl``; what comes before
that name (a shell prompt's ``$``, ``sudo -E``, ``NAME=value``, ``watch -n
1``) and the other commands of the pipeline are left out with a warning,
but for a plain ``cat FILE`` or ``echo TEXT`` piped into it, which is what
``@-`` reads. A command may leave out the name only when it is the text's one
command and starts with an option or a URL with its scheme; a command
without it otherwise is no curl command, and refused, so that its words are
not taken for URLs.

Then the command's options are read as curl reads them (`parse_curl_words`):
short options clustered (``-sSL``) or joined to their value (``-XPOST``),
long ones never with ``=``. The options that shape the request are mapped
(`_MAPPED`), those that only shape curl's own output or connection are ignored
(`_IGNORED`; ``-o``, which may be meant as the import's own, with a warning),
and any other is ignored with a warning naming it: a curl option the importer
does not map (its value skipped, as curl would read it), or one curl does not
have. A URL is globbed as curl globs it (`_globbed`), unless ``-g``.
"""

import itertools
import math
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from pytest_httpchain.importers.builder import FileData, ImportSourceError, Part, PartsData, RecordedBody, RecordedRequest, TextData, parse_cookie_header

# The options mapped into the request, by long name, with their short letter
# where curl has one; `True` for those taking a value.
_MAPPED: dict[str, tuple[str | None, bool]] = {
    "url": (None, True),
    "request": ("X", True),
    "header": ("H", True),
    "data": ("d", True),
    "data-ascii": (None, True),
    "data-binary": (None, True),
    "data-raw": (None, True),
    "data-urlencode": (None, True),
    "json": (None, True),
    "form": ("F", True),
    "form-string": (None, True),
    "get": ("G", False),
    "head": ("I", False),
    "user": ("u", True),
    "basic": (None, False),
    "digest": (None, False),
    "oauth2-bearer": (None, True),
    "user-agent": ("A", True),
    "referer": ("e", True),
    "cookie": ("b", True),
    "location": ("L", False),
    "location-trusted": (None, False),
    "insecure": ("k", False),
    "max-time": ("m", True),
    "url-query": (None, True),
    "next": (":", False),
    "globoff": ("g", False),
}

# Options that change nothing a scenario says: curl's output, progress and
# diagnostics, compression (httpx asks for and decodes it on its
# own), and the HTTP version (the scenario's client negotiates it, see
# client.http2). Ignored without a word.
_IGNORED: dict[str, tuple[str | None, bool]] = {
    "silent": ("s", False),
    "show-error": ("S", False),
    "verbose": ("v", False),
    "include": ("i", False),
    "show-headers": (None, False),
    "output": ("o", True),
    "output-dir": (None, True),
    "remote-name": ("O", False),
    "remote-name-all": (None, False),
    "remote-header-name": ("J", False),
    "create-dirs": (None, False),
    "write-out": ("w", True),
    "fail": ("f", False),
    "fail-with-body": (None, False),
    "fail-early": (None, False),
    "progress-bar": ("#", False),
    "no-progress-meter": (None, False),
    "no-buffer": ("N", False),
    "dump-header": ("D", True),
    "trace": (None, True),
    "trace-ascii": (None, True),
    "trace-time": (None, False),
    "trace-ids": (None, False),
    "trace-config": (None, True),
    "stderr": (None, True),
    "styled-output": (None, False),
    "compressed": (None, False),
    "http1.0": ("0", False),
    "http1.1": (None, False),
    "http2": (None, False),
    "http2-prior-knowledge": (None, False),
    "http3": (None, False),
    "http3-only": (None, False),
    "disable": ("q", False),
}

# Every other curl option taking a value (from `curl --help all`), so that an
# option the importer does not map skips its value as curl reads it, rather
# than taking it for a URL.
_OTHER_WITH_VALUE = frozenset(
    {
        "abstract-unix-socket", "alt-svc", "aws-sigv4", "cacert", "capath", "cert", "cert-type", "ciphers", "config", "connect-timeout", "connect-to",
        "continue-at", "cookie-jar", "create-file-mode", "crlfile", "curves", "delegation", "dns-interface", "dns-ipv4-addr", "dns-ipv6-addr", "dns-servers",
        "doh-url", "ech", "egd-file", "engine", "etag-compare", "etag-save", "expect100-timeout", "ftp-account", "ftp-alternative-to-user", "ftp-method",
        "ftp-port", "ftp-ssl-ccc-mode", "happy-eyeballs-timeout-ms", "haproxy-clientip", "help", "hostpubmd5", "hostpubsha256", "hsts", "interface",
        "ip-tos", "ipfs-gateway", "keepalive-time", "key", "key-type", "knownhosts", "krb", "libcurl", "limit-rate", "local-port", "login-options",
        "mail-auth", "mail-from", "mail-rcpt", "max-filesize", "max-redirs", "netrc-file", "noproxy", "parallel-max", "parallel-max-host", "pass",
        "pinnedpubkey", "preproxy", "proto", "proto-default", "proto-redir", "proxy", "proxy-cacert", "proxy-capath", "proxy-cert", "proxy-cert-type",
        "proxy-ciphers", "proxy-crlfile", "proxy-header", "proxy-key", "proxy-key-type", "proxy-pass", "proxy-pinnedpubkey", "proxy-service-name",
        "proxy-tls13-ciphers", "proxy-tlsauthtype", "proxy-tlspassword", "proxy-tlsuser", "proxy-user", "proxy1.0", "pubkey", "quote", "random-file",
        "range", "rate", "request-target", "resolve", "retry", "retry-delay", "retry-max-time", "sasl-authzid", "service-name", "sigalgs", "socks4",
        "socks4a", "socks5", "socks5-gssapi-service", "socks5-hostname", "speed-limit", "speed-time", "ssl-sessions", "telnet-option", "tftp-blksize",
        "time-cond", "tls-max", "tls13-ciphers", "tlsauthtype", "tlspassword", "tlsuser", "unix-socket", "upload-file", "upload-flags", "variable",
        "vlan-priority",
    }
)  # fmt: skip

# The short letters of the options the importer does not map, and whether each takes a value.
_OTHER_SHORT: dict[str, bool] = {
    "E": True, "K": True, "C": True, "c": True, "P": True, "x": True, "U": True, "Q": True, "r": True, "Y": True, "y": True, "t": True, "z": True,
    "T": True, "h": True, "a": False, "B": False, "j": False, "l": False, "M": False, "n": False, "p": False, "R": False, "V": False, "Z": False,
    "1": False, "2": False, "3": False, "4": False, "6": False,
}  # fmt: skip

_SHORT: dict[str, str] = {short: name for table in (_MAPPED, _IGNORED) for name, (short, _) in table.items() if short is not None}

# A shell expansion the text would have gone through: `$NAME`, `${...}`, `$(...)`.
_EXPANSION = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]*\}|\()")

_ANSI_C_ESCAPE = re.compile(r"\\(?:x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|([0-7]{1,3})|c(.)|(.))", re.DOTALL)
_ANSI_C_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _decode_ansi_c(text: str) -> str:
    """The text bash's ``$'...'`` stands for."""

    def one(match: re.Match[str]) -> str:
        hex2, hex4, hex8, octal, control, other = match.groups()
        if (code := hex2 or hex4 or hex8) is not None:
            return chr(min(int(code, 16), 0x10FFFF))
        if octal is not None:
            return chr(int(octal, 8) & 0xFF)
        if control is not None:
            return chr(ord(control) & 0x1F)
        return _ANSI_C_SIMPLE.get(other, "\\" + other)

    return _ANSI_C_ESCAPE.sub(one, text)


def _single_quoted(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


# A redirection's operator, after its file descriptor's digits.
_REDIRECTION = re.compile(r"<<<|<<-|<<|<&|<>|<|>>|>&|>\||>|&>>|&>")


@dataclass(frozen=True)
class Stdin:
    """What a command reads on its standard input where the text says: a
    file (``< body.json``, or piped from ``cat body.json``) or text (a
    here-document, a here-string, or piped from ``echo``), which ``@-``
    reads. ``what`` names it in messages."""

    what: str
    text: str | None = None
    path: str | None = None


@dataclass
class ShellCommand:
    """One command of the text, as a shell would run it: ``text``, its words
    for `shlex` to split; ``written``, the command as written, for messages;
    ``redirections``, those of its output (left out), as written; and
    ``stdin``, where the text gives it one."""

    text: str = ""
    written: str = ""
    redirections: list[str] = field(default_factory=list)
    stdin: Stdin | None = None


class _ShellReader:
    """`split_commands`' one pass over the text (see the module docstring)."""

    def __init__(self, text: str, hash_prompts: bool) -> None:
        self.text = text
        self.hash_prompts = hash_prompts
        self.pipelines: list[list[ShellCommand]] = []
        self.pipeline: list[ShellCommand] = []
        self.expansions: list[str] = []
        # Here-documents whose body follows the line: the command reading it
        # (None for another descriptor's), the delimiter, whether leading
        # tabs are stripped (<<-), whether it is expanded (an unquoted delimiter).
        self.heredocs: list[tuple[ShellCommand | None, str, bool, bool]] = []
        # After a `|`, a line break continues the pipeline.
        self.piped = False
        self._command(0)

    def _command(self, start: int) -> None:
        self.command = ShellCommand()
        self.pieces: list[str] = []
        # Where the word being read goes: the command's words, or the word
        # of the redirection being read (its operator and where it starts).
        self.target = self.pieces
        self.redirection: tuple[str, int] | None = None
        self.start = start
        self.word_start = 0  # index in `pieces` where the word being read began
        self.word_text_start = start  # and in the text
        self.at_word_start = True

    def _append(self, piece: str, i: int) -> None:
        if self.at_word_start:
            self.word_text_start = i
            self.at_word_start = False
        if piece.strip():
            self.piped = False
        self.target.append(piece)

    def _finish_redirection(self, end: int) -> None:
        if self.redirection is None:
            return
        operator, begin = self.redirection
        word = "".join(self.target)
        self.redirection = None
        self.target = self.pieces
        self.at_word_start = True
        self.word_start = len(self.pieces)
        written = self.text[begin:end].strip()
        if not word.strip():
            raise ImportSourceError(f"The curl command's redirection {written!r} has nothing after it")
        try:
            value = "".join(shlex.split(word, posix=True))
        except ValueError as e:
            raise ImportSourceError(f"Cannot read the redirection {written!r}: {e}") from None
        descriptor, op = operator[: len(operator) - len(operator.lstrip("0123456789"))], operator.lstrip("0123456789")
        reads_stdin = descriptor in ("", "0")
        if reads_stdin and op == "<":
            self.command.stdin = Stdin(f"the redirection {written}", path=value)
        elif reads_stdin and op == "<<<":
            self.command.stdin = Stdin("the here-string", text=value + "\n")
        elif op in ("<<", "<<-"):
            self.heredocs.append((self.command if reads_stdin else None, value, op == "<<-", not any(quote in word for quote in "'\"\\")))
            if not reads_stdin:
                self.command.redirections.append(written)
        else:
            self.command.redirections.append(written)

    def _end_command(self, end: int) -> None:
        self._finish_redirection(end)
        self.command.text = "".join(self.pieces).strip()
        self.command.written = self.text[self.start : end].strip()
        if self.command.text:
            self.pipeline.append(self.command)
        self._command(end)

    def _end_pipeline(self, end: int) -> None:
        self._end_command(end)
        if self.pipeline:
            self.pipelines.append(self.pipeline)
        self.pipeline = []
        self.piped = False

    def _read_heredocs(self, i: int) -> int:
        """The bodies of the line's here-documents, from ``i``, the line
        after it: each up to its delimiter's line (or the text's end)."""
        text, n = self.text, len(self.text)
        for command, delimiter, strip_tabs, expanded in self.heredocs:
            lines = []
            while i < n:
                end = text.find("\n", i)
                line = text[i : n if end == -1 else end].removesuffix("\r")
                i = n if end == -1 else end + 1
                if strip_tabs:
                    line = line.lstrip("\t")
                if line == delimiter:
                    break
                lines.append(line)
            body = "".join(line + "\n" for line in lines)
            if expanded:
                # As in double quotes: `\$`, `\``, `\\` and a line
                # continuation are the shell's; `$NAME` is not expanded here.
                self.expansions += [match.group(0) for match in _EXPANSION.finditer(body) if body[match.start() - 1 : match.start()] != "\\"]
                if "`" in body:
                    self.expansions.append("`...`")
                body = re.sub(r"\\(?:\n|([$`\\]))", lambda match: match.group(1) or "", body)
            if command is not None:
                command.stdin = Stdin("the here-document", text=body)
        self.heredocs = []
        return i

    def read(self) -> list[list[ShellCommand]]:
        text, n = self.text, len(self.text)
        i = 0
        while i < n:
            c = text[i]
            if c in "'\"" or (c == "$" and text[i + 1 : i + 2] in ("'", '"')):
                # A quoted string, copied whole (a $'...' decoded and requoted).
                start = i
                if c == "$":
                    i += 1
                    if text[i] == '"':
                        # bash's locale-translated string is the string itself.
                        continue
                    end = i + 1
                    while end < n and text[end] != "'":
                        end += 2 if text[end] == "\\" else 1
                    if end >= n:
                        raise ImportSourceError("The curl command has a $'...' string that is never closed")
                    self._append(_single_quoted(_decode_ansi_c(text[i + 1 : end])), start)
                    i = end + 1
                    continue
                end = i + 1
                if c == "'":
                    end = text.find("'", end)
                    if end == -1:
                        raise ImportSourceError("The curl command has a single quote (') that is never closed")
                    self._append(text[i : end + 1], start)
                    i = end + 1
                    continue
                piece = ['"']
                while end < n and text[end] != '"':
                    if text[end] == "\\" and end + 1 < n:
                        following = text[end + 1]
                        if following == "\n":
                            end += 2
                            continue
                        if text.startswith("\r\n", end + 1):
                            end += 3
                            continue
                        # shlex keeps these escapes' backslash, which bash drops.
                        piece.append(following if following in "$`" else text[end : end + 2])
                        end += 2
                        continue
                    if (match := _EXPANSION.match(text, end)) is not None:
                        self.expansions.append(match.group(0))
                    piece.append(text[end])
                    end += 1
                if end >= n:
                    raise ImportSourceError('The curl command has a double quote (") that is never closed')
                piece.append('"')
                self._append("".join(piece), start)
                i = end + 1
                continue
            if c == "\\":
                if text.startswith("\n", i + 1):
                    i += 2
                    continue
                if text.startswith("\r\n", i + 1):
                    i += 3
                    continue
                self._append(text[i : i + 2], i)
                i += 2
                continue
            if c == "#" and self.at_word_start:
                end = text.find("\n", i)
                end = n if end == -1 else end
                rest = text[i + 1 : end].split()
                if self.hash_prompts and self.target is self.pieces and not "".join(self.pieces).strip() and rest and _is_curl(rest[0]):
                    # A root shell's prompt, not a comment (see `parse_curl`).
                    i += 1
                    continue
                i = end
                continue
            if c == "\r" and text.startswith("\n", i + 1):
                i += 1
                continue
            if c in "\r\n":
                self._finish_redirection(i)
                empty = not "".join(self.pieces).strip()
                if not (self.piped and empty):
                    self._end_pipeline(i)
                i = self.start = self.word_text_start = self._read_heredocs(i + 1)
                continue
            if c in "<>" or text.startswith("&>", i):
                # A redirection, its descriptor the word before it (`2>`).
                self._finish_redirection(i)
                descriptor, begin = "", i
                if not self.at_word_start and re.fullmatch("[0-9]+", word := "".join(self.pieces[self.word_start :])):
                    descriptor, begin = word, self.word_text_start
                    del self.pieces[self.word_start :]
                operator = _REDIRECTION.match(text, i)
                assert operator is not None
                # A word after it is a word of its own.
                self.pieces.append(" ")
                self.redirection = (descriptor + operator.group(), begin)
                self.target = []
                self.at_word_start = True
                i = operator.end()
                continue
            if c == "|" and not text.startswith("||", i):
                self._end_command(i)
                i += 2 if text.startswith("|&", i) else 1
                self.start = self.word_text_start = i
                self.piped = True
                continue
            if c in ";&|":
                self._end_pipeline(i)
                i += 2 if text.startswith(("&&", "||"), i) else 1
                self.start = self.word_text_start = i
                continue
            if c in " \t":
                if self.redirection is not None:
                    if "".join(self.target).strip():
                        self._finish_redirection(i)
                else:
                    self.pieces.append(c)
                    self.at_word_start = True
                    self.word_start = len(self.pieces)
                i += 1
                continue
            if c == "$" and (match := _EXPANSION.match(text, i)) is not None:
                self.expansions.append(match.group(0))
            if c == "`":
                self.expansions.append("`...`")
            self._append(c, i)
            i += 1
        self._end_pipeline(n)
        self._read_heredocs(n)
        return self.pipelines


def split_commands(text: str, *, hash_prompts: bool = False) -> tuple[list[list[ShellCommand]], list[str]]:
    """The commands ``text`` holds, as pipelines of `ShellCommand`s (see the
    module docstring), and a warning naming the shell expansions left as
    written. With ``hash_prompts``, a ``#`` before a curl command at a
    command's start is a root shell's prompt, not a comment.

    Raises `ImportSourceError` for a quote that is never closed and a
    redirection without its word.
    """
    reader = _ShellReader(text, hash_prompts)
    pipelines = reader.read()
    warnings = []
    if reader.expansions:
        names = ", ".join(dict.fromkeys(reader.expansions))
        warnings.append(f"The curl command uses shell expansions, which are not expanded: {names} is imported as written")
    return pipelines, warnings


@dataclass
class _Options:
    """What one curl command's options say, in the order given."""

    urls: list[str] = field(default_factory=list)
    method: str | None = None
    head: bool = False
    get: bool = False
    headers: list[tuple[str, str]] = field(default_factory=list)
    # Headers curl writes itself that `-H 'Name:'` removed, lowercase.
    removed: set[str] = field(default_factory=set)
    data: list[tuple[str, str]] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)
    query: list[str] = field(default_factory=list)
    user: str | None = None
    digest: bool = False
    bearer: str | None = None
    agent: str | None = None
    referer: str | None = None
    cookies: list[tuple[str, str]] = field(default_factory=list)
    location: bool = False
    insecure: bool = False
    timeout: float | None = None
    globoff: bool = False
    # A -F part read standard input, and was left out.
    form_stdin: bool = False


@dataclass
class _Input:
    """The command's standard input, where the text gives it, and whether
    an option read it (``@-``)."""

    stdin: Stdin | None
    used: bool = False

    def text(self) -> str | None:
        """The text standard input holds, where the text gives it as text."""
        if self.stdin is None or self.stdin.text is None:
            return None
        self.used = True
        return self.stdin.text


def _file_name(path: str) -> str:
    """``path``, a file the command reads, as written; raises
    `ImportSourceError` for one holding a NUL, which no file name can."""
    if "\x00" in path:
        raise ImportSourceError(f"The curl command reads the file {path!r}, which cannot be: no file name holds a NUL character")
    return path


def _urlencoded(value: str, what: str, warnings: list[str], source: _Input | None = None) -> str | None:
    """A ``--data-urlencode`` or ``--url-query`` value as curl sends it:
    ``content``, ``=content`` and ``name=content`` with the content encoded
    (``+`` for a space), the name as given. One reading a file
    (``@file``, ``name@file``) is left out, with a warning, but for
    standard input (``@-``) the text gives (``source``)."""
    at = value.find("@")
    equals = value.find("=")
    if at != -1 and (equals == -1 or at < equals):
        name = value[:at]
        if value[at + 1 :] == "-" and source is not None and (content := source.text()) is not None:
            return f"{name}={quote_plus(content)}" if name else quote_plus(content)
        warnings.append(f"Left out {what} {value!r}, which reads a file: add its content to the scenario")
        return None
    if equals == -1:
        return quote_plus(value)
    name, content = value[:equals], value[equals + 1 :]
    return f"{name}={quote_plus(content)}" if name else quote_plus(content)


def _form_value(text: str) -> tuple[str, str]:
    """A ``-F`` value, which may be double-quoted to hold a ``;``, and what
    follows it (its ``;type=...`` parameters)."""
    if text.startswith('"'):
        value, i = [], 1
        while i < len(text) and text[i] != '"':
            if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in '"\\':
                i += 1
            value.append(text[i])
            i += 1
        return "".join(value), text[i + 1 :]
    value, _, rest = text.partition(";")
    return value, f";{rest}" if rest else ""


def _form_part(argument: str, literal: bool, options: _Options, warnings: list[str]) -> Part | None:
    """A ``-F name=content`` (``--form-string`` when ``literal``) as a part:
    ``@path`` a file, ``<path`` a field holding a file's content (a file
    part without a filename, here), anything else a text field; each may
    add ``;type=`` and ``;filename=``."""
    name, equals, content = argument.partition("=")
    if not equals:
        warnings.append(f"Left out the form part {argument!r}, which is no name=content")
        return None
    if literal:
        return Part(name, value=content)
    kind = content[:1] if content[:1] in ("@", "<") else ""
    value, rest = _form_value(content[len(kind) :])
    params: dict[str, str] = {}
    while rest.startswith(";"):
        key, _, rest = rest[1:].partition("=")
        param, rest = _form_value(rest)
        if key.strip().lower() in ("type", "filename"):
            params[key.strip().lower()] = param
        else:
            warnings.append(f"Ignored the ;{key.strip()}= of form part {name!r}")
    content_type, filename = params.get("type"), params.get("filename")
    if kind and value == "-":
        options.form_stdin = True
        warnings.append(f"Left out the form part {name!r}, which reads standard input: add it to the scenario")
        return None
    if kind:
        _file_name(value)
    if kind == "@":
        return Part(name, path=value, filename=filename, content_type=content_type)
    if kind == "<":
        return Part(name, path=value, filename=filename if filename is not None else "", content_type=content_type)
    return Part(name, value=value, filename=filename, content_type=content_type)


def _read_options(words: list[str], warnings: list[str]) -> _Options:
    """One command's options (see the module docstring)."""
    options = _Options()
    unmapped: list[str] = []
    unknown: list[str] = []
    i = 0

    def value_for(option: str, attached: str) -> str:
        nonlocal i
        if attached:
            return attached
        if i >= len(words):
            raise ImportSourceError(f"The curl option {option} needs a value")
        i += 1
        return words[i - 1]

    while i < len(words):
        word = words[i]
        i += 1
        if word.startswith("--") and len(word) > 2:
            name = word[2:]
            if name in _MAPPED:
                _apply(options, name, value_for(word, "") if _MAPPED[name][1] else None, warnings)
            elif name in _IGNORED:
                if _IGNORED[name][1]:
                    _ignore(word, value_for(word, ""), warnings)
            elif name in _OTHER_WITH_VALUE:
                value_for(word, "")
                unmapped.append(word)
            elif name.startswith("no-") or name in _OTHER_BOOLEAN:
                unmapped.append(word)
            else:
                unknown.append(word)
        elif word.startswith("-") and len(word) > 1:
            for j, letter in enumerate(word[1:], start=2):
                option = f"-{letter}"
                if (name := _SHORT.get(letter)) is not None:
                    table = _MAPPED if name in _MAPPED else _IGNORED
                    takes_value = table[name][1]
                    value = value_for(option, word[j:]) if takes_value else None
                    if name in _MAPPED:
                        _apply(options, name, value, warnings)
                    elif value is not None:
                        _ignore(option, value, warnings)
                elif letter in _OTHER_SHORT:
                    takes_value = _OTHER_SHORT[letter]
                    if takes_value:
                        value_for(option, word[j:])
                    unmapped.append(option)
                else:
                    unknown.append(word if j == 2 else option)
                    break
                if takes_value:
                    break
        else:
            options.urls.append(word)
    if unmapped:
        warnings.append(f"Ignored the curl options the import does not map: {', '.join(dict.fromkeys(unmapped))}")
    if unknown:
        warnings.append(f"Ignored what is not a curl option: {', '.join(dict.fromkeys(unknown))}")
    return options


def _ignore(option: str, value: str, warnings: list[str]) -> None:
    """An ignored option's value, read; ``-o`` is named in a warning, since
    it reads as the import's own ``-o`` given after the command, which then
    is the command's."""
    if option in ("-o", "--output"):
        warnings.append(f"Ignored {option} {value!r}, the file curl writes its answer to: to write the scenario to a file, give import's -o before the command")


# curl's options without a value that the importer does not map (the rest of
# `curl --help all`'s), told apart from a word curl does not know.
_OTHER_BOOLEAN = frozenset(
    {
        "anyauth", "append", "ca-native", "cert-status", "compressed-ssh", "crlf", "disable-eprt", "disable-epsv", "disallow-username-in-url",
        "doh-cert-status", "doh-insecure", "false-start", "form-escape", "ftp-create-dirs", "ftp-pasv", "ftp-pret", "ftp-skip-pasv-ip", "ftp-ssl-ccc",
        "ftp-ssl-control", "haproxy-protocol", "http0.9", "ignore-content-length", "ipv4", "ipv6", "junk-session-cookies", "list-only",
        "mail-rcpt-allowfails", "manual", "metalink", "mptcp", "negotiate", "netrc", "netrc-optional", "no-alpn", "no-clobber", "no-keepalive", "no-npn",
        "no-sessionid", "ntlm", "ntlm-wb", "parallel", "parallel-immediate", "path-as-is", "post301", "post302", "post303", "proxy-anyauth", "proxy-basic",
        "proxy-ca-native", "proxy-digest", "proxy-http2", "proxy-insecure", "proxy-negotiate", "proxy-ntlm", "proxy-ssl-allow-beast",
        "proxy-ssl-auto-client-cert", "proxy-tlsv1", "proxytunnel", "raw", "remote-time", "remove-on-error", "retry-all-errors", "retry-connrefused",
        "sasl-ir", "skip-existing", "socks5-basic", "socks5-gssapi", "socks5-gssapi-nec", "ssl", "ssl-allow-beast", "ssl-auto-client-cert",
        "ssl-no-revoke", "ssl-reqd", "ssl-revoke-best-effort", "sslv2", "sslv3", "suppress-connect-headers", "tcp-fastopen", "tcp-nodelay",
        "tftp-no-options", "tlsv1", "tlsv1.0", "tlsv1.1", "tlsv1.2", "tlsv1.3", "tr-encoding", "use-ascii", "version", "xattr",
    }
)  # fmt: skip


def _apply(options: _Options, name: str, value: str | None, warnings: list[str]) -> None:
    """One mapped option, ``value`` its value where it takes one."""
    assert value is not None or not _MAPPED[name][1]
    match name:
        case "url":
            options.urls.append(value or "")
        case "request":
            options.method = (value or "").upper()
        case "header":
            _header(options, value or "", warnings)
        case "data" | "data-ascii" | "data-binary" | "data-raw" | "data-urlencode" | "json":
            options.data.append((name, value or ""))
        case "form" | "form-string":
            if (part := _form_part(value or "", name == "form-string", options, warnings)) is not None:
                options.parts.append(part)
        case "get":
            options.get = True
        case "head":
            options.head = True
        case "user":
            options.user = value
        case "basic" | "digest":
            options.digest = name == "digest"
        case "oauth2-bearer":
            options.bearer = value
        case "user-agent":
            # An empty one removes curl's User-Agent: none is written.
            options.agent = value or None
        case "referer":
            options.referer = (value or "").removesuffix(";auto")
        case "cookie":
            if "=" in (value or ""):
                options.cookies += parse_cookie_header(value or "")
            else:
                warnings.append(f"Left out -b {value!r}, a cookie file: add its cookies to the scenario")
        case "location" | "location-trusted":
            options.location = True
        case "insecure":
            options.insecure = True
        case "globoff":
            options.globoff = True
        case "max-time":
            try:
                timeout = float(value or "")
            except ValueError:
                timeout = math.nan
            if timeout > 0 and math.isfinite(timeout):
                options.timeout = timeout
            elif timeout == 0:
                warnings.append("Ignored --max-time 0, curl's no limit, which a scenario has no setting for: the client's timeout applies (30 seconds by default)")
            else:
                warnings.append(f"Ignored --max-time {value!r}, which is no number of seconds")
        case "url-query":
            if value is not None and value.startswith("+"):
                options.query.append(value[1:])
            elif (encoded := _urlencoded(value or "", "--url-query", warnings)) is not None:
                options.query.append(encoded)


def _header(options: _Options, value: str, warnings: list[str]) -> None:
    """``-H 'Name: value'``; ``Name;`` sends it empty and ``Name:`` removes a
    header curl would write itself."""
    if value.startswith("@"):
        warnings.append(f"Left out -H {value!r}, which reads headers from a file: add them to the scenario")
        return
    name, colon, header = value.partition(":")
    if colon:
        if header.strip():
            options.headers.append((name.strip(), header.strip()))
        else:
            options.removed.add(name.strip().lower())
    elif value.rstrip().endswith(";"):
        options.headers.append((value.rstrip()[:-1].strip(), ""))
    else:
        warnings.append(f"Left out -H {value!r}, which is no header")


def _data_file(path: str, data_dir: Path | None) -> tuple[str | None, str]:
    """The text ``-d @path`` sends, read from ``data_dir``: the file's, less
    its carriage returns, line feeds and NUL bytes, which curl strips from
    it. ``(None, why)`` where it cannot be: no directory to read it from,
    no such file, or bytes that are not UTF-8 text."""
    _file_name(path)
    if data_dir is None:
        return None, "it is not read here"
    try:
        content = (data_dir / path).read_bytes()
    except OSError as e:
        return None, e.strerror or str(e)
    try:
        return content.translate(None, b"\r\n\x00").decode(), ""
    except UnicodeDecodeError:
        return None, "it is not UTF-8 text"


def _body(options: _Options, warnings: list[str], data_dir: Path | None, source: _Input) -> tuple[RecordedBody | None, str | None]:
    """The body the data options send, and the query text instead with ``-G``.

    Data options are joined with ``&``, ``--json`` ones as they are. The
    content ``-d @file`` (``--data-ascii @file``) sends is read now, from
    ``data_dir``, since curl strips its line breaks, which a body read from
    the file when the stage runs would keep. A body curl sends as the file
    holds it (``--data-binary @file``, ``--json @file``), and one of the
    others that cannot be read, is the file's (``binary``), when it is the
    only one; beside others, or with ``-G``, it is left out with a warning.
    ``@-`` reads standard input, where the text gives it (``source``): a
    file as ``@file`` does, text as the data itself (without its line
    breaks for ``-d``, as curl sends it).
    """
    pieces: list[str] = []
    files: list[str] = []
    unstripped: dict[str, str] = {}
    for name, value in options.data:
        if value == "@-" and name in ("data", "data-ascii", "data-binary", "json") and source.stdin is not None:
            if (text := source.text()) is not None:
                pieces.append(text.translate(_STRIPPED) if name in ("data", "data-ascii") else text)
                continue
            source.used = True
            value = f"@{source.stdin.path}"
        if name in ("data", "data-ascii") and value.startswith("@") and value != "@-":
            text, why = _data_file(value[1:], data_dir)
            if text is not None:
                pieces.append(text)
            else:
                files.append(value[1:])
                unstripped[value[1:]] = why
        elif name in ("data", "data-ascii", "data-binary", "json") and value.startswith("@"):
            files.append(value[1:] if value == "@-" else _file_name(value[1:]))
        elif name == "data-urlencode":
            if (encoded := _urlencoded(value, "--data-urlencode", warnings, source)) is not None:
                pieces.append(encoded)
        else:
            pieces.append(value)
    if files and (pieces or len(files) > 1 or options.get):
        warnings.append(f"Left out the data read from {', '.join(files)}: add it to the scenario")
        files = []
    if files:
        if files[0] == "-":
            warnings.append("Left out the data read from standard input (@-): add it to the scenario")
            return None, None
        if (why := unstripped.get(files[0])) is not None:
            warnings.append(f"curl sends {files[0]} with its line breaks removed, but {why}: the scenario sends the file as it is")
        return FileData(files[0]), None
    if not options.data:
        return None, None
    text = ("" if all(name == "json" for name, _ in options.data) else "&").join(pieces)
    if options.get:
        return None, text
    return TextData(text), None


# What curl strips from the data -d reads from a file or standard input.
_STRIPPED = str.maketrans("", "", "\r\n\x00")

_MAX_GLOB_REQUESTS = 100
_GLOBBING = "curl reads {a,b} and [1-3] in a URL as sets and ranges, and -g sends the URL as written"
# The brackets curl's globbing leaves as they are: an IPv6 address's, and `[]`.
_UNGLOBBED_BRACKETS = re.compile(r"\[\]|\[[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*(?:%[0-9A-Za-z._~%-]+)?\]")
_NUMBER_RANGE = re.compile(r"([0-9]+)-[ \t]*([0-9]+)(?::([0-9]+))?")
_LETTER_RANGE = re.compile(r"([A-Za-z])-(.)(?::([0-9]+))?", re.DOTALL)

# One place of a globbed URL: its values' indices, and each index's text.
type _GlobPart = tuple[range, Callable[[int], str]]


def _glob_range(spec: str) -> _GlobPart | None:
    """A ``[...]`` range as curl reads it (``1-10``, ``01-10`` zero-padded,
    ``a-z``, each with a ``:step``), or None for one curl refuses."""
    if (match := _NUMBER_RANGE.fullmatch(spec)) is not None:
        first, last = int(match[1]), int(match[2])
        width = len(match[1]) if match[1].startswith("0") else 0

        def text(value: int) -> str:
            return str(value).zfill(width)

    elif (match := _LETTER_RANGE.fullmatch(spec)) is not None:
        first, last = ord(match[1]), ord(match[2])
        text = chr
        if last - first > ord("z") - ord("a"):
            return None
    else:
        return None
    step = int(match[3] or 1)
    if not step or (first == last and step != 1) or (first != last and (first > last or step > last - first)):
        return None
    return range(first, last + 1, step), text


def _globbed(url: str, warnings: list[str]) -> list[str]:
    """The URLs curl makes of ``url`` without ``-g``: a ``{a,b}`` set and a
    ``[1-3]`` range (`_glob_range`) each stand for their values in turn,
    the last place varying fastest, and a ``\\`` before a brace or bracket
    for it. An IPv6 address's brackets and ``[]`` are text.

    Raises `ImportSourceError` for a URL curl refuses (an unclosed or empty
    set, a range it cannot read), and one making more than
    `_MAX_GLOB_REQUESTS` requests.
    """

    def refused(why: str) -> ImportSourceError:
        return ImportSourceError(f"curl refuses the URL {url!r} ({why}): {_GLOBBING}")

    parts: list[_GlobPart] = []
    literal: list[str] = []

    def flush() -> None:
        if literal:
            text = "".join(literal)
            parts.append((range(1), lambda _: text))
            literal.clear()

    i, n = 0, len(url)
    while i < n:
        c = url[i]
        if c == "\\" and url[i + 1 : i + 2] in ("{", "[", "}", "]") and i + 1 < n:
            literal.append(url[i + 1])
            i += 2
        elif c == "[" and (match := _UNGLOBBED_BRACKETS.match(url, i)) is not None:
            literal.append(match.group())
            i = match.end()
        elif c == "{":
            items, item = [], []
            i += 1
            while True:
                if i >= n:
                    raise refused("a { is never closed")
                c = url[i]
                if c in "{[":
                    raise refused("a set cannot hold another set or a range")
                if c == "]":
                    raise refused("a ] closes no [")
                if c == "}" and not items and not item and url[i - 1] == "{":
                    raise refused("{} is an empty set")
                if c in ",}":
                    items.append("".join(item))
                    item = []
                    i += 1
                    if c == "}":
                        break
                    continue
                if c == "\\" and i + 1 < n:
                    i += 1
                item.append(url[i])
                i += 1
            flush()
            parts.append((range(len(items)), items.__getitem__))
        elif c == "[":
            end = url.find("]", i)
            if end == -1 or (part := _glob_range(url[i + 1 : end])) is None:
                raise refused(f"{url[i : end + 1] if end != -1 else '['} is no range")
            flush()
            parts.append(part)
            i = end + 1
        elif c in "]}":
            raise refused(f"a {c} closes nothing")
        else:
            literal.append(c)
            i += 1
    flush()
    count = math.prod(len(values) for values, _ in parts)
    if count > _MAX_GLOB_REQUESTS:
        raise ImportSourceError(f"curl makes {count} requests of the URL {url!r}, more than {_MAX_GLOB_REQUESTS} stages: {_GLOBBING}")
    urls = ["".join(text(index) for (_, text), index in zip(parts, indices, strict=True)) for indices in itertools.product(*(values for values, _ in parts))]
    if urls != [url]:
        made = ", ".join(repr(each) for each in urls[:3]) + (", ..." if count > 3 else "")
        warnings.append(f"Imported {url!r} as the {'URL' if count == 1 else f'{count} URLs'} curl makes of it, {made}: {_GLOBBING}")
    return urls


def _is_curl(word: str) -> bool:
    """Whether a word names curl (a path to it included, not a URL ending in /curl)."""
    return "://" not in word and (word in ("curl", "curl.exe") or word.endswith(("/curl", "\\curl", "\\curl.exe", "/curl.exe")))


_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def _curl_words(words: list[str], *, nameless: bool) -> tuple[list[str], list[str]] | None:
    """A command's words after its name ``curl``, and those before it (a
    shell prompt's ``$``, ``sudo -E``, ``NAME=value``, ``watch -n 1``), or
    None for a command that is no curl command. With ``nameless``, a
    command starting with an option or a URL with its scheme is curl's
    without its name (``-X POST https://...``), a ``curl`` among its words
    then a value (``-A curl``). A ``curl`` after other words with none after
    it is a word of another command (``which curl``, ``apt-get install
    curl``), not curl's name: a command of curl's sends a request, which
    takes a URL."""
    if not words:
        return None
    if _is_curl(words[0]):
        return words[1:], []
    if (words[0].startswith("-") and len(words[0]) > 1) or _SCHEME.match(words[0]):
        return (words, []) if nameless else None
    for index, word in enumerate(words[:-1]):
        if _is_curl(word):
            return words[index + 1 :], words[:index]
    return None


def _piped_stdin(command: ShellCommand, words: list[str], written: str) -> Stdin | None:
    """What a command piped into curl writes, where it is plain (after a
    prompt's ``$``): ``cat FILE`` the file, ``cat`` alone its own standard
    input (``cat <<EOF``), ``echo [-n] TEXT`` the text (none of it a
    backslash, which some shells' echo reads as an escape); None for
    anything else."""
    what = f"the command piped into curl ({written})"
    if words[:1] in (["$"], ["%"]):
        words = words[1:]
    if words == ["cat"] and command.stdin is not None:
        return Stdin(what, text=command.stdin.text, path=command.stdin.path)
    if len(words) == 2 and words[0] == "cat" and not words[1].startswith("-"):
        return Stdin(what, path=words[1])
    if words[:1] == ["echo"]:
        text, end = words[1:], "\n"
        if text[:1] == ["-n"]:
            text, end = text[1:], ""
        if not any("\\" in word for word in text) and not (text and text[0].startswith("-")):
            return Stdin(what, text=" ".join(text) + end)
    return None


def parse_curl_words(words: list[str], *, data_dir: Path | None = None) -> tuple[list[RecordedRequest], list[str]]:
    """One curl command, as the words a shell split it into, as the requests
    it sends (one per URL, and per ``--next``), and warnings about what was
    not imported. The word ``curl`` is the command's name, and what comes
    before it is not the command's, and left out with a warning; a command
    without it starts with an option or a URL (see `_curl_words`).
    ``data_dir`` is where ``-d @file`` reads its file (see `_body`).

    Raises `ImportSourceError` for a command that is no curl command or
    sends no request: no URL, a URL that is not http(s), an option missing
    its value.
    """
    found = _curl_words(words, nameless=True)
    if found is None:
        raise ImportSourceError(f"The command is not a curl command: {shlex.join(words)[:60]!r}")
    arguments, before = found
    warnings = [f"Ignored what comes before curl: {' '.join(before)}"] if before else []
    requests, more = _arguments_requests(arguments, data_dir, None)
    return requests, warnings + more


def _arguments_requests(arguments: list[str], data_dir: Path | None, stdin: Stdin | None) -> tuple[list[RecordedRequest], list[str]]:
    """The requests of curl's arguments, and warnings."""
    warnings: list[str] = []
    source = _Input(stdin)
    requests: list[RecordedRequest] = []
    groups: list[list[str]] = [[]]
    for word in arguments:
        if word in ("--next", "-:"):
            groups.append([])
        else:
            groups[-1].append(word)
    for group in groups:
        requests += _requests(_read_options(group, warnings), warnings, data_dir, source)
    if stdin is not None and not source.used:
        warnings.append(f"Ignored {stdin.what}: the curl command reads no data from standard input (@-)")
    return requests, warnings


def _requests(options: _Options, warnings: list[str], data_dir: Path | None, source: _Input) -> list[RecordedRequest]:
    if not options.urls:
        raise ImportSourceError("The curl command has no URL")
    body, query = _body(options, warnings, data_dir, source)
    source.used = source.used or options.form_stdin
    parts = PartsData(tuple(options.parts)) if options.parts else None
    if parts is not None and body is not None:
        warnings.append("Left out the -d data beside the -F form parts, which curl refuses to send together")
    if parts is not None:
        body = parts

    if options.method:
        method = options.method
    elif options.head:
        method = "HEAD"
    elif (options.data or options.parts) and not options.get:
        method = "POST"
    else:
        method = "GET"

    # curl's own headers, unless -H gave or removed one of that name.
    given = {name.lower() for name, _ in options.headers} | options.removed
    headers = list(options.headers)
    defaults: list[tuple[str, str]] = []
    if isinstance(body, (TextData, FileData)):
        is_json = all(name == "json" for name, _ in options.data)
        defaults.append(("Content-Type", "application/json" if is_json else "application/x-www-form-urlencoded"))
        if is_json:
            defaults.append(("Accept", "application/json"))
    if options.agent is not None:
        defaults.append(("User-Agent", options.agent))
    if options.referer is not None:
        defaults.append(("Referer", options.referer))
    headers += [(name, value) for name, value in defaults if name.lower() not in given]

    cookies = list(options.cookies)
    if cookies and "cookie" in given:
        # curl sends a Cookie header given (or removed) with -H in place of -b's.
        warnings.append("Left out the -b cookies: curl sends the Cookie header given with -H in their place")
        cookies = []

    user = None
    if options.user is not None:
        name, colon, password = options.user.partition(":")
        user = (name, password if colon else None)

    urls = [url for word in options.urls for url in ([word] if options.globoff else _globbed(word, warnings))]
    requests = []
    for written in urls:
        # curl's default scheme.
        schemeless = "://" not in written
        url = f"http://{written}" if schemeless else written
        try:
            split = urlsplit(url)
        except ValueError as e:
            raise ImportSourceError(f"The curl command's URL {written!r} is no URL: {e}") from None
        if split.scheme.lower() not in ("http", "https"):
            raise ImportSourceError(f"The curl command's URL {url!r} is not http or https")
        host = split.netloc.rpartition("@")[2]
        if schemeless and "." not in host and ":" not in host and host.lower() != "localhost":
            # A word taken for a URL that most likely is none: a value of an
            # option curl does not have, a command's word.
            warnings.append(f"Imported {written!r} as the URL {url!r}, as curl reads a word that is no option: if it is no URL, remove it")
        # curl sends no fragment, and adds -G data and --url-query to the query.
        url = url.partition("#")[0]
        extra = "&".join(piece for piece in [query, *options.query] if piece)
        if extra:
            url += ("&" if split.query else "?" if not url.endswith("?") else "") + extra
        requests.append(
            RecordedRequest(
                method=method,
                url=url,
                headers=list(headers),
                cookies=list(cookies),
                body=body,
                user=user,
                digest=options.digest,
                bearer=options.bearer,
                follow_redirects=options.location,
                verify_tls=not options.insecure,
                timeout=options.timeout,
            )
        )
    return requests


def parse_curl(text: str, *, data_dir: Path | None = None) -> tuple[list[RecordedRequest], list[str]]:
    """The requests the curl commands in ``text`` send (see the module
    docstring), and warnings about what was not imported. ``data_dir`` is
    where ``-d @file`` reads its file (see `_body`).

    A command is a pipeline's curl command (see `_pipeline_requests`). With
    one command in the text, it may leave out its name, starting with an
    option or a URL: ``-X POST https://...``. With several, each must name
    ``curl`` (after a prompt, say), so that a line of something else is not
    read as a URL. Text with no command naming curl but a comment that is
    one (``# curl ...``) is a root shell's prompt before a command, read so,
    with a warning.
    """
    pipelines, warnings = split_commands(text)
    commands = _words(pipelines)
    if not _names_curl(commands):
        prompted, prompted_warnings = split_commands(text, hash_prompts=True)
        if _names_curl(prompted_commands := _words(prompted)):
            commands = prompted_commands
            warnings = ["Read the # before curl as a root shell's prompt, not as the start of a comment", *prompted_warnings]
    if not commands:
        raise ImportSourceError("There is no curl command to import")
    requests: list[RecordedRequest] = []
    for number, pipeline in enumerate(commands, start=1):
        found, more = _pipeline_requests(pipeline, None if len(commands) == 1 else number, data_dir)
        requests += found
        warnings += more
    return requests, warnings


def _names_curl(commands: list[list[tuple[ShellCommand, list[str]]]]) -> bool:
    return any(_curl_words(words, nameless=False) is not None for pipeline in commands for _, words in pipeline)


def _words(pipelines: list[list[ShellCommand]]) -> list[list[tuple[ShellCommand, list[str]]]]:
    """Each command of the pipelines with its words, as `shlex` (POSIX) splits them."""
    try:
        return [[(command, shlex.split(command.text, posix=True)) for command in pipeline] for pipeline in pipelines]
    except ValueError as e:
        raise ImportSourceError(f"Cannot read the curl command: {e}") from None


def _pipeline_requests(pipeline: list[tuple[ShellCommand, list[str]]], number: int | None, data_dir: Path | None) -> tuple[list[RecordedRequest], list[str]]:
    """The requests of a pipeline's curl command, the first command naming
    curl (or, the text's ``number`` None, being curl's without its name, see
    `_curl_words`), and warnings. The other commands are left out with a
    warning, but for one piped into curl that `_piped_stdin` reads, whose
    output is curl's standard input, unless its own redirection gives one;
    so are the redirections of curl's output.

    Raises `ImportSourceError` for a pipeline without a curl command.
    """
    written = " | ".join(command.written for command, _ in pipeline)
    for index, (_, words) in enumerate(pipeline):
        if (found := _curl_words(words, nameless=number is None)) is not None:
            try:
                return _curl_in_pipeline(pipeline, index, *found, data_dir)
            except ImportSourceError as e:
                # Among several commands, name the one that is refused.
                if number is None:
                    raise
                raise ImportSourceError(f"Command {number} ({written[:60]!r}): {e}") from None
    raise ImportSourceError(f"{'The command' if number is None else f'Command {number}'} is not a curl command: {written[:60]!r}")


def _curl_in_pipeline(
    pipeline: list[tuple[ShellCommand, list[str]]], index: int, arguments: list[str], before: list[str], data_dir: Path | None
) -> tuple[list[RecordedRequest], list[str]]:
    """`_pipeline_requests` for its curl command, the ``index``-th, its
    ``arguments`` after its name and the words ``before`` it."""
    command = pipeline[index][0]
    warnings = [f"Ignored what comes before curl: {' '.join(before)}"] if before else []
    stdin = command.stdin
    if index:
        # curl's own redirection of its input wins over the pipe.
        piped = " | ".join(earlier.written for earlier, _ in pipeline[:index])
        from_pipe = _piped_stdin(*pipeline[0], piped) if stdin is None and index == 1 else None
        if from_pipe is None:
            warnings.append(f"Ignored the command piped into curl: {piped}")
        stdin = stdin or from_pipe
    if command.redirections:
        warnings.append(f"Ignored the redirections of the curl command: {', '.join(command.redirections)}")
    if index + 1 < len(pipeline):
        warnings.append(f"Ignored what curl's output is piped to: {' | '.join(later.written for later, _ in pipeline[index + 1 :])}")
    requests, more = _arguments_requests(arguments, data_dir, stdin)
    return requests, warnings + more
