---
name: dsh-self-improve
description: "Self-improvement (self-learning experience library / 自学习经验库): after each task, reflect on what worked and what failed and record it; before starting tasks, load relevant past learnings and apply them. Builds a persistent cross-session knowledge base so the agent keeps getting better and avoids repeating mistakes. 自学习：任务后反思记录经验，任务前加载相关经验应用，形成越用越强的闭环。"
metadata:
  short-description: Self-learning loop (reflect after task / load & apply before task / review periodically)
---

# 自学习经验库（Self-Improve）

> **本副本归 DSH 专用，与 Codex 完全独立**（2026-09-25 部署）。
> 上游：[diancangdu/codex-self-improve](https://github.com/diancangdu/codex-self-improve)
> 技能目录：`C:\Users\<user>\.dsh\skills\dsh-self-improve\`
> 经验库目录：`C:\Users\<user>\.dsh\skills\dsh-self-improve\.learnings\`（**技能自带**，
> 2026-09-25 由 `<old-learnings-dir>` 迁入；**不是** Codex 的 `F:\CodexHome\.learnings`）
>
> 两库物理隔离、互不读写。环境变量 `CODEX_LEARNINGS_DIR` 是工具自带的覆盖接口，
> 名字保留是为了跟上游兼容；**不设它时兜底也已经指向本实例的库**，不会掉回共享库。

## 🎯 用途与定位

这是一个**自学习闭环**：让 agent 从每次任务中总结经验，并在未来任务中自动应用，做到"越用越强、不重复犯错"。

**为什么有效**：agent 每次会话都从零开始（无记忆），但通过**经验文件**可以在会话之间传递知识。任务后反思沉淀，任务前加载应用——这个闭环让经验像滚雪球一样累积。

## ★ 记录时的格式铁律：限定条件必须写进标题（2026-09-27 新增）

**为什么**：会话注入给模型看的**不是全文**，而是 `标题 + 正文首行`（每条约 170 字符，
只占条目全长的中位 27%）。所以**条件写在正文里就等于没写** —— 一条「带条件的经验」
会被当成「**无条件通则**」使用。

实测证据（167 条，2026-09-27）：
- 用紧表（真限定词）统计，**38%** 的条目其限定词在注入片段里看不到。这是**上界** ——
  标记词也会命中「本机被网络策略拦」这类**上下文提及**，不是条件。
- 试过在注入侧补救，**两条路都不成立**：
  - 按关键词挑「前提句」：抽查 6 条全是误命中（把「`learn.py add` 只有 `--body`」的
    「只有」当限定词）。**贴错标签比不贴更糟**，因为「（前提：…）」看着权威。
  - 加长正文前缀：110 字→丢失 38%；300 字→26%；要 700 字才到 5%，而那时单条 633
    字符、8 条就超预算。**预算内补不上这个缺口。**

⇒ **唯一可靠的修法是在记录时把条件写进标题**：标题在注入里**完整保留**，
且标题命中在相关性打分里权重 3.0（正文只有 1.0）。

**写法**：把条件放在标题后半段的括号里。
- ✅ `PowerShell .ps1 必须存成 ASCII（本机是 PS 5.1）`
- ❌ `PowerShell .ps1 必须存成 ASCII`（条件丢在正文里）

`learn.py add` 会在「正文前 300 字有限定词、而标题里一个都没有」时**当场给出提醒**
（`title_needs_condition`）。照提醒改标题即可 —— 只在**记录的那一刻**提醒，
因为那时前提还在脑子里。

同一处还有第二条提醒（`near_duplicates`，2026-11-14 加）：新标题与库里某条**共享
≥20 字「长且稀有」的连续原文**时，列出候选并提示可以 `supersede`。触发率实测 3.6%
（1283 条里 46 条），不足以变成噪音；返回 `dup_hint`，**不阻止入库**。
它抓不到「换一种说法讲同一件事」—— 那需要人判断，批量排查用 `learn.py dups`。

## 两条铁律

1. **任务后必反思**：每完成一个任务（或任务告一段落），停下来想 30 秒：**这次什么有效？什么失败？** 有值得记的就记（`learn.py add`），没有就跳过，**但必须想一下**。
2. **任务前必加载**：开始任何新任务前，先查经验库有没有相关经验（`learn.py query "关键词"` 或看 SessionStart 已注入的精华），命中就**照做**——绝不重复同样的失败。

## ⚡ DSH 下的调用方式（重要）

DSH 的 shell 工具**不保证**带有 `CODEX_LEARNINGS_DIR` 等自定义环境变量，
所以统一用**显式 `--dir`** 最稳（工具自带该参数）：

```powershell
& python `
  'C:\Users\<user>\.dsh\skills\dsh-self-improve\learn.py' `
  query "关键词" --dir 'C:\Users\<user>\.dsh\skills\dsh-self-improve\.learnings'
```

★ **别在这里写死解释器版本**（如 `python3.14.7`）：版本号会过期，写死之后本技能会**静默瘫痪**
——2026-11 就因为写死 `3.14.7`，经验注入整段失效 11 天而无人察觉。用 PATH 上的 `python`，
或 `py -3`（launcher 自动选最新 3.x）。

`add` / `review` / `status` 同理都支持 `--dir`。不加 `--dir` 时会走兜底路径，
效果相同，但显式指定可以避免任何环境差异。

## 📂 文件结构

```
dsh-self-improve/
  SKILL.md            # 本技能说明（agent 读取）
  README.md           # 详细文档
  learn.py            # CLI: add / query / rank / supersede / review / status / dups
  utils/learn_parser.py  # 经验库核心（含相关性排序、supersede、标题条件提醒）
  .learnings/         # 经验库数据本体（LEARNINGS/ERRORS/BEST_PRACTICES.md）
  hooks/
    si_session_start.py   # 上游 Claude Code 格式，**在本机不生效**（见下）
    si_stop.py            # 同上
```

★ **本机实际生效的 hook 不是 `hooks/` 里那两个**：它们是上游 Claude Code 格式，
照搬会静默失效。DSH 下由**原生插件** `profiles/desktop/self-improve/index.js` 承担，
三个挂点：`agent/created`（注入时间基线）、`agent/pre-step`（按任务相关性补注入）、
`agent/turn-stopping`（仅工具失败时提醒反思）。改那三个挂点要看该文件的注释。

经验库位置：默认 **技能自带** `C:\Users\<user>\.dsh\skills\dsh-self-improve\.learnings\`
（也支持项目级 `.learnings\`，自动检测，**项目级优先**）。三个文件：
- `LEARNINGS.md` — 什么有效（正向经验）
- `ERRORS.md` — 什么失败（错误教训）
- `BEST_PRACTICES.md` — 沉淀的高层准则

★ **项目级库怎么用**（2026-11-14 打通注入侧）：写**只在某个项目里成立**的经验时，加
`--dir <项目根>/.learnings`，例如 `--dir 'E:/FinalEngine/.learnings'`。这类条目
在会话 cwd 就是该项目时会被并进「相关性注入」（全局库的条目同时保留，按相关性抢
同一个名额，**不会增加注入条数**）。
⚠️ 判据是「**换个项目还成立吗**」，不是「正文里提到过某个项目」：实测提到 E:\Final
的 323 条里，标题真正绑定该项目的只有 7 条 —— 按「提及」迁库会把通用经验一起搬走。
⚠️ 注入侧并库依赖插件把会话 cwd 传给渲染器（`DSH_LEARN_CWD`），**改完插件需重启**；
没传 cwd 时行为与旧版一致（只读全局库）。

## 🩺 契约自检（DSH 升级后必跑）

插件依赖的是 DSH **内部** API（钩子名、`agent.inject` 的消息格式、`session.header.cwd`），
而 DSH 走 rc/nightly 通道、没有稳定 ABI 承诺 —— 升级后**可能静默失效**（历史上就这样
失效过 11 天）。因此有 `profiles/desktop/self-improve/contract.mjs`：

```bash
# 人读：13 条契约逐个 PASS/DEGRADED/FAIL，FAIL 会附「降级路径」
node C:\Users\<user>/.dsh/profiles/desktop/self-improve/contract.mjs
# 机器读 / 升级后严格比对（有漂移也算不通过）
node .../contract.mjs --json
node .../contract.mjs --strict
# DSH 升级**前后**各跑一次，才能做漂移比对（比对的是版本 + asar 里契约符号的出现次数）
node .../contract.mjs --baseline
```

插件启动时也会**后台自动跑一次**（约 1.5 s）；只有出现 FAIL 才把结论拼进会话开场的
经验注入里（不 FAIL 就只写一条 `contract-check` 日志，不打扰）。判据与降级路径都写在
`contract.mjs` 的 `CONTRACTS` 里，改契约就是改那张表。

⚠️ 它做不到的事（别误解）：**不保证** DSH 大改后插件还能自动工作。它保证的是
「坏得立刻可见 + 有降级路径 + 升级后一条命令查出哪里变了」，把「静默腐烂」换成「显式红灯」。

★ 两条实现约束（2026-11-14 实测踩过，改这个文件前先看）：
1. **不要在 Electron 主进程里读 `app.asar`** —— `readFileSync` 得到空 Buffer、
   `statSync().size` 报 0（asar 在 Electron 内是虚拟路径）。归档扫描走子进程
   `asar_probe.py`，Electron 的 fs 补丁管不到子进程。
2. **四态**：PASS / DEGRADED / FAIL / **UNVERIFIED**。「验不了」既不许写成通过，
   也不许写成不通过 —— 曾把「仪器读不到」伪装成 8 项 FAIL，往会话开场注入了假红灯。

## ⚙️ 使用命令

所有命令在任何工作目录下运行都有效（路径自动解析）。

```bash
# 新增一条正向经验（任务后反思，学到什么有效的）
python C:\Users\<user>/.dsh/skills/dsh-self-improve/learn.py add learn --title "经验一句话（带条件）" --body "细节..."

# 新增一条错误教训（什么失败了，怎么避免）
python .../learn.py add error --title "教训一句话" --body "细节..."

# 新增一条沉淀准则（从多次经验提炼的高层规则）
python .../learn.py add best_practice --title "准则" --body "..."

# 查询经验（任务前自查！命中就照做）—— 是「筛」，需精确子串或 bigram 重叠 >= 0.6
python .../learn.py query "关键词"
#   加 --full 打印正文全文（默认截前 150 字）；核对「适用条件是否成立」时用它
python .../learn.py query "关键词" --full

# 按相关性排序全库检索（是「排」；注入走的就是这条，与 query 不同）
python .../learn.py rank "任务描述" --limit 6 --min-score 6

# ★ 维护：扫全库「疑似同一结论」的成对候选（只报，不改库）
#   判据 = 共享 ≥14 字「长且稀有」的连续原文（剥掉环境套话）；--min-lcs/--max-df 可调
python .../learn.py dups --limit 40
#   → 由人来判要不要 supersede；词面工具做不到语义全召回，别指望它自动合并

# ★ 这条经验推翻了库里已有的某条 → 把它标为「已被取代」，别只是新增
#   不标的话，纠正条目和它要推翻的旧说法会**并排参与检索**，下次可能先看到错的
python .../learn.py supersede --old "旧标题的唯一子串" --new "新标题" --why "为什么被推翻"
#   --old 不唯一时**报 ambiguous 并列出候选**，不会瞎猜
#   默认 query/rank 会隐藏已取代条目；要看得加 --include-superseded

# 复盘统计（同时报「库有多大」和「有多少在起作用」）
python .../learn.py review
```

## 🔄 自学习闭环流程（铁律展开）

```
任务前:  查经验库 → 命中相关经验 → 照做（不重复失败）
   ↓
任务中:  正常执行，留意"这次有什么不一样/值得记"
   ↓
任务后:  反思（必须想）→ 有经验 → learn.py add → 入库
   ↓
下次任务: SessionStart 自动注入经验精华 → 开局就"记得"
```

**记录什么**（值得记的）：
- 解决了什么难题、用了什么好方法（→ learn）
- 犯了什么错、怎么避免（→ error）
- 反复出现的模式、应该固化的规则（→ best_practice）

**不记录什么**：
- 一次性琐事（无需反复应用的）
- 敏感信息（密钥、密码、token）
- 太长太啰嗦的（经验要精炼，一句话标题 + 短正文）

## 📌 与兄弟技能的分工

| 技能 | 管什么 |
|------|--------|
| **dsh-self-improve**（本技能） | Codex **主动反思**沉淀的正向经验 + 错误教训 + 准则 |
| **codex-mistake-archive** | 犯错的**被动记录**（错题档案） |
| **codex-hard-rules-manager** | 用户定的**死规则**（必须无条件遵守） |

三者互补：self-improve 负责"主动学"，mistake-archive 负责"记错题"，hard-rules 负责"守规矩"。

## ✅ 匹配策略

查询与自查使用**模糊匹配**（与 mistake-archive 一致）：精确子串命中，或字符 bigram 重叠 ≥ 0.6 视为命中。原则：**宁多勿漏**——漏掉一条相关经验导致重复踩坑，代价远大于多读几条。

## 🔧 维护

- 经验文件是纯 markdown，人可以随时打开编辑/审计。
- 库太大时（>100 条），运行 `learn.py review` 查看统计。**注意区分「总条数」与
  「实际参与检索的条数」** —— `review` 会分别报，别被总量骗了。
- **纠正旧条目要用 `learn.py supersede`，不要直接删**：就地插一行
  `> SUPERSEDED by: …`，保留审计痕迹（那条错的当初为什么被信、被什么推翻），
  且不产生读-改-写的内容丢失风险。误标就删掉那一行。
- 建议用 git 跟踪经验库变更历史（`C:\Users\<user>\.dsh\skills\dsh-self-improve\.learnings\`）。
- ⚠️ **并发写入无互斥**（2026-09-27 实测）：`learn_parser` 的 `_WRITE_LOCK` 是
  `threading.Lock`，**跨进程无效**；去重是「先读再追加」的 TOCTOU。多个 DSH 会话
  同时写同一个库时可能产生重复条目（实测两次统计之间条目数从 161 → 164，而我没写）。
- ⚠️ **`query` 是「宁多勿漏」的模糊匹配**，容易误命中：实测查「探针条目甲」会捞到
  一条无关条目，只因它正文里有「差分探针**条目**」（3/4 bigram 重叠 = 0.75）。
  需要精确找某条时用更长的关键词，或直接看 `review` 的全量列表。
