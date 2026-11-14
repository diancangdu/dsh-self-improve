#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把经验库渲染成可注入上下文的精华文本。

供 DSH 的原生 self-improve 插件调用，输出到 stdout。两种模式：

  1) **基线模式**（默认，无参数）—— 会话开场用。
     每个分类取**最近 N 条**。此时还没有用户消息，拿不到查询词，
     只能给时间基线（这也是 2026-09-27 之前唯一的行为）。

  2) **相关性模式**（`--query "..."`）—— 首轮 pre-step 拿到用户消息后用。
     ★ 2026-09-27 新增。动机：纯时间切片有**近因偏差** —— 全库 150 条
     （准则 51 / 错误 68 / 经验 31），开场只注入 18 条，一条三个月前的
     高价值经验会被新噪音挤掉。改成按当前任务的相关性排序后，
     「按需查询」这一步就不再依赖模型自觉（此前全靠 RULE_021 的纪律，
     而纪律漏了是静默的）。

设计要点：
  * 不 import 库里其它模块？—— **2026-09-27 改了**：相关性模式复用
    utils/learn_parser.py 的 rank_entries（单一真源，避免第二份会腐烂的排序逻辑）。
    基线模式仍不依赖它，两者都可独立降级。
  * 严格控长：注入进上下文的内容每次会话都要付 token 成本。
  * 任何异常都输出空串并返回 0，绝不让插件因它失败。
"""
from __future__ import annotations

import os
import re
import sys

# 经验库真源 = 技能自带的 .learnings
_SKILL_LEARNINGS = os.path.join(
    os.environ.get("DSH_HOME", os.path.join(os.path.expanduser("~"), ".dsh")),
    "skills", "dsh-self-improve", ".learnings",
)
LEARNINGS_DIR = os.environ.get("DSH_LEARNINGS_DIR", _SKILL_LEARNINGS)

# 顺序即优先级
FILES = [
    ("BEST_PRACTICES.md", "沉淀准则"),
    ("ERRORS.md", "错误教训"),
    ("LEARNINGS.md", "正向经验"),
]

def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# 这几个以前硬编码在代码里（插件 config 只有 3 个布尔）。改成环境变量可覆盖，
# 是为了能**不改代码、不重启**地调参并当场量效果。
MAX_CHARS = _env_int("DSH_LEARN_MAX_CHARS", 12000)
PER_FILE = _env_int("DSH_LEARN_PER_FILE", 6)          # 基线模式: 每类最近 N 条
# 2026-10-10 由 8 提到 24（实测驱动，见下）。
# RELEVANT_MAX 只是**上限**，实际出多少条由检索判据决定：8 个真实 query 里只有
# 2 个能吃满（「改配置重启」8→24、「插件没生效」8→19），其余 6 个一条不变。
# 所以提高它是**免费的上限**（有则受益、无则无损）——这与砍 PER_FILE 不同，
# 后者是确定损失：实测把 PER_FILE 降到 3 会让合计条数 −25%（22.8→17.1）。
# MAX_CHARS 同步提到 12000：实测用量只从 5033 涨到 5640（远够不到上限），
# 它只是「允许」更多，真实成本只来自多注入的那几条（+12% 字符）。
RELEVANT_MAX = _env_int("DSH_LEARN_RELEVANT_MAX", 24)  # 相关性模式: 最多注入 N 条
TITLE_MAX = 90
BODY_MAX = 110
COND_MAX = 130
MIN_SCORE = _env_float("DSH_LEARN_MIN_SCORE", 6.0)
# 2026-10-10 新增: 标题至少命中几个不同 token 才算相关。
# 动机: 只卡总量阈值的旧判据挡不住"正文里偶然撞上生僻 bigram" —— 实测 6 个
# 库内**确无**相关经验的 query，平均被硬塞 6.5 条噪声；加此判据后降到平均
# 0.5 条，而 8 个正例 query 仍全部有召回。
MIN_TITLE_HITS = _env_int("DSH_LEARN_MIN_TITLE_HITS", 2)

# ⚠️ 这里**曾经**有一张 CONDITION_MARKERS 表，用来按关键词从正文里挑「前提句」。
# 2026-09-27 实测后**砍掉**，原因记在 _fmt 的 docstring 里 —— 别再加回来。

# 条目标题行正下方的「已被取代」标记（与 learn_parser.SUPERSEDE_RE 同源）。
# 这里自带一份是为了保持本脚本**独立可跑**（插件用子进程调它）。
SUPERSEDE_RE = re.compile(r"^>\s*SUPERSEDED\s+by:\s*(.+?)\s*$", re.IGNORECASE)


def parse_entries(text: str) -> list[tuple[str, str, str, str]]:
    """解析 `### [日期] 标题` -> [(date, title, body, superseded_by)]"""
    entries = []
    current = None
    for line in text.splitlines():
        m = re.match(r"^### \[([^\]]+)\]\s*(.*)$", line)
        if m:
            if current:
                entries.append(current)
            current = [m.group(1).strip(), m.group(2).strip(), "", ""]
        elif current is not None:
            if line.strip() == "---":
                continue
            sm = SUPERSEDE_RE.match(line.strip())
            if sm:
                current[3] = sm.group(1).strip()
                continue
            current[2] += line + "\n"
    if current:
        entries.append(current)
    return [(d, t, b.strip(), s) for d, t, b, s in entries]


def _fmt(date: str, title: str, body: str) -> str:
    """标题 + 正文首行。

    ★ 为什么**不**自动抽「前提句」（2026-09-27 实测后砍掉的设计，别再试）：
    我先按关键词挑正文里「看起来在讲前提」的那一行。实测**精度很差** ——
    抽查抽出来的 3 条全是误命中，例：把「`learn.py add` 只有 `--body`」
    里的「只有」当成了限定条件。**贴错标签比不贴更糟**，因为「（前提：…）」
    看起来是权威判据，反而让人更放心地误用。

    又试过更笨的办法「多给正文前缀」，扫描结果（167 条实测）：
        正文 110 字 -> 前提丢失 38%，单条 178 字符
        正文 300 字 -> 前提丢失 26%，单条 368 字符
        正文 520 字 -> 前提丢失 13%，单条 579 字符
        正文 700 字 -> 前提丢失  5%，单条 633 字符（8 条 5064，已超预算）
    ⇒ **靠加长注入解决不了这个缺口**：要压到 5% 就得 633 字符/条，预算装不下。

    真正的修法在**记录侧**：把限定条件写进**标题**。理由是标题在注入里
    **完整保留**（不像正文被截），而且标题命中在相关性打分里权重 3.0。
    机械提醒见 `learn_parser.add_entry` 的 `title_needs_condition()`。
    """
    parts = ["- [%s] %s" % (date[:10], title[:TITLE_MAX])]
    first = (body.splitlines() or [""])[0].strip()
    if first:
        parts.append(" —— " + first[:BODY_MAX])
    return "".join(parts)


def render_baseline() -> str:
    """基线模式: 每类取最近 N 条（跳过已被取代的）。"""
    blocks = []
    budget = MAX_CHARS
    for fname, label in FILES:
        path = os.path.join(LEARNINGS_DIR, fname)
        try:
            with open(path, encoding="utf-8") as f:
                raw = parse_entries(f.read())
        except Exception:
            continue
        # 已被取代的条目不进基线：否则纠正条目和它要推翻的旧说法会并排出现，
        # 我可能照着旧的那条做（这正是 supersede 机制要治的病）。
        entries = [e for e in raw if not e[3]]
        if not entries:
            continue

        lines = ["【%s】共 %d 条（在库 %d，已取代 %d），以下为最近 %d 条："
                 % (label, len(entries), len(raw), len(raw) - len(entries),
                    min(PER_FILE, len(entries)))]
        for date, title, body, _sup in entries[-PER_FILE:]:
            lines.append(_fmt(date, title, body))
        block = "\n".join(lines)

        if len(block) > budget:
            block = block[:budget] + "\n...(已截断)"
        blocks.append(block)
        budget -= len(block)
        if budget <= 0:
            break

    if not blocks:
        return ""
    header = (
        "以下是 DSH 自学习经验库（%s）的**时间基线**，任务开始前请先扫一遍，"
        "命中相关经验就照做，不要重复已知错误。\n"
        "注：这只是每类最近 %d 条（全库远多于此）；"
        "若本轮任务涉及特定领域，可用 learn.py query 检索全库。\n"
        "★ 每条都只是**摘要**（标题 + 正文首行），不是全文：照做前先确认它的"
        "**适用条件**是否成立 —— 把带条件的经验当无条件通则用，是这套机制最容易"
        "犯的错。拿不准就 `learn.py query \"关键词\"` 看全文。\n\n"
        % (LEARNINGS_DIR, PER_FILE)
    )
    return header + "\n\n".join(blocks)


def render_relevant(query: str) -> str:
    """相关性模式: 按当前任务排序全库, 只注入高分条目。

    复用 utils/learn_parser.py 的 rank_entries —— 排序逻辑**只此一份**。
    """
    utils_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", "..", "skills", "dsh-self-improve", "utils")
    utils_dir = os.path.normpath(utils_dir)
    if utils_dir not in sys.path:
        sys.path.insert(0, utils_dir)
    try:
        import learn_parser as lp
    except Exception:
        return ""   # 拿不到排序器就静默降级, 基线模式已在开场给过内容

    try:
        rows = lp.rank_entries(query, base=__import__("pathlib").Path(LEARNINGS_DIR),
                               limit=RELEVANT_MAX, min_score=MIN_SCORE,
                               min_title_hits=MIN_TITLE_HITS)
    except Exception:
        return ""
    if not rows:
        # 2026-10-10: 以前这里静默返回 ""，等于"查不到也不说"。
        # 明说「没查到」比沉默有用得多 —— 否则我可能把上文基线里不相干的条目
        # 当成"这就是相关的经验"。但必须同时点明它**不等于库里没有**:
        # 本库是词法检索（中文 bigram、无语义），换个说法往往就能命中。
        return (
            "【本轮任务的相关经验】用当前措辞**没有检索到**相关经验"
            "（判据：总分 > %.1f 且标题至少命中 %d 个词）。\n"
            "★ 「没检索到」≠「库里没有」：本库是**词法**检索（中文 bigram，"
            "没有语义理解），换一种关键说法再查 "
            "`learn.py query \"...\"` 往往就能命中；\n"
            "  但也不要因此把上文基线里不相干的条目当成相关经验照搬。\n"
            % (MIN_SCORE, MIN_TITLE_HITS)
        )

    label = {"best_practice": "沉淀准则", "error": "错误教训", "learn": "正向经验"}
    lines = []
    budget = MAX_CHARS
    for r in rows:
        line = _fmt(r.get("date", ""), r.get("title", ""), r.get("body", ""))
        line = "[%s] %s" % (label.get(r.get("kind", ""), r.get("kind", "")), line[2:])
        if len(line) > budget:
            break
        lines.append(line)
        budget -= len(line)
    if not lines:
        return ""
    return (
        "【本轮任务的相关经验】以下按与你当前任务的相关性排序（全库检索，非时间切片）：\n"
        "★ 每条都只是**摘要**（标题 + 正文首行），不是全文：照做前先确认它的"
        "**适用条件**是否成立 —— 把带条件的经验当无条件通则用，是这套机制最容易"
        "犯的错。拿不准就 `learn.py query \"关键词\"` 看全文。\n"
        + "\n".join(lines)
    )


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    query = ""
    if "--query" in argv:
        i = argv.index("--query")
        if i + 1 < len(argv):
            query = argv[i + 1]

    text = render_relevant(query) if query.strip() else render_baseline()
    if text:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception:
        # 任何未预期异常都静默退出，避免影响会话
        raise SystemExit(0)
