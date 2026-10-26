# AI 化工反应器智能建模

根据自然语言描述，自动判断反应器类型（Conversion / Equilibrium / Gibbs / CSTR / PFR），
在 Aspen HYSYS 中完成建模与求解，返回产物分布等真实模拟结果。

**真机验证结论**（Aspen HYSYS V15，实测）：

| 场景 | 反应器 | 结果 |
|------|--------|------|
| 甲烷蒸汽重整 710°C（考核场景 1） | Equilibrium | 29.4s 收敛，H₂=0.56、CO=0.135、CO₂=0.039，出口 710.4°C |
| 甲烷蒸汽重整 600°C（考核场景 1） | Equilibrium | 41.4s 收敛，H₂=0.495、CO=0.165，出口 599.3°C |
| 甲苯歧化 50%（考核场景 2） | Conversion | 23.2s 收敛，甲苯剩余 0.50 / 苯 0.25 / 二甲苯 0.25 |
| 甲烷完全燃烧（通用验证） | Gibbs | 22.6s 收敛，2791.9°C |
| 甲醇合成（通用验证） | Equilibrium | 2.7s 收敛 |

本地冒烟测试 `scripts/smoke_local.py` 共 **92 项断言全部通过**（解析/选择逻辑/守恒校验/
Mock 全链路/COM duty 迭代算法/归一化/知识库兜底）。

## 架构

```
自然语言描述
    │
    ├─ 解析层  app/llm_parser.py     LLM（OpenAI 兼容接口）→ ModelingTask JSON
    │          app/parser_rules.py   规则引擎（知识库 + 正则抽取），LLM 失败/漏给反应时兜底
    │          app/selector.py       反应器选择逻辑（考核文档第 8 节规则的代码化）
    │          app/component_map.py  中文组分名/化学式 → HYSYS 组分库名
    │          app/validators.py     元素守恒硬校验（不守恒的反应直接拒绝并澄清）
    │
    │      统一契约：ModelingTask（app/schema.py）
    │
    ├─ 适配层  app/adapters/com_hysys.py  真实 HYSYS COM 联动（win32com）
    │          app/adapters/mock.py       本地 Mock（无 HYSYS 时联调，结果带 [Mock] 标记）
    │
    ├─ 服务层  app/service.py + app/main.py（FastAPI）
    └─ 界面    static/index.html（浏览器页面：类型/理由/假设/需要澄清/组成表/案例保存）
```

核心设计：**解析层只产出统一的 ModelingTask JSON，适配层只认 JSON** ——
任何新反应都走同一条流水线，天然支持"两个必测场景之外的相关反应"。
所有场景**先尝试真实 HYSYS 建模求解**；仅当 COM 侧受限（见下文"已知限制"）时才降级
Mock 估算，并在结果中明确标注为非真实 HYSYS 计算值。

## 快速开始

```bash
cd reactor-ai
pip install -r requirements.txt
python scripts/smoke_local.py        # 冒烟测试（不需要 HYSYS，92 项）
python -m app.main                   # 打开 http://127.0.0.1:8000
```

无 HYSYS / 未配置 LLM 也可完整跑通（规则引擎 + Mock 适配器）；配置后即接入真实链路。

## 部署到 HYSYS 工作站（真实模拟）

在装有 Aspen HYSYS V15 的 Windows 工作站上：

```powershell
pip install -r requirements.txt
python scripts/verify_com.py         # 逐项验证 COM 链路，全部 [OK] 再继续
# 若某步失败：python scripts/inspect_hysys.py 查看 COM 对象真实方法名

copy .env.example .env               # 填入 LLM APIKey（也可用系统环境变量）
python -m app.main                   # HOST=0.0.0.0 供局域网浏览器访问
```

`HYSYS_MODE=auto`（默认）时自动检测本机 HYSYS：检测到即走真实 COM，
界面与结果中会标注当前适配器（"Aspen HYSYS COM（真实模拟计算）"或 Mock）。

## 配置（.env 或环境变量）

| 变量 | 说明 | 默认 |
|------|------|------|
| `LLM_API_BASE` | OpenAI 兼容接口地址（硅基流动 `https://api.siliconflow.cn/v1`） | 空=仅规则解析 |
| `LLM_API_KEY` | 对应密钥 | 空 |
| `LLM_MODEL` | 推荐 `deepseek-ai/DeepSeek-V3`；免费档 `Qwen/Qwen2.5-7B-Instruct` 可跑通（有规则引擎兜底） | `Qwen/Qwen2.5-7B-Instruct` |
| `HYSYS_MODE` | `auto`（检测到 HYSYS 用真）/ `com` / `mock` | `auto` |
| `HYSYS_SAVE_CASE` | 求解成功后自动保存 .hsc 案例的全局默认 | `false` |
| `HYSYS_SAVE_DIR` | 案例保存目录（文件名=时间戳+反应器名） | `./results_hsc` |
| `HOST` / `PORT` | 服务监听 | `127.0.0.1` / `8000` |

案例保存为三级开关：界面「求解后保存 HYSYS 案例」勾选（每次请求可选）>
`HYSYS_SAVE_CASE` 全局默认；**仅在真实 HYSYS 求解成功后保存**，Mock 降级结果不生成文件。

## 反应器选择逻辑（考核第 8 节规则的代码化，app/selector.py）

1. 提供动力学参数 → 液相 **CSTR** / 气相 **PFR**（管式 PFR、釜式 CSTR；聚合体系单独评估）
2. 无动力学但给出转化率/收率/选择性 → **Conversion**（场景 2 甲苯歧化）
3. 产物分布未知、燃烧、极高温 → **Gibbs**（自由能最小化，无需定义反应式；优先于规则 4）
4. 可逆 + 平衡控制（提供 Ka / 吉布斯自由能）→ **Equilibrium**（场景 1 重整）
5. 信息不足 → 界面"需要澄清"区主动提问，不瞎猜

## 轻量知识库（结构化 RAG）

`app/knowledge/reactions.json` 收录 9 条典型反应（重整/变换/歧化/合成氨/燃烧/
乙苯脱氢/甲醇合成/二氧化硫氧化/乙烯水合），每条含化学计量比、典型工况与出处。
检索器 `app/knowledge/retriever.py` 用组分集合匹配打分（精确命中 + 相似召回 + 别名加分），
不引入向量库——知识规模小、结构化程度高，集合匹配比向量召回更准且零依赖。接入点：

1. **规则引擎已知反应匹配**（新增反应只需改 JSON）；
2. **典型工况假设**：温度/压力/流量缺失时参考命中条目（假设标注出处）；
3. **命名反应兜底**：文本提及条目别名且反应物齐全但未写方程式（如"甲烷蒸汽重整，
   进料甲烷和水蒸气"）→ 直接采用知识库反应式，避免 LLM 漏给反应时任务不可建模；
4. **检索增强的澄清**：无法识别反应式时附上相似反应供一键确认；
5. **LLM 检索增强**：相似反应注入 prompt（冲突时以用户描述为准）。

## HYSYS COM 建模配方（V15 真机验证，app/adapters/com_hysys.py）

- **先赋物性包、后加组分**：顺序反了会在赋包校验时对"不推荐"组合（酸/酯/醇+SRK）
  弹模态警告导致 com_error；加组分时的警告由 `_ModalWatchdog` 守护线程
  （Win32 枚举 #32770 + BM_CLICK）自动点掉，弹窗文字记入结果消息
- 组分真名差异：乙酸乙酯=`E-Acetate`、乙苯=`E-Benzene`、丙烯=`Propene`（`comps.Add`
  对错误名直接拒绝）；中文/化学式/别名由 component_map 统一映射
- 无独立 CSTR 单元：CSTR 用转化率反应器挂动力学反应集近似，须写反应器容积
- 指定出口温度：反应器无温度 spec 属性，用能量流 duty 割线迭代逼近
  （起点避开不可解的绝热点 duty=0，负荷上限 1e12 适配大进料量）
- 求解触发：COM 无 Run()，写规格须在 `Solver.CanSolve=False` 门控内进行，
  抬高后轮询 `IsSolving`，避免"未开始"被误读成"已收敛"
- 组成归一化：LLM 偶发输出之和≠1 的摩尔分数，解析层强制按比例归一

## 已知限制（诚实标注，不假装成功）

- **动力学反应 COM 挂载限制**：V15 COM 创建的 kineticrxn 无法挂载到任何反应器
  （`rx.ReactionSet` 赋值静默失效，跨组分/物性包/相态/是否 Balance 真机四组对照复现；
  GUI 手工可解）。故动力学 CSTR/PFR 在 COM 尝试失败后自动降级 Mock 估算，
  结果明确标注"非真实 HYSYS 计算值"及原因——符合"先 HYSYS、失败降级并说明"的策略。
- **HYSYS 实例长会话劣化**：同一实例连续跑数十个 case 后可能全线 NotSolved=3，
  taskkill 活动实例冷启动即恢复（机器另有若干杀不掉的僵尸进程，几十 KB，无影响）。
- COM 新建 case 不支持 Peng-Robinson，自动回退 SRK（结果中标注）。

## 项目结构

```
app/            后端（解析/选择/适配/服务/FastAPI）
  knowledge/    结构化反应知识库 + 检索器
  adapters/     com_hysys.py（真实 COM）/ mock.py（降级/联调）
static/         前端单页（index.html）
scripts/        smoke_local.py（92 项冒烟）/ verify_com.py（COM 链路自检）/ inspect_hysys.py
results_hsc/    求解成功后的 .hsc 案例存档
```
