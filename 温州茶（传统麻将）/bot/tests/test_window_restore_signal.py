"""Desktop lifecycle requests must reach only the requested UI serial."""
import pytest

from window_restore_signal import (acknowledge_control, consume_control,
                                   consume_restore, request_control,
                                   request_restore, restore_path,
                                   wait_for_ack)


def test_request_is_per_serial_and_consumed_once(tmp_path):
    request_restore("emulator-5554", tmp_path)
    request_restore("emulator-5556", tmp_path)
    assert consume_restore("emulator-5556", tmp_path) is True
    assert consume_restore("emulator-5556", tmp_path) is False
    assert restore_path("emulator-5554", tmp_path).is_file()
    assert consume_restore("emulator-5554", tmp_path) is True


def test_invalid_request_cannot_restore(tmp_path):
    path = restore_path("emulator-5556", tmp_path)
    path.write_text("start_full_auto", encoding="ascii")
    assert consume_restore("emulator-5556", tmp_path) is False
    assert not path.exists()


def test_structured_restore_requires_matching_ack(tmp_path):
    ticket = request_control("emulator-5556", "restore", tmp_path)

    consumed = consume_control("emulator-5556", tmp_path)
    assert consumed == ticket
    assert wait_for_ack(ticket, timeout=0.01) is None

    acknowledge_control(consumed, ok=True, detail="主窗口已恢复")
    ack = wait_for_ack(ticket, timeout=0.1)
    assert ack is not None
    assert ack.ok is True
    assert ack.command == "restore"
    assert ack.request_id == ticket.request_id
    assert ack.detail == "主窗口已恢复"
    assert not ticket.ack_path.exists()


def test_quit_is_a_lifecycle_command_not_a_restore(tmp_path):
    ticket = request_control("emulator-5554", "quit", tmp_path)
    consumed = consume_control("emulator-5554", tmp_path)

    assert consumed == ticket
    assert consumed.command == "quit"
    acknowledge_control(consumed, ok=False, detail="仍有动作正在验证")
    ack = wait_for_ack(ticket, timeout=0.1)
    assert ack is not None
    assert ack.ok is False
    assert ack.detail == "仍有动作正在验证"


def test_old_consumer_does_not_treat_structured_quit_as_restore(tmp_path):
    ticket = request_control("emulator-5556", "quit", tmp_path)

    assert consume_restore("emulator-5556", tmp_path) is False
    assert wait_for_ack(ticket, timeout=0.01) is None


def test_bad_command_is_rejected_before_file_write(tmp_path):
    try:
        request_control("emulator-5556", "start_full_auto", tmp_path)
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("unsafe command was accepted")
    assert not list(tmp_path.iterdir())


def test_structured_request_cannot_overwrite_an_unconsumed_command(tmp_path):
    first = request_control("emulator-5556", "quit", tmp_path)

    with pytest.raises(RuntimeError, match="已有未处理"):
        request_control("emulator-5556", "restore", tmp_path)

    consumed = consume_control("emulator-5556", tmp_path)
    assert consumed == first
    assert consumed.command == "quit"


def test_repeat_legacy_restore_is_idempotent(tmp_path):
    first = request_restore("emulator-5556", tmp_path)
    second = request_restore("emulator-5556", tmp_path)

    assert first == second
    assert consume_restore("emulator-5556", tmp_path) is True
    assert consume_restore("emulator-5556", tmp_path) is False


def test_legacy_restore_never_erases_structured_quit(tmp_path):
    ticket = request_control("emulator-5554", "quit", tmp_path)
    request_restore("emulator-5554", tmp_path)

    consumed = consume_control("emulator-5554", tmp_path)
    assert consumed == ticket
    assert consumed.command == "quit"
