# 自学习经验库（Self-Improve）

**自学习闭环**：让 agent 从每次任务中总结经验，并在未来任务中自动应用，做到"越用越强、不重复犯错"。

## 核心概念

- **经验（Learning）**：一次任务中学到的"什么有效 / 什么失败 / 应该固化的准则"。
- **记录**：任务后反思 → `learn.py add learn|error|best_practice` 写入经验库。
- **加载**：任务前 → `learn.py query "关键词"` 查相关经验，命中就照做。
- **自动注入**：SessionStart hook 自动把经验精华注入新会话上下文，开局就"记得"。

## 装到哪个宿主

| 宿主 | 用哪套 | 说明 |
|---|---|---|
| Claude Code / Codex | `deploy.ps1`（或 `install_skill.ps1`） | 走它们的 hook 协议（stdin 收 JSON、stdout 回决策） |
| **DeepSeek Harness (DSH)** | **`dsh-plugin/`** | ⚠️ 脚本式 hooks **在 DSH 上会静默失效** —— DSH 的会话事件不走那套协议，照搬会「装上了但什么都不做」 |

`dsh-plugin/` 是 DSH 的原生 hook 适配层，用 DSH 自己的扩展点重写了同样的职责：
会话开始注入经验（`agent/created`）、**仅当本轮有工具失败时**才提醒反思（`agent/turn-stopping`）。
安装方式、配置项与验证判据见 [`dsh-plugin/README.md`](dsh-plugin/README.md)，
两条安装路线（自动化 / 手动）见 [`INSTALL.md`](INSTALL.md)。

另外两处容易踩的坑，都写在 `dsh-plugin/README.md` 里：`isError` **不覆盖**普通命令的非零退出码；
以及「提示词的形状决定输出的形状」—— 想让模型静默，提示词里必须显式禁止输出。

## 快速开始

```bash
# 查看经验库状态
python learn.py status

# 新增一条正向经验
python learn.py add learn --title "解决XX问题用YY方法" --body "细节..."

# 新增一条错误教训
python learn.py add error --title "不要在XX目录执行命令" --body "教训..."

# 查询经验（任务前自查）
python learn.py query "目录"

# 复盘统计
python learn.py review
```

所有命令在任何工作目录下运行都有效（路径自动解析）。

## 经验库位置

- 默认（技能自带）：`$DSH_HOME/skills/dsh-self-improve/.learnings/`
- 也支持项目级：`项目根\.learnings\`（自动检测，存在即优先）
- 可用环境变量 `CODEX_LEARNINGS_DIR` 显式指定

三个文件（纯 markdown，可人工审计）：
| 文件 | 内容 |
|------|------|
| `LEARNINGS.md` | 什么有效（正向经验） |
| `ERRORS.md` | 什么失败（错误教训） |
| `BEST_PRACTICES.md` | 沉淀的高层准则 |

## Hooks 集成（自动闭环）

本技能通过两个 hook 实现自动化（已注册到 `managed_config.toml`，桌面 App 自动信任）：

| Hook | 作用 |
|------|------|
| **SessionStart** (`si_session_start.py`) | 会话开始注入经验精华到上下文 |
| **Stop** (`si_stop.py`) | 任务结束提示反思，引导记录经验 |

> 注：hooks 是**提示性**的（advisory），引导 Codex 记录经验；真正遵守要靠模型执行力。本地 9B 弱模型可能偶尔漏记，换强模型（deepseek-v4-pro）后效果最佳。

## 懒人部署

```powershell
# 一键部署（安装 skill + 注册 hooks + 加入 enabled 列表）
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

或仅安装 skill（不含 hooks）：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_skill.ps1
```

## 文件结构

```
dsh-self-improve/
  SKILL.md                  # 技能说明（agent 读取）
  README.md                 # 本文档
  learn.py                  # CLI 工具
  utils/learn_parser.py     # 经验库核心
  hooks/
    si_session_start.py     # SessionStart hook
    si_stop.py              # Stop hook
  install_skill.ps1         # 安装脚本
  deploy.ps1                # 懒人部署脚本
  test_learn.py             # 自动化测试
```

## 匹配策略

- 模糊匹配：精确子串命中，或字符 bigram 重叠 ≥ 0.6 视为命中。
- 原则：**宁多勿漏**——漏掉相关经验导致重复踩坑，代价远大于多读几条。

## 维护

- 经验文件是纯 markdown，可直接编辑审计。
- 库 >100 条时用 `learn.py review` 看统计，合并冗余。
- 建议 git 跟踪 `$DSH_HOME/skills/dsh-self-improve\.learnings\` 变更。
- 删除经验：直接编辑对应 markdown 文件删除条目。
