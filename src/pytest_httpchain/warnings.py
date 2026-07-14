"""Warning types emitted by the plugin.

Kept in a leaf module (imports only pytest) so that the package ``__init__``
can re-export the user-facing name without pulling in the plugin/execution
machinery — importing any subpackage (e.g. ``pytest_httpchain.models``) must
not load ``plugin``/``carrier``.
"""

import pytest


class ScenarioValidationWarning(pytest.PytestWarning):
    """A collected scenario has a non-fatal validation issue (e.g. an undefined variable)."""
