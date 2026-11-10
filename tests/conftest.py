from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def info_html() -> str:
    return (FIXTURES / "info_sample.html").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self.headers = {}

    def json(self):
        return self._json
