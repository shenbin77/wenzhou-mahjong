from tools.gold_standard import is_verified_gold_label


def test_filename_or_agent_label_is_not_visual_gold():
    assert not is_verified_gold_label({
        "reviewer": "agent", "evidence_frame": "", "gold_label": "self_zimo"
    })
    assert not is_verified_gold_label({
        "reviewer": "filename_provisional", "evidence_frame": "42", "gold_label": "self_zimo"
    })


def test_human_label_with_evidence_is_verified():
    assert is_verified_gold_label({
        "reviewer": "human", "evidence_frame": "frames/f_00042.jpg", "gold_label": "draw"
    })
