"""Warning types emitted by the plugin.

Kept in a leaf module (imports only pytest) so that the package ``__init__``
can re-export the user-facing name without pulling in the plugin/execution
machinery — importing any subpackage (e.g. ``pytest_httpchain.models``) must
not load ``plugin``/``carrier``.
"""

import pytest


class ScenarioValidationWarning(pytest.PytestWarning):
    """A collected scenario has a non-fatal validation issue (e.g. an undefined variable)."""


class AmbiguousReferenceWarning(UserWarning):
    """A ``$ref``/``$include`` path matches an existing file under BOTH lookup
    bases (the referencing file's directory and the root path). The
    file-relative candidate wins; the shadowed root-relative file is ignored —
    worth a warning, because adding a file next to a scenario can silently
    change which fragment a reference resolves to.

    Not a ``pytest.PytestWarning``: the jsonref resolver that raises it also
    runs outside pytest (the ``pytest-httpchain`` CLI maps it to the
    ``HTTPCHAIN026`` diagnostic)."""
