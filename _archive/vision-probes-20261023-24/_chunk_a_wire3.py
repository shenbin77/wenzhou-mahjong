from pathlib import Path

BOT = Path(r"D:\温州麻将\温州茶（传统麻将）\bot")
path = BOT / "auto_play_loop.py"
text = path.read_text(encoding="utf-8")

old_doc = "  * 房卡读数为 0 → 停止；不可读记 unknown，不阻断开发局，但绝不点充值；\n"
if old_doc in text:
    text = text.replace(
        old_doc,
        "  * 牌墙/房卡读数 ≤ 16 → 停止（P0-5）；不可读记 unknown，不阻断开发局，但绝不点充值；\n"
        "  * 启用且本帧财神未知 → 停止（P0-5 ``gate_unknown_caishen``）；\n",
        1,
    )
    print("doc ok")
else:
    print("WARN doc")

if "from table_state import" not in text:
    text = text.replace(
        "from turn_gate import (\n",
        "from table_state import WALL_CARDS_STOP_AT, build_table_state\n"
        "from turn_gate import (\n",
        1,
    )
    print("import ok")

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
    raise SystemExit("card block missing")
text = text.replace(old_cards, new_cards, 1)
print("cards ok")

# Need wall_cards in scope later — currently defined inside tick before analyze.
# Also define wall_cards = None earlier if card_reader is None path... new_cards sets wall_cards = None always. Good.

old_base = (
    "            \"suggestion_id\": result.get(\"suggestion_id\"),\n"
    "            \"caishen\": result.get(\"caishen\"),\n"
    "        }\n"
    "        # 门禁三：回合所有权。dry_run 与真点都必须先过这一关，台账逐帧留证。\n"
    "        verdict = self._evaluate_turn_gate(\n"
    "            result, image, record=not changed_round)\n"
)
if old_base not in text:
    # try find similar
    i = text.index("\"suggestion_id\": result.get(\"suggestion_id\")")
    raise SystemExit("base block missing: " + repr(text[i:i+280]))

new_base = (
    "            \"suggestion_id\": result.get(\"suggestion_id\"),\n"
    "            \"caishen\": result.get(\"caishen\"),\n"
    "        }\n"
    "        # P0-5: refresh unified TableState from THIS frame before decide/discard.\n"
    "        incoming_tile = None\n"
    "        incoming_source = None\n"
    "        if self._discard_state is not None and self._discard_trusted:\n"
    "            incoming_tile = getattr(self._discard_state, \"incoming_tile\", None)\n"
    "            incoming_source = getattr(self._discard_state, \"incoming_source\", None)\n"
    "        table = build_table_state(\n"
    "            result, image,\n"
    "            wall_cards=wall_cards,\n"
    "            discards_trusted=bool(self._discard_trusted),\n"
    "            incoming_tile=incoming_tile,\n"
    "            incoming_source=incoming_source,\n"
    "        )\n"
    "        base[\"table_state\"] = table.ledger()\n"
    "        if self.enabled and table.should_stop_for_unknown_caishen:\n"
    "            return self._stop(\n"
    "                \"gate_unknown_caishen\",\n"
    "                \"本帧财神未知：停止代打（P0-5）\",\n"
    "                base,\n"
    "            )\n"
    "        # 门禁三：回合所有权。dry_run 与真点都必须先过这一关，台账逐帧留证。\n"
    "        verdict = self._evaluate_turn_gate(\n"
    "            result, image, record=not changed_round)\n"
)
text = text.replace(old_base, new_base, 1)
print("table ok")

# wall_cards must exist even when we return early on claim guard path —
# claim guard returns before wall_cards assignment. Move wall_cards = None to start of tick body after image decode.
# Find where image is decoded and ensure wall_cards initialized before action_guard branch.
needle = "        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)\n"
if needle not in text:
    raise SystemExit("image decode missing")
if "wall_cards = None\n        if self._card_reader" in text:
    # also init earlier for claim-path safety when referencing later — claim path returns before wall_cards
    # Only the post-analyze path uses wall_cards variable; claim path doesn't. But we now assign wall_cards
    # only after claim guard. Post-analyze uses wall_cards — OK as long as claim returns first.
    pass

# Problem: wall_cards assigned after claim guard; post-analyze uses it. Good.
# But if _card_reader is None, wall_cards=None from new_cards. Good.

path.write_text(text, encoding="utf-8")
print("wrote auto_play_loop")