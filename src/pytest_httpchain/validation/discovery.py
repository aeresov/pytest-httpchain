"""The scenario files a directory given to ``validate`` holds, the
``httpchain_suffix`` they are named by, and the
``httpchain_ref_parent_traversal_depth`` their references are held to.

`find_scenario_files` walks a directory as pytest's collection does by default,
and `configured_suffix` and `configured_depth` read their options from the
configuration file pytest would read for the same paths, so ``validate tests/``
checks the files ``pytest tests/`` runs, loaded as collection loads them. A
file pytest reads on the way is read as pytest reads it, and refused where the
installed pytest refuses it: the configuration file, the ones it passes over
for holding no pytest configuration, and those it reads after it in the same
directory, to name the ones it ignores. All follow what pytest documents and
does, by public means (``tomllib``, ``iniconfig``, the parser pytest reads ini
files with, and ``pytest.version_tuple`` where pytest's releases read a file
differently), not ``_pytest``'s internals.
"""

import errno
import fnmatch
import os
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import NamedTuple

import iniconfig
import pytest

from pytest_httpchain.constants import DEFAULT_REF_PARENT_TRAVERSAL_DEPTH, DEFAULT_SUFFIX, ConfigOptions, check_suffix, scenario_file_pattern

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
# them in a directory. Having found the one it reads, it reads each one after
# it in the same directory too, to name those it ignores (a warning), and stops
# on one it cannot read or refuses as it stops on the one it reads.
_CONFIG_FILE_NAMES = ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")


class DiscoveryError(Exception):
    """A directory cannot be searched, or the configured suffix or depth cannot
    be read: the command exits with the message before it checks anything."""


class _Config(NamedTuple):
    """A configuration file pytest reads, and the settings it holds:
    ``native``, with TOML's own types (a ``[tool.pytest]`` or ``pytest.toml``
    table), else as the string-based forms hand them over, text or lists (an
    ini file, ``[tool.pytest.ini_options]``). pytest reads a typed option's
    value by which of the two it is."""

    file: Path
    settings: dict[str, object]
    native: bool


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

    Each file met on the way is read as pytest reads it, and so is each one
    after the configuration file in `_CONFIG_FILE_NAMES` in its directory,
    which pytest reads to name the ones it ignores: one there that holds no
    pytest configuration, or some, changes nothing. A ``pytest.toml`` or
    ``.pytest.toml`` holding options outside any table and no ``[pytest]``
    table is refused where the installed pytest refuses it (from 9.2.0 on,
    `_refuses_top_level_options`); before that, it holds pytest configuration
    that sets nothing.

    ``-o``/``--override-ini`` and ``PYTEST_ADDOPTS`` belong to a pytest run and
    are not read; ``validate --suffix`` stands in for them.

    Raises `DiscoveryError` when a file read on the way cannot be read, or is
    one pytest refuses, as pytest stops on it, or the value the configuration
    file sets is not a suffix pytest would accept.
    """
    found = _locate_config(paths)
    if found is None:
        return DEFAULT_SUFFIX
    file, settings, _ = found
    value = settings.get(ConfigOptions.SUFFIX, DEFAULT_SUFFIX)
    # A [tool.pytest] table keeps TOML's types, and pytest refuses a
    # non-string there; the string-based forms hand over text, or a list.
    if not isinstance(value, str):
        raise DiscoveryError(f"{file}: {ConfigOptions.SUFFIX} must be a string, not {type(value).__name__}: {value!r}")
    try:
        return check_suffix(value)
    except ValueError as e:
        raise DiscoveryError(f"{file}: {ConfigOptions.SUFFIX} {e}") from None


def configured_depth(paths: Sequence[Path]) -> int:
    """The ``httpchain_ref_parent_traversal_depth`` pytest would hold a run on
    ``paths`` from the current directory to: the value in the configuration
    file it would read (found as `configured_suffix` finds it), else
    `DEFAULT_REF_PARENT_TRAVERSAL_DEPTH`.

    Read as pytest reads an ``int`` option. Text, from an ini file or the
    string-based ``[tool.pytest.ini_options]`` (where pytest turns any value
    but a list into text first: a TOML integer, a float, a boolean), is read
    as Python's ``int()`` reads it. A ``[tool.pytest]`` or ``pytest.toml``
    table keeps TOML's types, and there pytest takes a TOML integer and
    refuses anything else, text and a boolean included. Below 0 is refused,
    as the plugin refuses it.

    ``-o``/``--override-ini`` and ``PYTEST_ADDOPTS`` belong to a pytest run and
    are not read; ``--ref-parent-traversal-depth`` stands in for them.

    A configuration file pytest would stop on, one it cannot read or refuses
    whole, sets no depth, nor does the one it would read when it stops on a
    file of the kind it reads after it in the same directory: the default
    holds.

    Raises `DiscoveryError` when the value is one pytest would refuse.
    """
    # Unlike the suffix, which only a directory needs, the depth is read for
    # every command, a run over files included. When the file pytest would
    # read, or one it reads after it in the same directory, cannot be read
    # (or pytest refuses it whole), pytest stops on it before it applies any
    # value, from it or from any file past it: no depth applies, so the
    # default does, as it did before the depth was read.
    # Refusing it here would stop `validate` on files, `resolve`, `show` and
    # `graph` on a project whose configuration is mid-edit, none of which
    # needs a setting from it (`validate` on a directory, needing the
    # suffix, still reports the file, as pytest does). Passing over it to
    # the file found past it, as `pytest_rootdir` does for the root, would
    # take a depth from a file pytest never reads, and refuse one there that
    # pytest never sees; taking the depth of the file pytest read before it
    # in the same directory, whose directory `pytest_rootdir` keeps as the
    # root, would take one pytest never applies. A depth in a file pytest
    # does read, and goes on from, is read, and refused, as pytest reads and
    # refuses it.
    try:
        found = _locate_config(paths)
    except DiscoveryError:
        return DEFAULT_REF_PARENT_TRAVERSAL_DEPTH
    option = ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH
    if found is None or option not in found.settings:
        return DEFAULT_REF_PARENT_TRAVERSAL_DEPTH
    value = found.settings[option]
    if found.native:
        # A bool is an int to Python, not to TOML, nor to pytest.
        if not isinstance(value, int) or isinstance(value, bool):
            raise DiscoveryError(f"{found.file}: {option} must be an integer, not {type(value).__name__}: {value!r}")
        depth = value
    else:
        # Text, or a list, which an int option is never given as.
        if not isinstance(value, str):
            raise DiscoveryError(f"{found.file}: {option} must be an integer, not {type(value).__name__}: {value!r}")
        try:
            depth = int(value)
        except ValueError:
            raise DiscoveryError(f"{found.file}: {option} must be an integer, not {value!r}") from None
    if depth < 0:
        raise DiscoveryError(f"{found.file}: {option} must be non-negative, not {value!r}")
    return depth


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
    passed over here, as one holding no pytest configuration is: pytest reads
    no setting from it, so a run over files takes none from it
    (`configured_suffix` reports it when a directory needs the suffix;
    `configured_depth` takes the default depth then).

    The files pytest reads after the configuration file in its directory,
    and stops on as well when it cannot read one or refuses it, are not read
    here: the configuration file is kept, and its directory is the root.
    pytest has found that file, and with it the rootdir, before it reads
    them, and none of them can move it: one holding pytest configuration is
    ignored, and one fixed so that pytest goes on leaves the rootdir where
    it was. Passing the configuration file over instead, as an unreadable
    one is, would move the root to a file pytest never takes it from, and a
    run over files needs no setting from either (`configured_suffix` reports
    the file pytest stops on when a directory needs the suffix;
    `configured_depth` takes the default depth then)."""
    return _setup(paths, strict=False)[0]


def _locate_config(paths: Sequence[Path]) -> _Config | None:
    """The configuration file pytest would read for ``paths`` and the settings
    it holds, or None when it would read none (see `configured_suffix`)."""
    return _setup(paths, strict=True)[1]


def _setup(paths: Sequence[Path], *, strict: bool) -> tuple[Path, _Config | None]:
    """pytest's rootdir for ``paths``, and the configuration file it reads
    with its settings, or None (see `pytest_rootdir`); ``strict`` raises
    `DiscoveryError` for a configuration file that cannot be read, or one
    read after it in its directory, else it is passed over, or not read
    (`_find_config`)."""
    dirs: list[Path] = []
    for path in paths:
        # Made absolute as pytest makes an argument absolute: `..` and `.`
        # are dropped from the text, not resolved through symlinks.
        absolute = Path(os.path.abspath(path))
        if os.path.exists(absolute):
            dirs.append(absolute if os.path.isdir(absolute) else absolute.parent)
    ancestor = _common_ancestor(dirs) if dirs else Path.cwd()
    if (found := _find_config([ancestor], strict=strict)) is not None:
        return found.file.parent, found
    for base in (ancestor, *ancestor.parents):
        if os.path.isfile(base / "setup.py"):
            return base, None
    if dirs != [ancestor] and (found := _find_config(dirs, strict=strict)) is not None:
        return found.file.parent, found
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


def _find_config(starts: Iterable[Path], *, strict: bool = True) -> _Config | None:
    """The first configuration file holding pytest configuration in each of
    ``starts`` or above it, in turn; else the first ``pyproject.toml`` met.
    ``strict``, the files after the one found in its directory are read too,
    as pytest reads them (`_read_ignored`). Not ``strict``, those are not
    read (see `pytest_rootdir`), and one met on the way that cannot be read,
    or that pytest would refuse, is passed over as one holding no pytest
    configuration is: a ``pyproject.toml`` of the kind is still the one met,
    if first."""
    bare_pyproject: Path | None = None
    for start in starts:
        for base in (start, *start.parents):
            for index, name in enumerate(_CONFIG_FILE_NAMES):
                file = base / name
                if not os.path.isfile(file):
                    continue
                try:
                    config = _read_config(file)
                except DiscoveryError:
                    if strict:
                        raise
                    config = None
                if config is not None:
                    if strict:
                        _read_ignored(base, _CONFIG_FILE_NAMES[index + 1 :])
                    return config
                if name == "pyproject.toml" and bare_pyproject is None:
                    bare_pyproject = file
    return _Config(bare_pyproject, {}, native=False) if bare_pyproject is not None else None


def _read_ignored(directory: Path, names: Iterable[str]) -> None:
    """Read each of ``names`` in ``directory`` as pytest reads the files
    after its configuration file there, to name the ones it ignores (those
    holding pytest configuration): one that cannot be read, or that pytest
    refuses, raises `DiscoveryError` as the configuration file would, since
    pytest stops on it. Only the files after the configuration file in its
    own directory: the ones before it were read on the way to it, and pytest
    reads none in another directory once it has found it."""
    for name in names:
        file = directory / name
        if os.path.isfile(file):
            _read_config(file)


def _read_config(file: Path) -> _Config | None:
    """``file`` and the pytest settings it holds, or None when it holds no
    pytest configuration, read as pytest reads each kind of file: the ``[pytest]``
    section of an ``.ini`` file (a ``pytest.ini`` is pytest's even without
    one), the ``[tool:pytest]`` section of a ``setup.cfg``, the ``[pytest]``
    table of a ``pytest.toml`` (pytest's even without one, unless it holds
    options outside any table that the installed pytest refuses), and the
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
            return _Config(file, dict(ini["tool:pytest"].items()), native=False)
        if "pytest" in ini:
            # pytest stops on it, in these words.
            raise DiscoveryError(f"{file}: [pytest] section in setup.cfg files is no longer supported, change to [tool:pytest] instead.")
        return None
    if "pytest" in ini:
        return _Config(file, dict(ini["pytest"].items()), native=False)
    return _Config(file, {}, native=False) if file.name in ("pytest.ini", ".pytest.ini") else None


def _read_toml_config(file: Path, data: dict[str, object]) -> _Config | None:
    if file.name in ("pytest.toml", ".pytest.toml"):
        if "pytest" not in data and _refuses_top_level_options():
            # What pytest calls a top-level option: a key whose value is no
            # table (an array of tables included), in the file's order.
            options = [key for key, value in data.items() if not isinstance(value, dict)]
            if options:
                # pytest stops on it, in these words.
                raise DiscoveryError(f"{file}: pytest configuration must be under a [pytest] table (found top-level options: {', '.join(options)})")
        table = data.get("pytest")
        return _Config(file, dict(table) if isinstance(table, dict) else {}, native=True)
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
        return _Config(file, native, native=True)
    if ini_options is None:
        return None
    # The string-based form: pytest turns every value but a list into text.
    items = ini_options.items() if isinstance(ini_options, dict) else ()
    return _Config(file, {key: value if isinstance(value, list) else str(value) for key, value in items}, native=False)


def _refuses_top_level_options() -> bool:
    """Whether the installed pytest refuses a ``pytest.toml`` or
    ``.pytest.toml`` that holds options outside any table and no ``[pytest]``
    table. pytest 9.2.0 is the first release that does
    (pytest-dev/pytest#14638); before it, such a file is the configuration
    file, and sets nothing. Read on each call, from pytest's public version."""
    # The major and the minor release are numbers in every pytest version.
    return tuple(map(int, pytest.version_tuple[:2])) >= (9, 2)
