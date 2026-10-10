#!/usr/bin/env python3
"""跨章一致性核对（continuity check）——补上 style_audit 与 series_audit 之间的空白。

为什么需要这个脚本
------------------
原有的两层检查各管一段，中间有一段没人管：

  style_audit.py   管**一章之内**：黑名单、配额、句长、段落、重复开头
  series_audit.py  管**章与章之间**，但只读 plot/节点-*.md 的 front-matter：
                   钩子轮换、倒计时算术、爽点间隔、伏笔超期、字数
  → 两者都**不读正文文本本身**，所以下面这些长期无人负责：
     · 同一句话跨章逐字重复（作者抄自己）
     · 同一微动作在一章里刷 3 次
     · 角色档案里登记的称呼／外貌细节，正文里一次都没落地
     · 关系表规定的"从第 N 章起改称呼"，正文提前用了
     · 节点文件被压缩时丢掉必填字段
     · 卷纲状态列写了模板不认的值、或同一章出现两行

这些不是"细节洁癖"：档案登记了不用 = 档案在撒谎；跨章逐字重复 = 最典型的机器味。
真实项目上它们全部发生过，而且没有任何一层检查报警。

用法
----
  python continuity_check.py --novel novel
  python continuity_check.py --novel novel --allow novel/state/连续性白名单.md
  python continuity_check.py --novel novel --write-allowlist   # 生成白名单骨架

退出码：有"必须改"时返回 1。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------- 常量（唯一权威）
DUP_SENT_MIN_LEN = 6          # 跨章重复检测的最短句长（去标点空白后）
DUP_SENT_MAX_LEN = 26         # 最长（太长就成了段落，不叫"同一句"）
DUP_SOFT_CHAPTERS = 2         # 出现在 ≥2 个不同章 → 建议改
DUP_HARD_CHAPTERS = 3         # 出现在 ≥3 个不同章 → 必须改
DUP_ADJACENT_HARD = True      # 相邻两章逐字重复 → 必须改（读者记忆最新，最像抄自己）
MICRO_REPEAT_SUGGEST = 3      # 同一短句/微动作在一章内出现 ≥N 次 → 建议改
VOLUME_STATUS_OK = {"outline", "draft", "done", "archived"}
WORD_MIN, WORD_MAX = 6, 40     # 正文实测字数的计入口径（与 style_audit 一致：去标点空白）

NODE_REQUIRED_FM = ["chapter", "title", "hook_type", "opening_type",
                    "payoff", "emotion", "characters"]
NODE_REQUIRED_SECTIONS = [
    ("事件", ["事件"]),
    ("人物状态变化", ["人物状态变化"]),
    ("结尾最后 200 字", ["结尾最后200字", "结尾最后200字原文", "最后200字"]),
    ("下一章必须记住的事",
     ["下一章必须记住的事", "下一章必须记住的事next_chapter_must_remember",
      "下章必须记住的事", "next_chapter_must_remember", "must_remember"]),
]

# 写前控制输入留痕（B029 缺口 d）：**写这一章之前到底加载了什么**，要有证据链。
# 只在"这一节存在"时查内容；缺这一节时**只提醒最新一章**（旧章不追改，避免刷屏）。
PREWRITE_SECTION_ALIASES = ["写前加载", "写前必读", "写前加载（留痕）", "写前加载(留痕)"]
PREWRITE_EXPECT_ANY = ["总纲", "创作宪法", "卷纲", "上一章", "节点-第", "主角",
                       "角色-", "伏笔表", "风格档案", "世界观"]

# 常见微动作观察词表。故意做成常量，便于按本书校准后追加（不要改判断逻辑）。
MICRO_BEATS = [
    "看了我一眼", "看了他一眼", "低头", "抬起头", "没答", "没说话", "我没接话",
    "笑了一下", "顿了一下", "静了一下", "皱了下眉", "叹了口气", "摇了摇头",
    "点了下头", "抿了抿嘴", "咽了口口水", "把手在裤腿上擦", "把烟",
    "看了一眼", "转过身", "停了一下", "想了两秒",
]

# 有些观察词是别的词的前缀，直接 count 会假计数（"没答" 命中 "没答应/没答复/没答话"）。
# 这里给这类词加后视排除字符集，避免把无关的词算进来。
MICRO_BEAT_GUARD = {
    "没答": "应复话腔声",
}

PUNCT = "，。！？；：、—…“”‘’「」（）《》 \t\u3000\"'"
# 逐字符判断用集合版（O(1)）：strip_norm 是对**每一句**调用的最热函数，
# 字符串版 `ch not in PUNCT` 每次线性扫 27 个字符（B032 压测）。原字符串保留给其它用途。
PUNCT_SET = frozenset(PUNCT)


def strip_norm(s: str) -> str:
    """归一化：去掉标点与空白，用于跨章逐字比较。"""
    return "".join(ch for ch in s if ch not in PUNCT_SET)


def content_lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]


# ---------------------------------------------------------------- 读取

def iter_chapter_files(chapters_dir: Path):
    """产出 (章号, 路径)。章号只从文件名解析。**递归**，兼容按卷分子目录
    （series_audit.count_chapter_chars 也是 rglob，两边口径必须一致）。"""
    for f in sorted(chapters_dir.rglob("*.md")):
        m = re.search(r"第(\d+)章", f.name)
        if m:
            yield int(m.group(1)), f


def iter_node_files(plot_dir: Path):
    """产出 (章号, 路径)。章号规则与 load_nodes 完全一致，供重复检测复用。"""
    for f in sorted(plot_dir.rglob("节点-*.md")):
        m = re.search(r"第0*(\d+)章", f.name)
        if m:
            yield int(m.group(1)), f


def load_chapters(chapters_dir: Path) -> dict[int, tuple[str, str]]:
    return {ch: (f.name, f.read_text(encoding="utf-8-sig", errors="replace"))
            for ch, f in iter_chapter_files(chapters_dir)}


def load_nodes(plot_dir: Path) -> dict[int, tuple[str, str]]:
    return {ch: (f.name, f.read_text(encoding="utf-8-sig", errors="replace"))
            for ch, f in iter_node_files(plot_dir)}


def load_allowlist(path: Path | None) -> set[str]:
    """白名单：一行一条，`#` 起注释。

    **必须跳过两种行**（B028 修的）：`#` 开头的注释行、`>` 开头的 markdown 引用行。
    之前只按"`#` 之后是注释"去做字符串切割，于是引用行（里面常提到反引号包着的 `#`）
    也被当成条目加载 —— 真实项目里 2 条真条目被读成"6 条"，多出来的全是说明文字。
    """
    if not path or not path.is_file():
        return set()
    out = set()
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        s = s.split("#", 1)[0].strip().strip("-* ")
        if s:
            out.add(strip_norm(s))
    return out


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"[。！？\n]", text) if s.strip()]


# ---------------------------------------------------------------- 检查 0：文件清单
# 章号重复会让"按章号"的核对失真（一个号被静默覆盖，另一个号被当成两章算）；
# 文件名里没章号的文件则被整份漏掉。两者都不出声，所以单列一项先报。

def check_file_inventory(root: Path):
    hard: list[str] = []
    soft: list[str] = []
    for label, d, it, pat, keep in (
        ("章节", root / "chapters", iter_chapter_files, "*.md", r"第(\d+)章"),
        ("节点", root / "plot", iter_node_files, "节点-*.md", r"第0*(\d+)章"),
    ):
        if not d.is_dir():
            continue
        seen: dict[int, list[str]] = defaultdict(list)
        for ch, f in it(d):
            seen[ch].append(f.name)
        for ch, names in sorted(seen.items()):
            if len(names) > 1:
                hard.append(
                    f"{label}章号重复：第 {ch} 章有 {len(names)} 个文件（{'、'.join(names)}）"
                    f" → 删掉多余的；否则有的检查会静默覆盖、有的会把两章当成一章算")
        skipped = [
            f.name for f in sorted(d.rglob(pat))
            if not re.search(keep, f.name)
            and not f.name.lower().startswith(("readme", "_", "."))
        ]
        if skipped:
            soft.append(
                f"{label}文件名里没有章号，已跳过核对：{'、'.join(skipped[:5])}"
                + ("…" if len(skipped) > 5 else "") + "　（约定写法：第12章-标题.md）")
    return hard, soft


# ---------------------------------------------------------------- 检查 1：跨章原句重复

def check_cross_chapter(sents_by_ch: dict[int, list[str]], allow: set[str]):
    where: dict[str, list[int]] = defaultdict(list)
    for ch, sents in sents_by_ch.items():
        for raw in sents:
            s = strip_norm(raw)
            n = len(s)
            if DUP_SENT_MIN_LEN <= n <= DUP_SENT_MAX_LEN:
                where[s].append(ch)

    hard, soft = [], []
    for s, chs in where.items():
        uniq = sorted(set(chs))
        if len(uniq) < DUP_SOFT_CHAPTERS or s in allow:
            continue
        adj = any(uniq[i + 1] - uniq[i] == 1 for i in range(len(uniq) - 1))
        tag = "、".join(f"第{c}章" for c in uniq)
        msg = f"跨章逐字重复：「{s}」出现在 {tag}"
        if len(uniq) >= DUP_HARD_CHAPTERS or (DUP_ADJACENT_HARD and adj):
            hard.append(msg + ("（含相邻章）" if adj else "") +
                        "　→ 改掉其中一处，或改写措辞；确属刻意回声就写进白名单")
        else:
            soft.append(msg + "　→ 非相邻，可保留；若是无心重复就改掉")
    return hard, soft


# ---------------------------------------------------------------- 检查 2：章内微动作重复

def check_micro_repeat(chapters: dict[int, tuple[str, str]]):
    soft = []
    for ch in sorted(chapters):
        t = chapters[ch][1]
        # 2a 逐字重复的短句
        c = Counter(strip_norm(s) for s in split_sentences(t))
        for s, n in c.items():
            if n >= MICRO_REPEAT_SUGGEST and 4 <= len(s) <= 16:
                soft.append(f"第{ch}章内「{s}」逐字出现 {n} 次 → 至少换掉一处措辞")
        # 2b 观察词表里的微动作（带后视排除，避免"没答"命中"没答应"）
        for w in MICRO_BEATS:
            guard = MICRO_BEAT_GUARD.get(w, "")
            pat = re.escape(w) + ("(?![%s])" % guard if guard else "")
            n = len(re.findall(pat, t))
            if n >= MICRO_REPEAT_SUGGEST:
                soft.append(f"第{ch}章内微动作「{w}」{n} 次 → 换一种写法，别复刻同一句")
    return [], soft


# ---------------------------------------------------------------- 检查 3：称呼契约

def check_address_contract(root: Path, chapters: dict[int, tuple[str, str]]):
    """关系表「称呼对照」写的是"从第几章开始"，把它当契约核对正文首次出现。

    启发式，只报建议改：脚本分不清"叙述里的简称"和"对白里的称呼"。
    第一人称作品里，叙述提前用简称是常态（该改的是关系表），所以这里只提示
    "档案和正文对不上"，由人来裁决改哪边。
    角色本名（如主角全名）一律跳过——本名在叙述里出现不算"改了口"。
    """
    hard, soft = [], []
    p = root / "world" / "关系表.md"
    if not p.is_file():
        return hard, ["找不到 world/关系表.md，称呼契约未核对"]
    text = p.read_text(encoding="utf-8-sig", errors="replace")

    # 角色本名集合，用来排除"本名出现在叙述里"的误报
    fullnames = set()
    cdir = root / "characters"
    if cdir.is_dir():
        for f in cdir.glob("*.md"):
            fullnames.add(re.sub(r"^(主角|角色)-", "", f.stem))

    sec = re.search(r"##\s*称呼对照(.*?)(?=\n##|\Z)", text, re.S)
    if not sec:
        return hard, ["关系表里没有「称呼对照」一节，称呼契约未核对"]
    rows = 0
    for line in sec.group(1).splitlines():
        if not line.strip().startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or cells[2] in ("从第几章开始", "---"):
            continue
        names = re.findall(r"[「“\"']([^」”\"']{1,6})[」”\"']", cells[1])
        chs = re.findall(r"第\s*(\d+)\s*章", cells[2])
        if not names or not chs:
            continue
        rows += 1
        start = int(chs[0])
        # 「叙述」标记 = 档案自己声明"这一条在叙述里会提前用，不算违约"。
        # 第一人称作品里叙述提前用简称是常态，写清标记就不该再报。
        if any("叙述" in c for c in cells):
            continue
        for nm in names:
            if nm in fullnames:          # 本名不算改口
                continue
            first = next((c for c in sorted(chapters) if nm in chapters[c][1]), None)
            if first is not None and first < start:
                soft.append(
                    f"称呼契约对不上：关系表规定「{nm}」从第{start}章起，"
                    f"但正文第{first}章就出现了　→ 要么改关系表、要么改正文。"
                    f"第一人称的叙述里提前用简称是常态，多数情况该改的是关系表"
                )
    if rows == 0:
        soft.append("称呼对照表没解析出任何一行（「叫什么」列可能没加引号），未核对")
    return hard, soft


# ---------------------------------------------------------------- 检查 4：档案细节落地

def check_appearance_landed(root: Path, chapters: dict[int, tuple[str, str]]):
    """角色档案的 appearance 块登记的关键细节，全篇一次都没出现 → 档案在撒谎。"""
    hard, soft = [], []
    cdir = root / "characters"
    if not cdir.is_dir():
        return hard, ["找不到 characters/ 目录，档案落地未核对"]
    blob = "\n".join(chapters[c][1] for c in chapters)
    found_block = 0
    for f in sorted(cdir.glob("*.md")):
        text = f.read_text(encoding="utf-8-sig", errors="replace")
        m = re.search(r"<!--\s*appearance:start\s*-->(.*?)<!--\s*appearance:end\s*-->",
                      text, re.S)
        who = re.sub(r"^(主角|角色)-", "", f.stem)
        if not m:
            soft.append(f"「{who}」档案没有 appearance 块，外貌细节无法逐条核对"
                        f"（见 references/character-template.md）")
            continue
        found_block += 1
        for item in [x.strip() for x in re.split(r"[|｜]", m.group(1)) if x.strip()]:
            if blob.count(item) == 0:
                hard.append(f"档案在撒谎：「{who}」登记了外貌细节「{item}」，"
                            f"但全篇正文一次都没出现　→ 要么写进正文，要么从档案删掉")
    if found_block == 0 and not soft:
        soft.append("没有任何角色档案带 appearance 块，外貌细节未核对")
    return hard, soft


# ---------------------------------------------------------------- 检查 5：节点字段完整度

def bare_line(line: str) -> str:
    """去掉 markdown 装饰与空白，便于按字段名做前缀匹配（全角空格也算）。"""
    return re.sub(r"[*`（）()【】\[\]：:　\s]", "", line)


def check_nodes(nodes: dict[int, tuple[str, str]]):
    hard, soft = [], []
    if not nodes:
        return ["找不到任何 plot/节点-*.md"], []
    for ch in sorted(nodes):
        name, t = nodes[ch]
        fm = re.match(r"^---\n(.*?)\n---", t, re.S)
        if not fm:
            hard.append(f"{name}：缺少 front-matter（series_audit 会直接漏读这一章）")
        else:
            keys = {l.split(":", 1)[0].strip() for l in fm.group(1).splitlines() if ":" in l}
            miss = [k for k in NODE_REQUIRED_FM if k not in keys]
            if miss:
                hard.append(f"{name}：front-matter 缺字段 {miss}")
        lines = t.splitlines()
        for label, aliases in NODE_REQUIRED_SECTIONS:
            idx = -1
            for i, line in enumerate(lines):
                b = bare_line(line).lstrip("-").replace("**", "")
                if any(b.startswith(a) for a in aliases):
                    idx = i
                    break
            if idx < 0:
                hard.append(f"{name}：缺「{label}」段（写节点时被压缩掉了，"
                            f"或字段名写得和模板不一致）")
                continue
            rest = "\n".join(x for x in lines[idx + 1:idx + 5] if x.strip()).strip()
            if not bare_line(rest):
                hard.append(f"{name}：「{label}」是空的（模板字段不许留空）")
    return hard, soft


# ---------------------------------------------------------------- 检查 6：卷纲状态列

def check_prewrite_trace(nodes: dict[int, tuple[str, str]]):
    """写前控制输入留痕（B029 缺口 d）。

    SKILL.md 规定了写前必读 5 项，但**没有任何产物记录"这次到底读了什么"**——
    事后无法审计，也无法回答"这章为什么写歪了"。
    这一节就是那张收据：① 读了哪些文件 ② 带进了哪些硬约束。
    """
    hard: list[str] = []
    soft: list[str] = []
    if not nodes:
        return hard, soft
    present: dict[int, bool] = {}
    for ch in sorted(nodes):
        name, t = nodes[ch]
        lines = t.splitlines()
        idx = -1
        for i, line in enumerate(lines):
            # 注意：bare_line 不去 `#`，所以这里要 lstrip("-#")——
            # 这一节可能写成标题（## 写前加载）也可能写成字段行（- 写前加载：…）
            b = bare_line(line).lstrip("-#").replace("**", "")
            if any(b.startswith(a) for a in PREWRITE_SECTION_ALIASES):
                idx = i
                break
        if idx < 0:
            present[ch] = False
            continue
        present[ch] = True
        body: list[str] = []
        for line in lines[idx + 1:]:
            if line.lstrip().startswith("#"):
                break
            s = line.strip().strip("-*· ")
            if s:
                body.append(s)
        if len(body) < 2:
            soft.append(
                f"{name}：「写前加载」留痕太空 —— 至少要写清①读了哪些文件 ②带进了哪些硬约束"
            )
            continue
        joined = "".join(body)
        if not any(k in joined for k in PREWRITE_EXPECT_ANY):
            soft.append(
                f"{name}：「写前加载」没提到任何必读项（总纲/创作宪法、卷纲、上一章节点、"
                "主角档案、风格档案）—— 留痕的意义就是能核对「读了没读」"
            )
    latest = max(nodes)
    if not present.get(latest, False):
        soft.append(
            f"第 {latest} 章（最新一章）的节点没有「写前加载」段：**新章请按模板补上**"
            f"（旧章不追改）—— 它记录「这次读了哪些文件、带了哪些硬约束」，"
            "是唯一能事后审计上下文的证据"
        )
    return hard, soft


def check_volume_outline(root: Path):
    hard, soft = [], []
    odir = root / "outline"
    files = sorted(odir.glob("卷纲-*.md")) if odir.is_dir() else []
    if not files:
        return hard, ["找不到 outline/卷纲-*.md，卷纲未核对"]
    for f in files:
        text = f.read_text(encoding="utf-8-sig", errors="replace")
        # 只解析「本卷章表」一节。卷节拍表/节奏配额表的列结构不同，
        # 一起解析会把它们的数值列当成状态列，报出一堆假错误。
        sec = re.search(r"##\s*本卷章表(.*?)(?=\n##|\Z)", text, re.S)
        if not sec:
            soft.append(f"{f.name}：找不到「本卷章表」一节，状态列未核对")
            continue
        base = text[:sec.start(1)].count("\n") + 1
        seen: dict[str, list[int]] = defaultdict(list)
        for i, line in enumerate(sec.group(1).splitlines()):
            lineno = base + i
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 4:
                continue
            if not re.fullmatch(r"[\d\-]+", cells[0]):
                continue
            status = cells[-1]
            seen[cells[0]].append(lineno)
            if status not in VOLUME_STATUS_OK:
                hard.append(
                    f"{f.name} 第{lineno}行：状态列写了「{status}」，不在允许取值 "
                    f"{sorted(VOLUME_STATUS_OK)} 内　→ 只能填这四个词之一，"
                    f"不要带括号注释（按状态检索的脚本会直接漏读这一行）"
                )
        for chs, lines in seen.items():
            if len(lines) > 1:
                hard.append(f"{f.name}：章号「{chs}」出现 {len(lines)} 行"
                            f"（第 {lines} 行）　→ 旧行没删干净，删掉多余的那行")
    return hard, soft


# ---------------------------------------------------------------- 主流程

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跨章一致性核对")
    ap.add_argument("--novel", default="novel", help="novel/ 目录，默认 ./novel")
    ap.add_argument("--allow", default=None, help="刻意回声白名单文件（一行一条）")
    ap.add_argument("--write-allowlist", action="store_true",
                    help="只生成白名单骨架，不检查")
    ap.add_argument("--force", action="store_true",
                    help="配合 --write-allowlist：覆盖已有白名单（默认拒绝，防止毁掉作者的豁免记录）")
    args = ap.parse_args(argv)

    root = Path(args.novel)
    chapters = load_chapters(root / "chapters")
    nodes = load_nodes(root / "plot")

    if args.write_allowlist:
        out = root / "state" / "连续性白名单.md"
        # 绝不允许默认覆盖已有白名单：那是"作者的豁免记录"，覆盖了就得靠 git 才救得回来。
        # （B028：这条护栏是踩出来的——一次测试就把真实项目的白名单骨架化了，靠 git 恢复。）
        if out.is_file() and not args.force:
            existing = load_allowlist(out)
            if existing:
                print(f"拒绝覆盖：{out} 里已有 {len(existing)} 条白名单。")
                print("  要重建就先备份/删除它，或者明确加 --force。")
                print("  提醒：白名单是**豁免刻意回声**用的，不是消音器——"
                      "无心重复要改正文，别往这里塞。")
                return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时文件再 os.replace（写坏会让之后的跨章检查全部失准）
        _tmp = out.with_name(out.name + ".tmp")
        _tmp.write_text(
            "# 连续性白名单（刻意回声）\n\n"
            "> 一行一条，写**归一化后的句子**（去掉标点与空白）。`#` 起注释。\n"
            "> 只放「读者能认出是刻意复现」的句子，例如主角的口头仪式、反复出现的核心台词。\n"
            "> 别拿它当消音器：无心重复要改正文，不是加白名单。\n\n"
            "# 示例：示例核心台词\n",
            encoding="utf-8",
        )
        os.replace(_tmp, out)
        print(f"已写出白名单骨架：{out}")
        return 0

    allow = load_allowlist(Path(args.allow)) if args.allow else set()
    sents_by_ch = {ch: split_sentences(chapters[ch][1]) for ch in chapters}

    hard: list[str] = []
    soft: list[str] = []
    for fn, res in (
        ("文件清单", check_file_inventory(root)),
        ("跨章原句重复", check_cross_chapter(sents_by_ch, allow)),
        ("章内微动作重复", check_micro_repeat(chapters)),
        ("称呼契约", check_address_contract(root, chapters)),
        ("档案细节落地", check_appearance_landed(root, chapters)),
        ("节点字段完整度", check_nodes(nodes)),
        ("写前加载留痕", check_prewrite_trace(nodes)),
        ("卷纲状态列", check_volume_outline(root)),
    ):
        h, s = res
        hard += [f"[{fn}] {x}" for x in h]
        soft += [f"[{fn}] {x}" for x in s]

    total_words = sum(len(strip_norm("\n".join(content_lines(chapters[c][1]))))
                      for c in chapters)
    print(f"=== 跨章一致性核对（{len(chapters)} 章 / {len(nodes)} 个节点 / "
          f"{total_words} 字脚本口径）===")
    if allow:
        print(f"（白名单 {len(allow)} 条）")
    print("必须改：" + ("无" if not hard else ""))
    for x in hard:
        print("  - " + x)
    print("建议改：" + ("无" if not soft else ""))
    for x in soft:
        print("  - " + x)
    print("\n说明：这是本地结构检查，不判文笔。它只管「档案／正文／提纲三者对不对得上」。")
    return 1 if hard else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
