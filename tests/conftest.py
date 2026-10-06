import json
from pathlib import Path

import pytest

FX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fx():
    def load(name: str):
        return json.loads((FX / name).read_text(encoding="utf-8"))
    return load
