# -*- coding: utf-8 -*-
"""阿法客户端端点配置测试：环境变量可覆盖，默认值保持研究态现状。"""

from engine import afa_client
from engine.afa_client import (
    AfaClient,
    _coerce_tile_id,
    afa_base_url,
    buketing_url,
    rivers_from_history,
)


def test_default_url_matches_hardcoded_research_endpoint():
    assert afa_base_url() == "https://www.afsuanpai.com/flutterapi_v2"
    assert buketing_url() == (
        "https://www.afsuanpai.com/flutterapi_v2/calc_v44_baoding_buketing.php"
    )


def test_env_var_overrides_base_url(monkeypatch):
    monkeypatch.setenv("MAHJONG_AFA_BASE_URL", "http://127.0.0.1:9/alt/v2/")
    assert buketing_url() == "http://127.0.0.1:9/alt/v2/calc_v44_baoding_buketing.php"


def test_empty_env_var_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MAHJONG_AFA_BASE_URL", "")
    assert afa_base_url() == afa_client.DEFAULT_AFA_BASE_URL


def test_dead_code_no_tingpai_url_removed():
    assert not hasattr(afa_client, "NO_TINGPAI_URL")


def test_recommend_posts_to_resolved_url(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"raw_result": {"单牌进张明细表": {}}}}

    class FakeSession:
        def post(self, url, **kwargs):
            captured["url"] = url
            return FakeResp()

    monkeypatch.setenv("MAHJONG_AFA_BASE_URL", "https://mirror.example/api")
    client = AfaClient(session_factory=FakeSession)
    result = client.recommend([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 24])
    assert captured["url"] == "https://mirror.example/api/calc_v44_baoding_buketing.php"
    assert result is not None and result["source"] == "afa"


def test_rivers_from_history_maps_tracker_seats_and_skips_invalid():
    history = [
        {"tile_id": 1, "player": "left"},
        {"tile_id": 22, "player": "right"},
        {"tile_id": 43, "player": "top"},
        {"tile_id": 55, "player": "left"},
        {"tile_id": 99, "player": "left"},
        {"tile_id": "bad", "player": "right"},
        {"tile_id": 9, "player": "bottom"},
    ]
    assert rivers_from_history(history) == {
        "up": [1, 55], "down": [22], "opposite": [43]
    }


def test_recommend_sends_corrected_hand_and_grouped_rivers():
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"raw_result": {"鍗曠墝杩涘紶鏄庣粏琛?": {}}}}

    class FakeSession:
        def post(self, url, **kwargs):
            import json
            captured["body"] = json.loads(kwargs["data"].decode("utf-8"))
            return FakeResp()

    hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 24]
    client = AfaClient(session_factory=FakeSession)
    result = client.recommend(hand, rivers={"up": [1], "down": [22], "opposite": [43]})
    assert result is not None
    assert captured["body"]["shoupai"] == hand
    assert captured["body"]["player_count"] == 4
    assert captured["body"]["tile_count"] == 112
    assert captured["body"]["hand_count"] == 14
    assert captured["body"]["can_chi"] is False
    assert captured["body"]["can_qidui"] is True
    assert captured["body"]["fangpao_only"] is False
    assert captured["body"]["shangjia_chupai_jilu"] == [1]
    assert captured["body"]["xiajia_chupai_jilu"] == [22]
    assert captured["body"]["duijia_chupai_jilu"] == [43]


def test_coerce_tile_id_accepts_numeric_and_common_named_keys():
    assert _coerce_tile_id("1万") == 1
    assert _coerce_tile_id("9筒") == 29
    assert _coerce_tile_id("3條") == 43
    assert _coerce_tile_id("red") == 55
    assert _coerce_tile_id(24) == 24


def test_recommend_skips_malformed_response_option_keys():
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": {
                    "raw_result": {
                        "最小进听数": 1,
                        "单牌进张明细表": {
                            "1万": {"最后评分": 2.5, "危险率": 0.2},
                            "not-a-tile": {"最后评分": 99},
                            "55": {"最后评分": 1.0, "危险率": 10},
                            "bad-object": None,
                        },
                    }
                }
            }

    class FakeSession:
        def post(self, url, **kwargs):
            return FakeResp()

    hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 24]
    result = AfaClient(session_factory=FakeSession).recommend(hand)
    assert result is not None
    assert [item["tile"] for item in result["options"]] == [1, 55]
    assert result["options"][0]["danger_rate"] == 20
