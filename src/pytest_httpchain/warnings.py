"""Warning types. A leaf module, so the package ``__init__`` can re-export the
user-facing name without loading the plugin/execution machinery."""

import pytest


class ScenarioValidationWarning(pytest.PytestWarning):
    """A collected scenario has a non-fatal validation issue (e.g. an undefined variable)."""


class AmbiguousReferenceWarning(UserWarning):
    """A ``$ref`` path matches a file under both lookup bases; the file-relative
    one wins and the other is silently ignored, so adding a file next to a
    scenario can change what a reference means.

    Not a ``PytestWarning``: the resolver also runs outside pytest, where the
    CLI maps this to the HTTPCHAIN026 diagnostic."""
