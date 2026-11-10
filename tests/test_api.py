from shoplist_crawler import api
from conftest import FakeResponse


def _hits(ids):
    return [{"seller": {"sellerId": i, "name": f"name-{i}"}} for i in ids]


def test_iter_sellers_paginates_until_short_page(monkeypatch):
    calls = []
    pages = [
        {"totalResultsAvailable": 150, "hits": _hits([f"s{i}" for i in range(100)])},
        {"totalResultsAvailable": 150, "hits": _hits([f"s{i}" for i in range(100, 150)])},
    ]

    def fake_get(session, url, params=None, **k):
        calls.append(params["start"])
        return FakeResponse(200, json_data=pages[len(calls) - 1])

    monkeypatch.setattr(api, "get_with_retry", fake_get)
    out = list(api.iter_sellers(None, "APPID", "テスト", sleep=lambda s: None))
    assert calls == [1, 101]
    assert len(out) == 150
    assert out[0] == ("s0", "name-s0")


def test_iter_sellers_stops_at_1000(monkeypatch):
    calls = []

    def fake_get(session, url, params=None, **k):
        calls.append((params["start"], params["results"]))
        return FakeResponse(200, json_data={"totalResultsAvailable": 99999, "hits": _hits([f"s{params['start']}-{i}" for i in range(params["results"])])})

    monkeypatch.setattr(api, "get_with_retry", fake_get)
    list(api.iter_sellers(None, "APPID", "x", sleep=lambda s: None))
    assert calls[-1] == (901, 100)
    assert len(calls) == 10


def test_iter_sellers_api_error(monkeypatch):
    monkeypatch.setattr(api, "get_with_retry", lambda *a, **k: FakeResponse(400, text="bad appid"))
    try:
        list(api.iter_sellers(None, "bad", "x"))
    except api.ApiError as e:
        assert "400" in str(e)
    else:
        raise AssertionError("ApiError expected")


def test_iter_sellers_skips_hits_without_seller(monkeypatch):
    data = {"totalResultsAvailable": 3, "hits": [{"seller": {"sellerId": "a", "name": "A"}}, {}, {"seller": {}}]}
    monkeypatch.setattr(api, "get_with_retry", lambda *a, **k: FakeResponse(200, json_data=data))
    assert list(api.iter_sellers(None, "APPID", "x", sleep=lambda s: None)) == [("a", "A")]
