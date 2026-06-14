"""End-to-end HAR export (M52).

The har_writer unit tests cover the serialization shape; this exercises the
full plugin path: running a scenario with ``--output-dir`` must drop a `.har`
file per executed stage, and that file must parse as valid HAR JSON.
"""

import json


def test_har_file_written(pytester):
    har_dir = pytester.path / "har_out"

    pytester.copy_example("conftest.py")
    pytester.copy_example("verify/test_verify_status.http.json")
    result = pytester.runpytest("-s", "--output-dir", str(har_dir))

    # Sanity: the scenario itself passes (2 stages = 2 test methods).
    result.assert_outcomes(errors=0, failed=0, passed=2)

    # A HAR file must have been written under the output dir.
    har_files = list(har_dir.glob("*.har"))
    assert har_files, f"expected a .har file under {har_dir}, found none"

    # It must parse as JSON with the canonical HAR top-level shape.
    har = json.loads(har_files[0].read_text(encoding="utf-8"))
    assert "log" in har
    assert isinstance(har["log"]["entries"], list)
    assert har["log"]["entries"], "HAR log must contain at least one entry"
