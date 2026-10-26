"""中文/常用名/化学式 -> HYSYS 组分库名称的映射与解析。

这一层很容易被低估：LLM 说"甲苯"，HYSYS 组分库里叫 "Toluene"；
二甲苯有三种异构体，必须分别映射到 o-/m-/p-Xylene。
"""
from __future__ import annotations

import re

ALIAS_TO_HYSYS: dict[str, str] = {
    # 烷烃 / 烯烃
    "甲烷": "Methane", "ch4": "Methane",
    "乙烷": "Ethane", "c2h6": "Ethane",
    "乙烯": "Ethylene", "c2h4": "Ethylene",
    "丙烷": "Propane", "c3h8": "Propane",
    "丙烯": "Propene", "c3h6": "Propene",
    "丁烷": "n-Butane", "c4h10": "n-Butane",
    "戊烷": "n-Pentane", "c5h12": "n-Pentane",
    "己烷": "n-Hexane", "c6h14": "n-Hexane",
    # 无机
    "水": "H2O", "水蒸气": "H2O", "蒸汽": "H2O", "h2o": "H2O",
    "氢气": "H2", "h2": "H2",
    "氮气": "Nitrogen", "n2": "Nitrogen",
    "氧气": "Oxygen", "o2": "Oxygen",
    "一氧化碳": "CO", "co": "CO",
    "二氧化碳": "CO2", "co2": "CO2",
    "硫化氢": "H2S", "h2s": "H2S",
    "二氧化硫": "SO2", "so2": "SO2",
    "三氧化硫": "SO3", "so3": "SO3",
    "氨": "Ammonia", "氨气": "Ammonia", "nh3": "Ammonia",
    "合成氨": "Ammonia",
    # 芳烃
    "苯": "Benzene", "c6h6": "Benzene",
    "环己烷": "Cyclohexane", "c6h12": "Cyclohexane",
    "甲苯": "Toluene", "c7h8": "Toluene",
    "二甲苯": "m-Xylene", "c8h10": "m-Xylene",
    "邻二甲苯": "o-Xylene",
    "间二甲苯": "m-Xylene",
    "对二甲苯": "p-Xylene",
    "乙苯": "E-Benzene", "c8h10_乙苯": "E-Benzene",
    # 注意：以下是 Aspen HYSYS V15 组分库的真实名称（COM 逐一真机验证），
    # 不是常见英文名——乙酸乙酯=E-Acetate、乙苯=E-Benzene、丙烯=Propene，
    # 写成 EthylAcetate/Ethylbenzene 会被 comps.Add 以 com_error 拒绝
    "苯乙烯": "Styrene",
    # 含氧化合物
    "甲醇": "Methanol", "ch3oh": "Methanol", "ch4o": "Methanol",
    "乙醇": "Ethanol", "c2h5oh": "Ethanol",
    "乙酸": "AceticAcid", "醋酸": "AceticAcid", "ch3cooh": "AceticAcid",
    "乙酸乙酯": "E-Acetate", "c4h8o2": "E-Acetate", "ch3cooc2h5": "E-Acetate",
    "甲醛": "Formaldehyde",
    # 英文别名：LLM 可能直接输出英文名（不一定是项目标准名），统一规整
    "hydrogen": "H2", "oxygen": "Oxygen", "nitrogen": "Nitrogen",
    "methane": "Methane", "ethane": "Ethane", "ethylene": "Ethylene",
    "propane": "Propane", "propylene": "Propene",
    "benzene": "Benzene", "toluene": "Toluene", "cyclohexane": "Cyclohexane",
    "ammonia": "Ammonia", "methanol": "Methanol", "ethanol": "Ethanol",
    "carbon monoxide": "CO", "carbon dioxide": "CO2",
    "carbonmonoxide": "CO", "carbondioxide": "CO2",
    "water": "H2O", "steam": "H2O", "styrene": "Styrene",
    # LLM 常输出"教科书式"英文名，而 HYSYS 库里是缩写名——统一兜底映射
    "ethylacetate": "E-Acetate", "ethyl acetate": "E-Acetate",
    "ethylbenzene": "E-Benzene", "ethyl benzene": "E-Benzene",
    "propylene": "Propene", "propene": "Propene",
}

KNOWN_HYSYS_NAMES = {v.lower() for v in ALIAS_TO_HYSYS.values()}

# Mock 适配器粗算用的分子量 (kg/kmol)
MOLAR_MASS: dict[str, float] = {
    "Methane": 16.04, "Ethane": 30.07, "Ethylene": 28.05,
    "Propane": 44.10, "Propene": 42.08, "n-Butane": 58.12,
    "n-Pentane": 72.15, "n-Hexane": 86.18,
    "H2O": 18.02, "H2": 2.016, "Nitrogen": 28.01, "Oxygen": 32.00,
    "CO": 28.01, "CO2": 44.01, "H2S": 34.08, "Ammonia": 17.03,
    "SO2": 64.06, "SO3": 80.06,
    "Benzene": 78.11, "Cyclohexane": 84.16, "Hexane": 86.18,
    "Toluene": 92.14,
    "m-Xylene": 106.17, "o-Xylene": 106.17, "p-Xylene": 106.17,
    "E-Benzene": 106.17, "Styrene": 104.15,
    "Methanol": 32.04, "Ethanol": 46.07, "Formaldehyde": 30.03,
    "AceticAcid": 60.05, "E-Acetate": 88.11,
}

# 元素组成（用于反应式元素守恒硬校验）；缺数据的组分将跳过校验
ELEMENT_COMPOSITION: dict[str, dict[str, int]] = {
    "Methane": {"C": 1, "H": 4}, "Ethane": {"C": 2, "H": 6},
    "Ethylene": {"C": 2, "H": 4}, "Propane": {"C": 3, "H": 8},
    "Propene": {"C": 3, "H": 6}, "n-Butane": {"C": 4, "H": 10},
    "n-Pentane": {"C": 5, "H": 12}, "n-Hexane": {"C": 6, "H": 14},
    "H2O": {"H": 2, "O": 1}, "H2": {"H": 2},
    "Nitrogen": {"N": 2}, "Oxygen": {"O": 2},
    "CO": {"C": 1, "O": 1}, "CO2": {"C": 1, "O": 2},
    "H2S": {"H": 2, "S": 1}, "Ammonia": {"N": 1, "H": 3},
    "SO2": {"S": 1, "O": 2}, "SO3": {"S": 1, "O": 3},
    "Benzene": {"C": 6, "H": 6}, "Cyclohexane": {"C": 6, "H": 12},
    "Hexane": {"C": 6, "H": 14},   # 己烷≠环己烷：LLM 常混用，有数据才能被守恒校验拦截
    "Toluene": {"C": 7, "H": 8},
    "m-Xylene": {"C": 8, "H": 10}, "o-Xylene": {"C": 8, "H": 10},
    "p-Xylene": {"C": 8, "H": 10},
    "E-Benzene": {"C": 8, "H": 10}, "Styrene": {"C": 8, "H": 8},
    "Methanol": {"C": 1, "H": 4, "O": 1}, "Ethanol": {"C": 2, "H": 6, "O": 1},
    "Formaldehyde": {"C": 1, "H": 2, "O": 1},
    "AceticAcid": {"C": 2, "H": 4, "O": 2}, "E-Acetate": {"C": 4, "H": 8, "O": 2},
}


def resolve_component(name: str) -> str | None:
    """把任意写法解析成 HYSYS 组分名。解析不了返回 None（交给澄清流程）。"""
    if not name:
        return None
    key = name.strip().lower()
    if key in KNOWN_HYSYS_NAMES:
        # 已经是 HYSYS 名称，还原为标准大小写
        for v in ALIAS_TO_HYSYS.values():
            if v.lower() == key:
                return v
    hit = ALIAS_TO_HYSYS.get(key)
    if hit:
        return hit
    # 容错：LLM 常输出 Carbon_Dioxide / carbon-dioxide / Acetic Acid 这类带
    # 分隔符的写法，去掉分隔符后再匹配一次
    squeezed = re.sub(r"[\s_\-]+", "", key)
    if squeezed != key:
        if squeezed in KNOWN_HYSYS_NAMES:
            for v in ALIAS_TO_HYSYS.values():
                if v.lower() == squeezed:
                    return v
        return ALIAS_TO_HYSYS.get(squeezed)
    return None


def scan_components(text: str) -> list[str]:
    """按别名扫描文本，返回按出现顺序去重的 HYSYS 组分名列表。"""
    found: list[str] = []
    lowered = text.lower()
    # ascii 别名要求词边界，避免 co 误匹配进 co2 / 单词内部
    ascii_aliases = sorted([a for a in ALIAS_TO_HYSYS if a.isascii()], key=len, reverse=True)
    cn_aliases = sorted([a for a in ALIAS_TO_HYSYS if not a.isascii()], key=len, reverse=True)
    hit_positions: list[tuple[int, str, str]] = []
    for a in cn_aliases:
        for m in re.finditer(re.escape(a), text):
            hit_positions.append((m.start(), a, ALIAS_TO_HYSYS[a]))
    for a in ascii_aliases:
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", lowered):
            hit_positions.append((m.start(), a, ALIAS_TO_HYSYS[a]))
    for _, _, hname in sorted(hit_positions, key=lambda x: x[0]):
        if hname not in found:
            found.append(hname)
    return found


def display_name(hysys_name: str) -> str:
    for alias, hname in ALIAS_TO_HYSYS.items():
        if hname == hysys_name and not alias[0].isascii():
            return alias
    return hysys_name
