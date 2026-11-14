// dsh-self-improve-hooks — 自学习闭环的原生 hook（零依赖）
//
// 把上游 Claude Code 风格的 si_session_start.py / si_stop.py 用 DSH 原生扩展点重写。
// 为什么不直接搬上游那两个脚本：DSH 的插件层必须通过 cordis 挂原生插件，
// 而上游 hook 依赖 Claude Code 的 hook 协议；照搬会得到「静默失效」——
// 本项目此前已经踩过这个坑（见项目 AGENTS.md）。
//
// 两个职责：
//   1) 会话开始：读出经验库精华，注入给孩子会话，做到「开局就记得」
//   2) 回合收尾：**仅当本轮出过问题**（工具调用失败）时才提醒做一次反思判定（RULE_021）。
//      2026-09-25 二改：原先是「用过工具就提醒」，等于每轮都响 —— 用户明确要求改成
//      「只在出问题时提醒」，且反思过程**不得输出给用户**。
//
// 约束（照抄 rule-gate 的成活经验）：
//   * 插入名必须指向具体文件（不能是目录），否则 ESM 报 ERR_UNSUPPORTED_DIR_IMPORT
//   * **不声明 inject**：内联插件在 apply 阶段拿不到 shell/sessionProjections 等服务
//   * 一律 fail-soft：任何异常都吞掉，绝不阻断会话
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { appendFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh');
const SKILL_DIR =
  process.env.DSH_SELF_IMPROVE_SKILL || join(DSH_HOME, 'skills', 'dsh-self-improve');
const PYTHON = process.env.DSH_SELF_IMPROVE_PYTHON || 'python';
// The renderer ships next to this file, so the default needs no configuration.
const RENDER =
  process.env.DSH_SELF_IMPROVE_RENDER || fileURLToPath(new URL('./render_learnings.py', import.meta.url));
const LEARNINGS_DIR =
  process.env.DSH_SELF_IMPROVE_LEARNINGS || join(SKILL_DIR, '.learnings');
// 日志落点。可用 DSH_SELF_IMPROVE_LOG 覆盖 —— 与 rule-gate 的 HARD_RULES_GATE_LOG
// 同一约定：**本地假跑必须能把日志重定向走**，绝不能污染真实排障历史。
const LOG = process.env.DSH_SELF_IMPROVE_LOG || join(tmpdir(), 'self-improve-events.jsonl');

function record(entry) {
  try {
    appendFileSync(LOG, JSON.stringify({ ts: new Date().toISOString(), ...entry }) + '\n', 'utf8');
  } catch {
    // 日志失败不影响 hook
  }
}

/** 调用渲染脚本拿经验精华；失败返回空串。 */
function renderLearnings() {
  try {
    const res = spawnSync(PYTHON, [RENDER], {
      env: { ...process.env, DSH_LEARNINGS_DIR: LEARNINGS_DIR },
      encoding: 'utf8',
      timeout: 15000,
      windowsHide: true,
    });
    if (res.error) {
      record({ event: 'render-failed', error: String(res.error) });
      return '';
    }
    return (res.stdout ?? '').trim();
  } catch (error) {
    record({ event: 'render-threw', error: String(error) });
    return '';
  }
}

/**
 * 构造一个 inbox 能接受的 UserMessage。
 *
 * ★ 为什么必须显式带 source（2026-09-25 踩坑，代价是三个会话打不开）：
 * DSH format v4 的每个 durable message 槽位都要求 "producer-owned source kind"。
 * 判据在 dsh-session-persistence-jsonl 的 source()：
 *     source 必须是对象，且 source.kind 是非空字符串，且 != "plugin"
 * 除此之外的额外字段**一律不限制**（所以 form / summary 可以自由添加）。
 * 而 agent.inject() / agent.steer() 的入参**直接**成为 `agent/inbox/spliced`
 * 事件的 inserted[]（类型就是 UserMessage[]），并且 **DSH 写入时不校验、只在
 * resume 解码时校验** —— 所以漏了 source 不会当场报错，而是事后让整个会话
 * 永久打不开：
 *     SessionFormatError: format v4 message requires a producer-owned source kind
 *     Cannot read properties of undefined (reading 'kind')
 *
 * ★ 为什么 kind 不再是 "user"（2026-09-25 二改，为了不刷屏）：
 * UI 的渲染形态**只由 source.kind 决定**，与内容、role 都无关。
 * 判据在 dsh-client-ui-chat 的 messageDefinition.start：
 *     if (event.data.source.kind !== "user") -> 渲染成 context 行
 *     else                                   -> 渲染成 steering / 用户消息
 * 以前写 kind: 'user'，所以反思提醒看起来像用户自己发的消息。
 * 现在改用自有 kind，并配 form: 'notice'：UI 的 contextBody 里 notice 会读
 * source.summary 作为**折叠行的一行摘要**（notice 的全部意义就是不展开也能读）。
 * 结果：提醒变成一行可折叠通知，模型侧收到的正文一字不少。
 * 官方 agent-instructions 同样是 createUserMessage + 自有 kind，可作参照。
 *
 * @param text - 完整正文（模型看到的内容）
 * @param summary - notice 折叠行上的一行摘要（可选）
 */
function userMessage(text, summary) {
  return {
    content: [{ type: 'text', text }],
    source: { kind: 'self-improve', form: 'notice', summary, rpcId: randomUUID() },
    role: 'user',
    id: randomUUID(),
  };
}

/**
 * 落盘前的自检闸 —— 保证交出去的 message 一定带合法的 source kind。
 * 不合法就拒绝投递（宁可少一次注入，也不要写坏一个会话）。
 * @param {unknown} message - 待投递的消息
 * @param {string} where - 调用点标识，便于在日志里定位
 * @returns {boolean} 是否可安全投递
 */
function assertDeliverable(message, where) {
  const source = message && typeof message === 'object' ? message.source : undefined;
  const kind = source && typeof source === 'object' ? source.kind : undefined;
  if (typeof kind !== 'string' || kind.length === 0 || kind === 'plugin') {
    record({ event: 'message-source-invalid', where, kind: kind ?? null });
    return false;
  }
  return true;
}

/** 尝试注入上下文；把实际用的方式记进日志，便于日后核对。 */
function tryInject(agent, text, summary) {
  if (!agent || typeof agent.inject !== 'function') {
    record({ event: 'inject-unavailable', hasAgent: Boolean(agent) });
    return false;
  }
  const message = userMessage(text, summary);
  if (!assertDeliverable(message, 'inject')) return false;
  try {
    agent.inject(message);
    record({ event: 'inject-ok', mode: 'plain', chars: text.length });
    return true;
  } catch (error) {
    record({ event: 'inject-failed', mode: 'plain', error: String(error) });
    return false;
  }
}

/**
 * 判断一次工具调用是否失败 —— 这是「出过问题」的**机器判据**。
 *
 * ★ 判据取自 DSH 真实源码，不是猜的（2026-09-25 读 app.asar 实测）：
 *   /dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js
 *     · 约 3503 行 postExecute(exec, result)：
 *         const decision = await this.ctx.waterfall(..., "tools/post-execute", exec, result, ...)
 *       ⇒ 监听器的第 2 个参数就是 result，失败标志是 result.isError === true。
 *     · 约 3321 行注释：Tool and unknown-tool failures still receive post-execute.
 *       ⇒ 工具抛错 / 未知工具都会走到这里，所以 isError 能覆盖到。
 *     · 约 3509-3511 行：被 post-execute policy block 时同样是 isError: true。
 *
 * ★ 2026-09-25 17:08 重启后**实测**（同一分钟内两个对照，同一个监听器）：
 *   ✅ **硬闸拦截会置 isError**：故意发一条被 RULE_003 拦下的命令，
 *      self_improve_events.jsonl 立刻出现 trouble-detected + RULE_003 的理由。
 *      ⇒ 这里原先写的「pre-execute 被拒可能不经过 post-execute」的**猜测已被推翻**。
 *      源码佐证：dsh-tools/lib/types/index.js:892-898 把拒绝构造成
 *      { content:[{text:`Error: ${denialReason}`}], isError:true, error:{message} }。
 *   ❌ **命令返回非零退出码不会置 isError**：PowerShell `throw` 得到 exit code 1，
 *      但日志里**没有** trouble-detected —— 而同一分钟内硬闸那条出现了，故监听器确实在工作。
 *      源码佐证：dsh-tools 的类型定义里 `exitCode` **零命中**。对 shell 类工具而言，
 *      「命令退出码非 0」是**正常结果**，不是工具失败。
 *   ⇒ **刻意只收 isError**：覆盖「被机制拒绝」（尤其硬闸）+「工具自身异常」，
 *      不覆盖普通命令失败 —— 那种我直接看得到，由我自己判断。
 *      刻意不解析 content 里的 `[exit code: N]`：格式脆弱，且与「少打扰」的目标相悖。
 */
function isFailedResult(result) {
  return Boolean(result && typeof result === 'object' && result.isError === true);
}

/** 从失败结果里截一段短消息，**仅用于日志**（绝不投递给模型）。 */
function errorSnippet(result) {
  const message = result && result.error && typeof result.error.message === 'string'
    ? result.error.message
    : '';
  return message.length > 160 ? `${message.slice(0, 160)}…` : message;
}

/**
 * 反思提醒正文。
 *
 * ★ 2026-09-25 二改（用户反馈）：旧版把反思写成「1. 什么有效？ 2. 什么失败？
 *   3. 该固化的模式？」三个编号问题 —— **提示词的形状直接诱导模型照着作答并输出**，
 *   于是每一轮回复里都多出一大段反思叙述。现在改成：
 *     · 明确要求「不要把判断过程或结论输出给用户」；
 *     · 只在确有复用价值时才记，宁缺毋滥（禁止凑数）；
 *     · 记完保持静默 —— 经验库本身即审计轨迹。
 *   顺带把「引用危险命令原文要先落文件」写进去：硬闸扫的是 shell 命令文本。
 */
const REFLECT_PROMPT =
  '【自学习 · 内部动作】刚才有工具调用失败（命令报错 / 被硬闸拦截 / 明显走了弯路）。\n' +
  '请自行判断，**不要把判断过程或结论输出给用户**：\n' +
  '· 确有可复用的教训或经验 → 记一条（失败教训用 `error`，正向经验用 `learn`，能提炼成准则用 `best_practice`）。\n' +
  '· 只是偶发、没有复用价值 → 什么都不做，直接继续正常回复。\n' +
  '**禁止为凑数写噪音**；记完之后**不要在回复里提及**（保持静默）。\n\n' +
  '记录命令：\n' +
  '`python "' + join(SKILL_DIR, 'learn.py') + '" ' +
  'add learn|error|best_practice --title "..." --body "..." --dir "' + LEARNINGS_DIR + '"`\n\n' +
  '⚠️ 若 body 里要引用危险命令原文，先用 write 落成文件、再让脚本读文件：硬闸扫的是 shell 命令文本，直接写进命令行会被 RULE_003 拦下。';

/** notice 折叠行上显示的一行摘要 —— 不展开也能读，这才是 notice 的意义。 */
const REFLECT_SUMMARY = '检测到工具调用失败 —— 请静默判断是否值得记一条经验';

export function apply(ctx, config) {
  const injectOnStart = config?.injectOnStart !== false;
  const reflectOnStop = config?.reflectOnStop !== false;
  const seenTurns = new Set();

  record({ event: 'hooks-ready', injectOnStart, reflectOnStop, learningsDir: LEARNINGS_DIR });

  // ---- 1) 会话开始：注入经验精华 ----
  if (injectOnStart) {
    try {
      ctx.on('agent/created', async ({ agent, source }) => {
        try {
          const text = renderLearnings();
          if (!text) {
            record({ event: 'session-start-skip', reason: 'empty-learnings', source: source ?? null });
            return;
          }
          const ok = tryInject(
            agent,
            text,
            `自学习经验库精华已注入（${(text.match(/^- \[/gm) ?? []).length} 条）`,
          );
          record({ event: 'session-start', source: source ?? null, chars: text.length, injected: ok });
        } catch (error) {
          record({ event: 'session-start-error', error: String(error) });
        }
      });
    } catch (error) {
      record({ event: 'listener-failed', point: 'agent/created', error: String(error) });
    }
  }

  // ---- 2) 回合收尾：仅在「出过问题」时提醒 ----
  // ★ 2026-09-25 二改（用户反馈）：旧版是「用过工具就提醒」，等于每轮都响。
  //   现在只在**工具调用失败**时提醒；正常回合完全静默，是否记录由模型自行判断。
  //   代价是失去「例行兜底」——这是刻意的取舍：用户要求由模型自己判断时机。
  const stats = { tools: 0, errors: 0 };

  if (reflectOnStop) {
    try {
      ctx.on('tools/post-execute', async (exec, result, next) => {
        try {
          stats.tools += 1;
          if (isFailedResult(result)) {
            stats.errors += 1;
            record({
              event: 'trouble-detected',
              tool: typeof exec?.name === 'string' ? exec.name : null,
              signal: 'isError',
              message: errorSnippet(result),
            });
          }
        } catch {
          // 观测失败绝不影响工具调用本身
        }
        return next();
      });
    } catch (error) {
      record({ event: 'listener-failed', point: 'tools/post-execute', error: String(error) });
    }

    try {
      ctx.on('agent/turn-stopping', async ({ agent, turn }) => {
        try {
          const key = String(turn ?? 'unknown');
          const observed = { turn: key, tools: stats.tools, errors: stats.errors };
          if (seenTurns.has(key)) {
            record({ event: 'stop-skip', ...observed, reason: 'already-reminded' });
            return;
          }
          if (stats.errors === 0) {
            // 每次都记一条，用来证明这个机制**还活着**（而不是静默地永不触发）
            record({ event: 'stop-skip', ...observed, reason: 'no-trouble' });
            return;
          }
          seenTurns.add(key);
          stats.tools = 0;
          stats.errors = 0;

          if (agent && typeof agent.steer === 'function') {
            const message = userMessage(REFLECT_PROMPT, REFLECT_SUMMARY);
            if (!assertDeliverable(message, 'steer')) return;
            agent.steer(message);
            record({ event: 'stop-steered', ...observed, mode: 'steer' });
          } else {
            record({ event: 'stop-skip', ...observed, reason: 'no-steer-method' });
          }
        } catch (error) {
          record({ event: 'stop-error', error: String(error) });
        }
      });
    } catch (error) {
      record({ event: 'listener-failed', point: 'agent/turn-stopping', error: String(error) });
    }
  }
}
