"""HYSYS COM 对象内省工具：在远程机器上发现真实可用的方法/属性名。

用法：
    python scripts/inspect_hysys.py

对每个关键对象打印"按关键词过滤后的属性列表"，用于核实适配器里
各处多路尝试的写法，并快速定位反应创建/挂接的正确 API。
"""
from __future__ import annotations

import os
import tempfile

import pythoncom
import win32com.client

KEYWORDS = ("reaction", "component", "coeff", "conversion", "stoich", "keq",
            "kvalue", "gibbs", "base", "feed", "product", "tout", "pressure",
            "duty", "energy", "connect", "add", "run", "solve", "converge",
            "temp", "flow", "package", "stream", "operation")

pythoncom.CoInitialize()
try:
    app = win32com.client.gencache.EnsureDispatch("HYSYS.Application")
    print("已连接（EnsureDispatch，类型信息可用）")
except Exception:  # noqa: BLE001
    app = win32com.client.Dispatch("HYSYS.Application")
    print("已连接（Dispatch，动态绑定）")


def dump(obj, label: str, keywords=KEYWORDS):
    print("\n" + "=" * 70)
    print(f"# {label}")
    print("=" * 70)
    try:
        attrs = sorted(set(dir(obj)))
    except Exception as e:  # noqa: BLE001
        print("  dir() 失败:", e)
        return
    hits = [a for a in attrs if any(k in a.lower() for k in keywords)]
    others = [a for a in attrs if a not in hits and not a.startswith("_")]
    for a in hits:
        print("  *", a)
    print("  -- 其他 --")
    print("  ", ", ".join(others[:60]))


hsc = os.path.join(tempfile.gettempdir(), "inspect_case.hsc")
if os.path.exists(hsc):
    os.remove(hsc)
try:
    case = app.SimulationCases.New(hsc)
except Exception:  # noqa: BLE001
    case = app.SimulationCases.Add()

dump(app, "HYSYS.Application")
dump(case, "SimulationCase")
fs = case.Flowsheet
dump(fs, "Flowsheet")

pkg = None
try:
    pkg = fs.FluidPackages.AddItem("Basis-1")
except Exception:  # noqa: BLE001
    try:
        pkg = fs.FluidPackages.Add("Basis-1")
    except Exception as e:  # noqa: BLE001
        print("FluidPackages 创建失败:", e)
if pkg is not None:
    dump(pkg, "FluidPackage")
    try:
        dump(pkg.ComponentList, "ComponentList")
    except Exception as e:  # noqa: BLE001
        print("ComponentList 不可用:", e)

try:
    dump(fs.MaterialStreams, "MaterialStreams 集合")
    s = fs.MaterialStreams.AddItem("S1")
    dump(s, "MaterialStream")
except Exception as e:  # noqa: BLE001
    print("物流创建失败:", e)

try:
    dump(fs.Operations, "Operations 集合")
except Exception as e:  # noqa: BLE001
    print("Operations 不可用:", e)

for holder_name, holder in (("case", case), ("fs", fs)):
    if hasattr(holder, "ReactionManager"):
        try:
            rm = holder.ReactionManager
            dump(rm, f"ReactionManager ({holder_name})")
            try:
                rxn = rm.AddItem("Conversion Reaction")
                dump(rxn, "ConversionReaction")
                dump(rxn, "ConversionReaction(全量)", keywords=())
            except Exception as e:  # noqa: BLE001
                print("Conversion Reaction 创建失败:", e)
            try:
                rxn2 = rm.AddItem("Equilibrium Reaction")
                dump(rxn2, "EquilibriumReaction")
            except Exception as e:  # noqa: BLE001
                print("Equilibrium Reaction 创建失败:", e)
            break
        except Exception as e:  # noqa: BLE001
            print(f"{holder_name}.ReactionManager 不可用:", e)

try:
    solver = fs.Solver
    dump(solver, "Solver")
except Exception as e:  # noqa: BLE001
    print("Solver 不可用:", e)

print("\n提示：把以上输出整段发给 AI 助手，可据此修正 com_hysys.py 中的 API 调用。")
