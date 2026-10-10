#!/usr/bin/env python3
"""跨章连载体检：钩子轮换、爽点间隔、情绪走向、支线出勤、倒计时算术、伏笔超期。

单章的文风与 AI 味由 style_audit.py 管；本脚本管"一本连载写久了会不会塌"。
它读 `novel/plot/节点-第XXXX章.md` 的 front-matter 和 `novel/foreshadowing/伏笔表.md`。

节点文件的 front-matter 契约（模板见 references/plot-node-template.md）：

    ---
    chapter: 12
    title: 雨夜收账
    date: 第3天 夜
    countdown: D-5
    hook_type: 反转
    opening_type: 对话
    payoff: 小爽点
    payoff_strength: 4
    emotion: 压抑
    strand: 主线
    characters: 陆平, 老周
    subplots: 账房线
    words: 2600
    ---

`hook_type` / `opening_type` / `payoff` / `emotion` 用词自由，但同类要用同一个词，
轮换检查才有意义。`hook_type` 与 `opening_type` 建议用下面的标准清单，用了清单外的词
脚本会提示（否则"反转"和"翻转"会被当成两种钩子，轮换检查静默失效）。

- hook_type：悬念 / 危机 / 反转 / 打脸预告 / 关系 / 升级 / 信息 / 倒计时 /
  动作中断 / 信息差 / 情绪反转 / 身份谜团
- opening_type：对话 / 动作 / 声音 / 环境 / 内心 / 悬念 / 物件 / 群像
- payoff_strength：**1-7 量纲**，≥4 才算大兑现（每 10 章要有一个）
- words：可填可不填，`--target-words` 门禁优先用 `--chapters-dir` 下的正文实测字数，
  读不到正文才退回这里自报的数字

用法:
  python series_audit.py --plot novel/plot
  python series_audit.py --plot novel/plot --foreshadowing novel/foreshadowing/伏笔表.md
  python series_audit.py --plot novel/plot --chapter 40 --target-words 2600
  python series_audit.py --plot novel/plot --strict
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

NODE_GLOB = "节点-第*章.md"
FIELD_RE = re.compile(r"^\s*([A-Za-z_]+)\s*[:：]\s*(.*?)\s*$")
NUM_RE = re.compile(r"\d+")
COUNTDOWN_RE = re.compile(r"^D?\s*([+-]?)\s*(\d+)", re.IGNORECASE)

HOOK_REPEAT_LIMIT = 3      # 连续 N 章同一种章末钩子 = 违规
OPENING_REPEAT_LIMIT = 3   # 连续 N 章同一种开头 = 违规
EMOTION_REPEAT_LIMIT = 3   # 连续 N 章同向情绪 = 违规
PAYOFF_GAP_LIMIT = 5       # 连续 N 章没有任何爽点/微兑现 = 违规
BIG_PAYOFF_WINDOW = 10     # 每 N 章至少 1 个强度 ≥4 的大兑现
BIG_PAYOFF_MIN_STRENGTH = 4
SUBLOT_GAP_LIMIT = 8       # 每条支线每 N 章至少出场 1 次
ACTIVE_FORESHADOW_LIMIT = 5
FORESHADOW_WARN_DELAY = 10
FORESHADOW_RED_DELAY = 20
FORESHADOW_CORE_TYPES = {"主线", "核心", "核心伏笔"}
# 变化轴（把铁律变成可查的东西）：每章至少推进**一条主轴 + 一条支撑轴**。
# 我们的铁律一直写着"每一章必须至少推进 剧情/人物弧光/世界观 之一"，但**从来没有任何检查**。
# 口径借鉴 InkOS 的双轴规则（主轴 1 + 支撑轴 1，七轴枚举）。
AXIS_MAIN_OK = {"剧情", "人物", "世界观"}
AXIS_SUPPORT_OK = {"知识", "关系", "地位", "危险", "资源", "自我认识"}
# 场景结果只能三选一（模板里写死的）：直接失败／成了但有代价／不但失败还更糟。
# **不许"顺利解决了"**——空着或写别的词，等于这场戏没有结果。
# 口径借鉴 InkOS 的"每场至少一条变化"（他们叫 SCENE_NO_MOVEMENT）。
SCENE_RESULT_OK = {"flat_no", "yes_but", "no_and_furthermore"}
# 值可能带反引号、也可能跟括号说明（真实节点里就有「`flat_no`（守第七条失败）」），
# 所以**从单元格里提取**枚举值，而不是要求整格完全等于它。
SCENE_RESULT_RE = re.compile(r"flat_no|yes_but|no_and_furthermore")
# 伏笔表的枚举与去重校验（B016 的同类：词表外的值会让检查静默失效）
FORESHADOW_STATUS_OK = {"已埋", "推进中", "已回收", "已废弃"}
DUP_HOOK_RATIO = 0.75   # 两条伏笔内容的相似度阈值（difflib 比值）
DUP_HOOK_SHORT = 4      # 内容短于这个长度就不判相似（判不准）
WORD_LOW_RATIO = 0.9       # 单章字数**只判下限**：低于目标 ×0.9 提示"骨架感"
# 这个比例可以按本书改：基线 JSON 顶层 `bands: {"word_low_ratio": 0.85}`（见 --baseline）。
# 不加 --baseline 时用它作默认值。**只放宽下限，不加上限**（P002：不做字数上限）。
# 依赖 front-matter 的检查靠这些字段名；**名字被改＝检查静默失效**，所以要查（B029）。
NODE_EXPECTED_FM = ("chapter", "hook_type", "opening_type", "payoff",
                    "payoff_strength", "emotion")
# 伏笔表的必要列：缺任何一列，对应检查都会静默失效（表头驱动解析的代价）
EXPECTED_FS_COLUMNS = ("编号", "内容", "类型", "埋设章", "计划回收章", "状态")
# 方向跑偏的三个机器信号（B030）。阈值**复用作者已经写在 SKILL.md 里的纪律**：
# "连续 3 章没推进主线就拉回"。所以不新发明数字。
MAIN_STRAND = "主线"
STRAND_GAP_LIMIT = 3        # 连续这么多章 strand 都不是主线 → 主线断档
PROTAGONIST_GAP_LIMIT = 3   # 连续这么多章主角不出场（characters 里没有他）
RECON_EVERY = 10            # 每这么多章做一次总纲对账并留痕
RECON_SECTION_ALIASES = ["总纲对账", "对账", "对纲", "总纲复核"]
# 节点 front-matter 的**全部合法字段**（模板里的那些）。多出来的就是未知字段。
NODE_KNOWN_FM = {"chapter", "title", "date", "span", "gap", "countdown", "hook_type",
                 "hook_strength", "opening_type", "payoff", "payoff_strength", "emotion",
                 "strand", "axes", "characters", "subplots", "words"}
# 伏笔表两张表的**全部合法列**。
FS_KNOWN_COLUMNS = {"编号", "内容", "类型", "层级", "标签", "埋设章", "计划回收章",
                    "状态", "最近推进章", "备注", "回收章", "回收方式"}
                           # 上限不判、不提示（本书不做字数上限，长章不是问题）

# countdown 值里带这个标记 = "会被人为改期的死线"（例如名单上的日期被人一次次往后推）。
# 这种死线不参与"时间不能倒流"的单调性检查，否则"延期"会被误报成"倒计时倒退"。
COUNTDOWN_RENEWABLE_MARK = "可延期"
# countdown 值里带这个标记 = "上一条死线已经结束，从这里开始是一条全新的死线"。
# 单调性检查会清空序列重新起算，避免把"新死线"误报成"时间倒流"。
COUNTDOWN_RESET_MARK = "重置"

# 标准词表：用清单外的词会让轮换检查失效，所以脚本会提示
HOOK_TYPES = [
    "悬念", "危机", "反转", "打脸预告", "关系", "升级", "信息", "倒计时",
    "动作中断", "信息差", "情绪反转", "身份谜团",
]
OPENING_TYPES = ["对话", "动作", "声音", "环境", "内心", "悬念", "物件", "群像"]
PAYOFF_STRENGTH_MAX = 7
PUNCT = "，。！？；：、—…“”‘’「」（）《》"
# 逐字符判断要 O(1)：`ch not in PUNCT`（字符串）每次都要线性扫一遍 20 个字符，
# 在"每章逐字符"的循环里就是几千万次比较（B032 的压测量出来的）。集合版给热路径用。
PUNCT_SET = frozenset(PUNCT)
# 逐字符 Python 循环很贵（455 章 × 2200 字 = 100 万次迭代，实测 3.1 秒）；
# 换成一次正则替换（C 层执行），结果相同但快得多（B032）。
PUNCT_STRIP_RE = re.compile(r"[\s" + re.escape(PUNCT) + "]")


def similar_hook(a: str, b: str) -> float:
    """两条伏笔内容的相似度 0-1，用来识别"同一条承诺开了两个 hook"（改名也算）。

    为什么不用"共享 n 字片段"：「樟木箱里的照片」vs「樟木箱里的旧照片」只共享 5 个字，
    取 n=6 会漏、取 n=5 又容易在无关条目上误报。比值对"插字/改名"更稳。
    """
    a = re.sub(r"\s", "", a or "")
    b = re.sub(r"\s", "", b or "")
    if min(len(a), len(b)) < DUP_HOOK_SHORT:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def parse_scene_cards(text: str) -> list[dict]:
    """解析节点里的「场景卡」表（表头驱动，口径同 `parse_foreshadowing`）。

    为什么单查这一列：**一场戏有没有"变化"，就写在结果类型里**。
    低于字数下限时正确的追问是"哪场戏没有结果"，而不是"往哪儿加句子"（见 P004）。
    """
    m = re.search(r"^#{2,3}\s*场景卡[^\n]*\n(.*?)(?=\n#{2,3}\s|\Z)", text, re.S | re.M)
    if not m:
        return []
    rows: list[dict] = []
    header: list[str] | None = None
    for line in m.group(1).splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        if header is None:
            header = cells
            continue
        idx = None
        for name in ("结果类型", "结果"):
            if name in header:
                idx = header.index(name)
                break
        if idx is None or idx >= len(cells):
            continue
        rows.append({
            "no": cells[0] or "?",
            "goal": cells[1] if len(cells) > 1 else "",
            "raw": cells[idx],
            "result": (SCENE_RESULT_RE.search(cells[idx]).group(0)
                       if SCENE_RESULT_RE.search(cells[idx]) else ""),
        })
    return rows


def load_nodes(plot_dir: Path) -> list[dict]:
    nodes = []
    for path in sorted(plot_dir.glob(NODE_GLOB)):
        data: dict[str, str] = {}
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        in_fm = False
        for i, line in enumerate(lines[:60]):
            s = line.strip()
            if s == "---":
                in_fm = not in_fm
                if i > 0 and not in_fm:
                    break
                continue
            m = FIELD_RE.match(s)
            if m and (in_fm or i < 40):
                data[m.group(1).lower()] = m.group(2)
        data["_scenes"] = parse_scene_cards("\n".join(lines))
        data["_text"] = "\n".join(lines)      # 段级检查（如总纲对账留痕）要用全文
        ch = None
        if data.get("chapter"):
            nums = NUM_RE.findall(data["chapter"])
            ch = int(nums[0]) if nums else None
        if ch is None:
            nums = NUM_RE.findall(path.stem)
            ch = int(nums[-1]) if nums else None
        data["_chapter"] = ch
        data["_path"] = path.name
        nodes.append(data)
    nodes.sort(key=lambda d: (d["_chapter"] is None, d["_chapter"] or 0))
    return nodes


def parse_foreshadowing(path: Path) -> list[dict]:
    """按表头定位列，只解析带「状态」列的表（即活跃伏笔表，回收记录表会被跳过）。"""
    if not path.is_file():
        return []
    rows = []
    header: list[str] | None = None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        if cells and cells[0] in ("编号", "ID", "id"):
            header = cells if "状态" in cells else None
            continue
        if header is None:
            continue

        def col(*names: str) -> int | None:
            for n in names:
                if n in header:
                    return header.index(n)
            return None

        i_type = col("类型")
        i_planted = col("埋设章", "埋设")
        i_target = col("计划回收章", "回收章")
        i_status = col("状态")
        i_content = col("内容", "伏笔", "说明")
        i_adv = col("最近推进章", "推进章", "最近推进")
        i_notes = col("备注", "说明", "理由")
        if None in (i_planted, i_target, i_status):
            continue

        def num(idx: int | None) -> int | None:
            if idx is None or idx >= len(cells):
                return None
            found = NUM_RE.findall(cells[idx])
            return int(found[0]) if found else None

        rows.append({
            "id": cells[0],
            "content": cells[i_content] if i_content is not None and i_content < len(cells) else "",
            "type": cells[i_type] if i_type is not None and i_type < len(cells) else "",
            "planted": num(i_planted),
            "target": num(i_target),
            "last_advanced": num(i_adv),
            "notes": cells[i_notes] if i_notes is not None and i_notes < len(cells) else "",
            # status 也必须防越界：某行少写一个 `|`（`| F1` 只有一格）时，
            # 原来在这里 cells[i_status] 直接 IndexError —— 而这一层的存在意义
            # 恰恰是"把畸形表格报出来"，自己崩掉就本末倒置了（B032）。
            "status": cells[i_status] if i_status < len(cells) else "",
        })
    return rows


def streak(nodes: list[dict], field: str, limit: int, label: str, out: list[str]) -> None:
    """连续 N 章的某个字段取同一个值就报警。空值不算重复。"""
    run = 1
    for i in range(1, len(nodes)):
        cur = str(nodes[i].get(field, "")).strip()
        prev = str(nodes[i - 1].get(field, "")).strip()
        if cur and cur == prev:
            run += 1
            if run == limit:
                a = nodes[i - run + 1]["_chapter"]
                b = nodes[i]["_chapter"]
                out.append(f"{label}：第 {a}-{b} 章连续 {limit} 章都是「{cur}」，换一种")
        else:
            run = 1


def check_vocab(nodes: list[dict]) -> list[str]:
    """钩子/开头类型是否用了标准清单里的词；payoff_strength 是否在量纲内。

    ⚠️ 这个函数**只返回 soft 列表**（没有 hard）——往这里插检查时不要用 hard 变量，
    否则那条分支一旦被走到就会 NameError（B029 踩过：插了一个用 hard 的块，
    真实稿子上所有节点都缺字段时才第一次触发，脚本直接崩）。
    """
    soft: list[str] = []

    # 变化轴：铁律的可查版本。**旧章没填不算错**（只提示以后填），填错才算问题。
    missing_axes: list = []
    bad_axes: list = []
    for n in nodes:
        raw_axes = str(n.get("axes", "")).strip()
        if not raw_axes:
            missing_axes.append(n["_chapter"])
            continue
        parts = [x.strip() for x in re.split(r"[,，、;；/+ ]", raw_axes) if x.strip()]
        if not any(x in AXIS_MAIN_OK for x in parts) or not any(x in AXIS_SUPPORT_OK for x in parts):
            bad_axes.append(f"第{n['_chapter']}章「{raw_axes}」")
    if missing_axes:
        soft.append(
            f"{len(missing_axes)} 个节点没声明变化轴（front-matter 的 axes:）——"
            "铁律要求每章至少推进「剧情/人物/世界观」之一，再加一条支撑轴。"
            "新章开始填，旧章可以回填"
        )
    if bad_axes:
        soft.append(
            "变化轴写法不对（要 ≥1 条主轴 " + "/".join(sorted(AXIS_MAIN_OK))
            + " ＋ ≥1 条支撑轴 " + "/".join(sorted(AXIS_SUPPORT_OK)) + "）："
            + "、".join(bad_axes[:5])
        )

    # 场景卡：一场戏有没有"变化"就写在结果类型这一列（这一节已存在就查，不存在不吵）
    bad_scenes: list[str] = []
    for n in nodes:
        for sc in (n.get("_scenes") or []):
            if not sc["result"]:
                bad_scenes.append(
                    f"第{n['_chapter']}章 场景{sc['no']}（结果写成「{sc['raw'] or '空'}」）")
    if bad_scenes:
        soft.append(
            "场景卡的结果类型不合规（只能是 " + " / ".join(sorted(SCENE_RESULT_OK))
            + "）：" + "、".join(bad_scenes[:5])
            + "　→ 空着或写别的词＝这场戏没有结果，**那正是「章节发虚」的来源**"
        )

    bad_hook = {str(n.get("hook_type", "")).strip() for n in nodes} - set(HOOK_TYPES) - {""}
    if bad_hook:
        soft.append(
            "钩子类型不在标准清单里：" + "、".join(sorted(bad_hook))
            + f"（清单：{'/'.join(HOOK_TYPES)}）—— 不一致会让轮换检查失效"
        )
    bad_open = {str(n.get("opening_type", "")).strip() for n in nodes} - set(OPENING_TYPES) - {""}
    if bad_open:
        soft.append(
            "开头类型不在标准清单里：" + "、".join(sorted(bad_open))
            + f"（清单：{'/'.join(OPENING_TYPES)}）"
        )
    for n in nodes:
        found = NUM_RE.findall(str(n.get("payoff_strength", "")))
        if found and int(found[0]) > PAYOFF_STRENGTH_MAX:
            soft.append(
                f"第 {n['_chapter']} 章 payoff_strength={found[0]} 超出 1-{PAYOFF_STRENGTH_MAX} 量纲，"
                "「大兑现」判定会失真"
            )
    return soft


def count_chars_of(path: Path) -> int:
    """从正文文件实测字数（去空白与标点）。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    body = "\n".join(
        s for s in (line.strip() for line in text.splitlines()) if s and s[0] not in "#>|"
    )
    return len(PUNCT_STRIP_RE.sub("", body))


def index_chapters(chapters_dir: Path) -> dict[int, Path]:
    """**一次遍历**建立「章号 → 文件」索引（兼容按卷分子目录）。

    为什么需要：原来 `count_chapter_chars` 每次调用都 `rglob` 两遍整个目录，
    逐章调用就退化成 **O(章数²)** —— 455 章实测这一段要 **7.13 秒**（占全流程一半以上），
    建索引后降到 0.0x 秒。这个坑是压测脚本（`stress_test.py`）发现的，见 B032。
    """
    idx: dict[int, Path] = {}
    if not chapters_dir.is_dir():
        return idx
    for p in sorted(chapters_dir.rglob("*.md")):
        m = re.search(r"第\s*(\d+)\s*章", p.name)
        if m:
            idx.setdefault(int(m.group(1)), p)       # 同名多份时取排序后第一个
    return idx


def count_chapter_chars(chapters_dir: Path, chapter: int | None) -> int | None:
    """从正文文件实测字数（去空白与标点）；找不到文件返回 None。

    注意：这个函数每次都会重建索引，**只适合零散调用**；
    循环里逐章取字数请自己先 `index_chapters()` 再复用（否则就是 O(章数²)）。
    """
    if chapter is None or not chapters_dir.is_dir():
        return None
    p = index_chapters(chapters_dir).get(chapter)
    return count_chars_of(p) if p else None


def foreshadowing_table_info(path: Path) -> dict:
    """诊断伏笔表的解析情况——**专门用来抓"静默失效"**。

    表头驱动解析对两种坏情况都是**静默跳过**：① 列名被改；② 编码/表格格式坏了。
    结果就是伏笔检查整条死掉而没人知道（B015 的 BOM 就是这么栽的：所有伏笔检查失效）。
    返回 {exists, headers, with_status, rows, missing_columns}。
    """
    info: dict = {"exists": path.is_file(), "headers": [], "with_status": 0,
                  "rows": 0, "missing_columns": []}
    if not info["exists"]:
        return info
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        if cells and cells[0] in ("编号", "ID", "id"):
            info["headers"].append(cells)
            if "状态" in cells:
                info["with_status"] += 1
                info["missing_columns"] += [c for c in EXPECTED_FS_COLUMNS
                                            if c not in cells]
    info["missing_columns"] = sorted(set(info["missing_columns"]))
    info["rows"] = len(parse_foreshadowing(path))
    return info


def word_length_stats(nodes: list[dict], chapters_dir: Path, target_words: int,
                      low_ratio: float, measured: dict | None = None) -> str:
    """长度遥测：目标 / 下限 / 实测均值 / 低于下限的章号清单。

    为什么要有一行总结：单章告警只告诉你"这一章短"，看不出**趋势**
    （是全书都偏短，还是只有某几章）。
    `measured` 是可选缓存（章号 → 实测字数）：与 `check_word_floor` 共用同一个缓存，
    避免把每章正文读两遍（455 章实测：省掉约 2 秒）。
    """
    vals = []
    idx = index_chapters(chapters_dir)              # 只遍历目录一次（不是每章一次）
    for n in nodes:
        ch = n["_chapter"]
        if measured is not None and ch in measured:
            w = measured[ch]
        else:
            w = count_chars_of(idx[ch]) if ch in idx else None
            if measured is not None:
                measured[ch] = w
        if w is not None:
            vals.append((ch, w))
    if not vals:
        return ""
    lo = int(target_words * low_ratio)
    short = [ch for ch, w in vals if w < lo]
    mean = sum(w for _, w in vals) / len(vals)
    line = (f"字数遥测：实测 {len(vals)} 章｜均值 {mean:.0f} 字/章"
            f"（目标 {target_words}，下限 {low_ratio:.0%}={lo}）"
            f"｜低于下限 {len(short)} 章")
    if short:
        line += "：" + "、".join(str(c) for c in short[:12]) + ("…" if len(short) > 12 else "")
    return line


def check_word_floor(nodes: list[dict], chapters_dir: Path, target_words: int,
                     low_ratio: float = WORD_LOW_RATIO,
                     measured: dict | None = None) -> list[str]:
    """单章字数**只判下限**。超上限不判、不提示——本书不做字数上限，长章不是问题。

    实测字数仍会打印在节点清单里（见 main 的输出），供人回看，但不作为告警。
    """
    if not target_words:
        return []
    soft: list[str] = []
    lo = target_words * low_ratio
    idx = index_chapters(chapters_dir)              # 只遍历目录一次（不是每章一次）
    for n in nodes:
        ch = n["_chapter"]
        if measured is not None and ch in measured:
            w = measured[ch]
        else:
            w = count_chars_of(idx[ch]) if ch in idx else None
            if measured is not None:
                measured[ch] = w
        if w is None:
            found = NUM_RE.findall(str(n.get("words", "")))
            if not found:
                soft.append(
                    f"第 {n['_chapter']} 章：正文文件读不到、节点也没填 words，字数下限检查跳过"
                )
                continue
            w = int(found[0])
            src = "节点自报，可能不准"
        else:
            src = "正文实测"
        if w < lo:
            soft.append(
                f"第 {n['_chapter']} 章 {w} 字 < 目标 {low_ratio:.0%}（{int(lo)}，{src}），骨架感，需要展开"
            )
    return soft


def check_nodes(nodes: list[dict], bands: dict | None = None) -> tuple[list[str], list[str]]:
    hard: list[str] = []
    soft: list[str] = []
    if not nodes:
        return ["没找到任何节点文件（novel/plot/节点-第XXXX章.md）"], soft

    # 倒计时一跳允许跨几天。默认 1 天（脚本只看得见 front-matter 的数字，
    # **看不见正文里写了没写过渡**，所以它只能提醒"这一跳需要过渡"）。
    # 本书的规矩比这宽（超 3 天给一个明确时间标记即可），就在基线 JSON 的
    # `bands: {"countdown_jump_days_max": 10}` 里按本书放宽——**只放宽，不收紧**。
    jump_max = 1
    if bands:
        _j = bands.get("countdown_jump_days_max")
        if isinstance(_j, int) and _j >= 1:
            jump_max = _j

    chapters = [n["_chapter"] for n in nodes if n["_chapter"] is not None]
    if chapters:
        missing = sorted(set(range(min(chapters), max(chapters) + 1)) - set(chapters))
        if missing:
            soft.append(f"缺章节节点：{missing}")

    # 章号重复：两个节点文件声称同一章，会让钩子轮换与倒计时算术按"同号两章"计算，结论失真
    by_ch: dict[int, list[str]] = {}
    for n in nodes:
        if n["_chapter"] is not None:
            by_ch.setdefault(n["_chapter"], []).append(n["_path"])
    for ch, names in sorted(by_ch.items()):
        if len(names) > 1:
            hard.append(
                f"节点章号重复：第 {ch} 章有 {len(names)} 个文件（{'、'.join(names)}）"
                f" → 删掉多余的；否则轮换与算术检查会算错")

    streak(nodes, "hook_type", HOOK_REPEAT_LIMIT, "章末钩子重复", hard)
    streak(nodes, "opening_type", OPENING_REPEAT_LIMIT, "开头类型重复", hard)
    streak(nodes, "emotion", EMOTION_REPEAT_LIMIT, "情绪连续同向", hard)

    gap = best_gap = 0
    for n in nodes:
        if str(n.get("payoff", "")).strip() not in ("", "无", "None"):
            best_gap = max(best_gap, gap)
            gap = 0
        else:
            gap += 1
    best_gap = max(best_gap, gap)
    if best_gap >= PAYOFF_GAP_LIMIT:
        hard.append(f"最长连续 {best_gap} 章没有爽点/微兑现（上限 {PAYOFF_GAP_LIMIT} 章），补一个")

    strengths = []
    for n in nodes:
        raw = NUM_RE.findall(str(n.get("payoff_strength", "")))
        strengths.append(int(raw[0]) if raw else 0)
    if len(strengths) > BIG_PAYOFF_WINDOW:
        for i in range(len(strengths) - BIG_PAYOFF_WINDOW + 1):
            if max(strengths[i:i + BIG_PAYOFF_WINDOW]) < BIG_PAYOFF_MIN_STRENGTH:
                soft.append(
                    f"第 {nodes[i]['_chapter']}-{nodes[i + BIG_PAYOFF_WINDOW - 1]['_chapter']} 章"
                    f"没有强度 ≥{BIG_PAYOFF_MIN_STRENGTH} 的大兑现（每 {BIG_PAYOFF_WINDOW} 章要有一个）"
                )
                break

    last_seen: dict[str, int] = {}
    current = max(chapters) if chapters else 0
    for n in nodes:
        for sub in re.split(r"[,，、;；/]", str(n.get("subplots", ""))):
            sub = sub.strip()
            if sub and sub not in ("无", "None"):
                last_seen[sub] = n["_chapter"] or 0
    for sub, ch in sorted(last_seen.items()):
        if current - ch > SUBLOT_GAP_LIMIT:
            soft.append(f"支线「{sub}」已 {current - ch} 章没出场（上限 {SUBLOT_GAP_LIMIT} 章），插一段")

    remaining = []
    for n in nodes:
        raw = str(n.get("countdown", "")).strip()
        if COUNTDOWN_RENEWABLE_MARK in raw:
            continue        # 可延期的死线不进单调性检查（延期不是倒流）
        if COUNTDOWN_RESET_MARK in raw:
            # 一条死线结束、另一条全新的死线开始：清空序列重新起算。
            # 连载里这是常态（"这件案子还有 3 天"办完，下一件案子又来一条新死线），
            # 不清序列就会把"新死线比旧死线远"误报成"时间倒流"。
            remaining = []
        m = COUNTDOWN_RE.match(raw)
        if not m:
            continue
        sign, num = m.group(1), int(m.group(2))
        remaining.append((n["_chapter"], -num if sign != "+" else num, str(n["countdown"])))
    # 时间坐标必须单调不减：D-5 → -5，D-3 → -3（前进）；D+1 → 1（已过天数，前进）
    for i in range(1, len(remaining)):
        prev_ch, prev_v, prev_s = remaining[i - 1]
        cur_ch, cur_v, cur_s = remaining[i]
        if cur_v < prev_v:
            hard.append(f"倒计时倒退：第 {prev_ch} 章 {prev_s} → 第 {cur_ch} 章 {cur_s}（时间不能倒流）")
        elif cur_v - prev_v > jump_max:
            soft.append(f"倒计时跳跃：第 {prev_ch} 章 {prev_s} → 第 {cur_ch} 章 {cur_s}"
                        f"（一跳超过 {jump_max} 天，需要过渡）")

    # front-matter 字段名体检：**名字被改 = 依赖它的检查静默失效**（B029）
    if nodes:
        for key in NODE_EXPECTED_FM:
            missing = [n["_chapter"] for n in nodes if not str(n.get(key, "")).strip()]
            if len(missing) == len(nodes):
                hard.append(
                    f"**所有节点的 front-matter 都没有 `{key}`** → 依赖它的检查全部失效"
                    "（字段名被改了？模板里的字段名不要动）"
                )
            elif missing:
                soft.append(
                    f"{len(missing)} 个节点缺 `{key}`：{missing[:6]}"
                    + ("…" if len(missing) > 6 else "")
                )

    soft += check_vocab(nodes)
    return hard, soft


def _bare(line: str) -> str:
    """去 markdown 装饰 + 去掉行首的 `#`/`-`，便于按标题名匹配（同 continuity_check 的 bare_line）。"""
    return re.sub(r"[*`（）()【】\[\]：:　\s]", "", line).lstrip("-#")


def load_protagonist_name(novel_root: Path) -> str:
    """主角名从 `characters/主角-<名>.md` 的文件名里取（开书时的命名约定）。"""
    cdir = novel_root / "characters"
    if not cdir.is_dir():
        return ""
    for p in sorted(cdir.glob("主角-*.md")):
        parts = p.stem.split("-", 1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
    return ""


def _runs_of(nodes: list[dict], bad) -> list[tuple[int, int, int]]:
    """把满足 bad(n) 的连续章号聚成 (起, 止, 章数)。"""
    runs: list[tuple[int, int, int]] = []
    cur: list[int] = []
    for n in nodes:
        if bad(n):
            cur.append(n["_chapter"])
        else:
            if cur:
                runs.append((cur[0], cur[-1], len(cur)))
            cur = []
    if cur:
        runs.append((cur[0], cur[-1], len(cur)))
    return runs


def check_direction_drift(nodes: list[dict], novel_root: Path):
    """方向跑偏的两个机器信号：**主线断档**与**主角消失**（B030 缺口 1、2）。

    - `strand` 连续 ≥3 章都不是「主线」→ 提示（**不判必须改**：3 章支线弧可能是刻意的，
      而 `strand` 是自报字段，判硬会把"有意为之"误杀）
    - 主角连续 ≥3 章不在 `characters` 里 → 提示（双线叙事也合法，所以要人确认）
    """
    hard: list[str] = []
    soft: list[str] = []
    if not nodes:
        return hard, soft

    def not_main(n: dict) -> bool:
        s = str(n.get("strand", "")).strip()
        return bool(s) and s != MAIN_STRAND

    for a, b, cnt in _runs_of(nodes, not_main):
        if cnt >= STRAND_GAP_LIMIT:
            soft.append(
                f"主线断档：第 {a}-{b} 章连续 {cnt} 章 `strand` 都不是「{MAIN_STRAND}」"
                f" → 纪律是「连续 {STRAND_GAP_LIMIT} 章没推进主线就拉回」；"
                "若这是刻意的支线弧，在节点里补一句说明"
            )

    who = load_protagonist_name(novel_root)
    if who:
        for a, b, cnt in _runs_of(nodes, lambda n: who not in str(n.get("characters", ""))):
            if cnt >= PROTAGONIST_GAP_LIMIT:
                soft.append(
                    f"主角「{who}」连续 {cnt} 章没出场（第 {a}-{b} 章）→ 确认是双线叙事，还是漏了他"
                )
    return hard, soft


def check_volume_beats(nodes: list[dict], novel_root: Path, current: int):
    """卷节拍对账（B030 缺口 3）：把卷纲里「节拍｜章号」跟实际章号对上。

    **它不判节拍有没有写好**（那要语义判断），只回答"这个节拍的章号已经过了没有"——
    这就是把 SKILL.md 那句"每 10 章对照总纲"从纪律变成可查。
    """
    hard: list[str] = []
    soft: list[str] = []
    if not nodes or not current:
        return hard, soft
    odir = novel_root / "outline"
    if not odir.is_dir():
        return hard, soft
    vols = sorted(odir.glob("卷纲-第*卷.md"))
    if not vols:
        soft.append("找不到 `outline/卷纲-第*卷.md` → 卷节拍对账做不了（卷节拍表是它的第二节）")
        return hard, soft
    vp = vols[-1]                      # 只对当前卷；旧卷已归档
    m = re.search(r"^#{2,3}\s*卷节拍表[^\n]*\n(.*?)(?=\n#{2,3}\s|\Z)",
                  vp.read_text(encoding="utf-8-sig", errors="replace"), re.S | re.M)
    if not m:
        soft.append(f"{vp.name} 里找不到「卷节拍表」段 → 卷节拍对账跳过（段名被改了？）")
        return hard, soft
    header: list[str] | None = None
    beats: list[tuple[str, int]] = []
    for line in m.group(1).splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        if header is None:
            header = cells
            continue
        if "章号" not in header:
            continue
        ci = header.index("章号")
        if ci >= len(cells):
            continue
        num = NUM_RE.findall(cells[ci])
        if num:
            label = cells[1] if len(cells) > 1 and cells[1] else cells[0]
            beats.append((label, int(num[0])))
    if not beats:
        soft.append(f"{vp.name} 的卷节拍表里读不出任何「章号」→ 对账跳过（列名被改，或还没填）")
        return hard, soft
    passed = [(b, ch) for b, ch in beats if ch < current]
    upcoming = [(b, ch) for b, ch in beats if ch >= current]
    if passed:
        b, ch = max(passed, key=lambda x: x[1])
        soft.append(
            f"卷节拍对账：最近一个已过的节拍是「{b}」（原定第 {ch} 章，现在写到第 {current} 章）"
            " → 落地了就在总纲对账里记一笔，没落地就该拉回"
        )
    if upcoming:
        b, ch = min(upcoming, key=lambda x: x[1])
        soft.append(f"卷节拍对账：下一个节拍「{b}」在第 {ch} 章（还差 {ch - current} 章）")
    return hard, soft


def check_outline_reconciliation(nodes: list[dict]):
    """总纲对账留痕（B030 缺口 4）：每 10 章要有一次对账记录。

    只报**最近一个**该对账却缺记录的章（旧章不逐条追改，避免一次刷好几条）。
    """
    hard: list[str] = []
    soft: list[str] = []
    multiples = [n["_chapter"] for n in nodes
                 if n["_chapter"] is not None and n["_chapter"] % RECON_EVERY == 0]
    if not multiples:
        return hard, soft
    missing: list[int] = []
    for n in nodes:
        ch = n["_chapter"]
        if ch is None or ch % RECON_EVERY:
            continue
        text = str(n.get("_text", ""))
        if not any(_bare(l).startswith(a) for a in RECON_SECTION_ALIASES
                   for l in text.splitlines()):
            missing.append(ch)
    if missing:
        soft.append(
            f"第 {missing[-1]} 章（最近一个 {RECON_EVERY} 的倍数）的节点没有「总纲对账」段 → "
            f"每 {RECON_EVERY} 章对照一次总纲并留痕：主线是否还在轨／有没有该拉回的支线／卷节拍进度"
        )
    return hard, soft


def parse_foreshadow_archive(path: Path) -> list[dict]:
    """解析「回收记录」表（表头带「回收章」的那张；活跃表由 parse_foreshadowing 解析）。

    为什么要它：两张表**各写各的、从不对账**——活跃表说"还在推进"、回收记录说"已回收"，
    这种矛盾以前没有任何检查（口径借鉴 InkOS 的 hook 状态机：已回收不可被改回）。
    """
    if not path.is_file():
        return []
    rows: list[dict] = []
    header: list[str] | None = None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        if cells and cells[0] in ("编号", "ID", "id"):
            header = cells if ("回收章" in cells or "回收方式" in cells) else None
            continue
        if header is None or not cells[0]:
            continue
        rows.append({"id": cells[0]})
    return rows


def check_foreshadow_consistency(rows: list[dict], archived: list[dict]) -> tuple[list[str], list[str]]:
    """活跃表 × 回收记录 的一致性（B031，#2）。"""
    hard: list[str] = []
    soft: list[str] = []
    active_ids = {r["id"] for r in rows
                  if r["status"] not in ("已回收", "已废弃", "回收")}
    arch_ids = {a["id"] for a in archived if a["id"]}
    both = sorted(active_ids & arch_ids)
    if both:
        hard.append(
            "伏笔同时出现在「活跃表」和「回收记录」里：" + "、".join(both)
            + " → 两边矛盾：要么状态没改，要么回收记录写早了。"
            "已回收的不该留在活跃表里（它不再占那 5 条上限）"
        )
    leftover = sorted({r["id"] for r in rows if r["status"] == "已回收"})
    if leftover:
        soft.append(
            "活跃表里还留着状态=「已回收」的行：" + "、".join(leftover)
            + " → 记进回收记录后就该把它移出活跃表"
        )
    return hard, soft


def check_recovery_declared(rows: list[dict], path: Path) -> tuple[list[str], list[str]]:
    """「本章伏笔操作」里写了「回收：FN」，但活跃表状态还是 已埋/推进中 → **说收了没收**。"""
    hard: list[str] = []
    soft: list[str] = []
    if not path.is_file() or not rows:
        return hard, soft
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    m = re.search(r"^#{2,3}\s*本章伏笔操作[^\n]*\n(.*?)(?=\n#{2,3}\s|\Z)", text, re.S | re.M)
    if not m:
        return hard, soft
    declared: set[str] = set()
    for line in m.group(1).splitlines():
        if "回收" not in line:
            continue
        for fid in re.findall(r"F\d+", line.split("回收", 1)[1]):
            declared.add(fid)
    by_id = {r["id"]: r for r in rows}
    bad = [f for f in sorted(declared)
           if f in by_id and by_id[f]["status"] not in ("已回收", "已废弃", "回收")]
    if bad:
        hard.append(
            "「本章伏笔操作」里说已回收，但活跃表状态还是 已埋/推进中：" + "、".join(bad)
            + " → **说收了没收**（要么补回收动作，要么改回收记录）"
        )
    missing = [f for f in sorted(declared) if f not in by_id]
    if missing:
        soft.append(
            "「本章伏笔操作」里回收了伏笔表里没有的编号：" + "、".join(missing)
            + " → 编号写错，或该条已被移出活跃表（那就没有对照关系了）"
        )
    return hard, soft


def check_unknown_fields(nodes: list[dict], fs_path: Path) -> list[str]:
    """未知字段/列名要报——这是 InkOS 那条 zod `.strict()` 的等价物（B031，#1）。

    它靠"未知字段直接拒绝"防住拼错与死字段；我们保持 markdown 作唯一真源，
    但**把多出来的字段当成信号报出来**：它要么是拼错（那一项检查就静默失效），
    要么是根本没人读的死字段（InkOS 自己就有 3 个）。
    """
    soft: list[str] = []
    unknown: dict[str, list] = {}
    for n in nodes:
        for k in n:
            if k.startswith("_"):
                continue
            if k not in NODE_KNOWN_FM:
                unknown.setdefault(k, []).append(n["_chapter"])
    for k, chs in sorted(unknown.items()):
        soft.append(
            f"节点 front-matter 里有**未知字段** `{k}`（{len(chs)} 个节点，例如第 {chs[0]} 章）"
            " → 多半是拼错（那一列不会被任何检查读到），或者它是个没人读的死字段"
        )
    if fs_path.is_file():
        bad_cols: set[str] = set()
        for line in fs_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and cells[0] in ("编号", "ID", "id"):
                bad_cols |= {c for c in cells if c and c not in FS_KNOWN_COLUMNS}
        if bad_cols:
            soft.append(
                "伏笔表里有**未知列**：" + "、".join(sorted(bad_cols))
                + " → 列名拼错的话，这一列的值不会被任何检查读到"
            )
    return soft


def check_foreshadowing(rows: list[dict], current: int,
                        known_chapters: set[int] | None = None) -> tuple[list[str], list[str]]:
    hard: list[str] = []
    soft: list[str] = []

    # 算术与引用一致性（口径借鉴 InkOS 的 delta 单调性校验：不可回退、不可自相矛盾）
    for r in rows:
        planted, target = r.get("planted"), r.get("target")
        if isinstance(planted, int) and isinstance(target, int) and target < planted:
            hard.append(
                f"伏笔 {r['id']} 的计划回收章（第 {target} 章）早于埋设章（第 {planted} 章）"
                " → 算术上不可能，改掉其中一个"
            )
        if isinstance(planted, int) and planted > current:
            soft.append(
                f"伏笔 {r['id']} 的埋设章是第 {planted} 章，但当前才第 {current} 章"
                " → 还没写到就登记成「已埋」了，要么是章号写错，要么状态该改"
            )
        if r["status"] == "已废弃" and not str(r.get("notes", "")).strip():
            soft.append(
                f"伏笔 {r['id']} 标了「已废弃」但备注列是空的 → **撤回必须有依据**，"
                "否则过几章没人知道当初为什么撤（口径同 InkOS 的 HOOK_WITHDRAWAL_REASON_REQUIRED）"
            )
        if known_chapters is not None and isinstance(planted, int) and planted not in known_chapters:
            soft.append(
                f"伏笔 {r['id']} 的埋设章第 {planted} 章没有对应的节点文件"
                " → 悬空引用（章号写错、或章节已归档）"
            )

    # 枚举校验：状态写错（如 "已回收了"）会被当成"仍然活跃" → 永远不归档，还一直算超期
    bad_status = {r["id"]: (r["status"] or "（空）") for r in rows
                  if r["status"] not in FORESHADOW_STATUS_OK}
    if bad_status:
        soft.append(
            "伏笔状态取值不在白名单（" + "/".join(sorted(FORESHADOW_STATUS_OK)) + "）："
            + "、".join(f"{k}={v}" for k, v in sorted(bad_status.items()))
            + "　→ 认不出的状态一律按「仍在进行」处理，会一直占额度并算超期"
        )

    # 编号重复：同一条承诺只能有一个编号，否则超期/回收记录会串在一起
    count: dict[str, int] = {}
    for r in rows:
        count[r["id"]] = count.get(r["id"], 0) + 1
    for rid, c in sorted(count.items()):
        if c > 1:
            hard.append(f"伏笔编号重复：{rid} 出现 {c} 次 → 合并成一条，否则超期与回收记录会串")

    active = [r for r in rows if r["status"] not in ("已回收", "已废弃", "回收")]
    if len(active) > ACTIVE_FORESHADOW_LIMIT:
        soft.append(f"同时进行的伏笔 {len(active)} 条 > {ACTIVE_FORESHADOW_LIMIT}（超过就收不住）")

    # 同一承诺重复开 hook（含"改名"造成的重复）：作者替同一个承诺开两条，读者会以为有两件事
    for a in range(len(active)):
        for b in range(a + 1, len(active)):
            ratio = similar_hook(active[a].get("content", ""), active[b].get("content", ""))
            if ratio >= DUP_HOOK_RATIO:
                soft.append(
                    f"伏笔 {active[a]['id']} 与 {active[b]['id']} 内容相似度 {ratio:.0%}，"
                    f"疑似同一条承诺（「{active[a]['content']}」／「{active[b]['content']}」）"
                    f" → 合并成一条，或把措辞改到能区分"
                )

    # 「最近推进章」这一列我们一直在收，却从来没查过。
    # 口径借鉴 InkOS 的 mention≠advance：**仅仅被提到不算推进**，只有作者主动更新这一列才算。
    # （阈值直接复用 FORESHADOW_WARN_DELAY，不为这条新检查另发明一个数字。）
    stale = [
        (current - r["last_advanced"], r["id"], r["last_advanced"])
        for r in active
        if isinstance(r.get("last_advanced"), int)
        and current - r["last_advanced"] >= FORESHADOW_WARN_DELAY
    ]
    # 最久没推进的排最前（InkOS 的 mustAdvance 就是"取最久未推进"）
    for gap, rid, last in sorted(stale, reverse=True):
        soft.append(
            f"伏笔 {rid} 已 {gap} 章没推进（最近推进第 {last} 章，现在第 {current} 章）"
            " → 被提到不算推进，要真的加证据 / 改风险范围 / 改关系 / 改可推断结论"
        )

    for r in active:
        if r["target"] is None:
            soft.append(f"伏笔 {r['id']} 没有计划回收章，补上或删掉")
            continue
        delay = current - r["target"]
        if delay <= 0:
            continue
        core = r["type"] in FORESHADOW_CORE_TYPES
        if core and delay >= 1:
            hard.append(f"主线伏笔 {r['id']} 超过计划回收章 {delay} 章（计划第 {r['target']} 章，现在第 {current} 章），必须安排")
        elif delay >= FORESHADOW_RED_DELAY:
            hard.append(f"伏笔 {r['id']} 超期 {delay} 章（≥{FORESHADOW_RED_DELAY}），本卷内必须回收")
        elif delay >= FORESHADOW_WARN_DELAY:
            soft.append(f"伏笔 {r['id']} 超期 {delay} 章（≥{FORESHADOW_WARN_DELAY} 标黄），排进最近 3 章")
        else:
            soft.append(f"伏笔 {r['id']} 已过期 {delay} 章，注意推进")
    return hard, soft


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跨章连载体检（钩子/节奏/伏笔/时间线）")
    ap.add_argument("--plot", default="novel/plot", help="节点文件目录，默认 novel/plot")
    ap.add_argument("--foreshadowing", help="伏笔表路径，默认 novel/foreshadowing/伏笔表.md")
    ap.add_argument("--chapters-dir", default="novel/chapters",
                    help="正文章节目录（用于实测字数），默认 novel/chapters")
    ap.add_argument("--chapter", type=int, help="当前章号，默认取节点文件里最大的章号")
    ap.add_argument("--target-words", type=int,
                    help="单章目标字数，**只用来判下限**（低于该比例提示骨架感）；"
                         "超上限不判、不提示——本书不做字数上限")
    ap.add_argument("--baseline", help="风格基线 JSON：读它的 bands.word_low_ratio "
                                      "作为本书字数下限比例（默认 0.90）")
    ap.add_argument("--strict", action="store_true", help="存在硬问题时以退出码 1 结束")
    args = ap.parse_args(argv)

    # 字数下限比例：默认 0.90，可在基线 JSON 的 bands 里按本书改（**只放宽下限，不加上限**）
    low_ratio = WORD_LOW_RATIO
    bands: dict = {}
    if args.baseline:
        bp = Path(args.baseline)
        if bp.is_file():
            try:
                bands = (json.loads(bp.read_text(encoding="utf-8-sig")) or {}).get("bands") or {}
                _r = bands.get("word_low_ratio")
                if isinstance(_r, (int, float)) and 0 < _r <= 1:
                    low_ratio = float(_r)
            except Exception as exc:
                print(f"警告：基线文件读不了，忽略：{exc}", file=sys.stderr)
        else:
            print(f"警告：基线文件不存在，忽略：{bp}", file=sys.stderr)

    plot_dir = Path(args.plot)
    if not plot_dir.is_dir():
        print(f"错误：目录不存在：{plot_dir}", file=sys.stderr)
        return 2

    nodes = load_nodes(plot_dir)
    hard, soft = check_nodes(nodes, bands)

    _measured: dict = {}
    if args.target_words:
        soft += check_word_floor(nodes, Path(args.chapters_dir), args.target_words,
                                 low_ratio, _measured)

    chapters = [n["_chapter"] for n in nodes if n["_chapter"] is not None]
    current = args.chapter or (max(chapters) if chapters else 0)

    fs_path = Path(args.foreshadowing) if args.foreshadowing else plot_dir.parent / "foreshadowing" / "伏笔表.md"
    rows = parse_foreshadowing(fs_path)

    # 解析体检：**静默失效是这里最大的风险**（表头驱动解析，列名一改就什么都不查了）
    fs_info = foreshadowing_table_info(fs_path)
    if fs_info["exists"]:
        if fs_info["missing_columns"]:
            hard.append(
                "伏笔表缺少必要列：" + "、".join(fs_info["missing_columns"])
                + " → 列名被改会让伏笔检查**静默失效**（不报错，只是什么都查不出来）"
            )
        elif fs_info["with_status"] and fs_info["rows"] == 0:
            hard.append(
                "伏笔表里有「状态」列，却**一条都没解析出来** → 伏笔检查整条失效"
                "（列名被改、或编码/表格格式坏了；B015 的 BOM 就是这么栽的）"
            )
        elif fs_info["headers"] and not fs_info["with_status"]:
            hard.append(
                "伏笔表里没有任何一张表带「状态」列 → 脚本只解析活跃伏笔表，"
                "缺这一列会让整个伏笔检查不生效"
            )

    fs_hard, fs_soft = check_foreshadowing(rows, current, set(chapters))
    hard += fs_hard
    soft += fs_soft

    # 方向跑偏的三项（B030）：主线断档 / 主角消失 / 卷节拍对账 + 总纲对账留痕
    _novel_root = plot_dir.parent
    for _res in (
        check_direction_drift(nodes, _novel_root),
        check_volume_beats(nodes, _novel_root, current),
        check_outline_reconciliation(nodes),
        check_foreshadow_consistency(rows, parse_foreshadow_archive(fs_path)),
        check_recovery_declared(rows, fs_path),
    ):
        _h, _s = _res
        hard += _h
        soft += _s
    # 未知字段/列名（`.strict()` 的等价物，B031）
    soft += check_unknown_fields(nodes, fs_path)

    print(f"=== 连载体检（节点 {len(nodes)} 个，当前第 {current} 章，伏笔 {len(rows)} 条）===")
    if args.target_words and nodes:
        _len_line = word_length_stats(nodes, Path(args.chapters_dir),
                                      args.target_words, low_ratio, _measured)
        if _len_line:
            print(_len_line)
    if nodes:
        print("最近 5 章：")
        for n in nodes[-5:]:
            print(
                f"  第{n['_chapter']}章 {n.get('title', '')}"
                f"｜钩子 {n.get('hook_type', '?')}"
                f"｜开头 {n.get('opening_type', '?')}"
                f"｜兑现 {n.get('payoff', '-')}"
                f"｜情绪 {n.get('emotion', '?')}"
                f"｜倒计时 {n.get('countdown', '-')}"
            )
    if hard:
        print("必须改：")
        for x in hard:
            print(f"  - {x}")
    else:
        print("必须改：无")
    if soft:
        print("建议改：")
        for x in soft:
            print(f"  - {x}")

    return 1 if (hard and args.strict) else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
