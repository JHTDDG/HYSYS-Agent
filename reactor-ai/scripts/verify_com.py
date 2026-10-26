"""HYSYS COM 链路验收脚本（已按 HYSYS 15 真实 API 重写）。

覆盖两条从零建模路线：
  1. 转化率反应器（ConversionReactorOp）+ 能量流供热
  2. 吉布斯反应器（GibbsReactorOp）+ 汽/液两相产物

用法（装有 Aspen HYSYS V15 的 Windows 机器）：
    python scripts/verify_com.py

每步打印 [OK]/[FAIL]，末尾汇总。退出码 0 表示全部通过。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

C2K = 273.15
BAR2KPA = 100.0

# 甲烷蒸汽重整 CH4 + H2O(g) -> CO + 3H2 的标准反应热 (kJ/mol)
DELTA_H_RXN = 206.17

COMPONENTS = ["Methane", "H2O", "CO", "H2", "CO2"]

results: list[tuple[str, bool, str]] = []


def step(name: str):
    def deco(fn):
        def wrapped(*args, **kwargs):
            try:
                fn(*args, **kwargs)
                results.append((name, True, ""))
                print(f"[OK]   {name}")
                return True
            except Exception as e:  # noqa: BLE001
                results.append((name, False, f"{type(e).__name__}: {e}"))
                print(f"[FAIL] {name} -> {type(e).__name__}: {e}")
                traceback.print_exc(limit=2)
                return False
        return wrapped
    return deco


def solved(case) -> bool:
    """GetFlowsheetStatus(flag) 返回处于该状态的对象个数。"""
    if case.GetFlowsheetStatus(2):        # NotSolved
        return False
    if case.GetFlowsheetStatus(8):        # UnderSpecified
        return False
    if case.GetFlowsheetStatus(16):       # Error
        return False
    return case.GetFlowsheetStatus(1) > 0  # 至少一个对象 OK


def status_line(case) -> str:
    labels = ((1, "OK"), (2, "NotSolved"), (4, "Warning"),
              (8, "UnderSpecified"), (16, "Error"))
    return ", ".join(f"{lab}={case.GetFlowsheetStatus(flag)}" for flag, lab in labels)


def build_case(app, comp_names: list[str]):
    """新建 case：SRK 物性包 + 组分。返回 (case, basis_manager, fluid_pkg, actual_names)。"""
    case = app.SimulationCases.Add()
    try:
        case.Visible = True
    except Exception:  # noqa: BLE001
        pass
    bm = case.BasisManager
    bm.StartBasisChange()
    fp = bm.FluidPackages.Add("Basis-1")
    comps = fp.ComponentList.Components
    for n in comp_names:
        comps.Add(n)
    fp.PropertyPackageName = "SRK"
    names = list(comps.Names)
    if len(names) != len(comp_names):
        raise RuntimeError(f"组分数量不符：{names}")
    return case, bm, fp, names


def add_feed(fs, names, stream_name="Feed"):
    feed = fs.MaterialStreams.Add(stream_name)
    feed.TemperatureValue = 793.15       # 520 C
    feed.PressureValue = 1350.0          # 13.5 bar
    feed.MolarFlowValue = 1000.0         # kgmole/h
    x = [0.0] * len(names)
    x[names.index("Methane")] = 0.5
    x[names.index("H2O")] = 0.5
    feed.ComponentMolarFraction.SetValues(tuple(x))
    return feed


def read_product(product, names) -> dict:
    d = {
        "T_C": product.TemperatureValue - C2K,
        "P_bar": product.PressureValue / BAR2KPA,
        "F_kgmole_h": product.MolarFlowValue,
        "mass_kg_h": product.MassFlowValue,
        "vap_frac": product.VapourFractionValue,
        "x": dict(zip(names, product.ComponentMolarFraction.GetValues(None))),
    }
    return d


print("=" * 68)
print("HYSYS COM 链路验收")
print("=" * 68)

import pythoncom  # noqa: E402
import win32com.client  # noqa: E402

pythoncom.CoInitialize()
app = None
case1 = None
case2 = None

@step("连接 HYSYS.Application")
def _connect():
    global app
    app = win32com.client.gencache.EnsureDispatch("HYSYS.Application")
    try:
        app.Visible = True
    except Exception:  # noqa: BLE001
        pass
    try:
        print(f"       版本 = {app.Version}")
    except Exception:  # noqa: BLE001
        print("       版本 = (读取失败)")


_connect()
if not results[-1][1]:
    print("无法连接 HYSYS，终止。")
    sys.exit(1)


# ----------------------------------------------------------------------
# 路线 1：转化率反应器 + 供热
# ----------------------------------------------------------------------
state: dict = {}


@step("新建 case + SRK 物性包 + 5 个组分")
def _case1():
    global case1
    case1, bm, fp, names = build_case(app, COMPONENTS)
    state.update(bm=bm, fp=fp, names=names)
    print(f"       组分回读 = {names}")


@step("在 basis change 内创建转化率反应与反应集")
def _reactions():
    bm, fp, names = state["bm"], state["fp"], state["names"]
    rpm = bm.ReactionPackageManager
    rxn = rpm.Reactions.Add("R-1", "conversionrxn")
    # 请求名 -> HYSYS 实际名（如 H2 -> Hydrogen），按添加顺序一一对应
    actual = dict(zip(COMPONENTS, names))
    stoich = {"Methane": -1.0, "H2O": -1.0, "CO": 1.0, "H2": 3.0}
    for comp, v in stoich.items():
        rxn.Reactants.Add(actual[comp]).StoichiometricCoefficientValue = v
    rxn.BaseComponent = fp.ComponentList.Components.Item(names.index("Methane"))
    rxn.Conversion = 90.0            # HYSYS 用百分数
    rxn.ReactionPhase = 0
    rxn.BalanceStoichiometry()
    print(f"       化学计量平衡误差 = {rxn.BalanceErrorValue}")
    rs = rpm.ReactionSets.Add("RS-1")
    rs.ActiveReactions.Add(rxn)
    rs.AssociateFluidPackage(fp)
    state.update(rpm=rpm, rxn=rxn, rs=rs)


@step("结束 basis change")
def _end_basis():
    bm, fp, names = state["bm"], state["fp"], state["names"]
    bm.EndBasisChange()
    state.update(fs=case1.Flowsheet)
    print(f"       物性包 = {fp.PropertyPackageName}")


@step("创建进料物流（520 C / 13.5 bar / 1000 kgmole/h，CH4:H2O=1:1）")
def _feed1():
    fs, names = state["fs"], state["names"]
    feed = add_feed(fs, names)
    state["feed"] = feed
    print(f"       回读 T={feed.TemperatureValue - C2K:.2f} C, "
          f"P={feed.PressureValue / BAR2KPA:.2f} bar, F={feed.MolarFlowValue:.1f}")


@step("创建转化率反应器 + 汽/液产物 + 能量流")
def _reactor1():
    fs, feed, rs, names = state["fs"], state["feed"], state["rs"], state["names"]
    rx = fs.Operations.Add("CRV-100", "ConversionReactorOp")
    rx.ReactionSet = rs
    rx.Feeds.Add(feed)
    vap = fs.MaterialStreams.Add("VapProd")
    liq = fs.MaterialStreams.Add("LiqProd")
    q = fs.EnergyStreams.Add("Q-100")
    reacted = 1000.0 * 0.5 * 0.90          # kmol/h
    q.HeatFlowValue = reacted * 1000 * DELTA_H_RXN   # kJ/h
    rx.VapourProduct = vap
    rx.LiquidProduct = liq
    rx.EnergyStream = q
    state.update(rx=rx, vap=vap, liq=liq, q=q)
    print(f"       供热 duty = {q.HeatFlowValue:.3e} kJ/h")


@step("求解并校验转化率反应器结果")
def _solve1():
    case, rx, vap, names = case1, state["rx"], state["vap"], state["names"]
    time.sleep(3.0)
    print("       " + status_line(case))
    if not solved(case):
        raise RuntimeError("flowsheet 未全部求解")
    d = read_product(vap, names)
    print(f"       VapProd T={d['T_C']:.2f} C, P={d['P_bar']:.2f} bar, "
          f"F={d['F_kgmole_h']:.1f}, vap={d['vap_frac']:.3f}")
    print("       组成 =", {k: round(v, 5) for k, v in d["x"].items()})
    conv = rx.RxnPercentConversionValue
    print(f"       实际转化率 = {conv}")
    if abs(d["F_kgmole_h"] - 1900.0) > 30:
        raise RuntimeError(f"产物流量异常：{d['F_kgmole_h']:.1f} != 1900")
    if abs(conv[0] - 90.0) > 1.0:
        raise RuntimeError(f"转化率异常：{conv}")
    if d["x"].get(names[COMPONENTS.index("H2")], 0.0) < 0.6:
        raise RuntimeError("H2 含量异常，反应未生效")
    state["result1"] = d


# ----------------------------------------------------------------------
# 路线 2：吉布斯反应器（汽/液两相，不接能量流）
# ----------------------------------------------------------------------
state2: dict = {}


@step("新建 case + SRK（吉布斯路线）")
def _case2():
    global case2
    case2, bm, fp, names = build_case(app, COMPONENTS)
    bm.EndBasisChange()
    fs = case2.Flowsheet
    feed = add_feed(fs, names)
    state2.update(case=case2, fs=fs, feed=feed, names=names)


@step("创建吉布斯反应器 + 汽/液产物")
def _reactor2():
    fs, feed = state2["fs"], state2["feed"]
    rx = fs.Operations.Add("GIBBS-100", "GibbsReactorOp")
    rx.Feeds.Add(feed)
    vap = fs.MaterialStreams.Add("GibbsVap")
    liq = fs.MaterialStreams.Add("GibbsLiq")
    rx.VapourProduct = vap
    rx.LiquidProduct = liq
    state2.update(rx=rx, vap=vap)


@step("求解并校验吉布斯反应器结果")
def _solve2():
    case, vap, names = state2["case"], state2["vap"], state2["names"]
    time.sleep(3.0)
    print("       " + status_line(case))
    if not solved(case):
        raise RuntimeError("flowsheet 未全部求解")
    d = read_product(vap, names)
    print(f"       GibbsVap T={d['T_C']:.2f} C, F={d['F_kgmole_h']:.1f}, "
          f"vap={d['vap_frac']:.3f}")
    print("       组成 =", {k: round(v, 5) for k, v in d["x"].items()})
    if d["x"].get("Methane", 1.0) > 0.45:
        raise RuntimeError("甲烷几乎未转化，平衡未生效")
    if d["x"].get(names[COMPONENTS.index("H2")], 0.0) < 0.1:
        raise RuntimeError("H2 含量异常")


# ----------------------------------------------------------------------
# 清理
# ----------------------------------------------------------------------
@step("关闭测试 case")
def _close():
    for c in (case1, case2):
        if c is not None:
            try:
                c.Close(0)
            except Exception:  # noqa: BLE001
                pass


if results[-1][1]:
    if _case1():
        for fn in (_reactions, _end_basis, _feed1, _reactor1, _solve1):
            if not fn():
                break
    if _case2():
        for fn in (_reactor2, _solve2):
            if not fn():
                break
    _close()

print("=" * 68)
ok = sum(1 for _, s, _ in results if s)
print(f"总结：{ok}/{len(results)} 步通过")
for name, s, err in results:
    print(f"  {'PASS' if s else 'FAIL'}  {name}" + (f"  ({err})" if err else ""))
if ok == len(results) and results:
    print("\n全部通过：HYSYS COM 链路可用。")
    print("启动服务：$env:HYSYS_MODE='com'; python -m app.main")
sys.exit(0 if ok == len(results) and results else 1)
