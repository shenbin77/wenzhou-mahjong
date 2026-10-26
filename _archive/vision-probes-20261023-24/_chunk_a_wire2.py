from pathlib import Path

BOT = Path(r"D:\温州麻将\温州茶（传统麻将）\bot")
path = BOT / "auto_play_loop.py"
text = path.read_text(encoding="utf-8")

# docstring
old_doc = "  * 房卡读数为 0 → 停止；不可读记 unknown，不阻断开发局，但绝不点充值；\n"
if old_doc in text:
    text = text.replace(
        old_doc,
        "  * 牌墙/房卡读数 ≤ 16 → 停止（P0-5）；不可读记 unknown，不阻断开发局，但绝不点充值；\n"
        "  * 启用且本帧财神未知 → 停止（P0-5 ``gate_unknown_caishen``）；\n",
        1,
    )
else:
    print("WARN: docstring card bullet not found")

if "from table_state import" not in text:
    text = text.replace(
        "from turn_gate import (\n",
        "from table_state import WALL_CARDS_STOP_AT, build_table_state\n"
        "from turn_gate import (\n",
        1,
    )

old_cards = (
    "        if self._card_reader is not None:\n"
    "            cards = self._read_card_count(image)\n"
    "            if cards is not None and cards <= 0:\n"
    "                return self._stop(\"card_exhausted\", f\"房卡={cards}，停止开桌\")\n"
)
new_cards = (
    "        wall_cards = None\n"
    "        if self._card_reader is not None:\n"
    "            wall_cards = self._read_card_count(image)\n"
    "            if wall_cards is not None and wall_cards <= WALL_CARDS_STOP_AT:\n"
    "                return self._stop(\n"
    "                    \"card_exhausted\",\n"
    "                    f\"牌墙/房卡={wall_cards}≤{WALL_CARDS_STOP_AT}，停止代打\",\n"
    "                )\n"
)
if old_cards not in text:
    raise SystemExit("card stop block missing")
text = text.replace(old_cards, new_cards, 1)

# After analyze + caishen resolution, before turn_gate: build TableState and unknown-caishen stop
# Insert after base dict is created and before turn_gate evaluate.
anchor = (
    "        base = {\n"
    "            \"ts\": datetime.now().astimezone().isoformat(),\n"
    "            \"round_seq\": self.round.seq, \"frame\": str(frame_path),\n"
    "            \"status\": result.get(\"status\"), \"suggestion\": result.get(\"suggestion\"),\n"
    "            \"suggestion_id\": result.get(\"suggestion_id\"),\n"
    "            \"caishen\": result.get(\"caishen\"),\n"
    "        }\n"
    "        # 门禁三：回合所有权"
)
# Find exact text around base =
idx = text.find('"suggestion_id": result.get("suggestion_id")')
print("anchor idx", idx)
print(repr(text[idx-120:idx+350]))