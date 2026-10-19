# -*- coding: utf-8 -*-
"""engine/session_advisor.py 测试：舅舅止损止盈窍门。"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine.session_advisor import SessionAdvisor, SessionAdvisorConfig  # noqa: E402


def test_win_continues():
    a = SessionAdvisor()
    r = a.advise(circle_pnl=3, last_round_pnl=1)
    assert r["action"] == "continue"


def test_loss_exits():
    a = SessionAdvisor()
    r = a.advise(circle_pnl=-2, last_round_pnl=-2)
    assert r["action"] == "rest"
    assert r["rest_hours"] > 0


def test_big_single_round_loss_exits():
    a = SessionAdvisor()
    r = a.advise(circle_pnl=5, last_round_pnl=-12)  # 本圈赢但单局大输
    assert r["action"] == "rest"
    assert "硬止损" in r["reason"]


def test_big_session_loss_exits():
    a = SessionAdvisor()
    r = a.advise(circle_pnl=1, last_round_pnl=0, session_pnl=-15)
    assert r["action"] == "rest"
    assert "硬止损" in r["reason"]


def test_configurable_threshold():
    cfg = SessionAdvisorConfig(big_loss_exit=5.0)
    a = SessionAdvisor(cfg)
    r = a.advise(circle_pnl=2, last_round_pnl=-6)
    assert r["action"] == "rest"
