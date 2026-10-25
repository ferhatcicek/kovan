import shutil
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "code"


@pytest.fixture
def code_root(tmp_path) -> Path:
    """Fixture projelerinin kopyası: her test temiz bir kök klasörle başlar."""
    root = tmp_path / "code"
    shutil.copytree(FIXTURES, root)
    return root


@pytest.fixture
def python_exe() -> str:
    return sys.executable
