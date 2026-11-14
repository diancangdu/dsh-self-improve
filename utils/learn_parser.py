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

★ 2026-10-16 性能改造（实测驱动，读数见文件末尾 `_load_index` 注释）:
  - **一次分词 + 磁盘索引缓存**：全库分词 1077 条 196 ms 原本每次检索都要付
    （原 rank 更是把每条正文分词**两遍**：idf 一遍、打分一遍，合计 431 ms）。
    现在把「条目 + 预分词集合 + df 表」按**文件指纹**缓存成 pickle，
    命中时加载 36 ms。指纹 = 三个 .md 的 (mtime_ns, size)。
  - **query / rank 统一到同一个打分器**，只留两档阈值（见 `_search`）。
    原先是两套判据（query 用 char-bigram 60% 重叠、rank 用 idf 加权），
    实测同一个库会出现「C盘 缓存 安装」query 5 条 / rank 0 条这种两个入口两个答案。
  - `text_contains` / `score_entry` / `_build_idf` **保留原签名**（测试与外部兼容），
    内部改为薄封装，逻辑只此一份。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import pickle
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

# 技能自带经验库 (2026-09-25 由 <old-learnings-dir> 迁入).
# 上游 deploy.ps1 本来就按 <skill>\.learnings 预留运行时数据位, 现改为真源.
SKILL_LEARNINGS_DIR = SKILL_DIR / ".learnings"

# 全局经验库根: 默认即技能自带的 .learnings
GLOBAL_LEARNINGS_DIR = Path(os.environ.get(
    "CODEX_LEARNINGS_DIR",
    str(SKILL_LEARNINGS_DIR),
))


def learnings_dir_for(cwd: Optional[Path] = None) -> Path:
    """返回当前工作目录对应的经验库目录.

    优先级（2026-10-15 调整）:
      1. **显式传入的 cwd/base** —— CLI 的 `--dir` 就落在这一层
      2. cwd 本身是经验库目录 (含 LEARNINGS.md) -> 直接用
      3. cwd 下的 .learnings (项目级)
      4. 环境变量 CODEX_LEARNINGS_DIR
      5. 技能自带的 <skill>\\.learnings (兜底, 保证任何 cwd 都有经验库)

    ★ 为什么把环境变量从第 1 位挪到第 4 位：`--dir` 是**显式**指令，
    而环境变量是**环境默认值** —— 旧顺序会让 `learn.py --dir <项目库> add ...`
    在 CODEX_LEARNINGS_DIR 被设过时**静默写到另一个库**（本机现在没设这个变量，
    所以是潜在坑而非现发故障）。SKILL.md 又恰恰推荐「统一用显式 --dir 最稳」，
    两者叠加就是「照文档做反而错」。同族问题见文件头 `_read_entries` 的注释：
    同一份数据两个真源，口径必须只有一个。
    """
    if cwd is not None:
        cwd = Path(cwd)
        if (cwd / "LEARNINGS.md").exists():
            return cwd
        proj = cwd / ".learnings"
        if proj.exists():
            return proj
    env = os.environ.get("CODEX_LEARNINGS_DIR")
    if env:
        return Path(env)
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
    "BEST_PRACTICES.md": "# 沉淀准则 (Best Practices)\n\n跨项目通用的高层准则（由 `add best_practice` 手工沉淀；`review` 只统计、不写入）。\n\n---\n\n",
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

# 「已被取代」标记：写在条目标题行正下方的一行引用。
# 为什么用就地标记而不是删除 —— 见 supersede_entry() 的 docstring。
SUPERSEDE_RE = re.compile(r"^>\s*SUPERSEDED\s+by:\s*(.+?)\s*$", re.IGNORECASE)


def _append_entry(filepath: Path, title: str, body: str) -> None:
    """原子追加一条经验 (带日期 + 标题 + 正文)."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"### [{now}] {title}\n\n{body.strip()}\n\n---\n\n"
    with _WRITE_LOCK:
        # 追加到 "---\n\n" 分隔线之前? 不, 直接追加到文件末尾 (模板末尾已有分隔线)
        with filepath.open("a", encoding="utf-8") as f:
            f.write(entry)


def _read_entries(filepath: Path) -> list[dict[str, str]]:
    """解析 markdown 经验条目 -> [{date, title, body, superseded_by}]

    `superseded_by` 非空表示这条已被更新的条目取代（见 `supersede_entry`）。
    标记行**不进 body**：否则 "superseded" 这个词会把所有被取代的条目
    在相关性打分上聚成一类。
    """
    if not filepath.exists():
        return []
    text = filepath.read_text(encoding="utf-8")
    entries: list[dict[str, str]] = []
    # 匹配 ### [date] title
    lines = text.splitlines()
    current: Optional[dict[str, str]] = None
    for line in lines:
        # ★ 表头 `### [日期] 标题` 的 `]` 后**允许没有空格**（2026-10-15 改 `\] (.*)` -> `\]\s*(.*)`）。
        #   动机：同一份 markdown 有**两个**解析器 —— 本模块与插件侧的
        #   `profiles/desktop/self-improve/render_learnings.py`（它自带一份以实现独立可跑）。
        #   原先本模块要求恰好一个空格，而那边用 `\s*`，于是同一份库会报出两个不同的
        #   「共 N 条」（实测曾出现 355 vs 357 的差），排查时白花一轮。
        #   放宽后两边口径一致：多认一个空格不会误判（`\s*` 对正常行等价于原来的 ` `）。
        m = re.match(r"^### \[([^\]]+)\]\s*(.*)$", line)
        if m:
            if current:
                entries.append(current)
            current = {"date": m.group(1).strip(), "title": m.group(2).strip(),
                       "body": "", "superseded_by": ""}
        elif current is not None:
            if line.strip() == "---":
                continue
            sm = SUPERSEDE_RE.match(line.strip())
            if sm:
                current["superseded_by"] = sm.group(1).strip()
                continue
            current["body"] += line + "\n"
    if current:
        entries.append(current)
    for e in entries:
        e["body"] = e["body"].strip()
    return entries


def supersede_entry(old: str, new_title: str, base: Optional[Path] = None,
                    why: str = "") -> dict[str, Any]:
    """把一条旧经验标记为「已被取代」—— 就地插一行，不删原文。

    ★ 为什么需要它（2026-09-27 实测）：库是 append-only 的，CLI 只有 add，
    所以「纠正」以前只能**新增一条**，原错误条目原地不动、**继续参与检索**。
    结果是纠正与错误并排竞争，下次可能先看到错的那条 —— 实测库里已有
    **6 条**纠正类条目处于这种状态（例：「纠正：『profile patch 只在启动时
    读一次』是过度概括」与它要推翻的说法并存）。

    为什么是「就地标记」而不是删除：
      ① 保留审计痕迹 —— 那条错的当初为什么被信、什么时候、被什么推翻；
      ② 不产生读-改-写的内容丢失风险（并发会话正在追加时会丢内容），只插一行；
      ③ 可逆 —— 误标就直接删掉那一行。

    标记后该条目**默认不再出现在 query / rank 结果里**
    （要看得传 include_superseded=True，或 CLI 加 --include-superseded）。

    @param old - 旧条目标题的**唯一子串**（不唯一会报 ambiguous，不猜）
    @param new_title - 取代它的新条目标题
    @param why - 可选，一句话说明为什么被取代
    """
    d = learnings_dir_for(base)
    matches: list[tuple[str, Path, dict]] = []
    for kind, fname in (("learn", "LEARNINGS.md"), ("error", "ERRORS.md"),
                        ("best_practice", "BEST_PRACTICES.md")):
        fp = d / fname
        for e in _read_entries(fp):
            if old.strip().lower() in e["title"].strip().lower():
                matches.append((kind, fp, e))

    if not matches:
        return {"ok": False, "reason": "not-found", "old": old}
    if len(matches) > 1:
        # 刻意不做「取第一个」：标错对象比标不上更糟。
        return {"ok": False, "reason": "ambiguous", "old": old,
                "count": len(matches),
                "candidates": [{"kind": k, "title": e["title"]}
                               for k, _, e in matches]}

    kind, fp, entry = matches[0]
    if entry.get("superseded_by"):
        return {"ok": False, "reason": "already-superseded",
                "title": entry["title"], "superseded_by": entry["superseded_by"]}

    stamp = datetime.datetime.now().strftime("%Y-%m-%d")
    marker = f"> SUPERSEDED by: {new_title} ({stamp})"
    if why:
        marker += f" —— {why}"

    text = fp.read_text(encoding="utf-8")
    out: list[str] = []
    anchored = False
    for line in text.splitlines(keepends=True):
        out.append(line)
        if anchored:
            continue
        # 与 _read_entries 同一口径（`]` 后允许无空格），否则「读得到却锚不上」。
        m = re.match(r"^### \[([^\]]+)\]\s*(.*?)\s*$", line.rstrip("\n"))
        if m and m.group(2).strip() == entry["title"]:
            out.append(marker + "\n")
            anchored = True

    if not anchored:
        return {"ok": False, "reason": "anchor-not-found", "title": entry["title"]}

    with _WRITE_LOCK:
        fp.write_text("".join(out), encoding="utf-8")
    return {"ok": True, "file": str(fp), "kind": kind, "title": entry["title"],
            "superseded_by": new_title, "date": stamp}


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
# 记录时的「前提是否写进标题」检查（2026-09-27 新增）
# ---------------------------------------------------------------------------
#
# 背景（实测）：注入片段是「标题 + 正文首行」，只露出中位 27% 的内容。
# 于是正文里的限定条件在注入时看不见，一条**带条件的经验**会被当成
# **无条件通则**用 —— 这是这套机制最容易犯、也最难自己发现的错。
#
# 试过两条在**注入侧**补救的路，都不成立：
#   ① 按关键词抽「前提句」：精度差，抽查全是误命中（把「只有 --body」的
#      「只有」当限定词）。贴错标签比不贴更糟，因为「（前提：…）」看着权威。
#   ② 加长正文前缀：110 字 -> 丢失 38%；300 字 -> 26%；要 700 字才到 5%，
#      而那时单条 633 字符、8 条就超预算。**预算内补不上这个缺口。**
# ⇒ 唯一可行的修法在**记录侧**：把条件写进标题。标题在注入里完整保留，
#   且标题命中在相关性打分里权重 3.0（正文只有 1.0）。
#
# 这里刻意用**紧**表（真限定词）。宽松表会天天误报，狼来了之后没人看。
TITLE_CONDITION_MARKERS = (
    "本机", "仅", "只有", "如果", "除非", "前提", "适用", "特定",
    "只对", "限于", "这种情况下",
    "Windows PowerShell", "5.1", "v18", "Node 1", "Python 3",
)

# 结构型条件（2026-09-27 新增）。
#
# 为什么需要它：上面那张表是**子串**匹配，而中文里最自然的条件表达是「当 X 时」——
# 「当」单独太宽（会命中「应当 / 适当 / 当作」），「时」更宽（会命中「同时 / 临时 /
# 小时」），所以两个单字都进不了紧表，结果这个**最常见的结构**反而完全不被识别。
#
# 实测代价（2026-09-27）：我写标题「（当脚本模式是「读文件 → 变换 → 写回同一文件」时）」
# 时提醒**没有响**，我一度以为「标题改对了」；查实现才发现是那一刻**正文前 300 字里
# 恰好没有 marker 词**，机制漏报了。也就是说：一个正确的条件写法被判成「没写条件」，
# 而我（作者）因为提醒消失反而更确信自己写对了 —— **沉默的漏报比误报危险**。
#
# 用**限长**正则表达结构而不是放开单字：既认得出「当…时」，又不会把「应当注意」
# 这类无关表述当成条件。限长还顺带避免了跨句误匹配。
TITLE_CONDITION_PATTERNS = (
    r"当[^。；\n]{0,40}?时",        # 当 … 时
    r"在[^。；\n]{0,30}?情况下",     # 在 … 情况下
    r"适用于?[^。；\n]{0,20}?的",    # 适用于 … 的
)


def title_has_condition(title: str) -> bool:
    """标题里是否已经表达了限定条件（子串表 或 结构正则）。"""
    if any(m in title for m in TITLE_CONDITION_MARKERS):
        return True
    return any(re.search(p, title) for p in TITLE_CONDITION_PATTERNS)


# ---------------------------------------------------------------------------
# 记录时查「同一个结论是不是已经记过」 (2026-11-14 新增)
# ---------------------------------------------------------------------------
#
# 为什么必须有这一步（2026-11-14 全库实测，读数见 _scratch/learnings-audit）：
#   add_entry 原本只挡**标题逐字相同**，于是「同一结论换个说法」可以无限入库。
#   实测两类代价：
#   ① 同一教训被记 4 次且四条**检索分完全相同**（41.3）——「脚本改过文件后
#      edit 报 file changed since read」，四条措辞不同、结论一致；
#   ② 注入基线（每类最近 6 条）里同一条「探针动作要幂等」占了两格，
#      6 个槽位实际只送来 5 条互不相同的经验。
#
# 为什么不用 char-bigram Jaccard 判重：它跨措辞就失效。实测那一对同教训标题
#   「帧探针/时序基准的动作必须用显式状态 API，不能用点击 toggle（同一动作重复测 10 次时）」
#   「当同一交互要重复测 N 次时，探针动作必须幂等（不能用点击 toggle）」
#   Jaccard 只有 0.17（长标题拉低了分母），0.7 的阈值一条都抓不到。
#   而它们共享一段 12 字的连续短语「不能用点击 toggle」⇒ 改判**最长公共子串**。
#
# 为什么阈值是 **20 字**（2026-11-14 在全库 1283 条上标定，不是拍脑袋）：
#   把每条标题当「新记录」跑一遍统计命中率：
#     L≥12 -> 20.0% 的条目会报（257 条），样本里大量是「同场景不同结论」的假阳性
#             （共享的只是 package.json / node_modules / 在FinalEngine 这类主题名）；
#     L≥20 -> 2.4%（31 条），样本逐条看基本是同一结论的两种写法。
#   ⇒ 取 20：宁可漏（2.4% 的噪声率才不会让人无视提醒），不可误报到「狼来了」。
#   ★ 已知漏检（实测，别当成 bug）：一对真重复也可能只共享 11 字 ——
#     「帧探针/时序基准的动作必须用显式状态 API，不能用点击 toggle（同一动作重复测 10 次时）」
#     与「当同一交互要重复测 N 次时，探针动作必须幂等（不能用点击 toggle）」共享 11 字，
#     逃过了阈值。措辞重述本质上是语义问题，词面工具做不到全召回；批量维护用 `dups` 命令。
#
# 另外要排除「公共开头/结尾」：中文标题里有大量套话前缀（「在 Windows PowerShell 5.1 里用 …」
# 「仅当在本机 …」），只按 LCS 判会让 129 条互不相干的 PS 5.1 条目连成一簇（实测过）。
# 套话头/尾多半是**前提**而不是**结论**，所以前缀/后缀命中只在覆盖 ≥80% 短标题
# （即两个标题本就近乎相同）时才算重复。
_DUP_PUNC_RE = re.compile(r"[\s，,。;；:：`'\"“”‘’|/\\()\[\]{}<>《》\-—…·]")
_NEAR_DUP_MIN_LCS = 20      # 连续公共子串下限（字）—— 标定见上，别随手调小
_NEAR_DUP_MAX_DF = 4        # 这段原文最多出现在几条里；更常见的就是套话/主题名
_NEAR_DUP_GRAM = 12         # 预筛：LCS≥20 必然共享一个 12-gram，先用它把候选从 1283 降到个位数

# 环境套话：这些词在标题里只说明「在哪儿做」，不说明「结论是什么」。
# 为什么还需要这一层：df 闸挡不住「同一台机器」的套话 —— 「在 Windows PowerShell 5.1 里用」
# 带 windows 的完整写法在库里只出现在 4 条标题里，于是 df=4 放行，把
# 「用 curl.exe 下载要加 --ssl-no-revoke」误报成与「用 Select-String 搜中文 pattern 不可靠」重复。
# 判据：把匹配段里的套话剥掉后，剩下的「结论性内容」必须 ≥8 字（实测两类样本的分界）。
_BOILER_RE = re.compile(
    r"(?i)(windows\s*)?powershell|pwsh|5\.1|python\s*3|node\s*v?\d+|"
    r"本机|仅当|当在|当你要|在|里用|下|时|的|用|里|中|要|把|给|做")


def _content_residual(match: str) -> str:
    """剥掉环境套话后剩下的「结论性原文」。"""
    return _BOILER_RE.sub("", match)


def _norm_title(t: str) -> str:
    """去标点/空白并小写：判重只看字面内容，标点差异不是差异。"""
    return _DUP_PUNC_RE.sub("", t or "").lower()


def _lcs_span(a: str, b: str) -> tuple[int, int, int]:
    """最长公共子串 -> (长度, a 里的起点, b 里的起点)。O(len(a)*len(b))。"""
    if not a or not b:
        return 0, 0, 0
    prev = [0] * (len(b) + 1)
    best_len, best_a, best_b = 0, 0, 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                n = prev[j - 1] + 1
                cur[j] = n
                if n > best_len:
                    best_len, best_a, best_b = n, i - n, j - n
        prev = cur
    return best_len, best_a, best_b


def near_duplicates(title: str, base: Optional[Path] = None,
                    limit: int = 3) -> list[dict[str, str]]:
    """找出与新标题「疑似同一个结论」的已有条目（排除已被取代的）。

    返回 [{kind, title, date, match}]，`match` 是双方共享的那段连续原文。
    """
    d = learnings_dir_for(base)
    new_n = _norm_title(title)
    if len(new_n) < _NEAR_DUP_MIN_LCS:
        return []
    grams = {new_n[i:i + _NEAR_DUP_GRAM]
             for i in range(len(new_n) - _NEAR_DUP_GRAM + 1)}
    if not grams:
        return []
    hits: list[tuple[int, dict[str, str], str]] = []
    pool: list[tuple[str, dict[str, str], str]] = []
    for kind, fname in (("best_practice", "BEST_PRACTICES.md"),
                        ("error", "ERRORS.md"), ("learn", "LEARNINGS.md")):
        for e in _read_entries(d / fname):
            if not e.get("superseded_by"):
                pool.append((kind, e, _norm_title(e["title"])))
    for kind, e, other in pool:
        if other == new_n:
            continue     # 逐字重复由 add_entry 的 duplicate 分支处理，这里不重复报
        if len(other) < _NEAR_DUP_MIN_LCS:
            continue
        if not grams & {other[i:i + _NEAR_DUP_GRAM]
                        for i in range(len(other) - _NEAR_DUP_GRAM + 1)}:
            continue
        L, pa, pb = _lcs_span(new_n, other)
        if L < _NEAR_DUP_MIN_LCS:
            continue
        shorter = min(len(new_n), len(other))
        prefix = (pa == 0 and pb == 0)
        suffix = (pa + L == len(new_n) and pb + L == len(other))
        if (prefix or suffix) and L < 0.8 * shorter:
            continue    # 共享的只是套话前提，不是同一个结论
        match = other[pb:pb + L]
        # 「长」还不够：「长且稀有」才是重复。实测主题名（package.json / node_modules /
        # 在FinalEngine）也能凑满 12-19 字，但它们出现在几十条里 —— df 闸把它们挡掉。
        if sum(1 for _k, _e, s in pool if match in s) > _NEAR_DUP_MAX_DF:
            continue
        if len(_content_residual(match)) < 8:
            continue    # 共享的只是环境套话（「在 Windows PowerShell 5.1 里用 …」）
        hits.append((L, {"kind": kind, "title": e["title"], "date": e["date"],
                         "match": match}, new_n[pa:pa + L]))
    hits.sort(key=lambda x: -x[0])
    return [h[1] for h in hits[:limit]]


def find_duplicate_pairs(min_lcs: int = 14, max_df: int = 4, limit: int = 40,
                         base: Optional[Path] = None) -> list[dict[str, Any]]:
    """维护用：全库扫「疑似同一结论」的成对候选（不写库，只报）。

    与 add 时的 near_duplicates 同一判据，只是把门槛放低到 14 字 —— 批量维护时
    人/模型会**逐条看**，所以可以多给候选（这正是 `dups` 命令 vs 提醒的区别：
    提醒是**无请求时插话**，必须高精度；命令是**请求时给料**，宁多勿漏）。
    """
    d = learnings_dir_for(base)
    pool: list[tuple[str, dict[str, str], str]] = []
    for kind, fname in (("best_practice", "BEST_PRACTICES.md"),
                        ("error", "ERRORS.md"), ("learn", "LEARNINGS.md")):
        for e in _read_entries(d / fname):
            if not e.get("superseded_by"):
                pool.append((kind, e, _norm_title(e["title"])))
    grams: dict[str, list[int]] = {}
    for i, (_k, _e, s) in enumerate(pool):
        for p in range(len(s) - _NEAR_DUP_GRAM + 1):
            grams.setdefault(s[p:p + _NEAR_DUP_GRAM], []).append(i)
    out: list[dict[str, Any]] = []
    for i, (ka, ea, a) in enumerate(pool):
        cand: set[int] = set()
        for p in range(len(a) - _NEAR_DUP_GRAM + 1):
            ids = grams.get(a[p:p + _NEAR_DUP_GRAM], [])
            if len(ids) <= 40:
                cand.update(ids)
        for j in cand:
            if j <= i:
                continue
            kb, eb, b = pool[j]
            L, pa, pb = _lcs_span(a, b)
            if L < min_lcs:
                continue
            shorter = min(len(a), len(b))
            prefix = (pa == 0 and pb == 0)
            suffix = (pa + L == len(a) and pb + L == len(b))
            if (prefix or suffix) and L < 0.8 * shorter:
                continue
            match = b[pb:pb + L]
            if sum(1 for _k, _e, s in pool if match in s) > max_df:
                continue
            if len(_content_residual(match)) < 8:
                continue
            out.append({"lcs": L, "match": match,
                        "a": {"kind": ka, "date": ea["date"], "title": ea["title"]},
                        "b": {"kind": kb, "date": eb["date"], "title": eb["title"]}})
    out.sort(key=lambda x: -x["lcs"])
    return out[:limit]


def title_needs_condition(title: str, body: str) -> str:
    """检查限定条件是否写进了标题；返回给作者看的提醒（空串 = 没问题）。

    只在**记录的那一刻**提醒 —— 那时信息还在作者脑子里，改标题的成本近乎零；
    等下次会话再发现，作者已经不知道当时的前提是什么了。
    """
    # 标题里**已经有任何一个**限定词，就认为作者处理过条件了。
    #
    # 为什么不做「逐词比对」（初版就是这么写的，实测误报）：同一个条件有两种
    # 写法 —— 标题写「PS 5.1」、正文写「Windows PowerShell 5.1」—— 逐词比对
    # 会把这种当成漏写。而误报的代价是**狼来了**：提醒天天响，记录时就不看了，
    # 这条检查的全部价值就在于「被看见」。宁可漏报，不可误报。
    if title_has_condition(title):
        return ""
    head = body[:300]
    for marker in TITLE_CONDITION_MARKERS:
        if marker in head:
            return (
                "⚠️ 正文前 300 字里出现限定词「%s」，但**标题里没有任何限定词**。\n"
                "   注入片段只取「标题 + 正文首行」，条件落在正文里就等于没写 ——\n"
                "   这条会被当成**无条件通则**使用（实测：注入只能露出正文中位 27%%）。\n"
                "   建议把条件补进标题，例如「…（仅 Windows PowerShell 5.1）」。\n"
                "   ★ 判定**只认**下面这些词与结构（表外写法即使表达了条件也不算）：\n"
                "     词：%s\n"
                "     结构：%s\n"
                "   （已照常入库，此提醒不影响结果。）"
                % (marker, " / ".join(TITLE_CONDITION_MARKERS),
                   " 或 ".join(TITLE_CONDITION_PATTERNS))
            )
    return ""


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
    # 准重复检查：逐字相同挡不住的「换个说法记同一结论」。
    # 先取候选**再**追加 —— 提示里的候选不该包含刚写进去的这条自己。
    near = near_duplicates(title, base)
    _append_entry(fp, title, body)
    res = {"ok": True, "file": str(fp), "title": title,
           "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    if near:
        res["dup_hint"] = near
    hint = title_needs_condition(title, body)
    if hint:
        res["hint"] = hint
    return res


# ---------------------------------------------------------------------------
# 相关性排序 (2026-09-27 新增)
# ---------------------------------------------------------------------------
#
# 动机: 会话开场注入原先取 `entries[-6:]` (纯时间切片), 有**近因偏差** ——
# 一条三个月前的高价值经验会被新噪音挤出上下文。而 agent/created 时刻
# 还没有用户消息, 拿不到查询词, 所以**单靠开场注入无解**。
# 解法是两段式: 开场先注入基线, 首轮 pre-step 拿到用户消息后再按相关性补注入。
#
# 为什么不直接用 text_contains 当排序键: 它是 boolean (精确子串 / bigram>=0.6),
# 只能回答"命中与否", 无法区分"命中 8 个词"和"命中 1 个词"。排序需要**连续分数**,
# 故这里另建 idf 加权的打分器; text_contains 保留原语义, 作为兜底判定不变。

# 中文/英文切词: 英文按词, 中文按 bigram (无分词器可用时的稳妥做法)。
# 中英混排的标题很常见 ("build 单文件" / "asar 路径 Test-Path 假阴性")。
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]{1,}|[0-9]+(?:\.[0-9]+)*|[\u4e00-\u9fff]")
# 2026-10-10 新增: 只取英文词/数字。中文另走「逐段 bigram」分支 (见 tokenize)。
_ENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]{1,}|[0-9]+(?:\.[0-9]+)*")
# 噪声词: 在几乎所有经验里都出现, 对区分度没有贡献, 反而拉平排序。
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "not", "are", "was",
    "实测", "经验", "问题", "必须", "不要", "一个", "这个", "可以", "就是",
    "需要", "已经", "什么", "还是", "因为", "所以", "但是", "如果", "没有",
}
# 2026-10-10 新增: **多字**停用词/套话，在拼 bigram *之前*从中文串里删掉。
# 为什么单靠 _STOPWORDS 不够（实测）: 那张表的过滤发生在 bigram 合成**之后**,
# 而合成产物是 "个经" / "验的" 这种跨词片段，永远匹配不上表里的「经验」「问题」
# —— 于是它实际上基本失效。先删多字词、再在剩下的连续片段上拼 bigram，
# 才真能挡住，同时顺带消掉跨标点/跨句拼出来的垃圾 token。
_STOP_CHUNKS = (
    "这个", "那个", "经验", "问题", "必须", "不要", "一个", "可以", "就是",
    "需要", "已经", "什么", "还是", "因为", "所以", "但是", "如果", "没有",
    "我们", "他们", "这样", "那样", "时候", "情况", "方法", "地方", "自己",
    "以后", "之前", "之后", "现在",
)


# ---------------------------------------------------------------------------
# 已证伪、**不要再加回来**：丢弃「含功能字的 bigram」（_FUNCTION_CHARS 方案，2026-10-16）
# ---------------------------------------------------------------------------
# 起因：查询「zzz 不存在的词组 qqq」在 rank 注入档拿到 16 条、单条 22.6 分（假阳性）。
# 当时的修法是「中文 bigram 只要含一个功能字就丢弃」，标定读数（20 真/10 假查询）看着很好：
#   假查询误报 3/10 -> 0/10，真查询零召回只从 4/20 变 5/20。
#
# ★ 但换成**真实措辞**（12 个自然句子任务，如「会话打不开了 怎么修」「把缓存从 C 盘迁到 F 盘」）
#   复验后，结论反转：
#     v4（不丢） 真任务查不到 4/12
#     v5（丢）   真任务查不到 5/12   ← 更差
#   机制：「打不」「不开」「怎么」这类 bigram 虽然含功能字，**本身是信息词**；丢掉它们后，
#   「会话打不开了 怎么修」只剩 [会话, 话打] 两个 token，注入档要求标题命中 ≥2 个，
#   于是**库里明明有 4 条相关经验（agent.inject 漏 source.kind 会让会话永久打不开 /
#   cordis.patch.yml 改完会话被 dispose / 会话为什么不可用的解帧排查）却一条都检索不到**。
#   而它治的是「人为构造的假词查询」—— 现实中用户与模型不会那样提问，收益≈0。
#   ⇒ 用一个真实场景的漏检，去换一个几乎不会发生的假阳性，是亏的。
#
# 另外两条也被实测否掉（同一批真实任务）：
#   · 给单字母盘符造 token（c盘/f盘）：对 12 个任务**零增益**（「把缓存从 C 盘迁到 F 盘」
#     仍 0 条 —— 它的实词只有「缓存」一个能在标题里对上，够不到 H=2）；
#   · 短查询自适应把 min_title_hits 降到 1：查不到数 5/12 -> 4/12，但换回来的是**无关条目**
#     （「会话打不开了 怎么修」命中的是「注册模型工具…JSON Schema」那条）⇒ 假阴性变假阳性，不要。
# ⇒ 结论：**库里没有的查不到是对的**（如「数据库索引怎么加」「推荐科幻电影」），
#   而「会话打不开」这类漏检的根因是**查询 token 太少 + H=2**，正解是回到 v4 的 tokenize，
#   并在记录侧用「标题里写上用户会用的说法」来解决（标题权重 3.0，是唯一便宜的杠杆）。
# ---------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """把文本切成用于相关性打分的词元 (英文词 + 中文 bigram + 数字)。

    2026-10-10 两处修正（均由实测驱动，读数见 _scratch 的 audit r1/r3）:
      1. 中文 bigram **逐段**合成。原版把所有中文单字 join 成一个长串再拼，
         于是跨越标点/英文/句子的相邻单字也被拼成 bigram
         （"配置文件路径。重启之后" -> 产出 "径重" 这种跨句垃圾）。
         实测：这类 df=1 的垃圾 token 曾占全库词表 64.9%，拉低排序区分度。
      2. 多字停用词在拼 bigram **之前**删除（原版在之后，等于失效，见上）。

    2026-10-16 第三处尝试（**已回滚，勿再加回**）:
      3. 曾加过「含功能字的 bigram 一律丢弃」，在 20 真/10 假**构造查询**上看着是净改善，
         但换成 12 个**真实措辞**任务复验后是净负（真任务查不到 4/12 -> 5/12，
         「会话打不开了 怎么修」从命中 4 条相关经验变成 0 条）。
         完整判据与两条同批被否掉的修法见上方「已证伪」注释块。
    """
    tokens: list[str] = []
    for m in _ENT_RE.finditer(text):
        tok = m.group(0).lower()
        if tok not in _STOPWORDS:
            tokens.append(tok)
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        s = seg
        for w in _STOP_CHUNKS:
            s = s.replace(w, "")
        tokens.extend(s[i:i + 2] for i in range(len(s) - 1))
    return [t for t in tokens if t not in _STOPWORDS]


# ---------------------------------------------------------------------------
# 全库索引缓存 (2026-10-16 新增)
# ---------------------------------------------------------------------------
#
# 为什么必须落盘而不是 lru_cache：**CLI 与 hook 每次都起一个新 Python 进程**
# （learn.py 子进程 / render_learnings.py 子进程），进程内缓存永远命中不了。
#
# 实测（本机 2026-10-16，1077 条 / 1.57 MiB 语料）：
#   全库分词 196 ms · df 构建 41 ms · pickle 写 38 ms · **pickle 读 36 ms**（2.4 MiB）
#   ⇒ 命中路径 ≈ 40 ms；未命中 ≈ 300 ms。
#   改造前：rank_entries 每次 431 ms（解析 18 + idf 217 + 打分 173 + title 23），
#   且每条正文被 tokenize **两遍**（idf 一遍、打分一遍）。
# 落点默认在 `<经验库>/.cache/`（实体在 F 盘；⛔ 不放 C 盘），可用
# `DSH_LEARN_CACHE_DIR` 覆盖。只读三个 .md，缓存文件不会被当成经验条目。
_INDEX_VERSION = 6   # ★ 改 tokenize / _STOP_CHUNKS / 打分算法时必须 +1，
                     #   否则旧缓存沿用错口径（静默、无报错）
# v4 -> v5 (2026-10-16): 试过「丢弃含功能字的 bigram」
# v5 -> v6 (2026-10-16): **该方案经真实措辞复验后回滚**（实测依据见上方「已证伪」注释块）
_INDEX_KEEP = 3      # 缓存目录里保留最近几个索引（文件指纹一变就生成新文件）

_FILE_OF_KIND = {"learn": "LEARNINGS.md", "error": "ERRORS.md",
                 "best_practice": "BEST_PRACTICES.md"}


def _index_dir(d: Path) -> Path:
    """缓存目录**跟随实际使用的那个库**，而不是固定在技能目录下。

    为什么不固定：项目级库（`<项目>/.learnings`）和技能自带库各有各的指纹，
    混在同一目录里时，「只保留最近 3 个」的清理会互相删对方的缓存（只是性能损失，
    不影响正确性），排查时也看不出这份缓存属于哪个库。
    """
    env = os.environ.get("DSH_LEARN_CACHE_DIR")
    return Path(env) if env else (d / ".cache")


def _files_fingerprint(d: Path) -> tuple:
    """三个 .md 的 (文件名, mtime_ns, size) —— 任一变了就重建索引。"""
    out = []
    for fname in ("LEARNINGS.md", "ERRORS.md", "BEST_PRACTICES.md"):
        p = d / fname
        try:
            st = p.stat()
            out.append((fname, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((fname, None, None))
    return tuple(out)


def _prune_cache(cdir: Path, keep: int = _INDEX_KEEP) -> None:
    try:
        # ★ 2026-11-14 实测补漏：写缓存走 `idx-*.pkl.tmp<pid>` 再 os.replace，而下面的
        #   glob("idx-*.pkl") **匹配不到** `.pkl.tmp123`（模式以 .pkl 结尾）⇒ 进程被中途
        #   杀掉留下的临时文件永远不清理。实测本库 .cache 里躺着 3 个 4.9 MB 的
        #   `idx-db207d...pkl.tmp<pid>`（2026-10-16 遗留，合计 14.6 MB）。
        #   清掉前先看年龄：别的进程可能**正在写**同名 tmp，删了它的 rename 会失败
        #   （失败只是丢缓存、不影响正确性，但没必要制造）。
        import time as _time
        now = _time.time()
        for p in cdir.glob("idx-*.pkl.tmp*"):
            try:
                if now - p.stat().st_mtime > 600:   # 10 分钟：远超一次写缓存耗时
                    p.unlink()
            except OSError:
                pass
        files = []
        for p in cdir.glob("idx-*.pkl"):
            try:
                if p.stat().st_size == 0:
                    p.unlink()   # 0 字节残留：既没用，又会霸占 keep 名额
                    continue
            except OSError:
                continue
            files.append(p)
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _build_index(d: Path, fingerprint: tuple) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for k, fname in (("best_practice", "BEST_PRACTICES.md"), ("error", "ERRORS.md"),
                     ("learn", "LEARNINGS.md")):
        for e in _read_entries(d / fname):
            item = dict(e)
            item["kind"] = k
            entries.append(item)
    toks = [(frozenset(tokenize(e["title"])), frozenset(tokenize(e["body"]))) for e in entries]
    # ★ df 刻意存**两套**口径，别合成一套：
    #   df_active = 原 rank_entries 的口径（排除已被取代条目、三类全要）—— 默认路径用它，
    #               这样「改造前后的默认检索结果」才能逐条对照（否则是两件事一起变了）；
    #   df_all    = 含已被取代条目的口径 —— 仅当 include_superseded=True 或只查部分 kind 时用。
    df_active: dict[str, int] = {}
    df_all: dict[str, int] = {}
    for e, (a, b) in zip(entries, toks):
        for tok in a | b:
            df_all[tok] = df_all.get(tok, 0) + 1
            if not e.get("superseded_by"):
                df_active[tok] = df_active.get(tok, 0) + 1
    return {"version": _INDEX_VERSION, "fingerprint": fingerprint, "entries": entries,
            "toks": toks, "df_active": df_active, "df_all": df_all}


def _load_index(d: Path) -> dict[str, Any]:
    """取索引：命中缓存直接加载，否则重建并原子落盘。任何异常都不向外抛。"""
    fp = _files_fingerprint(d)
    key = hashlib.sha1(repr((_INDEX_VERSION, fp)).encode("utf-8")).hexdigest()[:20]
    cache = _index_dir(d) / ("idx-%s.pkl" % key)
    if cache.exists():
        try:
            with cache.open("rb") as f:
                blob = pickle.load(f)
            if blob.get("version") == _INDEX_VERSION and blob.get("fingerprint") == fp:
                return blob
        except Exception:
            # 坏缓存 / 0 字节残留 -> 当没有，重建。**顺手删掉**：否则它会一直占着
            # `_INDEX_KEEP` 的名额，把真正有用的那份缓存挤出去（实测踩到过一个 0 字节文件）。
            try:
                cache.unlink()
            except OSError:
                pass
    blob = _build_index(d, fp)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.name + ".tmp%d" % os.getpid())
        with tmp.open("wb") as f:
            pickle.dump(blob, f, protocol=pickle.HIGHEST_PROTOCOL)
        # 落盘前先确认不是空文件：0 字节缓存会让每个读者都吃一次异常再重建，
        # 而且会霸占 `_INDEX_KEEP` 的名额（实测踩到过）。
        if tmp.stat().st_size == 0:
            raise OSError("empty index cache")
        os.replace(tmp, cache)   # 原子替换：并发会话同时写，读者也只会看到完整文件
        _prune_cache(cache.parent)
    except Exception:
        pass   # 写不进缓存不影响正确性，只是下次还得重建
    return blob


def _score_pre(title_tokens: frozenset, body_tokens: frozenset,
               query_tokens: list[str], idf: Optional[dict[str, float]] = None) -> float:
    """打分本体（吃预分词集合）—— 全库唯一一份打分逻辑。"""
    if not query_tokens:
        return 0.0
    score = 0.0
    for tok in query_tokens:
        weight = 1.0 if idf is None else idf.get(tok, 1.0)
        if tok in title_tokens:
            score += 3.0 * weight
        elif tok in body_tokens:
            score += 1.0 * weight
    return score


def _title_hits_pre(title_tokens: frozenset, query_tokens: list[str]) -> int:
    return len({t for t in query_tokens if t in title_tokens})


def score_entry(query_tokens: list[str], entry: dict[str, str],
                idf: Optional[dict[str, float]] = None) -> float:
    """给一条经验对查询词的相关性打分 (越高越相关)。

    权重设计: **标题命中比正文命中重要得多**。经验标题本身就是一句话结论,
    正文往往是过程细节; 按任务找经验时, 标题的可读信号更强。

    ★ 2026-10-16：本函数保留原签名（外部 / 测试兼容），内部转调 `_score_pre`；
      **检索主路径不走它**（那里用预分词集合，避免每条正文分词两遍）。
    """
    return _score_pre(frozenset(tokenize(entry.get("title", ""))),
                      frozenset(tokenize(entry.get("body", ""))),
                      query_tokens, idf)


def _build_idf(all_entries: list[dict[str, str]]) -> dict[str, float]:
    """按全库条目计算 idf: 出现在越少条目里的词越有区分度。"""
    total = max(len(all_entries), 1)
    df: dict[str, int] = {}
    for e in all_entries:
        for tok in set(tokenize(e.get("title", "")) ) | set(tokenize(e.get("body", ""))):
            df[tok] = df.get(tok, 0) + 1
    # 平滑 idf; 只出现 1 次的词权重最高 (log(1+N/1))
    import math
    return {tok: math.log(1.0 + total / n) for tok, n in df.items()}


def _title_hit_count(query_tokens: list[str], entry: dict[str, str]) -> int:
    """查询里有多少个**不同** token 命中该条目的标题。

    2026-10-10 新增, 服务于「诚实报告」判据。动机（实测）: 只卡总量阈值的旧判据
    挡不住"正文里偶然撞上几个生僻 bigram" —— 6 个**负例** query（库内确无相关
    经验）在旧判据下 6/6 全部误报、平均硬塞 6.5 条。加上"标题至少命中 2 个不同
    token"后，负例平均降到 0.5 条（噪声 -92%），而 8 个正例 query 仍 8/8 有召回。
    标题命中是"这条经验讲的就是这件事"的强信号; 正文命中太容易偶然发生
    （df=1 的 token 权重最高，正文撞上一个就得 6.17 分，轻易越过 6.0 阈值）。
    """
    tt = set(tokenize(entry.get("title", "")))
    return _title_hits_pre(frozenset(tt), query_tokens)


def _search(query: str, base: Optional[Path], kinds: Optional[list[str]], limit: int,
            min_score: float, min_title_hits: int, include_superseded: bool,
            min_token_overlap: float = 0.0) -> list[dict[str, Any]]:
    """query_entries 与 rank_entries 的**唯一**检索实现。

    2026-10-16 之前这里是两套判据：query 用 char-bigram 60% 重叠（boolean），
    rank 用 idf 加权分（连续）。实测代价：同一个库、同一个问题，两个入口两个答案
    ——「C盘 缓存 安装」query 5 条 / rank 0 条；而「策略检索」「检索 慢」两个入口
    都是 0 条，**库里却有相关条目** ⇒ 逼着使用者换措辞重查，每次重查 = 一次
    完整的模型往返，这才是「检索很慢」的主要来源。现在统一成一个打分器，
    只留两档**阈值**（阈值本身有实测依据，不是随手拍的）：

      * rank（严档：min_score 由调用方给，注入侧 6.0；标题命中 ≥2）—— 用来**注入上下文**。
        实测只卡总分时，6 个「库内确无相关经验」的负例 6/6 全部误报、平均硬塞 6.5 条；
        加「标题至少命中 2 个不同 token」后负例降到平均 0.5 条，8 个正例仍全召回。
      * query（宽档：min_score 0（即 s>0）；标题命中 ≥1）—— 用来**人/模型主动查全文**。
        宁可多给几条让读者自己筛，也不要「明明库里有却说没查到」。

    df 口径：默认组合（不查已取代条目 + 三类全要）用 `df_active`，与原实现逐条等价；
    非默认组合退回 `df_all`（含已取代条目口径），差异只在 idf 权重的小数位上。

    `min_token_overlap`（2026-10-16 新增，0 = 关闭）：**查询 token 覆盖率**下限 =
    「命中该条目的 query token 数 / query token 总数」。为什么需要它：只卡分数的话，
    假查询「zzz 不存在的词组 qqq」反而拿到 **22.6 分**（比多数真查询还高）—— 因为
    「不存」「存在」这类 bigram 在库里少见（idf 高）却与语义无关，**纯 idf 阈值分不开**。
    ★ 实测标定（16 个真查询 / 8 个假查询，全库 1077 条）：
        阈值 0.0 -> 真查询全召回，但假查询 4/8 误报（「任何输入都像查到了」）
        阈值 0.3 -> 真查询 16/16 全召回，假查询降到 2/8（5 条、2 条）
        阈值 0.6 -> 假查询 0 误报，但真查询 8/16 变 0 条（回到「库里明明有却说没查到」）
      ⇒ **不存在能同时兼顾两头的阈值**（词法检索的固有不可分性），按「查不到要换词
        重查 = 多一轮模型往返」比「多读两条无关」更贵来取舍，query 档取 0.3。
      ⇒ rank 档**不加**这个过滤（默认 0.0）：它的过滤判据已另有 min_title_hits/minscore，
        且加了会让真查询零召回从 4/16 涨到 5/16，得不偿失。
    """
    d = learnings_dir_for(base)
    blob = _load_index(d)
    entries, toks = blob["entries"], blob["toks"]
    wanted = set(kinds) if kinds else {"best_practice", "error", "learn"}
    use_active = (not include_superseded) and wanted >= {"best_practice", "error", "learn"}
    df = blob["df_active"] if use_active else blob["df_all"]

    pool_idx = [i for i, e in enumerate(entries)
                if e["kind"] in wanted
                and (include_superseded or not e.get("superseded_by"))]
    total = max(len(pool_idx), 1)

    q = (query or "").strip()
    if not q:
        # 空查询 = 「全都给我」（保持原 query_entries 语义），只看时间
        rows: list[dict[str, Any]] = []
        for i in pool_idx:
            e = dict(entries[i])
            e["file"] = str(d / _FILE_OF_KIND[e["kind"]])
            rows.append(e)
        rows.sort(key=lambda x: x.get("date", ""), reverse=True)
        return rows[:limit]

    qtokens = tokenize(q)
    qset = set(qtokens)
    if not qset:
        # 非空但一个 token 都切不出来（例：query 只有「的」这种单字虚词）——
        # 旧实现这里也返回 0 条（`len(q)<2` 直接 False），保持一致，别退回「全部」。
        return []
    # idf 只对**查询用到的** token 求值：原实现每次都遍历全库建 5 万项 idf 表（217 ms），
    # 而打分只用到 query 里那几个 token。不在 df 里的 token 权重 1.0（与 `.get(tok, 1.0)` 等价）。
    idf = {t: math.log(1.0 + total / df[t]) for t in qset if t in df}

    scored = []
    for i in pool_idx:
        tt, bt = toks[i]
        s = _score_pre(tt, bt, qtokens, idf)
        if s <= min_score:
            continue
        if min_token_overlap > 0.0:
            both = tt | bt
            if len({t for t in qset if t in both}) / len(qset) < min_token_overlap:
                continue
        nt = _title_hits_pre(tt, qtokens)
        if nt < min_title_hits:
            continue
        e = dict(entries[i])
        e["file"] = str(d / _FILE_OF_KIND[e["kind"]])
        e["score"] = round(s, 3)
        e["title_hits"] = nt
        scored.append((s, e))
    # 分数降序, 日期降序
    scored.sort(key=lambda pair: (pair[0], pair[1].get("date", "")), reverse=True)
    return [e for _s, e in scored[:limit]]


def rank_entries(query: str, kinds: Optional[list[str]] = None,
                 base: Optional[Path] = None, limit: int = 6,
                 min_score: float = 0.0, min_title_hits: int = 2,
                 include_superseded: bool = False,
                 min_token_overlap: float = 0.0) -> list[dict[str, Any]]:
    """按相关性返回经验条目 (与时间无关, 供任务级注入使用)。

    与 query_entries 的关系（2026-10-16 起）: **同一个打分器，只是阈值档更严** ——
    这里是「排 + 严筛」，query_entries 是「排 + 宽筛」。见 `_search` 的档位说明。
    高分在前; 同分时新条目优先 (用日期做二级排序键)。

    默认**排除已被取代的条目** —— 否则纠正条目和它要推翻的旧说法会并排
    出现在注入里, 我可能照着旧的那条做 (见 supersede_entry)。

    `min_title_hits` (2026-10-10 新增, 默认 2): 标题至少命中几个不同 token 才
    算相关。**没有它时检索不会承认"没查到"** —— 实测负例（库内确无相关经验）
    平均被硬塞 6.5 条纯噪声, 且每条都"有分数、看起来像相关"。设 0 即退回旧行为。
    """
    return _search(query, base=base, kinds=kinds, limit=limit, min_score=min_score,
                   min_title_hits=min_title_hits, include_superseded=include_superseded,
                   min_token_overlap=min_token_overlap)


def query_entries(query: str, kind: Optional[str] = None, base: Optional[Path] = None,
                  limit: int = 10, include_superseded: bool = False,
                  min_title_hits: int = 1, min_score: float = 0.0,
                  min_token_overlap: float = 0.3) -> list[dict[str, Any]]:
    """按关键词查询经验库（宽档：相关度排序，标题至少命中 1 个词）。

    默认排除已被取代的条目（见 supersede_entry）。

    ★ 2026-10-16 判据统一：旧实现用 char-bigram 重叠 ≥0.6 的 boolean 判定，
    实测「策略检索」「检索 慢」返回 0 条而库里有相关条目；现在与 rank_entries
    共用 `_search` 打分管线，只把过滤放到宽档：
      `min_token_overlap=0.3`（查询里至少三成 token 在库里出现过，旧阈值是 0.6）
      `min_title_hits=1`（旧行为下这一条不存在）/ `min_score=0`（即分数 >0）。
    排序也从「纯时间降序」改成「相关度优先、同分看时间」—— 查全文时先给最相关的。
    ★ 三个参数都可显式覆盖；要复刻最保守的旧行为就传 `min_token_overlap=0.6,
      min_title_hits=2`。阈值的取舍依据（真/假查询实测）见 `_search` 的 docstring。
    """
    return _search(query, base=base, kinds=[kind] if kind else None, limit=limit,
                   min_score=min_score, min_title_hits=min_title_hits,
                   include_superseded=include_superseded,
                   min_token_overlap=min_token_overlap)


def review(base: Optional[Path] = None) -> dict[str, Any]:
    """复盘: 统计经验库状态, 提炼最佳实践 (返回报告, 不自动写入)."""
    d = learnings_dir_for(base)
    stats = {}
    superseded = []
    all_entries: list[dict[str, str]] = []
    for k, fname in [("learn", "LEARNINGS.md"), ("error", "ERRORS.md"), ("best_practice", "BEST_PRACTICES.md")]:
        fp = d / fname
        entries = _read_entries(fp)
        stats[k] = len(entries)
        for e in entries:
            e["kind"] = k
            if e.get("superseded_by"):
                superseded.append({"kind": k, "title": e["title"],
                                   "superseded_by": e["superseded_by"]})
            all_entries.append(e)
    return {
        "dir": str(d),
        "stats": stats,
        "total": len(all_entries),
        "superseded": superseded,
        "superseded_count": len(superseded),
        # 真正参与检索的条数 —— 「库有多大」和「有多少在起作用」是两回事
        "active": len(all_entries) - len(superseded),
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
    q.add_argument("--include-superseded", action="store_true",
                   help="连已被取代的条目一起显示 (默认隐藏)")
    # 2026-11-14: 库内早有一条经验记着「query/rank 只回摘要正文、没有 --full 开关，
    # 要核对全文只能去读 .learnings 下三个 .md」。那条记录是**绕路方案**，不是需求：
    # 核对适用条件恰恰发生在「照做之前」，此时还要人去翻 1 MB 的 ERRORS.md 才是真成本。
    q.add_argument("--full", action="store_true",
                   help="打印正文全文 (默认只截前 150 字)")
    # 2026-10-16: 宽档阈值可调 —— 覆盖默认即可复刻旧行为或调召回/精度
    q.add_argument("--min-title-hits", type=int, default=1,
                   help="标题至少命中几个词 (默认 1；旧行为=2)")
    q.add_argument("--min-overlap", type=float, default=0.3,
                   help="查询 token 覆盖率下限 (默认 0.3；旧行为=0.6，0=关闭)")
    q.add_argument("--dir", help="经验库目录")

    # 2026-09-27 新增: 相关性排序检索 (与 query 的"筛"不同, 这是"排")
    rk = sub.add_parser("rank", help="按相关性排序检索经验 (全库, 非时间切片)")
    rk.add_argument("keyword", help="任务描述 / 关键词")
    rk.add_argument("--limit", type=int, default=6)
    rk.add_argument("--min-score", type=float, default=0.0)
    rk.add_argument("--min-title-hits", type=int, default=2,
                    help="标题至少命中几个不同词才算相关 (默认 2; 0=退回旧行为)")
    rk.add_argument("--min-overlap", type=float, default=0.0,
                    help="查询 token 覆盖率下限 (默认 0=关闭; rank 档刻意不加，见 _search)")
    rk.add_argument("--json", action="store_true")
    rk.add_argument("--include-superseded", action="store_true")
    rk.add_argument("--full", action="store_true",
                    help="打印正文全文 (默认只截前 150 字)")
    rk.add_argument("--dir", help="经验库目录")

    # 2026-09-27 新增: 纠正时不删原文, 只标「已被取代」。
    # 解决「append-only 库导致纠正条目和原错误条目并排竞争」的问题。
    sp = sub.add_parser("supersede", help="把一条旧经验标记为已被取代 (不删原文)")
    sp.add_argument("--old", required=True, help="旧条目标题的**唯一子串**")
    sp.add_argument("--new", required=True, dest="new_title", help="取代它的新条目标题")
    sp.add_argument("--why", default="", help="一句话说明为什么被取代")
    sp.add_argument("--dir", help="经验库目录")

    r = sub.add_parser("review", help="复盘统计")
    r.add_argument("--json", action="store_true")
    r.add_argument("--dir", help="经验库目录")

    s = sub.add_parser("status", help="显示经验库状态")
    s.add_argument("--dir", help="经验库目录")

    # 2026-11-14 新增: 维护用 —— 全库扫「疑似同一结论」的成对候选。只报不改。
    dp = sub.add_parser("dups", help="扫全库「疑似同一结论」的成对候选 (只报，不改库)")
    dp.add_argument("--min-lcs", type=int, default=14, help="公共连续原文下限 (默认 14)")
    dp.add_argument("--max-df", type=int, default=4, help="该原文最多出现在几条里 (默认 4)")
    dp.add_argument("--limit", type=int, default=40)
    dp.add_argument("--json", action="store_true")
    dp.add_argument("--dir", help="经验库目录")

    args = p.parse_args(argv)
    base = Path(args.dir) if getattr(args, "dir", None) else None

    if args.cmd == "add":
        res = add_entry(args.kind, args.title, args.body, base=base)
        print(json.dumps(res, ensure_ascii=False))
        if res.get("dup_hint"):
            print()
            print("⚠️ 库里已有**疑似同一个结论**的条目（共享 ≥%d 字的罕见原文）："
                  % _NEAR_DUP_MIN_LCS)
            for c in res["dup_hint"]:
                print(f"   [{c['kind']}] ({c['date']}) {c['title']}")
                print(f"        共同原文：{c['match']}")
            print("   → 若确是同一结论：`learn.py supersede --old \"<旧标题唯一子串>\" "
                  "--new \"<本条标题>\" --why \"...\"`")
            print("   → 若只是同一前提下的不同结论：忽略此提醒（本条已照常入库）。")
        if res.get("hint"):
            # 单独再打一遍：JSON 里换行被转义成 \n，一大段提醒挤成一行容易被看漏，
            # 而这条提醒的全部价值就在于「记录的那一刻被看见」。
            print()
            print(res["hint"])
        return 0 if res["ok"] else 1

    if args.cmd == "query":
        rows = query_entries(args.keyword, kind=getattr(args, "kind", None), base=base,
                             include_superseded=getattr(args, "include_superseded", False),
                             min_title_hits=getattr(args, "min_title_hits", 1),
                             min_token_overlap=getattr(args, "min_overlap", 0.3))
        if getattr(args, "json", False):
            print(json.dumps(rows, ensure_ascii=False))
            return 0
        if not rows:
            print("(无匹配经验)")
            return 0
        for r in rows:
            mark = "  [已取代]" if r.get("superseded_by") else ""
            print(f"[{r['kind']}] ({r['date']}) {r['title']}{mark}")
            if r["body"]:
                if getattr(args, "full", False):
                    print("\n".join("    " + ln for ln in r["body"].splitlines()))
                else:
                    print(f"    {r['body'][:150]}")
        return 0

    if args.cmd == "rank":
        rows = rank_entries(args.keyword, base=base, limit=args.limit,
                            min_score=args.min_score,
                            min_title_hits=getattr(args, "min_title_hits", 2),
                            include_superseded=getattr(args, "include_superseded", False),
                            min_token_overlap=getattr(args, "min_overlap", 0.0))
        if getattr(args, "json", False):
            print(json.dumps(rows, ensure_ascii=False))
            return 0
        if not rows:
            print("(无相关经验)")
            return 0
        label = {"best_practice": "沉淀准则", "error": "错误教训", "learn": "正向经验"}
        for r in rows:
            mark = "  [已取代]" if r.get("superseded_by") else ""
            print("[%.1f] [%s] (%s) %s%s" % (r["score"], label.get(r["kind"], r["kind"]),
                                             r["date"][:10], r["title"], mark))
            if r["body"]:
                if getattr(args, "full", False):
                    print("\n".join("    " + ln for ln in r["body"].splitlines()))
                else:
                    print("    " + r["body"][:150].replace("\n", " "))
        return 0

    if args.cmd == "supersede":
        res = supersede_entry(args.old, args.new_title, base=base,
                              why=getattr(args, "why", ""))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "review":
        rep = review(base)
        if getattr(args, "json", False):
            print(json.dumps(rep, ensure_ascii=False))
            return 0
        print(f"经验库目录: {rep['dir']}")
        print(f"总计 {rep['total']} 条: 正向经验 {rep['stats']['learn']} / 错误教训 {rep['stats']['error']} / 沉淀准则 {rep['stats']['best_practice']}")
        # 「库有多大」与「有多少在起作用」是两回事 —— 分开报，别让总量骗人
        print(f"其中已被取代 {rep['superseded_count']} 条 -> **实际参与检索 {rep['active']} 条**")
        # 2026-11-14: 把「标题缺条件」这件事变得**可见**。机制只在 add 那一刻提醒，
        # 存量条目没有回填路径 —— 不回填可以，但不能假装不存在。
        # 判据：正文前 300 字有紧表限定词、而标题一个都没有（与 add 时的提醒同一函数）。
        need = [e for e in rep["entries"]
                if not e.get("superseded_by") and title_needs_condition(e["title"], e["body"])]
        if need:
            print(f"⚠️ 标题缺条件（正文有限定词）: {len(need)} 条 —— "
                  f"改写=新增+supersede，会让库涨一条，故只提示不自动改")
            for e in need[-3:]:
                print(f"    · {e['title'][:70]}")
        for s in rep["superseded"]:
            print(f"  [已取代] {s['title'][:70]}")
        return 0

    if args.cmd == "dups":
        rows = find_duplicate_pairs(min_lcs=args.min_lcs, max_df=args.max_df,
                                    limit=args.limit, base=base)
        if getattr(args, "json", False):
            print(json.dumps(rows, ensure_ascii=False))
            return 0
        if not rows:
            print("(没有共享 ≥%d 字罕见原文的成对候选)" % args.min_lcs)
            return 0
        for r in rows:
            print("[%d 字] %s" % (r["lcs"], r["match"]))
            print("   A [%s] (%s) %s" % (r["a"]["kind"], r["a"]["date"][:10], r["a"]["title"]))
            print("   B [%s] (%s) %s" % (r["b"]["kind"], r["b"]["date"][:10], r["b"]["title"]))
        print("\n共 %d 对。要合并就：learn.py supersede --old \"<旧标题唯一子串>\" "
              "--new \"<保留的那条标题>\" --why \"...\"" % len(rows))
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
