# dsh-plugin — DSH 原生 hook 适配层

把「会话开始注入经验、出问题时提醒反思」这两个动作接到 **DSH 自己的插件扩展点**上。

## 为什么需要它

技能自带的 `hooks/si_session_start.py` 与 `hooks/si_stop.py` 是 **Claude Code / Codex 的
hook 协议**（stdin 收 JSON、stdout 回决策）。**照搬到 DSH 上会静默失效** —— DSH 的会话事件
不走那套协议，插件不会报错，只是什么都不做。

本插件用 DSH 的扩展点重写了同样的职责。

| 职责 | 挂点 | 行为 |
|---|---|---|
| 会话开始注入经验 | `agent/created` | 读经验库精华，`agent.inject()` 注入 |
| 出问题时提醒反思 | `agent/turn-stopping` | **仅当本轮有工具失败**才 `agent.steer()` |

## 两个设计要点

### 一、只在出问题时才提醒

早期版本是「本轮用过工具就提醒」，等于每轮都响，很吵。现在只在 `isError === true` 时触发。

⚠️ **`isError` 的覆盖范围**（实测，容易搞错）：

| 情况 | `isError` |
|---|---|
| 被机制拒绝（如硬闸拦下） | **是** —— 拒绝被构造成 `{content:[{text:'Error: '+reason}], isError:true}` |
| 工具自身异常 | **是** |
| 命令返回非零退出码 | **否** —— 对 shell 类工具，退出码非 0 是正常结果，不是工具失败 |

刻意**不**解析 content 里的 `[exit code: N]`：格式脆弱，且与「少打扰」的目标相悖。

### 二、提示词里必须显式禁止输出

早期提示词写成了三个编号问题（「1. 什么有效？ 2. 什么失败？ 3. 有什么模式？」），
结果模型**每一轮都照着这三问作答**，把反思过程输出给用户。

**提示词的形状决定输出的形状。** 要它静默，就得明说「不要把判断过程或结论输出给用户」，
并且**不要给作答骨架** —— 给了骨架，它就会填。

## 安装

```yaml
# profile cordis.patch.yml
- insert:
    - id: self-improve-hooks
      name: './self-improve/index.js'
      config: {}
```

`name` 必须指向**具体文件**；写成目录会触发 Node ESM 的 `ERR_UNSUPPORTED_DIR_IMPORT`，
**插件静默不加载**。

单独关掉某一半：`config` 里设 `injectOnStart: false` 或 `reflectOnStop: false`。

## 配置（全部可选）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DSH_HOME` | `~/.dsh` | 用来定位技能与经验库 |
| `DSH_SELF_IMPROVE_SKILL` | `$DSH_HOME/skills/dsh-self-improve` | 技能目录 |
| `DSH_SELF_IMPROVE_LEARNINGS` | `<技能>/.learnings` | 经验库目录 |
| `DSH_SELF_IMPROVE_RENDER` | 本目录下的 `render_learnings.py` | 渲染经验库精华的脚本 |
| `DSH_SELF_IMPROVE_PYTHON` | `python` | 解释器 |
| `DSH_SELF_IMPROVE_LOG` | 系统临时目录 `self-improve-events.jsonl` | 事件日志 |

⚠️ **本地假跑务必先用 `DSH_SELF_IMPROVE_LOG` 重定向**，否则会污染真实排障历史。
（`rule-gate` 的 `HARD_RULES_GATE_LOG` 是同一约定。）

## 验证

| 判据 | 期望 |
|---|---|
| 正常回合（无工具失败） | 日志里恰好一条 `stop-skip`，**且不给用户任何提示** |
| 出问题的回合 | 恰好一条 `trouble-detected` + 一条 `stop-steered`；同一回合不重复 |
| 会话开始 | 一条 `inject-ok`（带字符数） |

「安静」本身也是判据：如果正常回合里出现了反思提示，说明触发条件写宽了。
