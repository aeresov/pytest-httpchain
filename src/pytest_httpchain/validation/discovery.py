"""The scenario files a directory given to ``validate`` holds, and the
``httpchain_suffix`` they are named by.

`find_scenario_files` walks a directory as pytest's collection does by default,
and `configured_suffix` reads ``httpchain_suffix`` from the configuration file
pytest would read for the same paths, so ``validate tests/`` checks the files
``pytest tests/`` runs. Both follow what pytest documents, by public means
(``tomllib``, and ``iniconfig``, the parser pytest reads ini files with), not
``_pytest``'s internals.
"""

import errno
import fnmatch
import os
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

import iniconfig

from pytest_httpchain.constants import DEFAULT_SUFFIX, ConfigOptions, check_suffix, scenario_file_pattern

# pytest's default ``norecursedirs``, matched against a directory's name, as
# pytest matches a pattern without a path separator (``fnmatch``, which ignores
# case on Windows only). A project's own ``norecursedirs`` is not read: it often
# hides scenarios pytest must not collect directly but that are still meant to
# be valid (fixtures a test runs through pytester), and the other ways to leave
# files out of a run (``--ignore``, a conftest's ``collect_ignore``) cannot be
# read without running pytest.
DEFAULT_NORECURSEDIRS = ("*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}")

# The errors pytest's collection passes over when it looks at a directory's
# entry: the entry is gone, is under something that is not a directory, is a
# link to itself or a link loop (ELOOP; on Windows, error 1921), or its drive is
# not ready (Windows error 21). Any other error stops the search.
_IGNORED_ERRNOS = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)
_IGNORED_WINERRORS = (21, 1921)

# The files pytest takes its configuration from, in the order it looks for
# them in a directory.
_CONFIG_FILE_NAMES = ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")


class DiscoveryError(Exception):
    """A directory cannot be searched, or the configured suffix cannot be read:
    ``validate`` exits with the message before it validates anything."""


def find_scenario_files(directory: Path, suffix: str) -> Iterator[Path]:
    """Every scenario file under ``directory`` (named by
    ``constants.scenario_file_pattern``), in the order pytest collects them:
    depth first, each directory's entries by name, files and subdirectories
    interleaved.

    A subdirectory pytest's collection skips by default (`_skipped`) is not
    entered, nor one that is a symlink or a Windows junction, so a link back up
    the tree cannot loop the search. ``directory`` itself is searched whatever
    its name, and followed when it is a link, as pytest collects a directory it
    is given. A symlink to a file is found as the file is: pytest collects it.
    An entry pytest passes over when looking at it fails (`_IGNORED_ERRNOS`: a
    link to itself, one gone since the listing) is passed over too, as is a
    subdirectory gone before it is listed.

    Raises `DiscoveryError` when a directory cannot be listed, or an entry
    named like a scenario cannot be looked at for another reason, such as a
    permission error, which pytest stops on too.
    """
    pattern = scenario_file_pattern(suffix)
    stack = [_entries(directory)]
    while stack:
        entry = next(stack[-1], None)
        if entry is None:
            stack.pop()
            continue
        try:
            if entry.is_dir(follow_symlinks=False) and not entry.is_junction():
                if not _skipped(entry):
                    stack.append(_entries(Path(entry.path)))
                continue
            # The name first: only an entry named like a scenario is stat()ed.
            found = pattern.fullmatch(entry.name) is not None and entry.is_file()
        except OSError as e:
            if _ignored(e):
                continue
            raise DiscoveryError(f"cannot look at {entry.path}: {e.strerror or e}") from e
        if found:
            yield Path(entry.path)


def _ignored(error: OSError) -> bool:
    """Whether pytest's collection passes over an entry looking at which raised ``error``."""
    return error.errno in _IGNORED_ERRNOS or getattr(error, "winerror", None) in _IGNORED_WINERRORS


def _entries(directory: Path) -> Iterator[os.DirEntry[str]]:
    """``directory``'s entries in name order, as pytest's collection lists
    them; none when it is gone, as pytest finds none."""
    try:
        with os.scandir(directory) as entries:
            return iter(sorted(entries, key=lambda entry: entry.name))
    except FileNotFoundError:
        return iter(())
    except OSError as e:
        raise DiscoveryError(f"cannot search directory {directory}: {e.strerror or e}") from e


def _skipped(entry: os.DirEntry[str]) -> bool:
    """Whether pytest's collection passes over a directory by default: a
    default ``norecursedirs`` pattern matches its name, it is ``__pycache__``,
    or it is a virtual environment (``--collect-in-virtualenv`` is off by
    default), known as pytest knows one: it holds a ``pyvenv.cfg``, or a
    ``conda-meta/history`` (a conda environment, which may have no
    ``pyvenv.cfg``)."""
    return (
        entry.name == "__pycache__"
        or any(fnmatch.fnmatch(entry.name, pattern) for pattern in DEFAULT_NORECURSEDIRS)
        or os.path.isfile(os.path.join(entry.path, "pyvenv.cfg"))
        or os.path.isfile(os.path.join(entry.path, "conda-meta", "history"))
    )


def configured_suffix(paths: Sequence[Path]) -> str:
    """The ``httpchain_suffix`` pytest would collect by, run on ``paths`` from
    the current directory: the value in the configuration file it would read,
    else `DEFAULT_SUFFIX`.

    The file is the one pytest documents choosing ("determining rootdir and
    configfile"), given no ``-c``: the first of `_CONFIG_FILE_NAMES` that holds
    pytest configuration, in the common ancestor of the paths that exist (a
    file stands for its directory; the current directory when none exists) or
    the nearest directory above it. Failing that, none when a ``setup.py`` is
    there or above, else the same search from each path's directory in turn.
    A ``pyproject.toml`` without pytest configuration met on the way is the
    configuration file when no other is found, and sets nothing.

    ``-o``/``--override-ini`` and ``PYTEST_ADDOPTS`` belong to a pytest run and
    are not read; ``validate --suffix`` stands in for them.

    Raises `DiscoveryError` when a configuration file cannot be read, or the
    value it sets is not a suffix pytest would accept.
    """
    found = _locate_config(paths)
    if found is None:
        return DEFAULT_SUFFIX
    file, settings = found
    value = settings.get(ConfigOptions.SUFFIX, DEFAULT_SUFFIX)
    # A [tool.pytest] table keeps TOML's types, and pytest refuses a
    # non-string there; the string-based forms hand over text, or a list.
    if not isinstance(value, str):
        raise DiscoveryError(f"{file}: {ConfigOptions.SUFFIX} must be a string, not {type(value).__name__}: {value!r}")
    try:
        return check_suffix(value)
    except ValueError as e:
        raise DiscoveryError(f"{file}: {ConfigOptions.SUFFIX} {e}") from None


def pytest_rootdir(paths: Sequence[Path]) -> Path:
    """The rootdir pytest would determine for a run on ``paths`` from the
    current directory, given no ``-c`` or ``--rootdir``: the directory of the
    configuration file it would read (see `configured_suffix`); failing that,
    the nearest directory holding a ``setup.py``, at or above the common
    ancestor of the paths; failing that, the common ancestor of the current
    directory and the paths', or the paths' alone when that is the root of
    the file system, or when there is none (on Windows, paths on another
    drive than the current directory, where pytest keeps the current
    directory, which holds none of them).

    Collection sandboxes ``$ref`` targets in pytest's rootdir, so the CLI's
    default reference root is this: `validate` rejects no reference a run
    on the same paths resolves, and one run's files share one root.

    A configuration file that cannot be read, which pytest stops on, is
    passed over here, as one holding no pytest configuration is: a run over
    files needs no setting from it (`configured_suffix` reports it when a
    directory needs the suffix)."""
    return _setup(paths, strict=False)[0]


def _locate_config(paths: Sequence[Path]) -> tuple[Path, dict[str, object]] | None:
    """The configuration file pytest would read for ``paths`` and the settings
    it holds, or None when it would read none (see `configured_suffix`)."""
    return _setup(paths, strict=True)[1]


def _setup(paths: Sequence[Path], *, strict: bool) -> tuple[Path, tuple[Path, dict[str, object]] | None]:
    """pytest's rootdir for ``paths``, and the configuration file it reads
    with its settings, or None (see `pytest_rootdir`); ``strict`` raises
    `DiscoveryError` for a configuration file that cannot be read, else it is
    passed over."""
    dirs: list[Path] = []
    for path in paths:
        # Made absolute as pytest makes an argument absolute: `..` and `.`
        # are dropped from the text, not resolved through symlinks.
        absolute = Path(os.path.abspath(path))
        if os.path.exists(absolute):
            dirs.append(absolute if os.path.isdir(absolute) else absolute.parent)
    ancestor = _common_ancestor(dirs) if dirs else Path.cwd()
    if (found := _find_config([ancestor], strict=strict)) is not None:
        return found[0].parent, found
    for base in (ancestor, *ancestor.parents):
        if os.path.isfile(base / "setup.py"):
            return base, None
    if dirs != [ancestor] and (found := _find_config(dirs, strict=strict)) is not None:
        return found[0].parent, found
    try:
        rootdir = Path(os.path.commonpath([Path.cwd(), ancestor]))
    except ValueError:
        # On another drive than the current directory (Windows): pytest keeps
        # the current directory then, a rootdir that holds none of the paths,
        # so their own common ancestor is taken instead.
        return ancestor, None
    # The root of the file system (of a drive, on Windows) is no rootdir.
    return (ancestor if os.path.splitdrive(rootdir)[1] == os.sep else rootdir), None


def _common_ancestor(dirs: list[Path]) -> Path:
    ancestor = dirs[0]
    for directory in dirs[1:]:
        try:
            ancestor = Path(os.path.commonpath([ancestor, directory]))
        except ValueError:
            # On another drive (Windows): pytest leaves such a path out.
            pass
    return ancestor


def _find_config(starts: Iterable[Path], *, strict: bool = True) -> tuple[Path, dict[str, object]] | None:
    """The first configuration file holding pytest configuration in each of
    ``starts`` or above it, in turn; else the first ``pyproject.toml`` met.
    Not ``strict``, one that cannot be read, or that pytest would refuse, is
    passed over as one holding no pytest configuration is: a
    ``pyproject.toml`` of the kind is still the one met, if first."""
    bare_pyproject: Path | None = None
    for start in starts:
        for base in (start, *start.parents):
            for name in _CONFIG_FILE_NAMES:
                file = base / name
                if not os.path.isfile(file):
                    continue
                try:
                    settings = _read_config(file)
                except DiscoveryError:
                    if strict:
                        raise
                    settings = None
                if settings is not None:
                    return file, settings
                if name == "pyproject.toml" and bare_pyproject is None:
                    bare_pyproject = file
    return (bare_pyproject, {}) if bare_pyproject is not None else None


def _read_config(file: Path) -> dict[str, object] | None:
    """The pytest settings ``file`` holds, or None when it holds no pytest
    configuration, read as pytest reads each kind of file: the ``[pytest]``
    section of an ``.ini`` file (a ``pytest.ini`` is pytest's even without
    one), the ``[tool:pytest]`` section of a ``setup.cfg``, the ``[pytest]``
    table of a ``pytest.toml`` (pytest's even without one), and the
    ``[tool.pytest]`` or ``[tool.pytest.ini_options]`` table of a
    ``pyproject.toml``."""
    try:
        if file.suffix == ".toml":
            return _read_toml_config(file, tomllib.loads(file.read_text(encoding="utf-8")))
        ini = iniconfig.IniConfig(str(file))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, iniconfig.ParseError) as e:
        raise DiscoveryError(f"cannot read pytest configuration from {file}: {e}") from e
    if file.suffix == ".cfg":
        if "tool:pytest" in ini:
            return dict(ini["tool:pytest"].items())
        if "pytest" in ini:
            # pytest stops on it, in these words.
            raise DiscoveryError(f"{file}: [pytest] section in setup.cfg files is no longer supported, change to [tool:pytest] instead.")
        return None
    if "pytest" in ini:
        return dict(ini["pytest"].items())
    return {} if file.name in ("pytest.ini", ".pytest.ini") else None


def _read_toml_config(file: Path, data: dict[str, object]) -> dict[str, object] | None:
    if file.name in ("pytest.toml", ".pytest.toml"):
        table = data.get("pytest")
        return dict(table) if isinstance(table, dict) else {}
    tool = data.get("tool")
    table = tool.get("pytest") if isinstance(tool, dict) else None
    if not isinstance(table, dict):
        return None
    native = {key: value for key, value in table.items() if key != "ini_options"}
    ini_options = table.get("ini_options")
    if native and ini_options:
        # pytest stops on it too.
        raise DiscoveryError(f"{file}: cannot use both [tool.pytest] and [tool.pytest.ini_options]")
    if native:
        return native
    if ini_options is None:
        return None
    # The string-based form: pytest turns every value but a list into text.
    items = ini_options.items() if isinstance(ini_options, dict) else ()
    return {key: value if isinstance(value, list) else str(value) for key, value in items}
