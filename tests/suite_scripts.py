"""Run the executable integration scenarios in isolated Python processes."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "tests").glob("test_*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_script(script: Path) -> None:
    """One scenario per process, matching CI and preventing fake-client leaks."""
    environment = os.environ.copy()
    environment.setdefault("JELLYFIN_TOKEN", "test-token")
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
