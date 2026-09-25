# 安装教程

两条路：**自动化脚本**（推荐）或**手动**。

自学习有两个部件，可以分开装：

| 部件 | 作用 | 是否必须 |
|---|---|---|
| **技能本体**（`learn.py` / 经验库 / 渲染脚本） | 记经验、查经验 | 必须 |
| **hook 适配层** | 会话开始自动注入、出问题自动提醒 | 可选（没有它就手动调用 `learn.py`） |

---

## 前置条件

- Python 3.8+ 在 PATH 里（或可指定绝对路径）
- DSH / Codex / Claude Code 任一宿主

---

## 一、自动化安装

### Codex / 通用（`deploy.ps1`）

```powershell
# 干跑
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
# 写入
powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -Apply
```

它做四件事（**幂等**，可重复运行；改配置前会先备份）：

1. 把技能装到 `<DSH_HOME>\skills\dsh-self-improve\`
2. 复制 hooks 到 `<DSH_HOME>\hooks\`
3. 在 `managed_config.toml` 里注册 hooks
4. 把技能加进 `[skills]` 的 enabled 列表

目标目录优先取环境变量 `DSH_HOME`，否则用 `~/.dsh`。

### 只装技能本体（`install_skill.ps1`）

```powershell
powershell -ExecutionPolicy Bypass -File .\install_skill.ps1
```

只复制技能目录，**不注册 hooks**。

### DSH 原生 hook（`dsh-plugin/`）

DSH 不走 Claude-Code 那套 hook 协议，脚本形式的 hooks 会**静默失效**。DSH 用原生插件：

```powershell
Copy-Item -Recurse .\dsh-plugin "$env:DSH_HOME\profiles\desktop\self-improve"
```

然后在 `$env:DSH_HOME\profiles\desktop\cordis.patch.yml` 追加：

```yaml
- insert:
    - id: self-improve-hooks
      name: './self-improve/index.js'
      config: {}
```

细节见 [`dsh-plugin/README.md`](dsh-plugin/README.md)。

---

## 二、手动安装

### 1. 放技能本体

```
<DSH_HOME>/skills/dsh-self-improve/
├── learn.py
├── SKILL.md
├── README.md
├── utils/learn_parser.py
└── .learnings/            ← 运行时生成，不需要手动建
```

经验库的解析优先级（`utils/learn_parser.py`）：

1. 环境变量 `CODEX_LEARNINGS_DIR`（名字保留是为了跟上游兼容）
2. 当前目录本身含 `LEARNINGS.md`
3. 当前目录下的 `.learnings/`（项目级优先）
4. **技能自带的 `.learnings/`（兜底）**

### 2. 手动注册 hook（Codex 系）

在 `<DSH_HOME>/managed_config.toml`（或宿主的 hook 配置）里注册两个脚本：

```toml
[[hooks.SessionStart]]
command = "python <DSH_HOME>/hooks/si_session_start.py"

[[hooks.Stop]]
command = "python <DSH_HOME>/hooks/si_stop.py"
```

⚠️ **DSH 上不要这么做** —— 它不走这套协议。DSH 用上面的原生插件。

### 3. 建一个规则文件（可选）

想让「出问题时提醒」知道该记什么，参考 [SKILL.md](SKILL.md) 里对记录格式的说明：
一条经验写成 `- 事实。判据或原因。（日期）`，**只记结论不记过程**。

---

## 三、验证

**技能本体**：

```powershell
python <DSH_HOME>/skills/dsh-self-improve/learn.py status `
  --dir <DSH_HOME>/skills/dsh-self-improve/.learnings
```

期望：打印经验库条目统计（刚开始是 0 条，这是正常的）。

```powershell
python <DSH_HOME>/skills/dsh-self-improve/learn.py add learn `
  --title "自检条目" --body "安装验证用" `
  --dir <DSH_HOME>/skills/dsh-self-improve/.learnings
```

期望：写入成功，`.learnings/LEARNINGS.md` 出现该条目。验完删掉它。

**hook 适配层**（DSH）：见 [`dsh-plugin/README.md`](dsh-plugin/README.md) 的判据表 ——
重点是**正常回合必须完全安静**。

**一个容易误判的点**：`.learnings/` 被 `.gitignore` 排除，所以 `git status` 看不到它。
「目录不存在」不等于「装失败」—— 它是首次写入时才创建的。

---

## 四、卸载

```powershell
# 1. 从宿主的 hook 配置里删掉这两条
# 2. DSH：从 cordis.patch.yml 删掉 self-improve-hooks 那个 insert 块
# 3. 删目录（先备份经验库！）
Copy-Item -Recurse "<DSH_HOME>\skills\dsh-self-improve\.learnings" "$HOME\learnings-backup"
Remove-Item -Recurse -Force "<DSH_HOME>\skills\dsh-self-improve"
```

⚠️ **先备份 `.learnings/`**：它是你积累的全部经验，删掉不可恢复。

---

## 五、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `learn.py status` 读到 0 条 | 正常（新装的库是空的） | 先 `add` 一条再 `status` |
| 命令在任何目录下都能跑吗 | 能，路径自动解析 | 但仍建议显式带 `--dir`，最稳 |
| DSH 里 hook 完全没反应 | 用的是脚本 hooks，DSH 不走那协议 | 改用 `dsh-plugin/` 原生插件 |
| 每轮都被提醒反思 | 触发条件写宽了 | 检查是不是「用过工具就提醒」而不是「工具失败才提醒」 |
| 不设环境变量会不会掉回 Codex 的库 | 不会 | 兜底已指向技能自带的 `.learnings` |
