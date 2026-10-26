"""反应式硬校验：元素守恒检查。

设计原则：校验由确定性代码完成，不依赖 LLM 自查。
- 守恒          -> (True, [])
- 不守恒        -> (False, [不平衡元素...])   调用方应拒绝该反应并提示澄清
- 无法校验      -> (None, [])                组分缺元素数据（未知物），跳过校验
"""
from __future__ import annotations

from .component_map import ELEMENT_COMPOSITION


def check_balance(stoich: dict[str, float]) -> tuple[bool | None, list[str]]:
    """对有符号化学计量系数做元素守恒检查。系数：产物为正、反应物为负。"""
    totals: dict[str, float] = {}
    for comp, coeff in stoich.items():
        elem = ELEMENT_COMPOSITION.get(comp)
        if elem is None:
            return None, []  # 存在未知组分，放弃校验（HYSYS 侧会再验证组分是否存在）
        for el, n in elem.items():
            totals[el] = totals.get(el, 0.0) + coeff * n
    unbalanced = sorted(el for el, v in totals.items() if abs(v) > 1e-6)
    return (not unbalanced), unbalanced
