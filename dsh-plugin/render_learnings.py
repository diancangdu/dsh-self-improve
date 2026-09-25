#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把经验库渲染成可注入上下文的精华文本。

供 DSH 的原生 self-improve 插件在会话开始时调用，输出到 stdout。
设计要点：
  * 不 import 库里其它模块，保持独立可跑（插件用子进程调用它）。
  * 优先给「沉淀准则」和「错误教训」——它们的复用价值高于单条正向经验。
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

MAX_CHARS = 3500
PER_FILE = 6          # 每个文件最多取最近 N 条
TITLE_MAX = 90
BODY_MAX = 110


def parse_entries(text: str) -> list[tuple[str, str, str]]:
    """解析 `### [日期] 标题` 形式的条目 -> [(date, title, body)]"""
    entries = []
    current = None
    for line in text.splitlines():
        m = re.match(r"^### \[([^\]]+)\]\s*(.*)$", line)
        if m:
            if current:
                entries.append(current)
            current = [m.group(1).strip(), m.group(2).strip(), ""]
        elif current is not None:
            if line.strip() == "---":
                continue
            current[2] += line + "\n"
    if current:
        entries.append(current)
    return [(d, t, b.strip()) for d, t, b in entries]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    blocks = []
    budget = MAX_CHARS

    for fname, label in FILES:
        path = os.path.join(LEARNINGS_DIR, fname)
        try:
            with open(path, encoding="utf-8") as f:
                entries = parse_entries(f.read())
        except Exception:
            continue
        if not entries:
            continue

        lines = ["【%s】共 %d 条，以下为最近 %d 条：" % (label, len(entries), min(PER_FILE, len(entries)))]
        for date, title, body in entries[-PER_FILE:]:
            line = "- [%s] %s" % (date[:10], title[:TITLE_MAX])
            first = (body.splitlines() or [""])[0].strip()
            if first:
                line += " —— " + first[:BODY_MAX]
            lines.append(line)
        block = "\n".join(lines)

        if len(block) > budget:
            block = block[:budget] + "\n...(已截断)"
        blocks.append(block)
        budget -= len(block)
        if budget <= 0:
            break

    if blocks:
        header = (
            "以下是 DSH 自学习经验库（%s）的精华，任务开始前请先扫一遍，"
            "命中相关经验就照做，不要重复已知错误：\n\n" % LEARNINGS_DIR
        )
        sys.stdout.write(header + "\n\n".join(blocks))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # 任何未预期异常都静默退出，避免影响会话
        raise SystemExit(0)
