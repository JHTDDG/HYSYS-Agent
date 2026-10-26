"""LLM 解析层：调用任意 OpenAI 兼容接口（DeepSeek / Qwen / GLM / OpenAI 均可），
把自然语言描述解析成 ModelingTask JSON，并按第 8 节选择逻辑判断反应器类型。

环境变量：
  LLM_API_BASE  例如 https://api.deepseek.com/v1
  LLM_API_KEY   对应密钥
  LLM_MODEL     例如 deepseek-chat
未配置或调用/解析失败时，上层自动回退到规则解析器。
"""
from __future__ import annotations

import json
import os
import re

import requests

from . import trace
from .component_map import ELEMENT_COMPOSITION, display_name, resolve_component
from .knowledge.retriever import render_context, retrieve
from .schema import (
    Component, FeedStream, FlowSpec, ModelingTask, Reaction, ReactorSpec,
)
from .selector import select_reactor, Features
from .text_norm import normalize_text
from .validators import check_balance

PROMPT_SYSTEM = """你是化工流程模拟专家。用户的自然语言描述需要转换为结构化的"反应器建模任务"JSON。

## 反应器选择逻辑（必须严格遵守）
1. 用户提供了动力学参数（速率常数/活化能/指前因子）→ 动力学反应器：反应物均为液相选 cstr，气相选 pfr；管式选 pfr，釜式选 cstr。聚合反应器不适用此规则。
2. 未提供动力学参数，但明确给出转化率/收率/选择性 → conversion（转化率反应器）。
3. 可逆反应、受化学平衡控制，需提供 Ka（平衡常数）或吉布斯自由能 → equilibrium（平衡反应器）。甲烷蒸汽重整、水煤气变换属于典型平衡控制可逆反应。
4. Gibbs 反应器基于吉布斯自由能最小化，无需定义具体反应方程，适用于产物分布未知、燃烧、极高温体系 → gibbs。
5. 没有固定答案，只有合不合适；不确定时在 clarifications 中列出需要用户澄清的问题。

## 输出要求
- 只输出一个 JSON 对象，不要输出任何其他文字或 markdown 代码块标记。
- 温度单位摄氏度，压力单位 bar（MPa×10），流量单位 kgmole/h 或 kg/h。
- stoich 为有符号化学计量系数：产物为正、反应物为负。
- 转化率用 0~1 小数。
- 提供了动力学参数时：kind 填 "kinetic"，并把指前因子填入 rate_constant_a、活化能（kJ/mol）填入
  activation_energy、各组分正反应级数填入 reaction_orders（未指明级数的反应物按化学计量系数绝对值）、
  速率基准填入 rate_basis（"concentration"=摩尔浓度基准，"partial_pressure"=分压基准）。
- 若用户给出了多个工况（如不同出口温度），输出 tasks 数组，每个工况一个任务。
- components 使用 HYSYS 组分库名称（如 Methane, H2O, CO, CO2, H2, Toluene, Benzene, m-Xylene, o-Xylene, p-Xylene）。
- 信息不足不要编造：写进 clarifications；工程上合理可自定的（如流量规模）写进 assumptions。

## JSON 结构
{
  "tasks": [{
    "title": "字符串",
    "reactor_type": "conversion|equilibrium|gibbs|cstr|pfr",
    "selection_reason": "选择理由（中文）",
    "fluid_package": "Peng-Robinson",
    "components": [{"hysys_name": "Methane", "display_name": "甲烷"}],
    "feeds": [{"name": "Feed-Methane", "temperature_C": 520, "pressure_bar": 13.5,
               "flow": {"value": 1000, "unit": "kgmole/h", "basis": "molar"},
               "composition": {"Methane": 1.0}}],
    "reactions": [{"name": "", "equation": "CH4 + H2O = CO + 3H2",
                   "kind": "equilibrium|conversion|kinetic", "stoich": {"Methane": -1, "H2O": -1, "CO": 1, "H2": 3},
                   "conversion": null, "basis_component": null,
                   "rate_constant_a": null, "activation_energy": null,
                   "reaction_orders": {}, "rate_basis": "concentration"}],
    "reactor": {"type": "equilibrium", "name": "ER-100",
                "outlet_temperature_C": 710, "pressure_bar": 13.5, "delta_P_bar": 0},
    "assumptions": ["..."], "clarifications": ["..."]
  }]
}"""


def llm_available() -> bool:
    return bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_API_BASE"))


def _kb_context(text: str) -> str:
    """知识库检索增强：把相似已知反应注入 prompt（RAG 上下文）。"""
    hits = retrieve(text=text, top_k=3)
    if not hits:
        trace.info("知识库检索：无相似条目")
        return ""
    trace.info("知识库检索：命中 "
               + "、".join(f"「{h['entry']['name']}」{h['score']:.2f}" for h in hits))
    return ("\n\n【知识库参考】检索到的相似已知反应及典型工况，仅供解析时参考；"
            "与用户描述冲突时一律以用户描述为准：\n" + render_context(hits))


def _chat(messages: list[dict]) -> str:
    base = os.getenv("LLM_API_BASE").rstrip("/")
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {os.getenv('LLM_API_KEY')}"},
        json={
            "model": os.getenv("LLM_MODEL", "deepseek-chat"),
            "messages": messages,
            "temperature": 0.2,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _extract_json(content: str) -> dict:
    content = content.strip()
    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _normalize_task(d: dict, source: str) -> ModelingTask:
    """LLM 输出不总可靠：组分名强制过一遍别名解析，缺失字段补默认。"""
    comps: list[Component] = []
    for c in d.get("components", []):
        name = resolve_component(c.get("hysys_name", "")) or c.get("hysys_name", "")
        if name:
            comps.append(Component(hysys_name=name,
                                   display_name=c.get("display_name") or display_name(name)))
    feeds = []
    norm_notes: list[str] = []
    for fs in d.get("feeds", []):
        comp_frac = {}
        for k, v in (fs.get("composition") or {}).items():
            nk = resolve_component(k) or k
            comp_frac[nk] = float(v)
        # 摩尔分数之和必须为 1：LLM 偶尔输出未归一化数值（如 0.37 + 1.0），
        # 这里强制按比例归一化，避免 HYSYS/Mock 各自处理不一致
        frac_sum = sum(v for v in comp_frac.values() if v > 0)
        if frac_sum > 0 and abs(frac_sum - 1.0) > 1e-6:
            comp_frac = {k: v / frac_sum for k, v in comp_frac.items()}
            norm_notes.append(
                f"进料「{fs.get('name') or 'Feed'}」组成之和为 {frac_sum:g}，"
                "已按比例归一化为摩尔分数")
        flow_d = fs.get("flow") or {}
        flow = None
        if flow_d:
            unit = flow_d.get("unit", "kgmole/h")
            # 以单位为准推导 basis，避免 LLM 输出 unit=kg/h 却 basis=molar 的矛盾
            basis = {"kg/h": "mass", "kgmole/h": "molar",
                     "Nm3/h": "volume_std"}.get(unit, flow_d.get("basis", "molar"))
            flow = FlowSpec(value=float(flow_d.get("value", 100.0)),
                            unit=unit, basis=basis)
        feeds.append(FeedStream(
            name=fs.get("name") or "Feed",
            temperature_C=fs.get("temperature_C"),
            pressure_bar=fs.get("pressure_bar"),
            flow=flow,
            composition=comp_frac,
        ))
    reactions = []
    unbalanced_msgs: list[str] = []
    for r in d.get("reactions", []):
        stoich = {}
        for k, v in (r.get("stoich") or {}).items():
            nk = resolve_component(k) or k
            stoich[nk] = float(v)
        orders = {}
        for k, v in (r.get("reaction_orders") or {}).items():
            nk = resolve_component(k) or k
            orders[nk] = float(v)
        reaction = Reaction(
            name=r.get("name", ""), equation=r.get("equation", ""),
            kind=r.get("kind", "equilibrium"),
            stoich=stoich,
            conversion=r.get("conversion"), basis_component=r.get("basis_component"),
            rate_constant_a=r.get("rate_constant_a"),
            activation_energy=r.get("activation_energy"),
            reaction_orders=orders,
            rate_basis=r.get("rate_basis") or "concentration",
        )
        # 元素守恒硬校验：不守恒的反应拒绝并提示澄清（代码校验，不靠模型自查）
        ok, unbalanced = check_balance(stoich)
        if ok is None:
            unknown = sorted(k for k in stoich if k not in ELEMENT_COMPOSITION)
            trace.warn(f"[守恒校验] 反应「{reaction.equation or reaction.name}」"
                       f"含未知组分（{'、'.join(unknown)}），守恒未能校验，请人工核对")
        if ok is False:
            trace.warn(f"[守恒校验] 拒绝反应「{reaction.equation or reaction.name}」"
                       f"：{'、'.join(unbalanced)} 不守恒")
            unbalanced_msgs.append(
                f"反应「{reaction.equation or reaction.name}」元素不守恒"
                f"（{('、'.join(unbalanced))}），已拒绝；请核对化学计量系数。"
            )
            continue
        reactions.append(reaction)
    rd = d.get("reactor") or {}
    task = ModelingTask(
        title=d.get("title") or "反应器模拟",
        reactor_type=d.get("reactor_type") or "equilibrium",
        selection_reason=d.get("selection_reason", ""),
        fluid_package=d.get("fluid_package") or "Peng-Robinson",
        components=comps, feeds=feeds, reactions=reactions,
        reactor=ReactorSpec(
            type=rd.get("type") or d.get("reactor_type") or "equilibrium",
            name=rd.get("name") or "R-100",
            outlet_temperature_C=rd.get("outlet_temperature_C"),
            pressure_bar=rd.get("pressure_bar"),
            delta_P_bar=float(rd.get("delta_P_bar") or 0.0),
        ),
        assumptions=[str(a) for a in d.get("assumptions", [])] + norm_notes,
        clarifications=[str(a) for a in d.get("clarifications", [])] + unbalanced_msgs,
        source=source,        # type: ignore[arg-type]
    )
    return task


def llm_parse(text: str) -> list[ModelingTask]:
    """单轮解析 + 一次修复重试：输出无法解析为合法任务 JSON 时，
    把原始输出与报错喂回模型重试一次；仍失败则抛出（上层回退规则引擎）。"""
    text = normalize_text(text)   # 下标/上标/箭头/全角/相态标注统一成 ASCII 再进 prompt
    base_messages = [
        {"role": "system", "content": PROMPT_SYSTEM},
        {"role": "user", "content": text + _kb_context(text)},
    ]
    last_err: Exception | None = None
    raw = ""
    for attempt in range(2):
        trace.info(f"LLM 调用（第 {attempt + 1}/2 次）model={os.getenv('LLM_MODEL')} ...")
        if attempt == 0:
            raw = _chat(base_messages)
        else:
            raw = _chat(base_messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                    f"你的上一次输出无法解析为合法的任务 JSON，错误信息：{last_err}。"
                    "请修正后重新输出，只输出一个合法 JSON 对象，不要包含任何其他文字或代码块标记。"},
            ])
        trace.info(f"响应 {len(raw)} 字符，进行 JSON 结构校验...")
        try:
            data = _extract_json(raw)
            tasks = [_normalize_task(t, "llm") for t in data.get("tasks", [])]
            if not tasks:
                raise ValueError("tasks 为空")
            for t in tasks:
                if not t.selection_reason:
                    t.selection_reason = select_reactor(Features()).reason
            trace.ok(f"LLM 解析通过：{len(tasks)} 个任务，"
                     f"{sum(len(t.reactions) for t in tasks)} 个反应（均通过元素守恒校验）")
            return tasks
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt == 0:
                trace.warn(f"第 1 次输出不合法（{type(e).__name__}: {e}），把错误喂回模型重试...")
            else:
                trace.err(f"重试后仍失败：{e}")
    raise last_err if last_err else RuntimeError("LLM 解析失败")
