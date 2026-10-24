from pathlib import Path
import re

BOT = Path(r"D:\温州麻将\温州茶（传统麻将）\bot")

# ---- turn_gate.py ----
path = BOT / "turn_gate.py"
text = path.read_text(encoding="utf-8")

# Replace stub countdown fields population: after claim read, call detect_countdown
old_claim_tail = (
    "                record[\"claim_decision\"] = state.decision\n"
    "                record[\"claim_reason\"] = str(state.reason)\n"
    "                ratios = (state.evidence or {}).get(\"pixel_ratio\") or {}\n"
    "                record[\"action_gold_pixel_ratio\"] = _as_float(ratios.get(\"gold\"))\n"
)
if old_claim_tail not in text:
    raise SystemExit("claim tail missing")
new_claim_tail = (
    "                record[\"claim_decision\"] = state.decision\n"
    "                record[\"claim_reason\"] = str(state.reason)\n"
    "                ratios = (state.evidence or {}).get(\"pixel_ratio\") or {}\n"
    "                record[\"action_gold_pixel_ratio\"] = _as_float(ratios.get(\"gold\"))\n"
)
text = text.replace(old_claim_tail, new_claim_tail, 1)

# After the image/claim block and before mode_vision try, run countdown detector.
# Find the try mode_vision import after claim block.
marker = (
    "    try:\n"
    "        from mode_vision import DECK_LATCH, DECK_SCORE_MIN, MODE_LATCH\n"
)
if marker not in text:
    raise SystemExit("mode_vision marker missing")
countdown_insert = (
    "    # P0-3: real countdown detector (never leave the permanent unknown stub).\n"
    "    try:\n"
    "        from countdown_vision import detect_countdown\n"
    "        reading = detect_countdown(image if isinstance(image, np.ndarray) else None)\n"
    "        record[\"countdown_status\"] = reading.status\n"
    "        record[\"countdown_seconds\"] = reading.seconds\n"
    "        record[\"countdown_evidence\"] = dict(reading.evidence)\n"
    "    except Exception as exc:  # noqa: BLE001 - detector failure => unreadable\n"
    "        record[\"countdown_status\"] = \"unreadable\"\n"
    "        record[\"countdown_seconds\"] = None\n"
    "        record[\"countdown_evidence\"] = {\"error\": f\"{exc}\"}\n"
    "\n"
    + marker
)
text = text.replace(marker, countdown_insert, 1)

# Strengthen claim-UI gate with countdown urgency
old_gate = (
    "    # P0-3: claim row visible => do not discard; countdown detector still unknown.\n"
    "    if action_ui.get(\"action_discs_seen\"):\n"
    "        return verdict(\n"
    "            STATE_WAIT,\n"
    "            \"行动圆盘可见：本帧先处理吃碰杠胡/过，不出牌\"\n"
    "            + (\"（倒计时未接线，不假设还有时间）\"\n"
    "               if action_ui.get(\"countdown_status\") == \"unknown\" else \"\"),\n"
    "            code=\"gate_claim_ui\")\n"
)
new_gate = (
    "    # P0-3: claim row visible => do not discard.\n"
    "    # Countdown low/expired/unreadable => urgency note (still wait, never tap).\n"
    "    if action_ui.get(\"action_discs_seen\"):\n"
    "        cd = str(action_ui.get(\"countdown_status\") or \"unreadable\")\n"
    "        sec = action_ui.get(\"countdown_seconds\")\n"
    "        if cd in {\"low\", \"expired\", \"unreadable\"}:\n"
    "            detail = f\"倒计时{cd}\" + (f\"={sec}s\" if sec is not None else \"\")\n"
    "            return verdict(\n"
    "                STATE_WAIT,\n"
    "                f\"行动圆盘可见且{detail}：本帧只许弃权/过，不出牌\",\n"
    "                code=\"gate_claim_ui_urgent\")\n"
    "        extra = f\"（倒计时{cd}\" + (f\"={sec}s）\" if sec is not None else \"）\")\n"
    "        return verdict(\n"
    "            STATE_WAIT,\n"
    "            \"行动圆盘可见：本帧先处理吃碰杠胡/过，不出牌\" + extra,\n"
    "            code=\"gate_claim_ui\")\n"
)
if old_gate not in text:
    raise SystemExit("claim gate missing")
text = text.replace(old_gate, new_gate, 1)

# default record still has countdown_status unknown until detector runs — change default to absent
text = text.replace(
    "        \"countdown_status\": \"unknown\",\n"
    "        \"countdown_seconds\": None,\n",
    "        \"countdown_status\": \"absent\",\n"
    "        \"countdown_seconds\": None,\n"
    "        \"countdown_evidence\": {},\n",
    1,
)
path.write_text(text, encoding="utf-8")
print("OK turn_gate")

# ---- auto_play_loop.py ----
path = BOT / "auto_play_loop.py"
text = path.read_text(encoding="utf-8")

# Update docstring bullet about cards
text = text.replace(
    "  * 房卡读数为 0 → 停止；不可读记 unknown，不阻断开发局，但绝不点充值；\n",
    "  * 牌墙/房卡读数 ≤ 16 → 停止（P0-5）；不可读记 unknown，不阻断开发局，但绝不点充值；\n"
    "  * 启用且财神未知 → 停止（P0-5 ``gate_unknown_caishen``）；\n",
    1,
)

# import table_state
if "from table_state import" not in text:
    text = text.replace(
        "from turn_gate import (\n",
        "from table_state import WALL_CARDS_STOP_AT, build_table_state\n"
        "from turn_gate import (\n",
        1,
    )

# Replace card <= 0 stop with <= WALL_CARDS_STOP_AT
old_cards = (
    "        if self._card_reader is not None:\n"
    "            cards = self._read_card_count(image)\n"
    "            if cards is not None and cards <= 0:\n"
    "                return self._stop(\"card_exhausted\", f\"房卡={cards}，停止代打\")\n"
)
# Chinese may differ - find by card_exhausted
idx = text.find('card_exhausted')
if idx < 0:
    raise SystemExit("card_exhausted missing")
# show context
print("card context:", repr(text[idx-180:idx+120]))