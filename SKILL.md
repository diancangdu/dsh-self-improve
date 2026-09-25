---
name: dsh-self-improve
description: "Self-improvement (self-learning experience library / 自学习经验库): after each task, reflect on what worked and what failed and record it; before starting tasks, load relevant past learnings and apply them. Builds a persistent cross-session knowledge base so the agent keeps getting better and avoids repeating mistakes. 自学习：任务后反思记录经验，任务前加载相关经验应用，形成越用越强的闭环。"
metadata:
  short-description: Self-learning loop (reflect after task / load & apply before task / review periodically)
---

# 自学习经验库（Self-Improve）

> **本副本归 DSH 专用，与 Codex 完全独立**（2026-09-25 部署）。
> 上游：[diancangdu/codex-self-improve](https://github.com/diancangdu/codex-self-improve)
> 技能目录：`$DSH_HOME/skills/dsh-self-improve\`
> 经验库目录：`$DSH_HOME/skills/dsh-self-improve/.learnings/`（**技能自带**，
> 与其它 agent 的经验库物理隔离、互不读写）
>
> 两库物理隔离、互不读写。环境变量 `CODEX_LEARNINGS_DIR` 是工具自带的覆盖接口，
> 名字保留是为了跟上游兼容；**不设它时兜底也已经指向本实例的库**，不会掉回共享库。

## 🎯 用途与定位

这是一个**自学习闭环**：让 agent 从每次任务中总结经验，并在未来任务中自动应用，做到"越用越强、不重复犯错"。

**为什么有效**：agent 每次会话都从零开始（无记忆），但通过**经验文件**可以在会话之间传递知识。任务后反思沉淀，任务前加载应用——这个闭环让经验像滚雪球一样累积。

## 两条铁律

1. **任务后必反思**：每完成一个任务（或任务告一段落），停下来想 30 秒：**这次什么有效？什么失败？** 有值得记的就记（`learn.py add`），没有就跳过，**但必须想一下**。
2. **任务前必加载**：开始任何新任务前，先查经验库有没有相关经验（`learn.py query "关键词"` 或看 SessionStart 已注入的精华），命中就**照做**——绝不重复同样的失败。

## ⚡ DSH 下的调用方式（重要）

DSH 的 shell 工具**不保证**带有 `CODEX_LEARNINGS_DIR` 等自定义环境变量，
所以统一用**显式 `--dir`** 最稳（工具自带该参数）：

```powershell
& 'D:/Python/python3.14.7/python.exe' `
  '$DSH_HOME/skills/dsh-self-improve\learn.py' `
  query "关键词" --dir '$DSH_HOME/skills/dsh-self-improve\.learnings'
```

`add` / `review` / `status` 同理都支持 `--dir`。不加 `--dir` 时会走兜底路径，
效果相同，但显式指定可以避免任何环境差异。

## 📂 文件结构

```
dsh-self-improve/
  SKILL.md            # 本技能说明（agent 读取）
  README.md           # 详细文档
  learn.py            # CLI: add / query / review / status
  utils/learn_parser.py  # 经验库核心
  .learnings/         # 经验库数据本体（LEARNINGS/ERRORS/BEST_PRACTICES.md）
  hooks/
    si_session_start.py   # SessionStart: 注入经验精华
    si_stop.py            # Stop: 反思提醒
```

经验库位置：默认 **技能自带** `$DSH_HOME/skills/dsh-self-improve\.learnings\`
（也支持项目级 `.learnings\`，自动检测，**项目级优先**）。三个文件：
- `LEARNINGS.md` — 什么有效（正向经验）
- `ERRORS.md` — 什么失败（错误教训）
- `BEST_PRACTICES.md` — 沉淀的高层准则

## ⚙️ 使用命令

所有命令在任何工作目录下运行都有效（路径自动解析）。

```bash
# 新增一条正向经验（任务后反思，学到什么有效的）
python $DSH_HOME/skills/dsh-self-improve/learn.py add learn --title "经验一句话" --body "细节..."

# 新增一条错误教训（什么失败了，怎么避免）
python .../learn.py add error --title "教训一句话" --body "细节..."

# 新增一条沉淀准则（从多次经验提炼的高层规则）
python .../learn.py add best_practice --title "准则" --body "..."

# 查询经验（任务前自查！命中就照做）
python .../learn.py query "关键词"

# 复盘统计（看看经验库积累情况）
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
- 库太大时（>100 条），运行 `learn.py review` 查看统计，手动合并冗余条目。
- 建议用 git 跟踪经验库变更历史（`$DSH_HOME/skills/dsh-self-improve\.learnings\`）。
- 删除错误经验：直接编辑对应 markdown 文件删除该条目。
