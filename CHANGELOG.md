# Changelog

## v1.0.0 — 2026-09-25

首个公开版本。机器相关路径与迁移历史已移除。

### 技能本体

- **经验库**分三类落盘：正向经验 `LEARNINGS.md`、失败教训 `ERRORS.md`、
  从多条提炼出的高层准则 `BEST_PRACTICES.md`。
- `learn.py` 提供 `add learn|error|best_practice` / `query` / `review` / `status`。
- **经验库解析优先级**：环境变量 → 当前目录本身含 `LEARNINGS.md` → 当前目录下的 `.learnings/`
  （项目级优先）→ **技能自带的 `.learnings/`（兜底）**。兜底指向本技能，所以不设任何环境变量
  也不会掉回别的 agent 的库。
- `.learnings/` 是运行时数据，**不入库**（见 `.gitignore`）。

### 新增：DSH 原生 hook 插件 `dsh-plugin/`

技能自带的 `hooks/si_session_start.py` / `si_stop.py` 是 **Claude Code / Codex 的 hook 协议**
（stdin 收 JSON、stdout 回决策）。⚠️ **照搬到 DSH 上会静默失效** —— DSH 的会话事件不走那套协议，
插件不报错，只是什么都不做。

DSH 用 `dsh-plugin/`，挂 DSH 自己的扩展点：

| 职责 | 挂点 | 行为 |
|---|---|---|
| 会话开始注入经验 | `agent/created` | 读经验库精华，`agent.inject()` 注入 |
| 出问题时提醒反思 | `agent/turn-stopping` | **仅当本轮有工具失败**（`result.isError === true`）才 `agent.steer()` |

### 两处刻意的设计（都有实测依据）

- **只在出问题时提醒**。早期版本是「本轮用过工具就提醒」，等于每轮都响。`isError` 的覆盖范围
  实测为：被机制拒绝 ✅ / 工具自身异常 ✅ / **命令返回非零退出码 ❌**（对 shell 类工具，
  退出码非 0 是正常结果，不是工具失败）。刻意不解析 content 里的 `[exit code: N]`：格式脆弱，
  且与「少打扰」的目标相悖。
- **提示词的形状决定输出的形状**。早期提示词写成三个编号问题（「什么有效？什么失败？有什么模式？」），
  结果模型**每一轮都照着这三问作答**，把反思过程输出给用户。现在提示词显式禁止输出，
  且**不给作答骨架** —— 给了骨架它就会填。

### 配置（全部可选）

`DSH_HOME` / `DSH_SELF_IMPROVE_SKILL` / `DSH_SELF_IMPROVE_LEARNINGS` /
`DSH_SELF_IMPROVE_RENDER` / `DSH_SELF_IMPROVE_PYTHON` / `DSH_SELF_IMPROVE_LOG`。

⚠️ 日志默认写系统临时目录；**本地假跑务必先用 `DSH_SELF_IMPROVE_LOG` 重定向**，
否则会污染真实排障历史。

### 与上游的关系

技能源自 `diancangdu/codex-self-improve`，本仓库是它的 **DSH 适配版**：
新增了 DSH 原生插件、DSH 的安装与验证说明，并把脚本 hooks 在 DSH 上失效这件事写进了文档。

### 已知边界

- 经验库条目是**过程性教训**，与「稳定的记忆」分工不同：先记经验，同一流程反复出现后才升级为 Skill
- 本版**不含任何实际经验条目**（`.learnings/` 是运行时数据），仓库里只有机制与工具
