import pytest

from shoplist_crawler import api, categories

# 公式ドキュメントのサンプルレスポンスと同じ構造(名前空間つき)
XML = """<?xml version="1.0" encoding="UTF-8"?>
<ResultSet xmlns="urn:yahoo:jp:categorySearch" totalResultsReturned="1"><Result><Categories>
 <Current><Id>1</Id><ParentId>0</ParentId><Title><Short>Shopping</Short></Title></Current>
 <Children>
  <Child sortOrder="0"><Id>13457</Id><Title><Short>ファッション</Short><Medium>ファッション</Medium></Title></Child>
  <Child sortOrder="60"><Id>2500</Id><Title><Short>ダイエット、健康</Short><Medium>ダイエット、健康グッズ</Medium></Title></Child>
 </Children></Categories></Result></ResultSet>""".encode()


def test_parse_children():
    assert categories.parse_children(XML) == [(13457, "ファッション"), (2500, "ダイエット、健康")]


def test_parse_children_without_children_and_bad_xml():
    assert categories.parse_children(b"<ResultSet><Result><Categories><Current><Id>5</Id></Current></Categories></Result></ResultSet>") == []
    with pytest.raises(api.ApiError):
        categories.parse_children(b"not xml")


def test_list_targets_walks_to_depth_and_keeps_shallow_leaves():
    tree = {13457: [(2494, "レディース"), (2495, "メンズ"), (999, "その他(子なし)")], 2494: [(1, "バッグ")], 2495: [(2, "靴"), (3, "時計")], 999: []}
    asked = []

    def fake_children(session, appid, cid):
        asked.append(cid)
        return tree[cid]

    t = categories.list_targets(None, "A", 13457, depth=3, sleep=lambda s: None, children=fake_children)
    assert [(x.category_id, x.depth) for x in t] == [(1, 3), (2, 3), (3, 3), (999, 2)]
    assert 1 not in asked  # depth3の子は辿らない
    assert categories.list_targets(None, "A", 13457, depth=2, sleep=lambda s: None, children=fake_children)[0].category_id == 2494


def test_list_targets_rejects_depth_1():
    with pytest.raises(ValueError):
        categories.list_targets(None, "A", 1, depth=1)


HIT = {
    "genreCategory": {"id": 23699, "name": "メンズスニーカー", "depth": 5},
    "parentGenreCategories": [
        {"depth": 1, "id": 13457, "name": "ファッション"},
        {"depth": 2, "id": 2495, "name": "メンズファッション"},
        {"depth": 3, "id": 1730, "name": "メンズシューズ"},
        {"depth": 4, "id": 1731, "name": "スニーカー"},
    ],
}


def test_category_path_truncates_to_target_depth_and_drops_root():
    assert categories.category_path(HIT, 3) == "メンズファッション/メンズシューズ"
    assert categories.category_path(HIT, 2) == "メンズファッション"


def test_category_path_fallback_when_hit_has_no_category():
    assert categories.category_path({}, 3, fallback="代替") == "代替"
