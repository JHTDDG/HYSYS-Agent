"""编排层：自然语言 -> 解析 -> 适配器执行 -> 结果。"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from . import config, trace
from .adapters.base import IHYSYSAdapter
from .adapters.com_hysys import ComHysysAdapter
from .adapters.mock import MockAdapter
from .knowledge.retriever import load_kb
from .llm_parser import llm_available, llm_parse
from .parser_rules import rule_parse
from .schema import ModelingTask, SimResult, dump_model

# 启动时加载项目根目录 .env（已有环境变量优先）
config.load_env_file()

ADAPTER_LABELS = {
    "mock": "Mock 适配器（本地联调数据，非真实 HYSYS）",
    "com": "Aspen HYSYS COM（真实模拟计算）",
}

# ---- 求解后保存 HYSYS 案例（.hsc）----
# HYSYS_SAVE_CASE: "1"/"true" 时默认保存（每次请求仍可由前端勾选覆盖）
# HYSYS_SAVE_DIR:  保存目录，默认项目根 results_hsc/
SAVE_FLAG_ENV = "HYSYS_SAVE_CASE"
SAVE_DIR_ENV = "HYSYS_SAVE_DIR"


def save_case_default() -> bool:
    return os.getenv(SAVE_FLAG_ENV, "0").strip().lower() in ("1", "true", "yes", "on")


def save_dir() -> str:
    d = os.getenv(SAVE_DIR_ENV) or str(Path(__file__).resolve().parent.parent / "results_hsc")
    return d


def _save_path(reactor_name: str, used: set[str]) -> str:
    """生成唯一的保存路径：时间戳 + 反应器名，冲突自动加 _N 后缀。"""
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", reactor_name or "case").strip("_") or "case"
    base = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{safe}"
    d = save_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, base + ".hsc")
    n = 1
    while path in used or os.path.exists(path):
        path = os.path.join(d, f"{base}_{n}.hsc")
        n += 1
    used.add(path)
    return path


def get_adapter() -> IHYSYSAdapter:
    mode = os.getenv("HYSYS_MODE", "auto").lower()
    com = ComHysysAdapter()
    if mode == "com":
        return com
    if mode == "mock":
        return MockAdapter()
    # auto：本机有 HYSYS 就用真的，否则 Mock
    return com if com.available() else MockAdapter()


def _trace_tasks(tasks: list[ModelingTask]) -> None:
    for i, t in enumerate(tasks, 1):
        trace.info(f"任务{i}「{t.title}」→ 反应器 {t.reactor_type} | {t.selection_reason[:56]}")
        for r in t.reactions:
            tag = f"转化率={r.conversion:.2%}" if r.conversion is not None else r.kind
            trace.info(f"   反应: {r.equation} [{tag}]")
        for c in t.clarifications:
            trace.warn(f"   待澄清: {c[:70]}")
        for a in t.assumptions:
            trace.info(f"   假设: {a[:70]}")


def _unmodelable_reason(task: ModelingTask) -> str | None:
    """非 Gibbs 任务在两种注定失败的情况下提前拦截，避免送进 HYSYS 后必然 NotSolved：
    1) 没有任何有效反应（非 Gibbs 反应器必须先定义反应）；
    2) 某反应物未出现在任何进料中，且没有其他反应能生成它——
       平衡计算遇零分压、转化率计算遇零基准流量，均必然不收敛。
    注意：反应物虽未进料、但可由同任务其他反应生成时不算缺失
    （如重整+变换组合中 CO 由重整反应生成）。"""
    if task.reactor_type == "gibbs":
        return None
    if not task.reactions:
        return ("该任务无法自动建模：未识别到有效反应方程（非 Gibbs 反应器必须先定义反应）。"
                "请按 'A + B = C + D' 形式补充反应式并核对化学计量系数，"
                "或改用 Gibbs 反应器（产物分布未知/燃烧体系）。")
    fed: set[str] = set()
    for f in task.feeds:
        for comp, frac in (f.composition or {}).items():
            if frac > 0:
                fed.add(comp)
    produced = {c for r in task.reactions for c, nu in (r.stoich or {}).items() if nu > 0}
    missing: list[str] = []
    for r in task.reactions:
        for comp, nu in (r.stoich or {}).items():
            if nu < 0 and comp not in fed and comp not in produced and comp not in missing:
                missing.append(comp)
    if missing:
        return ("该任务无法自动建模：反应物 " + "、".join(missing)
                + " 未出现在任何进料中，也没有反应能生成它，反应无法进行。"
                "请补充进料组成，确保每个反应物都有进料。")
    return None


def _mark_unmodelable(tasks: list[ModelingTask]) -> list[ModelingTask]:
    """把拦截说明写进 clarifications，让界面在“需要澄清”里直接告诉用户。"""
    for t in tasks:
        reason = _unmodelable_reason(t)
        if reason and reason not in t.clarifications:
            t.clarifications.append(reason)
    return tasks


def parse_text(text: str) -> tuple[list[ModelingTask], bool]:
    """返回 (任务列表, 是否使用了 LLM)。LLM 失败自动回退规则解析。"""
    trace.stage("① 自然语言解析")
    trace.info(f"输入 {len(text)} 字符")
    if llm_available():
        trace.info(f"LLM 已配置：{os.getenv('LLM_MODEL')} @ {os.getenv('LLM_API_BASE')}")
        try:
            tasks = llm_parse(text)
            if any(_unmodelable_reason(t) for t in tasks):
                rule_tasks = rule_parse(text)
                if any(t.reactions for t in rule_tasks):
                    trace.warn("LLM 任务不可建模（缺反应或缺反应物进料）→ 回退规则引擎")
                    trace.ok(f"规则引擎解析完成：{len(rule_tasks)} 个任务")
                    rule_tasks = _mark_unmodelable(rule_tasks)
                    _trace_tasks(rule_tasks)
                    return rule_tasks, False
                trace.warn("LLM 未给出反应，规则引擎也无已知反应 → 保留 LLM 结果")
            trace.ok(f"LLM 解析完成：{len(tasks)} 个任务")
            tasks = _mark_unmodelable(tasks)
            _trace_tasks(tasks)
            return tasks, True
        except Exception as e:  # noqa: BLE001
            trace.warn(f"LLM 解析失败（{type(e).__name__}: {e}）→ 回退规则引擎")
    else:
        trace.info("LLM 未配置 → 使用规则引擎（已知反应库匹配 + 参数抽取）")
    tasks = rule_parse(text)
    tasks = _mark_unmodelable(tasks)
    trace.ok(f"规则引擎解析完成：{len(tasks)} 个任务")
    _trace_tasks(tasks)
    return tasks, False


def _dump(obj):
    return dump_model(obj)


def simulate(text: str, save_case: bool | None = None) -> dict:
    """save_case: True/False 强制保存/不保存；None 跟随 HYSYS_SAVE_CASE 默认值。"""
    tasks, used_llm = parse_text(text)
    adapter = get_adapter()
    do_save = save_case if save_case is not None else save_case_default()
    trace.stage("② HYSYS 建模与求解")
    trace.info(f"适配器: {adapter.name}（{'真实 COM 联动' if adapter.name == 'com' else '本地联调 Mock'}）")
    results: list[SimResult] = []
    used_paths: set[str] = set()
    for i, task in enumerate(tasks, 1):
        reason = _unmodelable_reason(task)
        if reason:
            trace.err(f"任务{i} 已跳过：{reason[:50]}")
            results.append(SimResult(
                converged=False, adapter=adapter.name, reactor_name=task.reactor.name,
                messages=[f"[SKIP] {reason}", "已跳过 HYSYS 建模与求解；请按“需要澄清”里的说明修改描述后重试。"],
            ))
            continue
        trace.info(f"求解任务{i}「{task.title}」（{task.reactor.name}，{task.reactor.type}）...")
        save_path = _save_path(task.reactor.name, used_paths) if (do_save and adapter.name == "com") else None
        if save_path:
            trace.info(f"任务{i} 求解完成后保存案例：{save_path}")
        try:
            result = adapter.run(task, save_path=save_path)
        except Exception as e:  # noqa: BLE001
            trace.err(f"任务{i} 执行失败：{e}")
            if adapter.name == "com" and task.reactor_type in ("cstr", "pfr", "gibbs"):
                # COM 先行尝试失败后的降级兜底（仅 cstr/pfr/gibbs 白名单）：
                # CSTR 用转化率反应器挂动力学反应集近似、PFR COM 接口对动力学
                # 反应集欠指定、Gibbs 建模失败——降级为本地估算并明确标注，
                # 不让用户拿到空结果。equilibrium/conversion 失败不降级直接报错。
                label = {
                    "cstr": "CSTR（转化率反应器挂动力学反应集近似全混釜，COM 求解未成功）",
                    "pfr": "PFR（COM 接口在新建 case 上对动力学反应集欠指定，GUI 手工可解，属 HYSYS COM 侧限制）",
                    "gibbs": "Gibbs（COM 建模/求解失败）",
                }[task.reactor_type]
                trace.warn(f"任务{i} COM 路线求解{label} → 降级 Mock 估算")
                result = MockAdapter().run(task)
                result.messages.insert(0,
                    f"[WARN] HYSYS COM 求解{label}失败（{e}）；"
                    "已降级为本地估算模型（Mock），本结果仅供参考，非真实 HYSYS 计算值。")
            else:
                result = SimResult(
                    converged=False, adapter=adapter.name, reactor_name=task.reactor.name,
                    messages=[f"[ERROR] 模拟执行失败：{e}"],
                )
        if result.converged:
            prod = result.streams[-1] if result.streams else None
            n = len(prod.composition) if prod else 0
            trace.ok(f"任务{i} 收敛，出口流股 {len(result.streams)} 个，产物 {n} 种组分")
        else:
            trace.err(f"任务{i} 未收敛")
        results.append(result)
    return {
        "adapter": adapter.name,
        "adapter_label": ADAPTER_LABELS.get(adapter.name, adapter.name),
        "used_llm": used_llm,
        "saved_case": do_save and adapter.name == "com",
        "tasks": [_dump(t) for t in tasks],
        "results": [_dump(r) for r in results],
    }


def status() -> dict:
    mode = os.getenv("HYSYS_MODE", "auto").lower()
    # mock 模式下不必探测 COM（探测会尝试启动 HYSYS）
    available = False if mode == "mock" else ComHysysAdapter().available()
    adapter = "com" if (mode == "com" or (mode == "auto" and available)) else "mock"
    return {
        "hysys_com_available": available,
        "adapter": adapter,
        "adapter_label": ADAPTER_LABELS[adapter],
        "llm_configured": llm_available(),
        "llm_model": os.getenv("LLM_MODEL", ""),
        "llm_api_base": os.getenv("LLM_API_BASE", ""),
        "kb_entries": len(load_kb()),
        "hysys_mode_env": mode,
        "save_case_default": save_case_default(),
        "save_dir": save_dir(),
    }
