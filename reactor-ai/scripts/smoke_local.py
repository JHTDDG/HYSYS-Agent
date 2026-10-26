"""本地冒烟测试（不需要 HYSYS）：验证解析器 + 选择逻辑 + Mock 适配器全链路。

用法：python scripts/smoke_local.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HYSYS_MODE", "mock")
os.environ["REACTOR_AI_QUIET"] = "1"  # 冒烟测试静默推理日志，保持输出干净

from app.adapters.mock import MockAdapter  # noqa: E402
from app.parser_rules import rule_parse  # noqa: E402
from app.selector import Features, select_reactor  # noqa: E402
from app.service import simulate  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {msg}")
    if not cond:
        FAILURES.append(msg)


print("== 1. 选择逻辑单元 ==")
d = select_reactor(Features(has_kinetics=True, phase="liquid"))
check(d.reactor_type == "cstr", "动力学+液相 -> CSTR")
d = select_reactor(Features(has_kinetics=True, phase="gas"))
check(d.reactor_type == "pfr", "动力学+气相 -> PFR")
d = select_reactor(Features(has_conversion=True))
check(d.reactor_type == "conversion", "无动力学+给转化率 -> Conversion")
d = select_reactor(Features(reversible=True, equilibrium_controlled=True))
check(d.reactor_type == "equilibrium", "可逆+平衡控制 -> Equilibrium")
d = select_reactor(Features(combustion=True, products_unknown=True))
check(d.reactor_type == "gibbs", "燃烧/产物未知 -> Gibbs")

print("== 2. 场景1：甲烷蒸汽重整 ==")
tasks = rule_parse(P1 := ("我需要模拟甲烷蒸汽重整。进料是甲烷和水蒸气（摩尔比 1:2.7），有两个反应，"
                          "主反应甲烷和水反应生成一氧化碳和氢气；副反应一氧化碳和水蒸汽反应生成二氧化碳和氢气。"
                          "1、重整炉出口气温度为 710°C，压力 13.5 bar，进料温度 520°C；"
                          "2、重整炉出口气温度为 600°C，压力 13.5 bar，进料温度 520°C。"
                          "进料流量可以自定，要求符合一个工厂一年正常的处理量。"))
check(len(tasks) == 2, f"识别出两个工况（实际 {len(tasks)}）")
t1 = tasks[0]
check(t1.reactor_type == "equilibrium", f"反应器类型 = equilibrium（实际 {t1.reactor_type}）")
check({c.hysys_name for c in t1.components} == {"Methane", "H2O", "CO", "H2", "CO2"}, "组分齐全")
check(len(t1.reactions) == 2, f"识别两个反应（实际 {len(t1.reactions)}）")
check(t1.reactor.outlet_temperature_C == 710.0, f"出口温度 710（实际 {t1.reactor.outlet_temperature_C}）")
check(t1.reactor.pressure_bar == 13.5, f"压力 13.5 bar（实际 {t1.reactor.pressure_bar}）")
check(any(f.flow and f.flow.value == 1000.0 for f in t1.feeds), "甲烷流量按典型规模假设 1000 kgmole/h")

print("== 3. 场景2：甲苯歧化 ==")
tasks = rule_parse(P2 := ("请帮我完成甲苯歧化反应的模拟。甲苯原料进入转化率反应器，发生歧化反应："
                          "2C7H8 = C6H6 + C8H10。甲苯进料流量 10000 kg/h，进料温度 380°C，操作压力 2.5MPa，"
                          "甲苯转化率为 50%，反应产物为苯和二甲苯（邻、间、对三种异构体）。"
                          "不需要探讨动力学，只关心该转化率下的出口浓度分布，不考虑其他副反应。"))
check(len(tasks) == 1, "单任务")
t2 = tasks[0]
check(t2.reactor_type == "conversion", f"反应器类型 = conversion（实际 {t2.reactor_type}）")
check(len(t2.reactions) == 3, f"三个异构体反应（实际 {len(t2.reactions)}）")
check(abs(sum(r.conversion for r in t2.reactions) - 0.5) < 1e-6, "三个反应转化率之和 = 50%")
check(t2.feeds[0].flow.value == 10000.0 and t2.feeds[0].flow.unit == "kg/h", "进料 10000 kg/h")
check(t2.reactor.pressure_bar == 25.0, f"2.5MPa -> 25 bar（实际 {t2.reactor.pressure_bar}）")
# 考核文档原文措辞："不对反应动力学进行深入探讨"——否定表达不完整会误判为动力学体系
t2b = rule_parse("用户明确提出不对反应动力学进行深入探讨，只需要知道在该转化率下反应器出口的浓度分布。"
                 "甲苯歧化：2C7H8 = C6H6 + C8H10，甲苯转化率 50%。")[0]
check(t2b.reactor_type == "conversion",
      f"原文措辞'不对反应动力学进行深入探讨'不误判动力学（实际 {t2b.reactor_type}）")

print("== 4. 通用验证：Gibbs 燃烧 / 动力学 CSTR / 动力学 PFR ==")
tasks = rule_parse(P3 := "模拟甲烷完全燃烧：甲烷和氧气反应生成二氧化碳和水，产物分布未知，进料甲烷 100 kgmole/h，氧气 300 kgmole/h，温度 25°C，压力 1 bar。")
check(tasks[0].reactor_type == "gibbs", f"燃烧 -> Gibbs（实际 {tasks[0].reactor_type}）")
tasks = rule_parse(P4 := "液相反应：乙酸和乙醇反应生成乙酸乙酯和水，动力学参数 k=0.01，活化能 50 kJ/mol，釜式反应器。")
check(tasks[0].reactor_type == "cstr", f"动力学+液相+釜式 -> CSTR（实际 {tasks[0].reactor_type}）")
t4 = tasks[0]
check({c.hysys_name for c in t4.components} == {"AceticAcid", "Ethanol", "E-Acetate", "H2O"},
      "乙酸/乙酸乙酯别名解析（AceticAcid/E-Acetate，HYSYS V15 真名）")
r4 = t4.reactions[0]
check(r4.kind == "kinetic", f"给出动力学参数 -> 反应标记 kinetic（实际 {r4.kind}）")
check(r4.rate_constant_a == 0.01, f"指前因子 k=0.01 抽取（实际 {r4.rate_constant_a}）")
check(r4.activation_energy == 50.0, f"活化能 50 kJ/mol 抽取（实际 {r4.activation_energy}）")
check(r4.reaction_orders == {"AceticAcid": 1.0, "Ethanol": 1.0},
      f"未指明级数按化学计量系数绝对值（实际 {r4.reaction_orders}）")
check(r4.basis_component == "AceticAcid", f"基准组分取首个反应物（实际 {r4.basis_component}）")
check(any("动力学" in a for a in t4.assumptions), "动力学参数抽取说明写入假设")
check(not any("守恒" in c for c in t4.clarifications),
      "酯化反应元素守恒校验通过（C2H4O2+C2H6O=C4H8O2+H2O）")

# 气相动力学 -> PFR：知识库乙苯脱氢条目 + 指前因子/活化能抽取
tasks = rule_parse(P5 := ("气相乙苯脱氢反应：乙苯在催化剂上脱氢生成苯乙烯和氢气，"
                          "指前因子 1e9，活化能 120 kJ/mol，采用管式反应器。"))
t5 = tasks[0]
check(t5.reactor_type == "pfr", f"动力学+气相+管式 -> PFR（实际 {t5.reactor_type}）")
r5 = t5.reactions[0]
check(r5.kind == "kinetic", f"气相动力学反应标记 kinetic（实际 {r5.kind}）")
check(r5.stoich == {"E-Benzene": -1, "Styrene": 1, "H2": 1},
      f"乙苯脱氢反应式来自知识库（实际 {r5.equation}）")
check(r5.rate_constant_a == 1e9, f"指前因子 1e9 抽取（实际 {r5.rate_constant_a}）")
check(r5.activation_energy == 120.0, f"活化能 120 kJ/mol 抽取（实际 {r5.activation_energy}）")
check(r5.reaction_orders == {"E-Benzene": 1.0},
      f"脱氢反应级数默认按反应物化学计量（实际 {r5.reaction_orders}）")

print("== 5. Mock 适配器端到端 ==")
mock = MockAdapter()
for t in rule_parse(P1):
    r = mock.run(t)
    ch4_in = next(f.composition.get("Methane", 0) * (f.flow.value if f.flow else 0)
                  for f in t.feeds if "Methane" in (f.composition or {}))
    prod = r.streams[-1]
    h2_frac = prod.composition.get("H2", 0.0)
    check(r.converged and h2_frac > 0.1, f"重整 @{t.reactor.outlet_temperature_C:.0f}°C：H2 摩尔分数 {h2_frac:.3f}")
r = mock.run(rule_parse(P2)[0])
prod = r.streams[-1]
check(abs(prod.composition.get("Toluene", 0) - 0.5) < 1e-6,
      f"歧化出口甲苯摩尔分数 0.5（实际 {prod.composition.get('Toluene', 0):.3f}）")
check(abs(prod.composition.get("Benzene", 0) - 0.25) < 1e-6, "歧化出口苯摩尔分数 0.25")
r = mock.run(t4)
prod = r.streams[-1]
check(r.converged and prod.composition.get("E-Acetate", 0) > 0.3,
      f"动力学 CSTR（酯化）Mock 收敛，乙酸乙酯摩尔分数 {prod.composition.get('E-Acetate', 0):.3f}")
r = mock.run(t5)
prod = r.streams[-1]
check(r.converged and prod.composition.get("Styrene", 0) > 0.3,
      f"动力学 PFR（乙苯脱氢）Mock 收敛，苯乙烯摩尔分数 {prod.composition.get('Styrene', 0):.3f}")

print("== 6. service 层端到端 ==")
out = simulate(P2)
check(out["adapter"] == "mock" and len(out["results"]) == 1, "service.simulate 全链路 OK")

print("== 7. 元素守恒硬校验 ==")
tasks = rule_parse("模拟甲烷和氧气反应生成二氧化碳的过程，进料甲烷 100 kgmole/h，氧气 100 kgmole/h，温度 25°C，压力 1 bar。")
t7 = tasks[0]
check(len(t7.reactions) == 0, f"不守恒反应（甲烷+氧气->二氧化碳，缺水）被拒绝（实际 {len(t7.reactions)} 个反应）")
check(any("守恒" in c for c in t7.clarifications),
      "澄清区提示元素不守恒（" + ("；".join(c for c in t7.clarifications if "守恒" in c)[:60]) + "...）")
tasks_ok = rule_parse("一氧化碳和水蒸气反应生成二氧化碳和氢气，温度 200°C，压力 30 bar。")
check(len(tasks_ok[0].reactions) == 1, "守恒反应（水煤气变换）正常通过")

print("== 8. LLM JSON 修复重试（模拟两次调用） ==")
import app.llm_parser as lp  # noqa: E402

calls = {"n": 0}

def _fake_chat(messages):
    calls["n"] += 1
    if calls["n"] == 1:
        return "好的，以下是解析结果：{tasks: [ 这不是合法 JSON"
    return ('{"tasks":[{"title":"测试任务","reactor_type":"conversion",'
            '"components":[{"hysys_name":"Toluene"}],"feeds":[],"reactions":[],'
            '"reactor":{"type":"conversion"},"assumptions":[],"clarifications":[]}]}')

lp._chat = _fake_chat
tasks = lp.llm_parse("任意文本")
check(calls["n"] == 2 and tasks[0].reactor_type == "conversion",
      f"坏 JSON 触发重试且第二次成功（调用 {calls['n']} 次）")

check_bad = {"n": 0}
def _fake_chat_always_bad(messages):
    check_bad["n"] += 1
    return "完全不是 JSON"
lp._chat = _fake_chat_always_bad
try:
    lp.llm_parse("任意文本")
    check(False, "始终坏输出应抛异常")
except Exception:
    check(check_bad["n"] == 2, f"始终坏输出在重试 2 次后抛异常（实际 {check_bad['n']} 次后抛出）")

print("== 9. 轻量知识库检索（RAG） ==")
from app.adapters.mock import MockAdapter as _MA  # noqa: E402
from app.knowledge.retriever import retrieve  # noqa: E402

hits = retrieve(text="做一个甲醇合成装置的模拟，原料是一氧化碳和氢气")
check(bool(hits) and hits[0]["entry"]["name"] == "甲醇合成",
      f"检索命中甲醇合成（实际 {[h['entry']['name'] for h in hits]}）")

tasks = rule_parse("模拟一氧化碳和氢气反应生成甲醇的过程，温度 220°C，压力 80 bar。")
t9 = tasks[0]
check(any(r.equation == "CO + 2H2 = CH3OH" for r in t9.reactions),
      f"知识库精确命中补全甲醇反应式（实际 {[r.equation for r in t9.reactions]}）")
check(t9.reactor_type == "equilibrium", f"甲醇合成 -> equilibrium（实际 {t9.reactor_type}）")
check(any("知识库" in a for a in t9.assumptions), "假设标注知识库出处")
r9 = _MA().run(t9)
check(r9.converged, "知识库反应 Mock 端到端收敛")

tasks = rule_parse("模拟乙烯和水反应生成乙醇的过程，温度 260°C，压力 70 bar。")
check(tasks[0].reactor_type == "equilibrium",
      f"乙烯水合 -> equilibrium（实际 {tasks[0].reactor_type}）")

tasks = rule_parse("模拟丙烷燃烧：丙烷和氧气反应生成二氧化碳和水，温度 25°C，压力 1 bar。")
t9c = tasks[0]
check(t9c.reactor_type == "gibbs", f"知识库未命中丙烷燃烧 -> 仍走 Gibbs（实际 {t9c.reactor_type}）")
check(len(t9c.reactions) == 0,
      f"知识库未命中时不瞎借反应式（实际 {len(t9c.reactions)} 个反应）")

tasks = rule_parse("帮我模拟一下二氧化硫催化氧化制三氧化硫，用于硫酸装置。")
t9d = tasks[0]
check(any("知识库检索到相似反应" in c and "2SO2 + O2 = 2SO3" in c for c in t9d.clarifications),
      "澄清文案附知识库相似反应提示（SO2 组分不全场景）")

print("== 10. COM 失败降级路径（cstr/pfr -> Mock 估算，明确标注） ==")
from contextlib import contextmanager  # noqa: E402

import app.service as svc  # noqa: E402


class _FakeComFail:
    """伪造 COM 适配器：名字是 com，但一执行就失败。"""
    name = "com"

    def available(self) -> bool:
        return True

    def run(self, task):
        raise RuntimeError("模拟 COM 调用失败（冒烟测试）")


@contextmanager
def _fake_com():
    """临时替换 get_adapter 并屏蔽 LLM 配置（保证走规则解析），退出后恢复。"""
    orig_get, orig_key = svc.get_adapter, os.environ.pop("LLM_API_KEY", None)
    svc.get_adapter = lambda: _FakeComFail()
    try:
        yield
    finally:
        svc.get_adapter = orig_get
        if orig_key is not None:
            os.environ["LLM_API_KEY"] = orig_key


with _fake_com():
    out_cstr = svc.simulate(P4)
    out_pfr = svc.simulate(P5)
    out_eq = svc.simulate(P1)
    out_gibbs = svc.simulate(P3)

check(out_cstr["adapter"] == "com", "伪造 COM 适配器生效")
r0 = out_cstr["results"][0]
check(r0["converged"] is True, "cstr：COM 失败后降级 Mock 且收敛")
check("降级" in r0["messages"][0] and "CSTR" in r0["messages"][0],
      f"cstr：消息明确标注降级原因（{r0['messages'][0][:46]}...）")
r0 = out_pfr["results"][0]
check(r0["converged"] is True, "pfr：COM 失败后降级 Mock 且收敛")
check("降级" in r0["messages"][0] and "PFR" in r0["messages"][0],
      f"pfr：消息明确标注降级原因（{r0['messages'][0][:46]}...）")
r0 = out_gibbs["results"][0]
check(r0["converged"] is True, "gibbs：COM 失败后同样降级 Mock 且收敛")
check("Gibbs" in r0["messages"][0] and "降级" in r0["messages"][0],
      f"gibbs：消息明确标注降级原因（{r0['messages'][0][:46]}...）")
r0 = out_eq["results"][0]
check(r0["converged"] is False and "[ERROR]" in r0["messages"][0],
      "equilibrium：COM 失败不降级，直接报错（cstr/pfr/gibbs 走降级）")

print("== 11. 案例保存开关（save_case） ==")
p1_ = svc._save_path("ER-100", set())
p2_ = svc._save_path("ER-100", {p1_})
check(p1_ != p2_ and p1_.endswith("ER-100.hsc") and p2_.endswith("ER-100_1.hsc"),
      f"保存路径唯一化（同秒同名自动加后缀：{os.path.basename(p2_)}）")
check(os.path.dirname(p1_) == svc.save_dir(), "保存目录默认项目根 results_hsc/（可被 HYSYS_SAVE_DIR 覆盖）")
out = svc.simulate(P2, save_case=True)   # Mock 适配器无案例可存，不应报错
check(out["saved_case"] is False and out["results"][0]["case_file"] is None,
      "Mock 适配器不产出 .hsc（saved_case=False，case_file=None）")

print("== 12. 特殊符号/单位规范化 ==")
from app.parser_rules import _extract_flows, _extract_pressures, _extract_temps  # noqa: E402
from app.text_norm import normalize_text  # noqa: E402

check(normalize_text("CH₄ ＋ ２H₂O ⇌ CO＋３H₂") == "CH4 + 2H2O = CO+3H2",
      "下标/全角/可逆箭头规范化")
check(normalize_text("Nm³/h、kg/cm²、10³ Pa") == "Nm3/h、kg/cm2、10³ Pa",
      "上标：紧跟字母转数字（Nm³），数字后保留（10³）")
check(normalize_text("CO(g) ＋ H₂O(l) → CO₂(g) ＋ H₂(g)") == "CO + H2O = CO2 + H2",
      "相态标注去除、单向箭头转等号")

tv = _extract_temps("反应温度 373K，进料 -10°C，操作 100℃")
check(len(tv) == 3 and tv[0] == -10.0 and tv[1] == 100.0 and abs(tv[2] - 99.85) < 1e-6,
      f"负温/℃/开尔文换算 373K->99.85°C（实际 {tv}）")
pv = _extract_pressures(normalize_text("0.6 MPaG、500 kPa、2 atm、10 kg/cm²、3 bar"))
check(len(pv) == 5 and all(abs(a - b) < 1e-9 for a, b in zip(pv, [6.0, 5.0, 2.0265, 9.807, 3.0])),
      f"MPaG/kPa/atm/kgf/cm² 换算到 bar（实际 {pv}）")
fl = _extract_flows(normalize_text("进料 100 kmol/hr、50 t/h、10000 kg/h、200 Nm³/h"))
check([(f["value"], f["unit"]) for f in fl]
      == [(100.0, "kgmole/h"), (50000.0, "kg/h"), (10000.0, "kg/h"), (200.0, "Nm3/h")],
      f"kmol/hr、t/h(吨/时)、Nm³/h 流量单位（实际 {fl}）")

tasks = rule_parse("液相反应：CH₃COOH(液) ＋ C₂H₅OH(液) ⇌ CH₃COOC₂H₅(液) ＋ H₂O(液)，"
                   "动力学参数 k＝0.01，活化能 50 kJ/mol，釜式反应器。")
t12 = tasks[0]
check(t12.reactor_type == "cstr", f"下标+⇌+(液)+全角文本 -> CSTR（实际 {t12.reactor_type}）")
check({c.hysys_name for c in t12.components} == {"AceticAcid", "Ethanol", "E-Acetate", "H2O"},
      "下标化学式组分识别（ch3cooc2h5 别名）")
r12 = t12.reactions[0]
check(r12.kind == "kinetic" and r12.rate_constant_a == 0.01 and r12.activation_energy == 50.0,
      f"k＝0.01（全角等号）与活化能抽取（实际 kind={r12.kind}, A={r12.rate_constant_a}, Ea={r12.activation_energy}）")
check(r12.stoich == {"AceticAcid": -1, "Ethanol": -1, "E-Acetate": 1, "H2O": 1},
      f"⇌ 方程解析与元素守恒（实际 {r12.equation}）")

# ---- 组分名容错 + 多股进料配对（先尝试 HYSYS 的前提是任务本身正确） ----
from app.component_map import resolve_component  # noqa: E402

check(resolve_component("Carbon_Dioxide") == "CO2"
      and resolve_component("Acetic Acid") == "AceticAcid",
      "组分名容错：带下划线/空格的 LLM 写法可解析（Carbon_Dioxide->CO2）")

t3 = rule_parse(P3)[0]
got3 = [(f.name, f.flow.value, f.flow.unit) for f in t3.feeds]
check(got3 == [("Feed-Methane", 100.0, "kgmole/h"), ("Feed-Oxygen", 300.0, "kgmole/h")],
      f"多股进料按「组分+流量」配对（实际 {got3}）")
check({c.hysys_name for c in t3.components} >= {"Methane", "Oxygen", "CO2", "H2O"},
      "Gibbs 燃烧任务组分齐全（不再丢氧气进料）")

# ======================================================================
print("== 13. COM duty 迭代算法（假 HYSYS 对象：绝热点 duty=0 不可解） ==")

import math  # noqa: E402
from app.adapters import com_hysys as _ch  # noqa: E402


class _FakeClock:
    """假时钟：让 _wait_solved 的 20s 超时轮询即时完成。"""
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class _FakeSolver:
    def __init__(self, case):
        self._case = case
        self._can = True

    @property
    def IsSolving(self):
        return False

    @property
    def CanSolve(self):
        return self._can

    @CanSolve.setter
    def CanSolve(self, v):
        self._can = v
        self._case.gates.append(v)
        if v:
            self._case._solve()


class _FakeCase:
    """模拟 HYSYS：duty=0 绝热点 NotSolved=3，其余光滑单调可解（仿真机扫描）。"""
    ST_NS, ST_OK = 2, 1

    def __init__(self, t_func, solvable):
        self.t_func = t_func
        self.solvable = solvable
        self.duty = None
        self._status = {self.ST_NS: 1}
        self.gates: list[bool] = []
        self.Solver = _FakeSolver(self)

    def _solve(self):
        if self.duty is not None and self.solvable(self.duty):
            self._status = {self.ST_OK: 7}
        else:
            self._status = {self.ST_NS: 3}

    def GetFlowsheetStatus(self, bit):
        return self._status.get(bit, 0)


class _FakeEnergy:
    def __init__(self, case):
        self._case = case
        self._v = 0.0

    @property
    def HeatFlowValue(self):
        return self._v

    @HeatFlowValue.setter
    def HeatFlowValue(self, v):
        self._v = v
        self._case.duty = v


class _FakeVap:
    def __init__(self, case):
        self._case = case

    @property
    def TemperatureValue(self):
        return self._case.t_func(self._case.duty) + _ch.C2K


def _reformer_T(duty):   # 仿 P1 重整实测曲线：T(1e6)≈282°C，T=710°C 的真解 ≈2.3e8
    return 280.0 + 900.0 * (1.0 - math.exp(-duty / 3.54e8))


_orig_time = _ch.time
_ch.time = _FakeClock()
try:
    ad = _ch.ComHysysAdapter()

    case = _FakeCase(_reformer_T, lambda d: d is not None and d != 0 and abs(d) <= 5.0e8)
    duty = ad._tune_duty(case, _FakeEnergy(case), _FakeVap(case), 710.0)
    check(duty is not None, f"重整式曲线 duty 迭代收敛（实际 {duty}）")
    if duty is not None:
        t_ach = _reformer_T(duty)
        check(abs(t_ach - 710.0) < 1.0,
              f"710°C 目标达到（实际 {t_ach:.2f}°C @ duty={duty:.3e} kJ/h）")
    check(case.GetFlowsheetStatus(_FakeCase.ST_NS) == 0, "迭代结束后 case 处于已解状态")
    check(False in case.gates and case.gates.count(False) == case.gates.count(True),
          "CanSolve 门控写入生效（成对拉低/抬高）")

    case3 = _FakeCase(_reformer_T, lambda d: d is not None and d != 0 and abs(d) <= 5.0e8)
    duty3 = ad._tune_duty(case3, _FakeEnergy(case3), _FakeVap(case3), 350.0)
    check(duty3 is not None and abs(_reformer_T(duty3) - 350.0) < 1.0,
          f"350°C 低温目标收敛（实际 {duty3} -> {_reformer_T(duty3):.2f}°C）")

    case2 = _FakeCase(_reformer_T, lambda d: False)
    duty2 = ad._tune_duty(case2, _FakeEnergy(case2), _FakeVap(case2), 500.0)
    check(duty2 is None, "全线不可解时返回 None（调用方走回退分支）")


    def _reformer_T_big(duty):   # 大进料量（≈27 倍流量）：T=710°C 的真解 ≈9.6e9 > 旧上限 1e9
        return 280.0 + 900.0 * (1.0 - math.exp(-duty / 1.3e10))

    case_big = _FakeCase(_reformer_T_big, lambda d: d is not None and d != 0 and abs(d) <= 2.0e10)
    duty_big = ad._tune_duty(case_big, _FakeEnergy(case_big), _FakeVap(case_big), 710.0)
    check(duty_big is not None and abs(_reformer_T_big(duty_big) - 710.0) < 1.0,
          f"大进料量（真解 duty≈9.6e9 超旧上限）收敛（实际 {duty_big} -> "
          f"{_reformer_T_big(duty_big) if duty_big is not None else 0:.2f}°C）")
    check(duty_big is not None and duty_big > 1.0e9,
          f"duty 迭代可越过旧 1e9 上限（实际 {duty_big if duty_big is not None else 0:.3e}）")

finally:
    _ch.time = _orig_time

# ======================================================================
print("== 14. LLM 进料组成归一化（组成之和 ≠ 1 强制归一） ==")

from app.llm_parser import _normalize_task  # noqa: E402

_task = _normalize_task({
    "title": "归一化测试", "reactor_type": "equilibrium",
    "components": [], "reactions": [],
    "feeds": [{"name": "Feed-M", "temperature_C": 520, "pressure_bar": 13.5,
               "flow": {"value": 1000, "unit": "kgmole/h"},
               "composition": {"Methane": 0.37, "H2O": 1.0}}],
    "reactor": {"type": "equilibrium", "name": "ER-100"},
}, "llm")
_x = _task.feeds[0].composition
check(abs(sum(_x.values()) - 1.0) < 1e-9, f"组成之和归一为 1（实际 {sum(_x.values()):.6f}）")
check(abs(_x.get("Methane", 0) - 0.37 / 1.37) < 1e-9,
      f"甲烷分数 = 0.37/1.37（实际 {_x.get('Methane', 0):.6f}）")
check(any("归一化" in a for a in _task.assumptions),
      f"归一化假设已透出（实际 assumptions={_task.assumptions}）")

# ======================================================================
print("== 15. 规则引擎命名反应兜底（别名提及 + 反应物齐全 → 采用知识库反应式） ==")

_tf = rule_parse("模拟甲烷蒸汽重整，进料甲烷和水蒸气（摩尔比1:2.7），重整炉出口气温度600°C，压力13.5bar，进料温度520°C")
check(len(_tf) == 1 and len(_tf[0].reactions) >= 1,
      f"未写方程式的重整任务可解析出反应（实际 {len(_tf[0].reactions) if _tf else 0} 个）")
check(_tf and any("重整" in r.name for r in _tf[0].reactions),
      f"反应取自知识库（实际 {[r.name for r in _tf[0].reactions] if _tf else []}）")
check(_tf and any("知识库" in a for a in _tf[0].assumptions),
      "知识库来源假设已透出")
_tg = rule_parse("模拟某反应，进料氮气和氢气各 100 kgmole/h，温度 400°C，压力 200 bar")
check(len(_tg[0].reactions) == 0 and _tg[0].clarifications,
      "未提及已知反应名且无方程式时仍走澄清（不误兜底）")

print("=" * 60)
if FAILURES:
    print(f"共 {len(FAILURES)} 项失败：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("全部通过")
