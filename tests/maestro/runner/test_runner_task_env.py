"""The Runner refuses task environment variables that would override the names it sets itself."""

import subprocess
import sys

from support import ROOT


def test_the_runner_refuses_reserved_names_in_task_env():
    """The Runner's own copy of the list agrees with the platform's."""
    from maestro.execution.materials import RESERVED_PARAMETER

    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); from maestro_runner import job; "
        "print(job.RESERVED_ENV.pattern, file=sys.stderr)"
    )
    runner_src = ROOT / "runner" / "src"
    found = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(runner_src)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert found.stderr.strip() == RESERVED_PARAMETER.pattern
