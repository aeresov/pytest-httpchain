"""Warning types. A leaf module, importing nothing of the package: the ``$ref``
resolver, low in the layering, emits `AmbiguousReferenceWarning`
(`jsonref.plumbing.path` imports it), so this module sits on the lowest layer,
below jsonref, independent of `errors` and `constants` beside it. The package
``__init__`` re-exports `ScenarioValidationWarning`, the user-facing name."""

import pytest


class ScenarioValidationWarning(pytest.PytestWarning):
    """A collected scenario has a non-fatal validation issue (e.g. an undefined variable)."""


class AmbiguousReferenceWarning(UserWarning):
    """A ``$ref`` path matches a file under both lookup bases; the file-relative
    one wins and the other is silently ignored, so adding a file next to a
    scenario can change what a reference means.

    Not a ``PytestWarning``: the resolver also runs outside pytest, where the
    CLI maps this to the HTTPCHAIN026 diagnostic."""
