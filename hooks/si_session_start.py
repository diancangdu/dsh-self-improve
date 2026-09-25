#!/usr/bin/env python3
"""dsh-self-improve: SessionStart hook.

在会话开始时注入经验库精华 (additionalContext), 让 Codex 开局就"记得"
之前的经验: 什么有效、什么失败、沉淀准则。

输出: {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": [...]}}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_hook_adapter as adapter  # noqa: E402

# learn_parser 在 skill 的 utils 目录 (hooks 运行时复制到 .codex/hooks/, 需显式加路径)
_SKILL_UTILS = r"$DSH_HOME/skills/dsh-self-improve\utils"
if _SKILL_UTILS not in sys.path:
    sys.path.insert(0, _SKILL_UTILS)
from learn_parser import summarize_for_prompt  # noqa: E402

# 防止 stdout 被中文/emoji 搞挂 (Windows GBK)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOOK_EVENT = "SessionStart"


def main() -> None:
    payload = adapter.load_payload()
    cwd = adapter.cwd_from_payload(payload)

    try:
        summary = summarize_for_prompt(base=cwd)
    except Exception as e:  # 任何错误都不该让 hook 崩掉
        adapter.emit_json({
            "hookSpecificOutput": {
                "hookEventName": HOOK_EVENT,
                "additionalContext": [{
                    "title": "Self-Improve Learnings",
                    "content": f"(经验库加载失败: {e})",
                }],
            }
        })
        return

    if not summary.strip():
        # 经验库为空时给一句极简提示
        summary = "(经验库为空 — 完成后可记录经验: python $DSH_HOME/skills/dsh-self-improve/learn.py add learn --title ...)"

    adapter.emit_json({
        "hookSpecificOutput": {
            "hookEventName": HOOK_EVENT,
            "additionalContext": [{
                "title": "Self-Improve Learnings (自学习经验库)",
                "content": summary,
            }],
        }
    })


if __name__ == "__main__":
    raise SystemExit(adapter.main_guard(main))
