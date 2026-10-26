"""Mock 适配器：本地无 HYSYS 时的联调实现。

- conversion 反应：按转化率做化学计量延拓（数学上精确）；
- equilibrium 反应：用温度相关的粗略转化率估计（仅用于界面联调，非热力学结果）；
所有结果都带明确的 [Mock] 标记，避免误当成真实模拟值。
"""
from __future__ import annotations

from ..component_map import MOLAR_MASS
from ..schema import ModelingTask, SimResult, StreamResult
from .base import IHYSYSAdapter


class MockAdapter(IHYSYSAdapter):
    name = "mock"

    def available(self) -> bool:
        return True

    # ------------------------------------------------------------------
    def run(self, task: ModelingTask, save_path: str | None = None) -> SimResult:
        # save_path 忽略：Mock 没有 HYSYS 案例，不产出 .hsc 文件
        # 1. 汇总进料（统一折算成 kgmole/h）
        moles: dict[str, float] = {}
        for feed in task.feeds:
            if feed.flow is None:
                continue
            if feed.flow.basis == "mass":
                # 质量流量 -> 摩尔流量（按主要组分的分子量折算）
                fracs = feed.composition or {c.hysys_name: 1.0 for c in task.components[:1]}
                comp = max(fracs, key=lambda k: fracs[k]) if fracs else ""
                mw = MOLAR_MASS.get(comp, 100.0)
                total_kmol = feed.flow.value / mw
            elif feed.flow.basis == "volume_std":
                total_kmol = feed.flow.value / 23.645  # HYSYS 标准摩尔体积 m3/kmol
            else:
                total_kmol = feed.flow.value
            fracs = feed.composition or {}
            if not fracs:
                fracs = {c.hysys_name: 1.0 for c in task.components[:1]}
            s = sum(fracs.values()) or 1.0
            for comp, frac in fracs.items():
                moles[comp] = moles.get(comp, 0.0) + total_kmol * frac / s

        conversions: dict[str, float] = {}
        messages: list[str] = ["[Mock] 本结果为本地联调估算数据，并非真实 HYSYS 计算结果。"]

        # 2. 按反应类型延拓
        if task.reactor_type == "conversion" or any(r.kind == "conversion" for r in task.reactions):
            # HYSYS 语义：每个反应的转化率基于反应器入口的基准组分流量
            inlet_snapshot = {r.name or r.equation: dict(moles) for r in task.reactions}
            for r in task.reactions:
                conv = r.conversion if r.conversion is not None else 0.0
                basis = r.basis_component or next(
                    (k for k, v in r.stoich.items() if v < 0), None)
                if not basis:
                    continue
                inlet = inlet_snapshot[r.name or r.equation]
                if basis not in inlet:
                    continue
                coeff = abs(r.stoich[basis])
                extent = conv * inlet[basis] / coeff
                for comp, coef in r.stoich.items():
                    moles[comp] = moles.get(comp, 0.0) + coef * extent
                conversions[r.name or r.equation] = conv
        elif task.reactor_type == "equilibrium":
            tout = task.reactor.outlet_temperature_C or 700.0
            for r in task.reactions:
                if {"Methane", "H2O", "CO", "H2"} <= set(r.stoich):
                    conv = min(0.30 + max(0.0, tout - 600.0) * 0.0035, 0.85)
                    extent = conv * moles.get("Methane", 0.0)
                    for comp, coef in r.stoich.items():
                        moles[comp] = moles.get(comp, 0.0) + coef * extent
                    conversions[r.name or r.equation] = conv
                elif {"CO", "H2O", "CO2", "H2"} <= set(r.stoich):
                    conv = 0.70
                    extent = conv * moles.get("CO", 0.0)
                    for comp, coef in r.stoich.items():
                        moles[comp] = moles.get(comp, 0.0) + coef * extent
                    conversions[r.name or r.equation] = conv
                else:
                    extent = 0.5 * moles.get(
                        next((k for k, v in r.stoich.items() if v < 0), ""), 0.0)
                    for comp, coef in r.stoich.items():
                        moles[comp] = moles.get(comp, 0.0) + coef * extent
                    conversions[r.name or r.equation] = 0.5
            messages.append(f"[Mock] 平衡转化率为温度插值的粗估（出口 {tout:.0f} °C），仅示意分布趋势。")
        else:  # gibbs / cstr / pfr 占位
            for r in task.reactions:
                basis = next((k for k, v in r.stoich.items() if v < 0), None)
                if basis and basis in moles:
                    extent = 0.8 * moles[basis] / abs(r.stoich[basis])
                    for comp, coef in r.stoich.items():
                        moles[comp] = moles.get(comp, 0.0) + coef * extent

        # 3. 组装出口流股
        total = sum(max(v, 0.0) for v in moles.values()) or 1.0
        comp_frac = {k: max(v, 0.0) / total for k, v in moles.items() if max(v, 0.0) > 1e-12}
        tout = task.reactor.outlet_temperature_C or max(
            (f.temperature_C or 25.0) for f in task.feeds) if task.feeds else 25.0
        p_out = task.reactor.pressure_bar or (task.feeds[0].pressure_bar if task.feeds else 1.0)
        mass_flow = sum(n * MOLAR_MASS.get(k, 100.0) for k, n in moles.items())

        streams = [StreamResult(
            name=f.name, temperature_C=f.temperature_C, pressure_bar=f.pressure_bar,
            molar_flow_kgmole_h=(f.flow.value if f.flow and f.flow.basis == "molar" else None),
            mass_flow_kg_h=(f.flow.value if f.flow and f.flow.basis == "mass" else None),
            composition=f.composition,
        ) for f in task.feeds]
        streams.append(StreamResult(
            name="Product", temperature_C=tout, pressure_bar=p_out,
            molar_flow_kgmole_h=total, mass_flow_kg_h=mass_flow,
            composition=dict(sorted(comp_frac.items(), key=lambda kv: -kv[1])),
        ))

        return SimResult(
            converged=True, adapter=self.name, reactor_name=task.reactor.name,
            streams=streams, conversions=conversions, messages=messages,
        )
