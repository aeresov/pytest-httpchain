"""Starter scenarios from recorded traffic: ``pytest-httpchain import har``
and ``import curl``.

- `builder`: the recorded request (`RecordedRequest`) the readers produce,
  `build_scenario`, which writes them as a scenario, secrets left out, and
  `scenario_text` and `validate_text`, the file's text and its validation
- `har`: a HAR file's entries, filtered (`read_har`)
- `curl`: curl commands, read with POSIX shell quoting (`parse_curl`), and
  the shell's commands, pipelines and redirections around them
  (`split_commands`)

A CLI-side package: the plugin and the domain packages never import it.
"""

from pytest_httpchain.importers.builder import (
    TRANSPORT_HEADERS,
    Base64Data,
    FileData,
    ImportResult,
    ImportSourceError,
    Part,
    PartsData,
    Placeholder,
    RecordedRequest,
    TextData,
    build_scenario,
    scenario_text,
    validate_text,
)
from pytest_httpchain.importers.curl import ShellCommand, Stdin, parse_curl, parse_curl_words, split_commands
from pytest_httpchain.importers.har import read_har

__all__ = [
    "TRANSPORT_HEADERS",
    "Base64Data",
    "FileData",
    "ImportResult",
    "ImportSourceError",
    "Part",
    "PartsData",
    "Placeholder",
    "RecordedRequest",
    "ShellCommand",
    "Stdin",
    "TextData",
    "build_scenario",
    "parse_curl",
    "parse_curl_words",
    "read_har",
    "scenario_text",
    "split_commands",
    "validate_text",
]
