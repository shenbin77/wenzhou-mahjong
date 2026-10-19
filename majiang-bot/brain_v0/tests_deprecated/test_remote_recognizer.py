# -*- coding: utf-8 -*-
"""Opt-in competition-skin remote hand recognizer tests."""

import os
import sys

import numpy as np

SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from remote_recognizer import AgnesHandRecognizer, _normalize_tile


HAND = ["1万", "5万", "8万", "8万", "一条", "二条", "三条",
        "五条", "八条", "二筒", "八筒", "九筒", "6万"]


class _Response:
    def __init__(self, content):
        self.content = content

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


def test_normalize_tile_supports_competition_labels():
    assert _normalize_tile("1万") == "1m"
    assert _normalize_tile("三條") == "3s"
    assert _normalize_tile(28) == "8p"
    assert _normalize_tile("红中") == "red"


def test_remote_recognizer_accepts_only_legal_hand():
    calls = []

    def post(payload, timeout):
        calls.append((payload, timeout))
        return _Response(__import__("json").dumps(HAND, ensure_ascii=False))

    rec = AgnesHandRecognizer(api_key="test-key", post=post, min_interval=0)
    result = rec.recognize(np.zeros((160, 1200, 3), dtype=np.uint8))
    assert result == ["1m", "5m", "8m", "8m", "1s", "2s", "3s",
                      "5s", "8s", "2p", "8p", "9p", "6m"]
    assert rec.stats == {"calls": 1, "accepted": 1, "rejected": 0}
    assert calls and calls[0][0]["chat_template_kwargs"]["enable_thinking"] is False


def test_remote_recognizer_rejects_short_or_ambiguous_hand():
    def post(payload, timeout):
        return _Response('["1万", null, "2万"]')

    rec = AgnesHandRecognizer(api_key="test-key", post=post, min_interval=0)
    assert rec.recognize(np.zeros((160, 1200, 3), dtype=np.uint8)) == []
    assert rec.stats["rejected"] == 1
    assert "张数" in rec.last_error

