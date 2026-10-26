"""HYSYS 适配器接口：Mock（本地联调）与 COM（真实 HYSYS）实现同一契约。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..schema import ModelingTask, SimResult


class IHYSYSAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """当前环境下该适配器是否可用。"""

    @abstractmethod
    def run(self, task: ModelingTask, save_path: str | None = None) -> SimResult:
        """执行一次建模任务并返回计算结果。

        save_path 非 None 时求解完成后把案例另存为 .hsc 文件（Mock 实现忽略）。"""
