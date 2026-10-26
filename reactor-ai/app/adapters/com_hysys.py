"""真实 Aspen HYSYS COM 适配器（仅在安装 HYSYS 的 Windows 机器上可用）。

已在 Aspen HYSYS V15 (41.0) 上验证的建模配方：
  1. 新建 case -> BasisManager -> StartBasisChange
  2. 先赋物性包 SRK（Peng-Robinson 在 COM 新建 case 上会报 E_INVALIDARG，回退 SRK）。
     顺序不能反：若先加组分再赋包，HYSYS 在赋包校验时会对"不推荐"组合
     （乙酸/酯/醇等 + SRK）弹模态警告，赋包调用直接 com_error 失败
     （真机 11 用例二分验证，见 _ModalWatchdog）
  3. 组分逐个加入，回读实际名（如 H2 -> Hydrogen）
  4. basis change 内创建反应（conversionrxn / equilibriumrxn）与反应集
  5. 物流直接写 TemperatureValue(K) / PressureValue(kPa) / MolarFlowValue(kgmole/h)
  6. 反应器必须在 basis change 结束后创建，同时挂 VapourProduct + LiquidProduct
  7. 无 Run() 方法：写完全部规格后交互式求解自动触发，轮询 Solver.IsSolving
  8. 指定出口温度时，用能量流 duty 迭代逼近（反应器本身没有温度 spec 属性）

COM 层单位约定：温度 K、压力 kPa、摩尔流量 kgmole/h、质量流量 kg/h。
"""
from __future__ import annotations

import threading
import time

from ..schema import ModelingTask, SimResult, StreamResult
from .base import IHYSYSAdapter

C2K = 273.15
BAR2KPA = 100.0
NM3_PER_KMOL = 23.645


class _ModalWatchdog:
    """后台线程：自动点掉 HYSYS 模态警告弹窗，防止 COM 调用被卡死。

    HYSYS 对"不推荐"的组分/物性包组合（如乙酸+SRK）会弹形如
    "Component AceticAcid is being added but is not recommended with Fluid
    Package Basis-1." 的模态警告。模态循环期间 COM 调用要么直接 com_error，
    要么长时间挂死（真机复现）。守护线程枚举 #32770 对话框、点默认按钮，
    并记录弹窗文字供结果消息透出。"""

    def __init__(self, interval: float = 0.25):
        self._interval = interval
        self._texts: list[str] = []
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def log_lines(self) -> list[str]:
        return [f"[WARN] HYSYS 弹窗已自动关闭：{t}" for t in self._texts]

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        try:
            import ctypes
            import ctypes.wintypes as wt
        except Exception:  # noqa: BLE001
            return
        user32 = ctypes.windll.user32
        while not self._stop.is_set():
            try:
                self._sweep(user32, wt)
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self._interval)

    def _sweep(self, user32, wt) -> None:
        import ctypes

        dialogs: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def enum_top(h, l):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, cls, 256)
            if cls.value == "#32770":
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(h, title, 256)
                if "HYSYS" in title.value:
                    dialogs.append(h)
            return True

        user32.EnumWindows(enum_top, 0)
        for hwnd in dialogs:
            texts: list[str] = []
            buttons: list[int] = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            def enum_child(h, l):
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(h, cls, 256)
                if cls.value.lower() == "static":
                    txt = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(h, txt, 512)
                    if txt.value:
                        texts.append(txt.value)
                elif cls.value.lower() == "button":
                    buttons.append(h)
                return True

            user32.EnumChildWindows(hwnd, enum_child, 0)
            for t in texts:
                if t not in self._seen:
                    self._seen.add(t)
                    self._texts.append(t)
            if buttons:
                user32.SendMessageW(buttons[0], 0x00F5, 0, 0)  # BM_CLICK 默认按钮


# 反应器类型 -> HYSYS 操作 TypeToken
REACTOR_OPS = {
    "conversion": "ConversionReactorOp",
    "equilibrium": "EquilibriumReactorOp",
    "gibbs": "GibbsReactorOp",
    "pfr": "PFReactorOp",          # 注意：V15 COM 上动力学反应（kineticrxn）无法挂载到
    "cstr": "ConversionReactorOp", # 任何反应器——rx.ReactionSet=rs 静默不生效（回读
    #                               com_error），跨组分/物性包/相态/是否 Balance 全部
    #                               复现（真机 W8-W11 定论），GUI 手工可解，属 COM
    #                               侧限制。故 PFR 挂动力学反应集、CSTR（转化率反应
    #                               器近似全混釜）挂动力学反应集均无法真实求解，
    #                               调用方应对 pfr/cstr 失败做 Mock 降级并明确标注
}

# 求解后 FlowsheetObjStatus 查询位（GetFlowsheetStatus 返回对象个数）
ST_OK = 1
ST_NOT_SOLVED = 2
ST_UNDER_SPECIFIED = 8
ST_ERROR = 16


class ComHysysAdapter(IHYSYSAdapter):
    name = "com"

    def __init__(self):
        self._app = None
        self._log: list[str] = []

    # ------------------------------------------------------------------
    def available(self) -> bool:
        try:
            self._ensure_app()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _ensure_app(self):
        if self._app is not None:
            try:
                _ = self._app.Visible   # 心跳：HYSYS 进程被杀后缓存引用会 RPC 失败
                return self._app
            except Exception:  # noqa: BLE001
                self._app = None        # 缓存引用已死，重新连接或拉起新实例
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            self._app = win32com.client.gencache.EnsureDispatch("HYSYS.Application")
        except Exception:  # noqa: BLE001
            self._app = win32com.client.Dispatch("HYSYS.Application")
        try:
            self._app.Visible = True
        except Exception:  # noqa: BLE001
            pass
        return self._app

    # ------------------------------------------------------------------
    def run(self, task: ModelingTask, save_path: str | None = None) -> SimResult:
        self._log = []
        self._watchdog = _ModalWatchdog()
        case = None
        try:
            self._watchdog.start()
            case = self._ensure_app().SimulationCases.Add()
            try:
                case.Visible = True
            except Exception:  # noqa: BLE001
                pass
            result = self._build_and_solve(case, task)
            if save_path:
                # 在 Close 前另存 .hsc（求解后、读结果后均可，case 仍是活动状态）
                try:
                    case.SaveAs(save_path)
                    result.case_file = save_path
                except Exception as e:  # noqa: BLE001
                    result.messages.append(f"[WARN] HYSYS 案例保存失败（{e}）：{save_path}")
            return result
        finally:
            if case is not None:
                try:
                    case.Close(0)
                except Exception:  # noqa: BLE001
                    pass
            self._watchdog.stop()

    # ------------------------------------------------------------------
    def _build_and_solve(self, case, task: ModelingTask) -> SimResult:
        log = self._log
        bm = case.BasisManager

        # ---- 物性包 + 组分 ------------------------------------------------
        # 组分表取 task.components ∪ 进料组成 ∪ 反应计量，避免解析层遗漏组分
        comp_names: list[str] = []
        for name in ([c.hysys_name for c in task.components]
                     + [k for f in task.feeds for k in (f.composition or {})]
                     + [k for r in task.reactions for k in r.stoich]):
            if name not in comp_names:
                comp_names.append(name)

        bm.StartBasisChange()
        fp = bm.FluidPackages.Add("Basis-1")
        # 顺序必须是"先赋物性包、后加组分"：反过来会在赋包校验时对"不推荐"
        # 组合（乙酸/酯/醇 + SRK）弹模态警告，赋包调用直接 com_error 失败
        # （真机 11 用例二分验证）。先赋空包，加组分时出现的警告由守护线程点掉
        if task.fluid_package in ("Peng-Robinson", "PR", ""):
            fp.PropertyPackageName = "SRK"
            log.append("[WARN] COM 新建 case 不支持 Peng-Robinson，已使用 SRK")
        else:
            try:
                fp.PropertyPackageName = task.fluid_package
            except Exception:  # noqa: BLE001
                fp.PropertyPackageName = "SRK"
                log.append(f"[WARN] 物性包 {task.fluid_package} 不可用，已回退 SRK")
        comps = fp.ComponentList.Components
        for name in comp_names:
            comps.Add(name)
        actual_names = list(comps.Names)          # HYSYS 实际组分名
        req_to_actual: dict[str, str] = dict(zip(comp_names, actual_names))
        actual_to_req = {v: k for k, v in req_to_actual.items()}

        # ---- 反应 ---------------------------------------------------------
        # Gibbs 反应器基于自由能最小化，不需要（也不接受）反应定义；
        # LLM 可能给 gibbs 任务附带任意 kind 的反应，一律跳过创建。
        reaction_set = None
        conv_rxn_names: list[str] = []
        kin_rxn_names: list[str] = []
        if task.reactions and task.reactor.type != "gibbs":
            rpm = bm.ReactionPackageManager
            created: list[tuple[object, object]] = []
            for i, r in enumerate(task.reactions, 1):
                if r.kind == "gibbs":
                    continue                     # Gibbs 反应器不需要反应定义
                token = {"conversion": "conversionrxn",
                         "equilibrium": "equilibriumrxn",
                         "kinetic": "kineticrxn"}.get(r.kind)
                if token is None:
                    raise RuntimeError(f"反应 {r.name or i} 的类型 {r.kind} 无法在 COM 路线创建")
                rxn = rpm.Reactions.Add(f"R-{i}", token)
                for comp, coef in r.stoich.items():
                    actual = req_to_actual.get(comp)
                    if actual is None:
                        raise RuntimeError(f"反应 {r.name or i} 的组分 {comp} 不在组分表中")
                    rxn.Reactants.Add(actual).StoichiometricCoefficientValue = coef
                # 反应相态：0=气相、1=液相。CSTR 任务按选择逻辑必然是液相动力学
                # （动力学+液相才选 CSTR），液相进料配气相反应会导致流程无法求解
                rxn.ReactionPhase = 1 if task.reactor.type == "cstr" else 0
                if r.kind == "conversion":
                    base = r.basis_component or next(
                        (c for c, v in r.stoich.items() if v < 0), None)
                    if base is not None:
                        rxn.BaseComponent = comps.Item(
                            actual_names.index(req_to_actual[base]))
                    conv = r.conversion if r.conversion is not None else 0.0
                    rxn.Conversion = conv * 100.0 if conv <= 1.0 else conv
                    conv_rxn_names.append(r.name or f"R-{i}")
                elif r.kind == "kinetic":
                    # HYSYS 动力学反应：Basis 3=摩尔浓度(kgmole/m3)、2=分压(kPa)
                    rxn.Basis = 2 if r.rate_basis == "partial_pressure" else 3
                    if r.rate_constant_a is not None:
                        rxn.ForwardFrequencyFactor = float(r.rate_constant_a)
                    if r.activation_energy is not None:
                        # 解析层统一 kJ/mol，HYSYS 用 kJ/kgmole
                        rxn.ForwardActivationEnergy = float(r.activation_energy) * 1000.0
                    base = r.basis_component or next(
                        (c for c, v in r.stoich.items() if v < 0), None)
                    if base is not None:
                        rxn.BaseComponent = comps.Item(
                            actual_names.index(req_to_actual[base]))
                    orders = r.reaction_orders or {
                        c: abs(v) for c, v in r.stoich.items() if v < 0}
                    # 级数向量按组分表顺序整体写入（RealFlexVariable.Values）
                    rxn.ComponentForwardOrder.Values = tuple(
                        float(orders.get(req, 0.0)) for req in comp_names)
                    rxn.ComponentReverseOrder.Values = tuple(0.0 for _ in comp_names)
                    try:
                        rxn.ReverseFrequencyFactor = 0.0   # 不可逆
                    except Exception:  # noqa: BLE001
                        pass
                    kin_rxn_names.append(r.name or f"R-{i}")
                rxn.BalanceStoichiometry()
                created.append((rxn, r))
            rs = rpm.ReactionSets.Add("RS-1")
            for rxn, _ in created:
                rs.ActiveReactions.Add(rxn)
            rs.AssociateFluidPackage(fp)
            reaction_set = rs
        bm.EndBasisChange()
        fs = case.Flowsheet

        # ---- 进料 ---------------------------------------------------------
        feeds = []
        for f in task.feeds:
            s = fs.MaterialStreams.Add(f.name)
            if f.temperature_C is not None:
                s.TemperatureValue = f.temperature_C + C2K
            if f.pressure_bar is not None:
                s.PressureValue = f.pressure_bar * BAR2KPA
            if f.flow is not None:
                if f.flow.basis == "mass":
                    s.MassFlowValue = f.flow.value
                elif f.flow.basis == "volume_std":
                    s.MolarFlowValue = f.flow.value / NM3_PER_KMOL
                else:
                    s.MolarFlowValue = f.flow.value
            if f.composition:
                x = [0.0] * len(actual_names)
                for comp, frac in f.composition.items():
                    actual = req_to_actual.get(comp)
                    if actual is not None:
                        x[actual_names.index(actual)] = frac
                s.ComponentMolarFraction.SetValues(tuple(x))
            feeds.append(s)

        # ---- 反应器 -------------------------------------------------------
        op_token = REACTOR_OPS.get(task.reactor.type)
        if op_token is None:
            raise RuntimeError(
                f"COM 路线暂不支持 {task.reactor.type} 反应器"
                "（仅 conversion / equilibrium / gibbs / pfr / cstr）")
        rx = fs.Operations.Add(task.reactor.name, op_token)
        if reaction_set is not None:
            rx.ReactionSet = reaction_set
        for s in feeds:
            rx.Feeds.Add(s)
        is_pfr = task.reactor.type == "pfr"
        if is_pfr:
            prod = fs.MaterialStreams.Add(f"{task.reactor.name}-Prod")
            rx.Product = prod
            vap = liq = prod
            rx.NumberOfTubes = 1
            rx.TubeLengthValue = 6.0          # m（默认几何，后续可从任务扩展）
            rx.TubeDiameterValue = 0.1
            rx.PressureDropValue = task.reactor.delta_P_bar * BAR2KPA
            rx.HeatTransfer = 0               # 绝热
            try:
                rx.NumberOfSegments = 20
            except Exception:  # noqa: BLE001
                pass
        else:
            vap = fs.MaterialStreams.Add(f"{task.reactor.name}-Vap")
            liq = fs.MaterialStreams.Add(f"{task.reactor.name}-Liq")
            rx.VapourProduct = vap
            rx.LiquidProduct = liq
            if task.reactor.type == "cstr" and kin_rxn_names:
                # 转化率反应器挂动力学反应集时必须指定容积（速率=k·C^order·V），
                # 否则整个流程 UnderSpecified（真机复现）；任务未给容积按 2 m³ 假设
                try:
                    rx.VolumeValue = 2.0
                    log.append("[WARN] 任务未指定反应器容积，动力学计算按 2.0 m³ 假设")
                except Exception as e:  # noqa: BLE001
                    log.append(f"[WARN] 反应器容积写入失败：{e}")
        try:
            rx.ActAsSeparatorWhenCannotSolve = False   # 不可解时报错而不是静默直通
        except Exception:  # noqa: BLE001
            pass
        if task.reactor.delta_P_bar and not is_pfr:
            rx.PressureDropValue = task.reactor.delta_P_bar * BAR2KPA

        # ---- 求解 ---------------------------------------------------------
        target_t = task.reactor.outlet_temperature_C
        if target_t is not None and is_pfr:
            log.append(f"[WARN] COM PFR 按绝热模式求解，暂不支持指定出口温度，"
                       f"已忽略 {target_t:.0f}°C")
            target_t = None
        # HYSYS 转化率/平衡/Gibbs 反应器在 GUI 上要求必须挂能量流，不挂会
        # UnderSpecified（真机复现）：一律创建，绝热工况显式写 duty=0
        energy = fs.EnergyStreams.Add(f"{task.reactor.name}-Q")
        rx.EnergyStream = energy
        if target_t is not None:
            duty = self._tune_duty(case, energy, vap, target_t)
            if duty is None:
                if self._is_solved(case):
                    # _tune_duty 已恢复到最接近目标的可解负荷
                    log.append(f"[WARN] 出口温度 {target_t:.1f}°C 未能精确达到，"
                               f"已按最接近的可解工况输出（出口温度以结果为准）")
                else:
                    # 连一个可解负荷都没找到：摘除能量流按绝热重解
                    # （HYSYS COM 不接受 EnergyStream=None，失败则保留能量流如实报错）
                    detached = False

                    def _detach():
                        nonlocal detached
                        try:
                            rx.EnergyStream = None
                            detached = True
                        except Exception as e:  # noqa: BLE001
                            log.append(f"[WARN] 摘除能量流失败（{e}）")

                    self._gated_write(case, _detach)
                    if detached:
                        energy = None
                    self._wait_solved(case)
                    log.append(f"[WARN] 出口温度 {target_t:.1f}°C 的 duty 迭代未收敛，按绝热结果输出")
            else:
                log.append(f"[INFO] 为达到出口 {target_t:.1f}°C，热负荷 = {duty:.4e} kJ/h")
        else:
            self._gated_write(case, lambda: setattr(energy, "HeatFlowValue", 0.0))
            self._wait_solved(case)

        if not self._is_solved(case):
            tail = "；".join(self._log[-4:])
            raise RuntimeError(
                "HYSYS 未收敛（NotSolved=%d, UnderSpecified=%d, Error=%d）%s"
                % (case.GetFlowsheetStatus(ST_NOT_SOLVED),
                   case.GetFlowsheetStatus(ST_UNDER_SPECIFIED),
                   case.GetFlowsheetStatus(ST_ERROR),
                   f"；日志：{tail}" if tail else ""))

        return self._read_result(task, rx, vap, liq, actual_names, actual_to_req,
                                 req_to_actual, conv_rxn_names, kin_rxn_names,
                                 feeds, energy)

    # ------------------------------------------------------------------
    def _wait_solved(self, case, timeout: float = 20.0) -> bool:
        """等待求解完成。

        先用 CanSolve 拉低再抬高强制触发一次完整求解：刚写入规格后求解器可能
        尚未启动，此时 NotSolved=0 是过期状态，直接轮询会把"未开始"误读成
        "已收敛"（P1 重整 duty=0 时实测踩过该坑）。"""
        try:
            case.Solver.CanSolve = False
            case.Solver.CanSolve = True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                solving = bool(case.Solver.IsSolving)
            except Exception:  # noqa: BLE001
                solving = False
            if not solving and case.GetFlowsheetStatus(ST_NOT_SOLVED) == 0:
                return True
            time.sleep(0.4)
        return self._is_solved(case)

    @staticmethod
    def _is_solved(case) -> bool:
        return (case.GetFlowsheetStatus(ST_NOT_SOLVED) == 0
                and case.GetFlowsheetStatus(ST_UNDER_SPECIFIED) == 0
                and case.GetFlowsheetStatus(ST_ERROR) == 0
                and case.GetFlowsheetStatus(ST_OK) > 0)

    @staticmethod
    def _gated_write(case, write_fn):
        """暂停求解器后写入规格再恢复，避免写入被进行中的求解吞掉。"""
        try:
            case.Solver.CanSolve = False
        except Exception:  # noqa: BLE001
            pass
        try:
            write_fn()
        finally:
            try:
                case.Solver.CanSolve = True
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    def _tune_duty(self, case, energy, vap, target_c: float) -> float | None:
        """用能量流 duty 迭代逼近目标出口温度（反应器没有温度 spec 属性）。

        V15 真机扫描结论：duty→T_out 曲线光滑单调、各负荷基本秒解，但绝热点
        duty=0 本身可能长期不收敛（如蒸汽重整落在两相产物区），因此起点避开 0；
        割线法迭代，负荷上限 1e12 以适配大进料量工况（所需 duty 随流量线性放大）；
        失败时恢复到最接近目标且已解的负荷，保证 case 留在可读结果的状态。"""
        def evaluate(duty: float):
            self._gated_write(case, lambda: setattr(energy, "HeatFlowValue", duty))
            if not self._wait_solved(case):
                return None
            return float(vap.TemperatureValue) - C2K

        # 绝热点 duty=0 在部分体系不可解，从小的正负荷起步，不可解时换挡重试
        d_prev = t_prev = None
        for d0 in (1.0e6, 1.0e7, -1.0e7, 1.0e8):
            t0 = evaluate(d0)
            if t0 is not None:
                d_prev, t_prev = d0, t0
                break
        if t_prev is None:
            return None
        if abs(t_prev - target_c) < 1.0:
            return d_prev

        best_d, best_gap = d_prev, abs(t_prev - target_c)
        duty = d_prev + (target_c - t_prev) * 3.0e5   # 经验初值 kJ/h per °C
        for _ in range(20):
            duty = max(-1.0e12, min(1.0e12, duty))
            if abs(duty) < 1.0e5:                     # 避开不可解的绝热附近
                duty = 1.0e5 if duty >= 0.0 else -1.0e5
            t = evaluate(duty)
            if t is None:
                # 该负荷不可解：向最后一个可解点方向折半回退
                t = None
                back = 0.5 * (d_prev + duty)
                for _ in range(3):
                    if abs(back - d_prev) < 1.0:
                        break
                    t = evaluate(back)
                    if t is not None:
                        duty = back
                        break
                    back = 0.5 * (d_prev + back)
                if t is None:
                    break
            gap = abs(t - target_c)
            if gap < best_gap:
                best_d, best_gap = duty, gap
            if gap < 1.0:
                return duty
            if duty == d_prev or t == t_prev:
                break
            slope = (t - t_prev) / (duty - d_prev)
            d_prev, t_prev = duty, t
            duty = duty + (target_c - t) / slope
        # 未达目标：恢复到最接近目标的已解负荷，让 case 处于可读结果的状态
        evaluate(best_d)
        return best_d if best_gap < 2.0 else None

    # ------------------------------------------------------------------
    def _read_result(self, task, rx, vap, liq, actual_names, actual_to_req,
                     req_to_actual, conv_rxn_names, kin_rxn_names,
                     feed_streams, energy) -> SimResult:
        def read_stream(s, default_name: str) -> StreamResult | None:
            try:
                molar = float(s.MolarFlowValue)
            except Exception:  # noqa: BLE001
                return None
            if molar <= 1e-9:
                return None
            comp_frac: dict[str, float] = {}
            try:
                vals = s.ComponentMolarFraction.GetValues(None)
                for actual, v in zip(actual_to_req, vals):
                    if float(v) > 1e-9:
                        comp_frac[actual_to_req[actual]] = float(v)
            except Exception:  # noqa: BLE001
                pass
            name = default_name
            for attr in ("TaggedName", "name"):
                try:
                    name = str(getattr(s, attr))
                    break
                except Exception:  # noqa: BLE001
                    continue
            return StreamResult(
                name=name,
                temperature_C=float(s.TemperatureValue) - C2K,
                pressure_bar=float(s.PressureValue) / BAR2KPA,
                molar_flow_kgmole_h=molar,
                mass_flow_kg_h=float(s.MassFlowValue),
                vapor_fraction=float(s.VapourFractionValue),
                composition=dict(sorted(comp_frac.items(), key=lambda kv: -kv[1])),
            )

        if liq is vap:               # PFR 单一产品流
            liq = None
        streams = [st for st in (read_stream(vap, f"{task.reactor.name}-Vap"),
                                 read_stream(liq, f"{task.reactor.name}-Liq") if liq else None)
                   if st]

        conversions: dict[str, float] = {}
        if conv_rxn_names:
            try:
                vals = rx.RxnPercentConversionValue
                for name, v in zip(conv_rxn_names, vals):
                    conversions[name] = float(v) / 100.0
            except Exception:  # noqa: BLE001
                pass
        if kin_rxn_names:
            # 动力学转化率：按基准组分进出口摩尔平衡计算（单反应时精确）
            try:
                krxn = next(r for r in task.reactions if r.kind == "kinetic")
                base_req = krxn.basis_component or next(
                    (c for c, v in krxn.stoich.items() if v < 0), None)
                base_actual = req_to_actual.get(base_req)
                idx = actual_names.index(base_actual)
                n_in = 0.0
                for s in feed_streams:
                    n_in += (float(s.MolarFlowValue)
                             * float(s.ComponentMolarFraction.GetValues(None)[idx]))
                n_out = (float(vap.MolarFlowValue)
                         * float(vap.ComponentMolarFraction.GetValues(None)[idx]))
                if n_in > 1e-12:
                    conversions[kin_rxn_names[0]] = max(0.0, min(1.0, (n_in - n_out) / n_in))
            except Exception:  # noqa: BLE001
                pass

        duty_kw = None
        heat = energy.HeatFlowValue if energy is not None else None
        if heat is None:
            try:
                heat = rx.HeatFlowValue
            except Exception:  # noqa: BLE001
                heat = None
        if heat is not None:
            duty_kw = float(heat) / 3600.0

        messages = ["[COM] 结果读取自真实 Aspen HYSYS 求解。"]
        messages += [ln for ln in self._log if ln.startswith("[WARN]") or ln.startswith("[INFO]")]
        wd = getattr(self, "_watchdog", None)
        if wd is not None:
            messages += wd.log_lines()
        return SimResult(
            converged=True, adapter=self.name, reactor_name=task.reactor.name,
            streams=streams, duty_kw=duty_kw, conversions=conversions,
            messages=messages,
        )
