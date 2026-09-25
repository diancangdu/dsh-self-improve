#!/usr/bin/env python3
"""dsh-self-improve: Stop hook (任务结束反思提醒).

任务结束 (Stop) 时提示 Codex 做反思: 本轮有没有学到值得记录的东西?
如果有 → 记录到经验库; 没有 → 跳过。同时给出经验库路径。

输出: {"systemMessage": "..."}
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_hook_adapter as adapter  # noqa: E402

# learn_parser 在 skill 的 utils 目录
_SKILL_UTILS = r"$DSH_HOME/skills/dsh-self-improve\utils"
if _SKILL_UTILS not in sys.path:
    sys.path.insert(0, _SKILL_UTILS)
from learn_parser import review  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> None:
    payload = adapter.load_payload()
    cwd = adapter.cwd_from_payload(payload)

    # 静默失败: 任何异常都不输出提醒 (避免打扰)
    try:
        rep = review(base=cwd)
    except Exception:
        return

    total = rep.get("total", 0)
    learn_py = r"$DSH_HOME/skills/dsh-self-improve\learn.py"

    msg = (
        f"[self-improve] 本轮任务结束。反思提示: 如果学到了新经验(什么有效/什么失败), "
        f"请记录到经验库: python {learn_py} add learn --title ... (当前经验库共 {total} 条)。"
        f"若无可记录则忽略本提示。"
    )
    adapter.emit_json({"systemMessage": msg})


if __name__ == "__main__":
    raise SystemExit(adapter.main_guard(main))
