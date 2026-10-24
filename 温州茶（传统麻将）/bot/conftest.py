"""Keep pytest's temporary writes on the project drive.

The local system drive may have no free space, so both pytest's ``tmp_path``
base and Python's generic temporary directory must live under this checkout.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


PYTEST_TEMP = Path(__file__).resolve().parent / ".pytest_tmp" / "system"
PYTEST_TEMP.mkdir(parents=True, exist_ok=True)
for variable in ("TEMP", "TMP", "TMPDIR"):
    os.environ[variable] = str(PYTEST_TEMP)
tempfile.tempdir = str(PYTEST_TEMP)


def pytest_configure(config):
    """测试不得往生产语料库写任何东西。

    事故回放：``TapExecutor(enabled=True)`` 不传 ``session_dir`` 时，
    ``tap_executor.py`` 的缺省值会按 ``SESSION_ROOT/<时间戳>`` 新建目录，
    于是一次 pytest 就在 ``采集/assist_sessions/`` 下凭空产生 4 个“假会话”，
    里面只有合成的 tap_probe 证据（38 个目录、跨 48 分钟字节完全一致）。
    这些假目录混进真局语料，曾经让复核把合成帧误读成 9 次真实出牌成功。

    这里在 pytest 启动时一次性把两个落盘根重定向到 pytest 临时区，
    从此任何测试即使不传 ``session_dir`` 也写不到生产目录。
    """
    import sys

    sandbox = PYTEST_TEMP / "tap_probe_sandbox"
    (sandbox / "assist_sessions").mkdir(parents=True, exist_ok=True)
    (sandbox / "fallback").mkdir(parents=True, exist_ok=True)
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import tap_executor
    except ImportError:  # pragma: no cover - 测试不需要 tap 落盘时不强求
        return
    tap_executor.SESSION_ROOT = sandbox / "assist_sessions"
    tap_executor.FALLBACK_PROBE_ROOT = sandbox / "fallback"
