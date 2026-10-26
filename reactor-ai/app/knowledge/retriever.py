"""轻量知识库检索器（RAG 的"缩水版"）。

设计决策：知识库只有十几条结构化条目，组分集合的精确/重叠匹配
比向量召回更准、更快、零依赖 —— 不引入向量库与 embedding。

对外能力：
- load_kb()              读取 reactions.json（带缓存）
- load_known_reactions()  转成 parser_rules 的已知反应格式
- retrieve(...)           组分重叠 + 名称别名打分，返回 top-k
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

from ..component_map import scan_components

_KB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reactions.json")


@lru_cache(maxsize=1)
def load_kb() -> tuple[dict, ...]:
    with open(_KB_PATH, encoding="utf-8") as f:
        return tuple(json.load(f)["reactions"])


def load_known_reactions() -> list[dict]:
    """转成 parser_rules 使用的已知反应条目格式（key 为小写组分集合）。"""
    out: list[dict] = []
    for e in load_kb():
        out.append({
            "key": {c.lower() for c in e["match_components"]},
            "name": e["name"],
            "equation": e["equation"],
            "kind": e["kind"],
            "stoich": dict(e["stoich"]),
            "match_any": list(e.get("match_any") or []),
            "priority": e.get("priority", 99),
            "entry": e,
        })
    return out


def retrieve(comps: list[str] | None = None, text: str | None = None,
             top_k: int = 3, min_score: float = 0.3) -> list[dict]:
    """组分集合检索。

    - exact：条目 match_components 全部出现在组分里（且满足 match_any 约束）
    - similar：重叠组分 >= 2 且 Jaccard >= min_score
    返回 [{"entry":..., "score":..., "hit_type": "exact"|"similar"}]，exact 优先。
    """
    if comps is None:
        comps = scan_components(text or "")
    found = {c.lower() for c in comps}
    results: list[dict] = []
    for e in load_kb():
        key = {c.lower() for c in e["match_components"]}
        any_set = {a.lower() for a in (e.get("match_any") or [])}
        hit, score = "", 0.0
        if key <= found and (not any_set or found & any_set):
            hit, score = "exact", 1.0
        else:
            inter = key & found
            if len(inter) >= 2:
                j = len(inter) / len(key | found)
                if j >= min_score:
                    hit, score = "similar", j
        if not hit:
            continue
        if text and any(a in text for a in e.get("aliases", [])):
            score += 0.25  # 名称别名加成（仅在给文本时）
        results.append({"entry": e, "score": round(score, 3), "hit_type": hit})
    results.sort(key=lambda r: (r["hit_type"] != "exact", -r["score"],
                                r["entry"].get("priority", 99)))
    return results[:top_k]


def render_context(hits: list[dict]) -> str:
    """把检索结果渲染成给 LLM 的参考文本块。"""
    lines: list[str] = []
    for h in hits:
        e = h["entry"]
        typ = e.get("typical") or {}
        extra: list[str] = []
        if typ.get("temperature_C"):
            extra.append(f"{typ['temperature_C'][0]:g}-{typ['temperature_C'][-1]:g}°C")
        if typ.get("pressure_bar"):
            extra.append(f"{typ['pressure_bar'][0]:g}-{typ['pressure_bar'][-1]:g} bar")
        if typ.get("reversible"):
            extra.append("可逆")
        lines.append(f"- {e['name']}：{e['equation']}（{'、'.join(extra) or e['kind']}）")
    return "\n".join(lines)
