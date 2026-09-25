# -*- coding: utf-8 -*-
"""dsh-self-improve 自动化测试."""
import os
import subprocess
import sys
import tempfile
import json
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))
from learn_parser import add_entry, query_entries, review, ensure_learnings_dir, text_contains, summarize_for_prompt

BASE = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "learn.py")]
passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS: {name}")
    else:
        failed += 1
        print(f"FAIL: {name}")


def main():
    # 用临时目录做测试库 (避免污染真实经验库)
    tmp = Path(tempfile.mkdtemp(prefix="si_test_"))
    env = dict(os.environ)
    env["CODEX_LEARNINGS_DIR"] = str(tmp)

    def run(args):
        return subprocess.run(BASE + args, capture_output=True, text=True, encoding="utf-8", env=env)

    # 1. status (自动创建经验库)
    r = run(["status"])
    check("status exits 0", r.returncode == 0)
    check("status shows 3 files", "LEARNINGS.md" in r.stdout and "ERRORS.md" in r.stdout and "BEST_PRACTICES.md" in r.stdout)

    # 2. add learn
    r = run(["add", "learn", "--title", "测试有效经验", "--body", "用X方法解决了Y问题"])
    check("add learn exits 0", r.returncode == 0)
    j = json.loads(r.stdout)
    check("add learn ok=True", j.get("ok") is True)

    # 3. duplicate reject
    r = run(["add", "learn", "--title", "测试有效经验", "--body", "重复"])
    j = json.loads(r.stdout)
    check("duplicate rejected", j.get("ok") is False and j.get("reason") == "duplicate")

    # 4. add error
    r = run(["add", "error", "--title", "不要在临时目录执行命令", "--body", "教训内容"])
    check("add error exits 0", r.returncode == 0)

    # 5. query hit (精确子串)
    r = run(["query", "临时目录"])
    check("query exact hit", "不要在临时目录执行命令" in r.stdout)

    # 6. query fuzzy (bigram overlap)
    r = run(["query", "临时目录执行"])
    check("query fuzzy hit", "不要在临时目录执行命令" in r.stdout)

    # 7. query no hit
    r = run(["query", "完全不相关的词xyz"])
    check("query no hit", "(无匹配经验)" in r.stdout)

    # 8. query json
    r = run(["query", "临时", "--json"])
    rows = json.loads(r.stdout)
    check("query json returns list", isinstance(rows, list) and len(rows) >= 1)

    # 9. review
    r = run(["review", "--json"])
    rep = json.loads(r.stdout)
    check("review total >= 2", rep.get("total", 0) >= 2)
    check("review stats", rep["stats"]["learn"] >= 1 and rep["stats"]["error"] >= 1)

    # 10. add best_practice
    r = run(["add", "best_practice", "--title", "先自查再动手", "--body", "每次任务前先查经验库"])
    check("add best_practice exits 0", r.returncode == 0)

    # 11. text_contains fuzzy boundary
    check("fuzzy 删除整个目录 -> 删除整个项目目录", text_contains("删除整个目录", "删除整个项目目录"))
    check("fuzzy exact substring", text_contains("备份", "修改配置前必须备份"))
    check("fuzzy single char no match", not text_contains("备", "随便什么文本"))
    check("fuzzy unrelated", not text_contains("天气很好", "代码编译出错"))

    # 12. summarize_for_prompt (直接 import, 传 base=tmp)
    ensure_learnings_dir(tmp)  # 先创建经验库文件
    s = summarize_for_prompt(base=tmp)
    check("summarize non-empty", isinstance(s, str) and len(s) > 0)
    check("summarize contains title", "测试有效经验" in s or "不要在临时目录" in s or "先自查再动手" in s)

    # 13. ensure 独立 api 直接调用 (传 base)
    d = ensure_learnings_dir(tmp)
    check("ensure dir created", os.path.isdir(d))

    # 14. add_entry / query_entries 直接 API
    res = add_entry("learn", "API直接调用经验", "细节", base=tmp)
    check("api add_entry ok", res.get("ok") is True)
    rows2 = query_entries("API直接", base=tmp)
    check("api query_entries hit", len(rows2) >= 1)

    # cleanup
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
