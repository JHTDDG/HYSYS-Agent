"""反应器类型选择逻辑 —— 考核文档第 8 节规则的代码化。

规则摘要：
1. 提供了动力学参数 -> 动力学反应器：液相默认 CSTR，气相默认 PFR（聚合反应器不适用此规则）。
2. 未提供动力学，但明确给出转化率/收率/选择性 -> Conversion Reactor。
3. 可逆反应、受化学平衡控制，且提供平衡常数 Ka 或吉布斯自由能 -> Equilibrium Reactor。
4. 产物分布未知、燃烧、极高温体系 -> Gibbs Reactor（自由能最小化，无需定义反应式）。
5. 管式+动力学 -> PFR；釜式+动力学 -> CSTR；无动力学时参照规则 2。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Features:
    """从自然语言中提取的反应体系特征。"""
    has_kinetics: bool = False
    phase: str = "unknown"            # gas / liquid / unknown
    equipment: str = "unknown"        # tubular / tank / unknown
    has_conversion: bool = False      # 明确给出转化率/收率/选择性
    conversions: list[float] = field(default_factory=list)
    has_ka: bool = False              # 提供平衡常数 / 吉布斯自由能数值
    reversible: bool = False
    equilibrium_controlled: bool = False
    products_unknown: bool = False
    combustion: bool = False
    polymerization: bool = False


@dataclass
class Decision:
    reactor_type: str                 # conversion / equilibrium / gibbs / cstr / pfr / unknown
    reason: str
    clarifications: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def select_reactor(f: Features) -> Decision:
    notes: list[str] = []
    questions: list[str] = []

    if f.polymerization:
        notes.append("检测到聚合体系：按规则不适用'液相CSTR/气相PFR'默认判断，需按体系特征单独评估。")

    # 规则 1 / 3：动力学体系
    if f.has_kinetics:
        if f.phase == "liquid":
            return Decision("cstr", "用户提供了动力学参数，体系为液相，按选择逻辑默认选全混流反应器（CSTR）。", questions, notes)
        if f.phase == "gas":
            return Decision("pfr", "用户提供了动力学参数，体系为气相，按选择逻辑默认选平推流反应器（PFR）。", questions, notes)
        if f.equipment == "tubular":
            return Decision("pfr", "提供了动力学参数且为管式反应器，选平推流反应器（PFR）。", questions, notes)
        if f.equipment == "tank":
            return Decision("cstr", "提供了动力学参数且为釜式反应器，选全混流反应器（CSTR）。", questions, notes)
        questions.append("已提供动力学参数，但请补充体系相态（液相/气相）或设备形式（管式/釜式），以确定用 CSTR 还是 PFR。")
        return Decision("unknown", "动力学体系，但相态与设备形式未知，无法在 CSTR/PFR 之间判断。", questions, notes)

    # 规则 2：无动力学，但明确给了转化率/收率/选择性
    if f.has_conversion:
        return Decision(
            "conversion",
            "未提供动力学参数，但明确给出了转化率（或收率/选择性）等工艺指标，"
            "按选择逻辑选转化率反应器（Conversion Reactor）：只需在该转化率下求出口浓度分布，无需动力学。",
            questions, notes,
        )

    # 规则 5：产物分布未知 / 燃烧 / 极高温（优先于平衡判断：燃烧虽可逆但按 Gibbs 处理）
    if f.products_unknown or f.combustion:
        return Decision(
            "gibbs",
            "产物分布未知（或为燃烧/极高温体系），选吉布斯反应器（Gibbs Reactor）："
            "基于吉布斯自由能最小化，无需定义具体反应方程，自动求产物分布。",
            questions, notes,
        )

    # 规则 4：可逆 + 平衡控制（含提供 Ka 的情形）
    if f.has_ka or (f.reversible and f.equilibrium_controlled):
        reason = "体系为可逆反应且受化学平衡控制"
        reason += "（用户提供了平衡常数/吉布斯自由能信息）" if f.has_ka else "，产物分布由化学平衡决定"
        reason += "，选平衡反应器（Equilibrium Reactor），以温度/压力为约束计算平衡组成。"
        return Decision("equilibrium", reason, questions, notes)

    if f.reversible:
        questions.append(
            "体系为可逆反应：如可提供平衡常数（Ka/Kp）可选用平衡反应器；"
            "若只关心指定转化率下的出口分布，也可用转化率反应器，请确认。"
        )
        return Decision("unknown", "可逆反应，但缺少平衡常数与转化率信息。", questions, notes)

    questions.append(
        "信息不足以确定反应器类型。请提供以下之一：转化率/收率指标、动力学参数（并说明气相/液相）、"
        "平衡常数（可逆反应），或说明产物分布未知（将用 Gibbs 反应器）。"
    )
    return Decision("unknown", "缺少关键信息，无法确定反应器类型。", questions, notes)
