from shoplist_crawler import http
from conftest import FakeResponse


class FakeSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return FakeResponse(self.statuses.pop(0))


def test_retries_429_with_growing_waits_then_succeeds():
    waits = []
    s = FakeSession([429, 429, 200])
    r = http.get_with_retry(s, "u", retries=5, backoff=10.0, max_wait=120.0, sleep=waits.append)
    assert r.status_code == 200 and s.calls == 3
    assert 10 <= waits[0] < 11 and 20 <= waits[1] < 21


def test_gives_up_after_retries_and_returns_last_response():
    waits = []
    s = FakeSession([429] * 10)
    r = http.get_with_retry(s, "u", retries=3, backoff=10.0, max_wait=25.0, sleep=waits.append)
    assert r.status_code == 429 and s.calls == 4
    assert all(w < 26 for w in waits)  # max_waitで頭打ち
