# -*- coding: utf-8 -*-
"""run_auto_play_dev.py — 连续代打的命令行入口（开发局专用，默认 dry_run）。

用法（默认只读，不点任何东西）::

    python bot/run_auto_play_dev.py --serial emulator-5556 --mode tea_2p \
        --caishen auto --max-actions 20 --duration 600

真点必须**同时**给两个开关，且两个开关在代码里是独立的（缺一个就退回
dry_run，不报错、不猜测）::

    python bot/run_auto_play_dev.py --no-dry-run --enable --max-actions 5

启动前默认打印门禁自检：adb 路径、设备序列号、前台包、财神、房卡。
自检不过直接以非零码退出（2 = 门禁拦截，1 = 参数/启动异常，
130 = 用户 Ctrl+C，0 = 正常跑完并停手）。

安全边界（与 ``auto_play_loop`` 一致，不在本文件放宽）：
  * 默认 ``dry_run``；``--enable`` 与 ``--no-dry-run`` 必须同时出现；
  * 前台包必须是 ``com.xm.zjgamecenter``，否则一帧都不抓就退出；
  * 房卡 0 立即停手，房卡读不到记 unknown（不阻断、绝不点充值）；
  * 连续 3 次异常立即停手；
  * 所有点击都过 ``TapExecutor`` 四道闸（几何白名单 / 点击前重截屏 /
    按牌面重定位 / 点后手牌 −1 验证），本文件不提供任何绕过开关。

**全自动首次实战尚未验证**：明日开发局首跑请先不加 ``--enable``，
用默认 dry_run 跑一把，核对台账里的坐标与建议后再开真点。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from auto_play_loop import DEFAULT_ADB, AutoPlayLoop, GateBlocked  # noqa: E402
from harvest_tea import DEFAULT_SERIAL, EXPECTED_PACKAGE  # noqa: E402

EXIT_OK = 0
EXIT_STARTUP_ERROR = 1
EXIT_GATE_BLOCKED = 2
EXIT_INTERRUPTED = 130

BANNER = """\
======================================================================
 温州茶（传统麻将）· 连续代打 · 开发局模式
 全自动首次实战【尚未验证】；默认 dry_run，只记录不点击。
 真点需要同时给 --no-dry-run --enable，缺一个一律退回 dry_run。
======================================================================"""


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser (all safety-relevant defaults are the safe ones)."""
    parser = argparse.ArgumentParser(
        prog="run_auto_play_dev.py",
        description="温州茶 2 人局连续代打（开发局；默认 dry_run）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--serial", default=DEFAULT_SERIAL,
                        help="模拟器序列号")
    parser.add_argument("--adb", dest="adb_path", default=DEFAULT_ADB,
                        help="adb 可执行文件路径")
    parser.add_argument("--mode", default="tea_2p", choices=["tea_2p"],
                        help="玩法（目前只支持 2 人局）")
    parser.add_argument("--caishen", default="auto",
                        help="财神牌 label（如 w1）或 auto（每帧自识别）")
    parser.add_argument("--enable", action="store_true",
                        help="允许真实点击（必须与 --no-dry-run 同时使用）")
    parser.add_argument("--no-dry-run", dest="no_dry_run", action="store_true",
                        help="关闭 dry_run（必须与 --enable 同时使用）")
    parser.add_argument(
        "--claim-auto-click", action="store_true",
        help="启用吃碰杠胡过执行通道（仍须 --enable + --no-dry-run，吃默认关闭）",
    )
    parser.add_argument("--max-actions", type=int, default=20,
                        help="本次最多出牌次数（dry_run 下为最多记录的计划数）")
    parser.add_argument("--duration", type=float, default=600.0,
                        help="本次最长运行秒数")
    parser.add_argument("--interval", type=float, default=1.2,
                        help="两次周期之间的间隔秒数")
    parser.add_argument("--session", default="",
                        help="指定会话目录（默认 采集/dev_auto_play/<时间戳>）")
    parser.add_argument("--check-only", dest="check_only", action="store_true",
                        help="只打印门禁自检结果，不进入循环")
    return parser


def resolve_caishen(value: str) -> str:
    """``auto``/空串 → 每帧自识别；其余原样作为手动财神 label。"""
    text = (value or "").strip()
    return "" if text.lower() in ("", "auto", "none") else text


def resolve_enabled(args: argparse.Namespace) -> bool:
    """Real tapping needs BOTH switches; anything else stays dry_run."""
    if args.enable and args.no_dry_run:
        return True
    if args.enable or args.no_dry_run:
        missing = "--no-dry-run" if args.enable else "--enable"
        print(f"[WARN] 真点需要同时给 --enable 与 --no-dry-run；"
              f"本次缺少 {missing}，保持 dry_run（不点击）。")
    return False


def print_gate_report(report: dict) -> None:
    """Print the pre-flight self-check in a form a human can eyeball."""
    print("---------------- 门禁自检 ----------------")
    print(f"  adb           : {report.get('adb')}")
    print(f"  adb 存在      : {'是' if report.get('adb_exists') else '否'}")
    print(f"  设备序列号    : {report.get('serial')}")
    print(f"  玩法          : {report.get('mode')}")
    print(f"  前台包        : {report.get('foreground')} "
          f"(要求 {EXPECTED_PACKAGE})")
    cards = report.get("card_count")
    print(f"  房卡读数      : {'未知（不阻断，但绝不点充值）' if cards is None else cards}")
    print(f"  模式          : {'真实点击 enabled=True' if report.get('enabled') else 'dry_run（只记录）'}")
    print("------------------------------------------")


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns a process exit code (see module docstring)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    print(BANNER)

    enabled = resolve_enabled(args)
    if enabled:
        print("[LIVE] 本次会真实点击手牌（单张、点后验证、连续 3 次异常即停）。")
    else:
        print("[DRY-RUN] 只记录建议与坐标，不会发出任何 input tap。")

    try:
        loop = AutoPlayLoop(
            enabled=enabled,
            serial=args.serial,
            adb_path=args.adb_path,
            mode=args.mode,
            caishen_label=resolve_caishen(args.caishen),
            session_dir=args.session or None,
            max_actions=args.max_actions,
            duration_s=args.duration,
            interval_s=args.interval,
            claim_auto_click=args.claim_auto_click,
        )
    except (TypeError, ValueError) as exc:
        print(f"[FAIL] 参数不合法：{exc}")
        return EXIT_STARTUP_ERROR

    try:
        report = loop.check_gates()
    except GateBlocked as exc:
        print(f"[FAIL] 门禁拦截：{exc}")
        return EXIT_GATE_BLOCKED
    except Exception as exc:  # noqa: BLE001 - 启动期任何异常都不进入循环
        print(f"[FAIL] 自检异常：{exc}")
        return EXIT_STARTUP_ERROR
    print_gate_report(report)

    if args.check_only:
        print("[OK] 自检通过；--check-only 指定只自检，不进入循环。")
        return EXIT_OK

    print(f"会话目录: {loop.session_dir}")
    print(f"台账文件: {loop.ledger_path}")
    if not enabled:
        print("提示：本机 dry_run 台账里的 tap 坐标只是“计划”，没有真的点。")

    try:
        summary = loop.run()
    except GateBlocked as exc:
        print(f"[FAIL] 循环中被门禁拦截：{exc}")
        return EXIT_GATE_BLOCKED
    except KeyboardInterrupt:
        loop._stop("interrupted", "用户中断（Ctrl+C）")  # noqa: SLF001 - 打印前留痕
        print("[STOP] 用户中断，已停手。")
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - 未预期异常：停手并回报
        print(f"[FAIL] 循环异常终止：{exc}")
        return EXIT_STARTUP_ERROR

    print("---------------- 运行摘要 ----------------")
    print(f"  出牌/计划次数 : {summary['actions']}")
    print(f"  模式          : {'真实点击' if not summary['dry_run'] else 'dry_run'}")
    print(f"  是否停手      : {'是' if summary['stopped'] else '否'}")
    print(f"  停手原因      : {summary['stop_reason'] or '（未触发停手条件）'}")
    print(f"  连续异常次数  : {summary['consecutive_errors']}")
    print(f"  局数          : {summary['rounds']}")
    print(f"  房卡读数      : {summary['card_count']}")
    print(f"  台账          : {summary['ledger']}")
    print("------------------------------------------")
    print("提醒：全自动首次实战尚未验证；下次仍建议先跑 dry_run 核对台账。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
