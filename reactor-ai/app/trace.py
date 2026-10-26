"""推理过程终端输出。

服务运行时把「自然语言 -> 解析 -> 校验 -> 反应器选择 -> HYSYS 求解」
流水线的每一步实时打印到终端，便于演示与排障。
设 REACTOR_AI_QUIET=1 可静默（如跑冒烟测试时）。
"""
from __future__ import annotations

import os
import time

_T0 = time.time()
_QUIET: bool | None = None


def _quiet() -> bool:
    global _QUIET
    if _QUIET is None:
        _QUIET = os.getenv("REACTOR_AI_QUIET", "").strip() == "1"
    return _QUIET


def _p(prefix: str, msg: str) -> None:
    if _quiet():
        return
    print(f"[{time.time() - _T0:7.2f}s] {prefix} {msg}", flush=True)


def stage(name: str) -> None:
    _p("══════", f"◆ {name}")


def info(msg: str) -> None:
    _p("·", msg)


def ok(msg: str) -> None:
    _p("√", msg)


def warn(msg: str) -> None:
    _p("!", msg)


def err(msg: str) -> None:
    _p("×", msg)
