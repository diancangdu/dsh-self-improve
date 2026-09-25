# -*- coding: utf-8 -*-
"""dsh-self-improve 经验库核心解析/读写模块.

经验库结构 (markdown 文件, 项目根目录 .learnings/ 或技能自带 <skill>\\.learnings/ 下):
  .learnings/
    LEARNINGS.md        # 什么有效 (正向经验 / patterns that work)
    ERRORS.md           # 什么失败 (错误教训 / mistakes to avoid)
    BEST_PRACTICES.md   # 沉淀的高层准则 (consolidated principles)

设计原则:
  - 纯 markdown + 日期标注, 人可读可审计
  - 只追加 / 不覆盖, 每条经验一个原子条目
  - 模糊匹配复用 bigram 重叠 >= 0.6 (宁多勿漏, 与 mistake-archive 一致)
  - 所有路径基于技能目录解析, 任何 cwd 下运行都有效
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------

# 技能目录 (本文件所在目录的上级)
SKILL_DIR = Path(__file__).resolve().parent.parent

# 技能自带经验库 (本实例独立, 与其它 agent 的库物理隔离).
# 上游 deploy.ps1 本来就按 <skill>/.learnings 预留运行时数据位, 现改为真源.
SKILL_LEARNINGS_DIR = SKILL_DIR / ".learnings"

# 全局经验库根: 默认即技能自带的 .learnings
GLOBAL_LEARNINGS_DIR = Path(os.environ.get(
    "CODEX_LEARNINGS_DIR",
    str(SKILL_LEARNINGS_DIR),
))


def learnings_dir_for(cwd: Optional[Path] = None) -> Path:
    """返回当前工作目录对应的经验库目录.

    优先级:
      1. 环境变量 CODEX_LEARNINGS_DIR 显式指定
      2. cwd 本身是经验库目录 (含 LEARNINGS.md) -> 直接用
      3. cwd 下的 .learnings (项目级)
      4. 技能自带的 <skill>\\.learnings (兜底, 保证任何 cwd 都有经验库)
    """
    env = os.environ.get("CODEX_LEARNINGS_DIR")
    if env:
        return Path(env)
    if cwd is not None:
        cwd = Path(cwd)
        if (cwd / "LEARNINGS.md").exists():
            return cwd
        proj = cwd / ".learnings"
        if proj.exists():
            return proj
    # 默认全局
    return GLOBAL_LEARNINGS_DIR


# 文件 -> (分类标签, 标题)
FILE_META = {
    "LEARNINGS.md": ("learn", "正向经验 (What Works)"),
    "ERRORS.md": ("error", "错误教训 (Mistakes to Avoid)"),
    "BEST_PRACTICES.md": ("best_practice", "沉淀准则 (Best Practices)"),
}

DEFAULT_FILES = {
    "LEARNINGS.md": "# 正向经验 (Learnings)\n\n什么有效、值得记住的模式、偏好。每条用日期标注, 只追加不覆盖。\n\n---\n\n",
    "ERRORS.md": "# 错误教训 (Errors)\n\n命令失败、集成错误、走弯路的教训。每条用日期标注。\n\n---\n\n",
    "BEST_PRACTICES.md": "# 沉淀准则 (Best Practices)\n\n从 LEARNINGS / ERRORS 中提炼的高层准则。由 review 命令生成。\n\n---\n\n",
}


def ensure_learnings_dir(base: Optional[Path] = None) -> Path:
    """确保经验库目录存在, 返回目录路径."""
    d = learnings_dir_for(base)
    d.mkdir(parents=True, exist_ok=True)
    for fname, content in DEFAULT_FILES.items():
        fp = d / fname
        if not fp.exists():
            fp.write_text(content, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------

_WRITE_LOCK = threading.Lock()


def _append_entry(filepath: Path, title: str, body: str) -> None:
    """原子追加一条经验 (带日期 + 标题 + 正文)."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"### [{now}] {title}\n\n{body.strip()}\n\n---\n\n"
    with _WRITE_LOCK:
        # 追加到 "---\n\n" 分隔线之前? 不, 直接追加到文件末尾 (模板末尾已有分隔线)
        with filepath.open("a", encoding="utf-8") as f:
            f.write(entry)


def _read_entries(filepath: Path) -> list[dict[str, str]]:
    """解析 markdown 经验条目 -> [{date, title, body}]"""
    if not filepath.exists():
        return []
    text = filepath.read_text(encoding="utf-8")
    entries: list[dict[str, str]] = []
    # 匹配 ### [date] title
    lines = text.splitlines()
    current: Optional[dict[str, str]] = None
    for line in lines:
        m = re.match(r"^### \[([^\]]+)\] (.*)$", line)
        if m:
            if current:
                entries.append(current)
            current = {"date": m.group(1).strip(), "title": m.group(2).strip(), "body": ""}
        elif current is not None:
            if line.strip() == "---":
                continue
            current["body"] += line + "\n"
    if current:
        entries.append(current)
    for e in entries:
        e["body"] = e["body"].strip()
    return entries


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    if len(s) < 2:
        return set(s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def text_contains(query: str, text: str) -> bool:
    """模糊匹配: 精确子串命中, 或字符 bigram 重叠 >= 0.6."""
    q = query.strip()
    t = text
    if not q:
        return False
    if q in t:
        return True
    # 单字符关键词不做模糊 (避免噪音)
    if len(re.sub(r"\s+", "", q)) < 2:
        return False
    qb = _bigrams(q)
    tb = _bigrams(t)
    if not qb:
        return False
    overlap = len(qb & tb) / len(qb)
    return overlap >= 0.6


# ---------------------------------------------------------------------------
# 核心 API
# ---------------------------------------------------------------------------

def add_entry(kind: str, title: str, body: str, base: Optional[Path] = None) -> dict[str, Any]:
    """新增一条经验. kind: learn | error | best_practice"""
    fname = {"learn": "LEARNINGS.md", "error": "ERRORS.md", "best_practice": "BEST_PRACTICES.md"}.get(kind)
    if fname is None:
        raise ValueError(f"未知类型: {kind} (应为 learn/error/best_practice)")
    d = ensure_learnings_dir(base)
    fp = d / fname
    # 重复检查 (title 相同视为重复)
    for e in _read_entries(fp):
        if e["title"].strip().lower() == title.strip().lower():
            return {"ok": False, "reason": "duplicate", "file": str(fp), "title": title}
    _append_entry(fp, title, body)
    return {"ok": True, "file": str(fp), "title": title, "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}


def query_entries(query: str, kind: Optional[str] = None, base: Optional[Path] = None, limit: int = 10) -> list[dict[str, str]]:
    """按关键词模糊查询全部经验库条目."""
    d = learnings_dir_for(base)
    results: list[dict[str, str]] = []
    kinds = [kind] if kind else ["learn", "error", "best_practice"]
    for k in kinds:
        fname = {"learn": "LEARNINGS.md", "error": "ERRORS.md", "best_practice": "BEST_PRACTICES.md"}[k]
        fp = d / fname
        for e in _read_entries(fp):
            hay = f"{e['title']}\n{e['body']}"
            if not query or text_contains(query, hay):
                item = dict(e)
                item["kind"] = k
                item["file"] = str(fp)
                results.append(item)
    # 排序: 新在前
    results.sort(key=lambda x: x.get("date", ""), reverse=True)
    return results[:limit]


def review(base: Optional[Path] = None) -> dict[str, Any]:
    """复盘: 统计经验库状态, 提炼最佳实践 (返回报告, 不自动写入)."""
    d = learnings_dir_for(base)
    stats = {}
    all_entries: list[dict[str, str]] = []
    for k, fname in [("learn", "LEARNINGS.md"), ("error", "ERRORS.md"), ("best_practice", "BEST_PRACTICES.md")]:
        fp = d / fname
        entries = _read_entries(fp)
        stats[k] = len(entries)
        for e in entries:
            e["kind"] = k
            all_entries.append(e)
    return {
        "dir": str(d),
        "stats": stats,
        "total": len(all_entries),
        "entries": all_entries,
    }


def summarize_for_prompt(base: Optional[Path] = None, max_chars: int = 4000) -> str:
    """生成注入上下文的经验精华 (SessionStart hook 用)."""
    d = learnings_dir_for(base)
    parts: list[str] = []
    budget = max_chars
    # 最佳实践最优先, 然后是错误, 最后正向经验
    for fname, header in [("BEST_PRACTICES.md", "【沉淀准则】"), ("ERRORS.md", "【错误教训】"), ("LEARNINGS.md", "【正向经验】")]:
        fp = d / fname
        entries = _read_entries(fp)
        if not entries:
            continue
        block = [f"{header} ({len(entries)} 条):"]
        for e in entries[-8:]:  # 每个文件最多取最近 8 条
            line = f"- [{e['date'][:10]}] {e['title']}"
            if e["body"]:
                # 正文太长只取第一句
                first_sentence = e["body"].split("\n")[0][:120]
                line += f" — {first_sentence}"
            block.append(line)
        text = "\n".join(block)
        if len(text) > budget:
            text = text[:budget] + "..."
        parts.append(text)
        budget -= len(text)
        if budget <= 0:
            break
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def cli_main(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="learn.py", description="dsh-self-improve 经验库工具")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="新增一条经验")
    a.add_argument("kind", choices=["learn", "error", "best_practice"], help="经验类型")
    a.add_argument("--title", required=True, help="标题 (一句话)")
    a.add_argument("--body", default="", help="正文 (细节)")
    a.add_argument("--dir", help="经验库目录 (默认自动)")

    q = sub.add_parser("query", help="查询经验")
    q.add_argument("keyword", nargs="?", default="", help="关键词 (空=全部)")
    q.add_argument("--kind", choices=["learn", "error", "best_practice"])
    q.add_argument("--json", action="store_true")
    q.add_argument("--dir", help="经验库目录")

    r = sub.add_parser("review", help="复盘统计")
    r.add_argument("--json", action="store_true")
    r.add_argument("--dir", help="经验库目录")

    s = sub.add_parser("status", help="显示经验库状态")
    s.add_argument("--dir", help="经验库目录")

    args = p.parse_args(argv)
    base = Path(args.dir) if getattr(args, "dir", None) else None

    if args.cmd == "add":
        res = add_entry(args.kind, args.title, args.body, base=base)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 1

    if args.cmd == "query":
        rows = query_entries(args.keyword, kind=getattr(args, "kind", None), base=base)
        if getattr(args, "json", False):
            print(json.dumps(rows, ensure_ascii=False))
            return 0
        if not rows:
            print("(无匹配经验)")
            return 0
        for r in rows:
            print(f"[{r['kind']}] ({r['date']}) {r['title']}")
            if r["body"]:
                print(f"    {r['body'][:150]}")
        return 0

    if args.cmd == "review":
        rep = review(base)
        if getattr(args, "json", False):
            print(json.dumps(rep, ensure_ascii=False))
            return 0
        print(f"经验库目录: {rep['dir']}")
        print(f"总计 {rep['total']} 条: 正向经验 {rep['stats']['learn']} / 错误教训 {rep['stats']['error']} / 沉淀准则 {rep['stats']['best_practice']}")
        return 0

    if args.cmd == "status":
        d = ensure_learnings_dir(base)
        print(f"经验库目录: {d}")
        for fname, label in [("LEARNINGS.md", "正向经验"), ("ERRORS.md", "错误教训"), ("BEST_PRACTICES.md", "沉淀准则")]:
            fp = d / fname
            n = len(_read_entries(fp))
            print(f"  {fname}: {n} 条 ({label})")
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
