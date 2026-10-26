"""规则解析器：不依赖 LLM 的兜底解析。

策略：
1. 扫描组分（别名表）；
2. 用"已知反应库"按组分集合匹配反应（覆盖重整/变换/歧化/燃烧/合成氨等典型体系）；
3. 正则抽取温度、压力、流量、摩尔比、转化率、出口/进料温度等工艺参数；
4. 按 selector 规则定反应器类型，组装 ModelingTask。
LLM 可用时优先走 LLM，失败自动回退到这里 —— 两者输出同一 ModelingTask。
"""
from __future__ import annotations

import copy
import re

from . import trace
from .component_map import (  # noqa: F401
    ALIAS_TO_HYSYS, display_name, resolve_component, scan_components)
from .knowledge.retriever import load_known_reactions, retrieve
from .schema import (
    Component, FeedStream, FlowSpec, ModelingTask, Reaction, ReactorSpec,
)
from .selector import Features, select_reactor
from .text_norm import normalize_text
from .validators import check_balance

# 化学式下标转普通数字（如 C₇H₈ -> C7H8）
_SUB = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")

# ---------------------------------------------------------------------------
# 已知反应库（数据在 knowledge/reactions.json，组分集合精确匹配）
# stoich: 组分 -> 有符号系数（产物为正，反应物为负）
# ---------------------------------------------------------------------------
KNOWN_REACTIONS: list[dict] = load_known_reactions()

XYLENES = ["o-Xylene", "m-Xylene", "p-Xylene"]

# 甲烷蒸汽重整典型规模假设：1000 kmol/h CH4 约对应 1000 t/d 合成氨装置的一段转化炉
DEFAULT_REFORMING_CH4_KMOL_H = 1000.0


# ---------------------------------------------------------------------------
# 文本特征抽取
# ---------------------------------------------------------------------------
def _extract_features(text: str, comps: list[str]) -> Features:
    f = Features()
    # 先剔除"不需要/不考虑动力学"等否定表达，避免误判为提供了动力学
    # 覆盖"不对反应动力学进行深入探讨"等写法（考核文档原文措辞）
    text_kin = re.sub(
        r"(不需要|不需|无需|不用|不考虑|不探讨|不深入探讨|不研究|忽略|抛开|略去|不对|不进行|不深入)[^，。;；\n]{0,12}?动力学",
        "", text)
    if re.search(r"动力学|速率常数|活化能|指前因子|arrhenius|反应速率", text_kin, re.I):
        f.has_kinetics = True
    if re.search(r"液相|液态|均为液", text):
        f.phase = "liquid"
    elif re.search(r"气相|气态|蒸汽|蒸气", text):
        f.phase = "gas"
    if re.search(r"管式", text):
        f.equipment = "tubular"
    elif re.search(r"釜式|搅拌釜", text):
        f.equipment = "tank"
    for v in re.findall(r"转化率\s*(?:为|是|约)?\s*(\d+(?:\.\d+)?)\s*%", text):
        f.conversions.append(float(v) / 100.0)
    if f.conversions or re.search(r"收率|选择性", text):
        f.has_conversion = True
    if re.search(r"平衡常数|吉布斯自由能数值|\bka\b|\bkp\b", text, re.I):
        f.has_ka = True
    if re.search(r"可逆", text):
        f.reversible = True
    if re.search(r"化学平衡|平衡控制", text):
        f.equilibrium_controlled = True
    if re.search(r"产物分布未知|产物未知|分布未知", text):
        f.products_unknown = True
    if re.search(r"燃烧|部分氧化", text):
        f.combustion = True
    if re.search(r"聚合", text):
        f.polymerization = True

    # 甲烷蒸汽重整/变换属于典型平衡控制可逆反应（领域知识），文本未明说时自动补充；
    # 但燃烧/产物未知体系优先走 Gibbs，不做此补充
    cset = set(comps)
    if ({"Methane", "H2O"} <= cset and ({"CO", "CO2"} & cset)
            and not f.has_conversion and not f.combustion and not f.products_unknown):
        f.reversible = True
        f.equilibrium_controlled = True
    return f


def _find_numbers(text: str, pattern: str) -> list[float]:
    return [float(v) for v in re.findall(pattern, text)]


def _extract_temps(text: str) -> list[float]:
    """°C/℃ 优先；开尔文 K 自动换算为 °C（373K -> 99.85）。

    C/K 后面都带字符类否定前瞻：排除 "2C7H8" 被当成温度、K 误匹配 kmol/kg/K2CO3/kJ。"""
    vals = _find_numbers(text, r"(-?\d+(?:\.\d+)?)\s*°?\s*[cC℃](?![A-Za-z0-9])")
    for v in _find_numbers(text, r"(-?\d+(?:\.\d+)?)\s*[Kk](?![A-Za-z0-9])"):
        vals.append(v - 273.15)
    return vals


def _extract_pressures(text: str) -> list[float]:
    """返回 bar 列表。MPa×10、kPa÷100、atm×1.01325、kgf/cm²×0.9807（规范化后 kgf/cm2）。"""
    bars: list[float] = []
    for v, unit in re.findall(
            r"(\d+(?:\.\d+)?)\s*(mpa|kpa|atm|barg|bar|kgf/cm2|kg/cm2)(?:[gau])?\b",
            text, re.I):
        val = float(v)
        u = unit.lower()
        if u == "mpa":
            bars.append(val * 10.0)
        elif u == "kpa":
            bars.append(val / 100.0)
        elif u == "atm":
            bars.append(val * 1.01325)
        elif u in ("kgf/cm2", "kg/cm2"):
            bars.append(val * 0.9807)
        else:  # bar / barg（表压按绝压近似处理）
            bars.append(val)
    return bars


_FLOW_PAT = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(kgmole/hr|kgmole/h|kgmol/hr|kgmol/h|kmol/hr|kmol/h"
    r"|kg/hr|kg/h|nm3/hr|nm3/h|sm3/hr|sm3/h"
    r"|t/hr|t/h|吨/小时|吨/时|吨每小时)", re.I
)


def _norm_flow_unit(unit: str) -> tuple[float, str]:
    """单位标准化 -> (数值缩放系数, 标准单位)。"""
    u = unit.lower()
    if u in ("kgmole/h", "kgmol/h", "kmol/h", "kgmole/hr", "kgmol/hr", "kmol/hr"):
        return 1.0, "kgmole/h"
    if u in ("kg/h", "kg/hr"):
        return 1.0, "kg/h"
    if u in ("nm3/h", "nm3/hr", "sm3/h", "sm3/hr"):
        return 1.0, "Nm3/h"
    return 1000.0, "kg/h"       # t/hr | t/h | 吨/小时 | 吨/时 | 吨每小时


def _extract_flows(text: str) -> list[dict]:
    """返回 [{'value':..,'unit':..}]，单位标准化到 kg/h、kgmole/h、Nm3/h。

    覆盖 hr 变体、Sm3/h、t/h（吨/时，1 t = 1000 kg）与中文"吨/小时"写法。"""
    flows = []
    for m in _FLOW_PAT.finditer(text):
        scale, unit = _norm_flow_unit(m.group(2))
        flows.append({"value": float(m.group(1)) * scale, "unit": unit})
    return flows


def _extract_feed_pairs(text: str) -> list[dict]:
    """按"组分名 + 流量"位置邻近配对，解析多股进料。

    如"进料甲烷 100 kgmole/h，氧气 300 kgmole/h"——_extract_flows 只能给出
    流量序列，无法知道各自对应哪个组分；这里把组分别名与紧随其后的流量
    配对（间隔不超过 6 个字符，允许"流量"二字），每组分取第一处配对。"""
    # 组分别名的位置（与 scan_components 相同的边界规则）
    alias_hits: list[tuple[int, int, int, str]] = []  # (start, end, len, hysys_name)
    lowered = text.lower()
    for alias, hname in ALIAS_TO_HYSYS.items():
        if alias.isascii():
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", lowered):
                alias_hits.append((m.start(), m.end(), len(alias), hname))
        else:
            for m in re.finditer(re.escape(alias), text):
                alias_hits.append((m.start(), m.end(), len(alias), hname))
    flow_hits = [(m.start(1), *_norm_flow_unit(m.group(2)), float(m.group(1)))
                 for m in _FLOW_PAT.finditer(text)]  # (数字起始, scale, unit, value)

    pairs: dict[str, dict] = {}
    for f_start, scale, unit, value in flow_hits:
        best = None
        for a_start, a_end, a_len, hname in alias_hits:
            gap = f_start - a_end
            # 间隔最小者优先；同间隔取更长别名（"甲苯"优先于嵌在其中的"苯"）
            if 0 <= gap <= 6 and (best is None or (gap, -a_len) < best[0]):
                best = ((gap, -a_len), hname)
        if best:
            hname = best[1]
            if hname not in pairs:      # 每组分只取第一处配对
                pairs[hname] = {"component": hname, "value": value * scale, "unit": unit}
    return list(pairs.values())


def _extract_ratio(text: str) -> tuple[float, float] | None:
    m = re.search(r"摩尔比\s*(?:为|是)?\s*(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)", text)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)\s*的?\s*摩尔比", text)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def _extract_kinetics(text: str) -> tuple[float | None, float | None]:
    """从文本抽取（指前因子 A, 活化能 kJ/mol）。支持 1e9 / 1×10^9 / k=0.01 等写法。"""
    a = ea = None
    m = (re.search(r"活化能[^，。;；\n]{0,12}?(\d+(?:\.\d+)?)\s*(?:kJ/mol|kJmol|千焦/摩)", text, re.I)
         or re.search(r"活化能[^，。;；\n]{0,12}?(\d+(?:\.\d+)?)", text))
    if m:
        ea = float(m.group(1))
    m = re.search(
        r"(?:指前因子|频率因子|速率常数|速率系数)\s*[^，。;；\n]{0,10}?"
        r"(\d+(?:\.\d+)?)(?:\s*(?:[×xX*]\s*10\s*\^?|[eE])-?(\d+))?", text)
    if m:
        a = float(m.group(1))
        if m.group(2):
            a *= 10.0 ** float(m.group(2))
    else:
        m = re.search(r"\bk\s*[=＝]\s*(\d+(?:\.\d+)?(?:[eE]-?\d+)?)", text)
        if m:
            a = float(m.group(1))
    return a, ea


def _outlet_temps(text: str) -> list[float]:
    return _find_numbers(text, r"出口[^。;；\n]{0,24}?(\d+(?:\.\d+)?)\s*°?\s*[cC℃](?![A-Za-z0-9₀-₉])")


def _feed_temps(text: str) -> list[float]:
    return _find_numbers(text, r"进料[^。;；\n]{0,24}?(\d+(?:\.\d+)?)\s*°?\s*[cC℃](?![A-Za-z0-9₀-₉])")


# ---------------------------------------------------------------------------
# 反应匹配
# ---------------------------------------------------------------------------
def _match_known_reactions(comps: list[str]) -> list[dict]:
    found = {c.lower() for c in comps}
    matched = []
    for rx in sorted(KNOWN_REACTIONS, key=lambda r: r["priority"]):
        if set(rx["key"]) <= found:
            any_set = {a.lower() for a in rx.get("match_any", [])}
            if any_set and not (found & any_set):
                continue
            matched.append(rx)
    # 同一体系中重整与变换同时出现时两个都要；歧化单独处理
    return matched


def _top_exact_entry(text: str, comps: list[str]) -> dict | None:
    """知识库精确命中（match_components 全部出现）的 top1 条目，没有则 None。"""
    hits = retrieve(comps=comps, text=text, top_k=1)
    return hits[0]["entry"] if hits and hits[0]["hit_type"] == "exact" else None


def _build_reactions(text: str, comps: list[str], features: Features):
    """返回 (reactions, assumptions, clarifications)。"""
    assumptions: list[str] = []
    clar: list[str] = []
    matched = _match_known_reactions(comps)

    reactions: list[Reaction] = []

    # 甲苯歧化：三种异构体等比例，总转化率均分到 3 个反应
    if any(r["name"] == "甲苯歧化" for r in matched):
        total_conv = features.conversions[0] if features.conversions else None
        if total_conv is None:
            clar.append("未明确甲苯转化率，请给出转化率（如 50%）；当前按 50% 估算。")
            total_conv = 0.5
        per = total_conv / 3.0
        for x in XYLENES:
            reactions.append(Reaction(
                name=f"甲苯歧化（生成{display_name(x)}）",
                equation=f"2 C7H8 = C6H6 + {x.replace('Xylene', 'C8H10')}",
                kind="conversion", conversion=per, basis_component="Toluene",
                stoich={"Toluene": -2, "Benzene": 1, x: 1},
            ))
        assumptions.append(
            f"三种二甲苯异构体按等比例生成（各占转化量的 1/3，单反应转化率 {per*100:.2f}%，总转化率 {total_conv*100:.1f}%）。"
        )
        return reactions, assumptions, clar

    for r in matched:
        reactions.append(Reaction(
            name=r["name"], equation=r["equation"], kind=r["kind"],
            stoich=dict(r["stoich"]),
        ))
    if matched:
        trace.info("[知识库] 精确命中已知反应：" + "、".join(f"「{r['name']}」" for r in matched))

    if not reactions:
        generic = _parse_generic_equation(text)
        if generic:
            reactions.append(generic)
            assumptions.append("反应方程由文本解析得到，化学计量系数默认为 1，请核对。")
        else:
            # 命名反应兜底：文本提及知识库条目别名且该条目反应物全部出现在组分中
            # （如"甲烷蒸汽重整，进料甲烷和水蒸气"未写方程式）→ 直接采用知识库反应式，
            # 避免 LLM 漏给反应时任务整体不可建模
            sim = retrieve(comps=comps, text=text, top_k=1)
            e = sim[0]["entry"] if sim else None
            if (e is not None
                    and any(a in text for a in (e.get("aliases") or []))
                    and {c for c, v in e["stoich"].items() if v < 0} <= set(comps)):
                reactions.append(Reaction(
                    name=e["name"], equation=e["equation"],
                    kind=e["kind"], stoich=dict(e["stoich"]),
                ))
                assumptions.append(
                    f"文本提及「{e['name']}」但未给出方程式，反应式取自知识库，请核对。")
                trace.info(f"[知识库] 命名反应兜底：采用「{e['name']}」（{e['equation']}）")
            else:
                msg = "未能识别反应方程：请按 'A + B = C + D' 的形式描述反应，或说明各组分的化学计量系数。"
                # 澄清时附上知识库相似反应，供用户一键确认（检索增强的澄清）
                if sim:
                    e2 = sim[0]["entry"]
                    trace.info(f"[知识库] 相似条目：「{e2['name']}」（{e2['equation']}）")
                    msg += (f"知识库检索到相似反应「{e2['name']}」（{e2['equation']}），"
                            "若与您的描述相符，请确认采用该反应式。")
                clar.append(msg)

    # 动力学体系：文本给出指前因子/活化能时，把反应标记为 kinetic 并附参数
    # （第 8 节规则 1 的落地：动力学参数 -> CSTR/PFR，执行层据此创建 HYSYS 动力学反应）
    if features.has_kinetics and reactions:
        a, ea = _extract_kinetics(text)
        if a is not None or ea is not None:
            for r in reactions:
                r.kind = "kinetic"
                r.rate_constant_a = a
                r.activation_energy = ea
                if not r.reaction_orders:
                    r.reaction_orders = {c: abs(v) for c, v in r.stoich.items() if v < 0}
                if r.basis_component is None:
                    r.basis_component = next((c for c, v in r.stoich.items() if v < 0), None)
            params = []
            if a is not None:
                params.append(f"指前因子 A={a:g}")
            if ea is not None:
                params.append(f"活化能 {ea:g} kJ/mol")
            assumptions.append(
                "动力学参数由文本抽取（" + "，".join(params) + "）；"
                "未指明级数的反应物按化学计量系数作为反应级数，速率基准为摩尔浓度。请核对。")

    # 元素守恒硬校验：不守恒的反应直接拒绝并进入澄清流程
    valid: list[Reaction] = []
    for r in reactions:
        ok, unbalanced = check_balance(r.stoich)
        if ok is False:
            clar.append(
                f"反应「{r.equation or r.name}」元素不守恒（{('、'.join(unbalanced))}），"
                "已拒绝该反应；请核对化学计量系数后重新描述。"
            )
            continue
        valid.append(r)
    reactions = valid
    return reactions, assumptions, clar


def _clean_cn_token(t: str) -> str:
    """清洗中文描述里的修饰词：'氧气反应'->'氧气'，'二氧化碳的过程'->'二氧化碳'。"""
    t = re.sub(r"(的?过程|反应)$", "", t.strip())
    return re.sub(r"的$", "", t)


def _resolve_loose(t: str) -> str | None:
    """宽松解析：先整词解析，失败则在 token 内滑动窗口找最长可解析子串。

    处理 '模拟甲烷' / '氧气反应' / '二氧化碳的过程' 这类带修饰词的写法。
    """
    t = _clean_cn_token(t)
    direct = resolve_component(t)
    if direct:
        return direct
    for length in range(len(t) - 1, 1, -1):
        for i in range(0, len(t) - length + 1):
            r = resolve_component(t[i:i + length])
            if r:
                return r
    return None


def _parse_generic_equation(text: str) -> Reaction | None:
    """支持两种写法：
    1) 中文：'甲烷和水反应生成一氧化碳和氢气'
    2) 化学式：'CH4 + H2O = CO + 3H2' / '2C7H8 -> C6H6 + C8H10'
    """
    text = text.translate(_SUB)
    # 中文式
    m = re.search(
        r"([^\s，。,]{1,12}(?:和|与|加)[^\s，。,]{1,12})[^。；;]{0,6}?生成(.{1,40}?)(?=[,，。;；\n]|$)",
        text,
    )
    if m:
        left = re.split(r"和|与|加", m.group(1))
        right = re.split(r"和|与|、", m.group(2))
        lnames = [_resolve_loose(x) for x in left]
        rnames = [_resolve_loose(x) for x in right]
        if all(lnames) and all(rnames):
            stoich = {n: -1.0 for n in lnames}
            for n in rnames:
                stoich[n] = stoich.get(n, 0) + 1.0
            return Reaction(
                name="文本解析反应", equation=" + ".join(left) + " = " + " + ".join(right),
                kind="equilibrium", stoich=stoich,
            )
    # 化学式式
    m = re.search(r"([\w\s+．.]+?)\s*(?:=|->|→|=>)\s*([\w\s+．.]+)", text)
    if m:
        def side(s: str) -> dict[str, float]:
            out: dict[str, float] = {}
            for coeff, tok in re.findall(r"(?:(\d+(?:\.\d+)?)\s*)?([A-Za-z][A-Za-z0-9]*)", s):
                name = resolve_component(tok)
                if name:
                    out[name] = out.get(name, 0) + (float(coeff) if coeff else 1.0)
            return out
        l, r = side(m.group(1)), side(m.group(2))
        if l and r:
            stoich = {k: -v for k, v in l.items()}
            for k, v in r.items():
                stoich[k] = stoich.get(k, 0) + v
            return Reaction(
                name="文本解析反应",
                equation=m.group(1).strip() + " = " + m.group(2).strip(),
                kind="equilibrium", stoich=stoich,
            )
    return None


# ---------------------------------------------------------------------------
# 进料组装
# ---------------------------------------------------------------------------
def _build_feeds(text: str, comps: list[str], features: Features):
    feeds: list[FeedStream] = []
    assumptions: list[str] = []
    clar: list[str] = []

    outlet_t = _outlet_temps(text)
    feed_t_list = _feed_temps(text) or _extract_temps(text)
    feed_t = feed_t_list[0] if feed_t_list else None
    pressures = _extract_pressures(text)
    p_bar = pressures[0] if pressures else None
    flows = _extract_flows(text)
    ratio = _extract_ratio(text)

    # 知识库精确命中时，缺失的进料温度/压力参考该反应的典型工况（有出处的假设）
    kb_entry = _top_exact_entry(text, comps)
    typ = (kb_entry or {}).get("typical") or {}

    if feed_t is None:
        t_typ = typ.get("temperature_C")
        if t_typ:
            feed_t = float(t_typ[0])
            assumptions.append(
                f"进料温度未提供，参考知识库「{kb_entry['name']}」典型工况取 {feed_t:.0f} °C。")
            trace.info(f"[知识库] 进料温度参考「{kb_entry['name']}」典型工况：{feed_t:.0f} °C")
        else:
            clar.append("未提供进料温度，请补充（否则默认 25 °C）。")
            feed_t = 25.0
    if p_bar is None:
        p_typ = typ.get("pressure_bar")
        if p_typ:
            p_bar = float(p_typ[0])
            assumptions.append(
                f"操作压力未提供，参考知识库「{kb_entry['name']}」典型工况取 {p_bar:g} bar。")
            trace.info(f"[知识库] 操作压力参考「{kb_entry['name']}」典型工况：{p_bar:g} bar")
        else:
            clar.append("未提供操作压力，请补充（否则默认 1 bar）。")
            p_bar = 1.0

    main_reactants = [c for c in comps if c not in ("H2", "CO", "CO2", "Ammonia", "Nitrogen", "Oxygen")]

    pairs = _extract_feed_pairs(text)
    if pairs:
        # 多股进料：文本里每个组分名紧跟自己的流量（如"甲烷 100 kgmole/h，氧气 300 kgmole/h"）
        basis_map = {"kg/h": "mass", "Nm3/h": "volume_std"}
        for p in pairs:
            feeds.append(FeedStream(
                name=f"Feed-{p['component'].replace('-', '')}",
                temperature_C=feed_t, pressure_bar=p_bar,
                flow=FlowSpec(value=p["value"], unit=p["unit"],
                              basis=basis_map.get(p["unit"], "molar")),
                composition={p["component"]: 1.0},
            ))
        assumptions.append(
            "进料按文本中「组分+流量」配对解析："
            + "、".join(f"{p['component']} {p['value']:g} {p['unit']}" for p in pairs)
            + "；若与原意不符请修改描述。")
    elif ratio and len(comps) >= 2 and not flows:
        # 场景1型：摩尔比 + 无显式流量 -> 按典型装置规模假设主组分流量
        primary = "Methane" if "Methane" in comps else main_reactants[0]
        secondary = "H2O" if "H2O" in comps else next(c for c in comps if c != primary)
        q0 = DEFAULT_REFORMING_CH4_KMOL_H
        q1 = q0 * ratio[1] / ratio[0]
        flows_map = {primary: q0, secondary: q1}
        for name, q in flows_map.items():
            feeds.append(FeedStream(
                name=f"Feed-{name.replace('-', '')}", temperature_C=feed_t, pressure_bar=p_bar,
                flow=FlowSpec(value=q, unit="kgmole/h", basis="molar"), composition={name: 1.0},
            ))
        assumptions.append(
            f"进料流量未指定，按典型合成氨装置一段转化炉规模取 {primary} {q0:.0f} kgmole/h"
            f"（约对应 1000 t/d 合成氨，≈ 8.8×10^8 Nm³/年天然气处理量），并按摩尔比 {ratio[0]}:{ratio[1]} 配比 {secondary}。"
        )
    elif flows and len(comps) >= 1:
        fl = flows[0]
        basis = "mass" if fl["unit"] == "kg/h" else "molar"
        for i, c in enumerate(main_reactants[:1] or comps[:1]):
            feeds.append(FeedStream(
                name=f"Feed-{c.replace('-', '')}", temperature_C=feed_t, pressure_bar=p_bar,
                flow=FlowSpec(value=fl["value"], unit=fl["unit"], basis=basis),
                composition={c: 1.0},
            ))
        if len(main_reactants) > 1:
            clar.append("检测到多个进料组分但只解析到一个流量，请补充各股流量或摩尔比。")
    else:
        n = max(len(comps), 1)
        for c in comps:
            feeds.append(FeedStream(
                name=f"Feed-{c.replace('-', '')}", temperature_C=feed_t, pressure_bar=p_bar,
                flow=FlowSpec(value=100.0, unit="kgmole/h", basis="molar"),
                composition={c: 1.0},
            ))
        note = "各组分流量未知，暂按 100 kgmole/h 等摩尔分股进料，请核对修改。"
        tf = (kb_entry or {}).get("typical_flow")
        if tf:
            note += (f"知识库「{kb_entry['name']}」典型进料：{tf['component']} 约 "
                     f"{tf['value']:g} {tf['unit']}（{tf.get('note', '')}），建议按此调整。")
        assumptions.append(note)

    return feeds, feed_t, p_bar, outlet_t, assumptions, clar


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def rule_parse(text: str) -> list[ModelingTask]:
    text = normalize_text(text)   # 下标/上标/箭头/全角/相态标注统一成 ASCII
    comps = scan_components(text)
    if not comps:
        raise ValueError("未能从描述中识别任何化学组分，请使用中文名或化学式描述进料（如：甲烷、水蒸气、CH4）。")

    features = _extract_features(text, comps)
    reactions, assumptions, clar = _build_reactions(text, comps, features)

    # 反应库匹配到的平衡反应强化 selector 的判断
    if any(r.kind == "equilibrium" for r in reactions) and not features.has_conversion:
        features.reversible = True
        features.equilibrium_controlled = True

    decision = select_reactor(features)
    assumptions = assumptions + decision.notes
    clar = clar + decision.clarifications

    feeds, feed_t, p_bar, outlet_t_list, asm2, clar2 = _build_feeds(text, comps, features)
    assumptions += asm2
    clar += clar2

    if decision.reactor_type == "unknown":
        # 无法定类型时仍给出任务骨架，前端展示澄清问题
        rtype = "equilibrium"
        reactor_name = "ER-100(待确认)"
    else:
        rtype = decision.reactor_type
        reactor_name = {"conversion": "CRV-100", "equilibrium": "ER-100", "gibbs": "GR-100",
                        "cstr": "CSTR-100", "pfr": "PFR-100"}[rtype]

    outlet_ts = outlet_t_list or [None]
    tasks: list[ModelingTask] = []
    for i, tout in enumerate(outlet_ts):
        task = ModelingTask(
            title=(f"{_infer_title(text, comps)}" + (f"（出口 {tout:.0f} °C）" if tout is not None and len(outlet_ts) > 1 else "")),
            reactor_type=rtype,            # type: ignore[arg-type]
            selection_reason=decision.reason,
            components=[Component(hysys_name=c, display_name=display_name(c)) for c in comps],
            feeds=copy.deepcopy(feeds),
            reactions=copy.deepcopy(reactions),
            reactor=ReactorSpec(
                type=rtype, name=reactor_name,               # type: ignore[arg-type]
                outlet_temperature_C=tout, pressure_bar=p_bar, delta_P_bar=0.0,
            ),
            assumptions=assumptions,
            clarifications=clar,
            source="rules",
        )
        tasks.append(task)
    return tasks


def _infer_title(text: str, comps: list[str]) -> str:
    if re.search(r"重整", text):
        return "甲烷蒸汽重整"
    if re.search(r"歧化", text):
        return "甲苯歧化"
    if re.search(r"燃烧", text):
        return "燃烧反应"
    if re.search(r"合成氨|氨合成", text):
        return "合成氨"
    return "反应器模拟（" + "、".join(comps[:3]) + ("等)" if len(comps) > 3 else ")")
