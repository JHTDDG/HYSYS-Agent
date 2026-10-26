"""统一建模任务数据结构：LLM 解析层输出的就是它，HYSYS 适配层只认它。

这是整个系统"通用能力"的核心约定：
自然语言 -> ModelingTask(JSON) -> 任一 HYSYS 适配器执行 -> SimResult
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

ReactorType = Literal["conversion", "equilibrium", "gibbs", "cstr", "pfr", "unknown"]


def dump_model(obj):
    """pydantic v1/v2 兼容序列化。"""
    return obj.model_dump() if hasattr(obj, "model_dump") else obj.dict()


class Component(BaseModel):
    hysys_name: str                      # 必须是 HYSYS 组分库中的名称，如 Methane / H2O / m-Xylene
    display_name: str = ""               # 中文名，仅用于展示


class FlowSpec(BaseModel):
    value: float
    unit: Literal["kgmole/h", "kg/h", "Nm3/h"] = "kgmole/h"
    basis: Literal["molar", "mass", "volume_std"] = "molar"


class FeedStream(BaseModel):
    name: str = "Feed"
    temperature_C: Optional[float] = None
    pressure_bar: Optional[float] = None
    flow: Optional[FlowSpec] = None
    composition: dict[str, float] = Field(default_factory=dict)  # hysys_name -> 摩尔分数


class Reaction(BaseModel):
    name: str = ""
    equation: str = ""                   # 人类可读式，如 "CH4 + H2O = CO + 3H2"
    kind: Literal["equilibrium", "conversion", "gibbs", "kinetic"] = "equilibrium"
    stoich: dict[str, float] = Field(default_factory=dict)  # hysys_name -> 有符号系数（产物+，反应物-）
    conversion: Optional[float] = None   # 0~1，仅 conversion 反应使用
    basis_component: Optional[str] = None  # 转化率/动力学反应的基准组分
    # ---- 动力学参数（kind="kinetic" 时使用，对应 HYSYS Kinetic Reaction）----
    rate_constant_a: Optional[float] = None    # 指前因子 A（与速率基准配套的量纲）
    activation_energy: Optional[float] = None  # 活化能，单位 kJ/mol（HYSYS 内换算 kJ/kgmole）
    reaction_orders: dict[str, float] = Field(default_factory=dict)  # 组分 -> 正反应级数
    rate_basis: Literal["concentration", "partial_pressure"] = "concentration"


class ReactorSpec(BaseModel):
    type: ReactorType
    name: str = "R-100"
    outlet_temperature_C: Optional[float] = None   # 平衡反应器常用 spec
    pressure_bar: Optional[float] = None
    delta_P_bar: float = 0.0


class ModelingTask(BaseModel):
    """一次反应器模拟任务的完整描述。"""
    title: str
    reactor_type: ReactorType
    selection_reason: str = ""
    fluid_package: str = "Peng-Robinson"
    components: list[Component] = Field(default_factory=list)
    feeds: list[FeedStream] = Field(default_factory=list)
    reactions: list[Reaction] = Field(default_factory=list)
    reactor: ReactorSpec
    assumptions: list[str] = Field(default_factory=list)      # 建模假设，透明呈现给用户
    clarifications: list[str] = Field(default_factory=list)   # 信息不足时向用户提出的问题
    source: Literal["llm", "rules"] = "rules"


class StreamResult(BaseModel):
    name: str
    temperature_C: Optional[float] = None
    pressure_bar: Optional[float] = None
    molar_flow_kgmole_h: Optional[float] = None
    mass_flow_kg_h: Optional[float] = None
    vapor_fraction: Optional[float] = None
    composition: dict[str, float] = Field(default_factory=dict)  # 组分 -> 摩尔分数


class SimResult(BaseModel):
    converged: bool = False
    adapter: str = "mock"                # mock / com
    reactor_name: str = ""
    streams: list[StreamResult] = Field(default_factory=list)
    duty_kw: Optional[float] = None
    conversions: dict[str, float] = Field(default_factory=dict)
    messages: list[str] = Field(default_factory=list)
    case_file: Optional[str] = None      # 已保存的 HYSYS 案例 .hsc 路径（未保存为 None）
