from shoplist_crawler import api
from conftest import FakeResponse


def _hits(ids):
    return [{"seller": {"sellerId": i, "name": f"name-{i}"}} for i in ids]


def test_iter_sellers_paginates_until_short_page(monkeypatch):
    calls = []
    pages = [
        {"totalResultsAvailable": 70, "hits": _hits([f"s{i}" for i in range(50)])},
        {"totalResultsAvailable": 70, "hits": _hits([f"s{i}" for i in range(50, 70)])},
    ]

    def fake_get(session, url, params=None, **k):
        calls.append(params["start"])
        return FakeResponse(200, json_data=pages[len(calls) - 1])

    monkeypatch.setattr(api, "get_with_retry", fake_get)
    out = list(api.iter_sellers(None, "APPID", "テスト", sleep=lambda s: None))
    assert calls == [1, 51]
    assert len(out) == 70
    assert out[0] == ("s0", "name-s0")


def test_iter_sellers_never_exceeds_api_position_limit(monkeypatch):
    """APIは start + results <= 1000 を超えると400を返す(実際に踏んだ不具合)。"""
    calls = []

    def fake_get(session, url, params=None, **k):
        calls.append((params["start"], params["results"]))
        return FakeResponse(200, json_data={"totalResultsAvailable": 99999, "hits": _hits([f"s{params['start']}-{i}" for i in range(params["results"])])})

    monkeypatch.setattr(api, "get_with_retry", fake_get)
    list(api.iter_sellers(None, "APPID", "x", sleep=lambda s: None))
    assert all(start + results <= 1000 for start, results in calls)
    assert calls[-1][0] + calls[-1][1] == 1000  # 末尾まで使い切る
    assert len(calls) == 20


def test_sorts_are_documented_values():
    assert set(api.SORTS) == {"-score", "+price", "-price", "-review_count"}


def test_iter_sellers_api_error(monkeypatch):
    monkeypatch.setattr(api, "get_with_retry", lambda *a, **k: FakeResponse(400, text="bad appid"))
    try:
        list(api.iter_sellers(None, "bad", "x"))
    except api.ApiError as e:
        assert "400" in str(e) and e.status == 400
    else:
        raise AssertionError("ApiError expected")


def test_iter_sellers_skips_hits_without_seller(monkeypatch):
    data = {"totalResultsAvailable": 3, "hits": [{"seller": {"sellerId": "a", "name": "A"}}, {}, {"seller": {}}]}
    monkeypatch.setattr(api, "get_with_retry", lambda *a, **k: FakeResponse(200, json_data=data))
    assert list(api.iter_sellers(None, "APPID", "x", sleep=lambda s: None)) == [("a", "A")]
