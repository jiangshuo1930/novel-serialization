#!/usr/bin/env python3
"""本地 AI 味预筛（代理指标），不是朱雀。

真正的朱雀实测交给 `zhuque_check.py`（腾讯云 EdgeOne Makers 官方接口，密钥走环境
变量）。本脚本负责在花额度之前把明显的问题改掉。
本脚本测的是这类检测器真正抓的统计特征——句长/段长波动、词汇丰富度、
3-gram 重复、标点分布、可预测用词——外加本项目的黑名单、用词配额、世界观禁止矛盾
清单、角色声纹，好在花一次朱雀额度之前先把明显的问题改掉。

绝不要把本脚本的输出当成朱雀结论。

阈值分两档：
  HARD      与文本长度无关的规则：黑名单、用词配额、禁止矛盾、重复开头、
            段落/对话结构。违反就是明确的 AI 味或违反硬规则，--strict 据此判定。
  ADVISORY  统计参考区间（句长/段长波动、词汇丰富度、3-gram 重复、标点分布、
            声纹次数）。与题材相关，且正文不足 500 字时不做判断。
            用 --write-baseline 建立本书基线看漂移，比照抄绝对值准。

词汇丰富度用 MATTR（滑动窗口平均 TTR）、3-gram 用定长窗口重复率 —— 都是
长度归一化的指标，所以不同字数的章节之间可以直接比较。

数值的唯一权威是本文件顶部的常量；references/ 里的数字是它们的快照。

用法:
  python style_audit.py novel/chapters/第12章-雨夜收账.md
  python style_audit.py --dir novel/chapters --recursive
  python style_audit.py <file> --blacklist <skill-directory>/references/ai-flavor-blacklist.md
  python style_audit.py <file> --forbidden novel/world/世界观.md
  python style_audit.py <file> --fingerprint novel/characters/角色-老周.md
  python style_audit.py --dir novel/chapters --write-baseline novel/state/风格基线.json
  python style_audit.py <file> --baseline novel/state/风格基线.json
  python style_audit.py <file> --strict        # 有 HARD 问题时退出码 1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------- 常量

SENT_END = "。！？!?…"
OPENER_RE = re.compile(r"""^[“”"'‘’「」『』（(【\[—-]+""")
PUNCT_MARKS = "，。！？；：、—…“”‘’「」（）《》"
# 逐字符判断用集合版（O(1)）：has_content 对**每一句**调用、char_stream 对**每一章**逐字符，
# 字符串版每次线性扫描会很贵（B032 压测）。原字符串保留（标点分布统计要遍历它）。
PUNCT_SET = frozenset(PUNCT_MARKS)
# 逐字符 Python 循环在"每章 2200 字 × 455 章"上要 100 万次迭代；正则替换在 C 层跑，结果相同（B032）
PUNCT_STRIP_RE = re.compile(r"[\s" + re.escape(PUNCT_MARKS) + "]")
OPENERS = "“「『"
CLOSERS = "”」』"

# 每章出现次数上限（与黑名单互斥：黑名单里的词不设配额）
QUOTA_CHAPTER = {
    "突然": 4, "忽然": 4, "猛然": 4,
    "竟然": 3, "居然": 3,
    "渐渐": 2, "逐渐": 2,
    "依旧": 2, "仍然": 2,
    "于是": 3,
    "然而": 4, "不过": 4,
    "瞳孔一缩": 2, "身体一僵": 2, "呼吸一滞": 2,
}
# 每 500 字上限
QUOTA_PER_500 = {"似乎": 3, "也许": 3, "大概": 3}
WEAK_ADVERBS = ["似乎", "好像", "大概", "或许", "也许", "几乎", "稍微", "确实"]
CONNECTIVES = [
    "与此同时", "然而", "因此", "此外", "总之", "总而言之", "值得注意的是",
    "不仅仅", "换句话说", "更重要的是", "事实上", "毫无疑问", "随后", "接着",
]
EMOTION_WORDS = [
    "愤怒", "悲伤", "难过", "开心", "高兴", "害怕", "恐惧", "紧张", "焦虑",
    "兴奋", "失落", "委屈", "无奈", "痛苦", "绝望", "激动", "尴尬", "羞愧",
]

DEFAULT_BLACKLIST = [
    "嘴角勾起", "眸光微闪", "不由得", "深吸一口气", "心中暗道", "他意识到",
    "五味杂陈", "空气中弥漫着", "与此同时", "命运的齿轮", "这只是一个开始",
    "瞳孔骤然收缩", "如释重负", "眼神暗了暗", "心头一颤", "殊不知",
]

TAG_AFTER = re.compile(
    "[" + CLOSERS + r"][^。！？\n]{0,10}?(?:说|道|问|答|喊|叫|开口|接口|低声|嘟囔|咕哝|应声)"
)
TAG_BEFORE = re.compile(r"(?:说|道|问|答|喊|叫|低声|嘟囔|咕哝)[：:]\s*[" + OPENERS + r"]")

# 配额词的守卫：有些配额词是别的词的组成部分，直接 count 会假超标。
# 实际踩过："不过"（连接词，上限 4）被"再好不过""不过分"刷爆。
WORD_GUARD_PRE = {"不过": "再好坏差多少远近"}
WORD_GUARD_SUF = {"不过": "分去问意于"}


def count_word(body: str, w: str) -> int:
    """带守卫的词频统计：排除"再好不过""不过分"这类同形不同义的写法。"""
    pat = ""
    if WORD_GUARD_PRE.get(w):
        pat += "(?<![" + WORD_GUARD_PRE[w] + "])"
    pat += re.escape(w)
    if WORD_GUARD_SUF.get(w):
        pat += "(?![" + WORD_GUARD_SUF[w] + "])"
    return len(re.findall(pat, body))

# 统计阈值（全部是 ADVISORY 参考区间，不阻断 --strict）
REF_TRI_REP = 0.08             # 窗口化 3-gram 重复率参考上限
REF_TRI_UNIQ = 0.85            # 窗口化 3-gram 唯一率参考下限（与上一项配套）
MIN_VAR_LEN = 8.0              # 句长方差 σ²
MIN_BURST_CV = 0.40            # 句长波动系数
MIN_PARA_CV = 0.30             # 段长波动系数
MIN_PARA_VAR = 20.0            # 段长方差
MIN_MATTR = 0.40               # 词汇丰富度（滑动窗口平均 TTR）
MIN_PUNCT_CV = 0.30
MIN_WINDOW_PASS = 0.90         # 每 200 字窗口"1 短句 + 1 长句"通过率
SOFT_PARA_CHARS = 200          # 单段建议上限
# ↑ 2026 按真实稿实测校准：某书 13 章最长段 130 字（各章 79-130），
#   原来的 250 与硬门槛 400 都**形同虚设**（永远不会响）。降到 200 后，
#   按这本书的正常行文仍然零告警，但段落长到超出常态 50% 时会先提醒。
#   硬门槛**保持 400 不动**：这本书离 400 还很远，降硬门槛只会增加误伤风险、没有收益。
MIN_CHARS_FOR_STATS = 500      # 低于此字数不做统计类判断（摘录会产生假阳性）
MIN_SHORT_PARA = 0.15          # 单句段占比
MAX_SHORT_PARA = 0.45
MAX_TAG_RATIO = 0.50           # 对话标签占对话行比例（目标 30%）
MAX_LE_PER_PARA = 2.5
# 连续带「了」的句子：单调黏连的信号。阈值来源见 references/skill-changelog.md 的阈值裁决：
# InkOS 历史版本用 6，但按真实稿实测（13 章最长连续 6 句，其中 2 章正好 6）会打中 15% 的章；
# 故取实测上限 +2 = 8，只提示不判必须改。
LE_RUN_SOFT = 8
# 公式化对称句式：中文 AI 腔的强特征。用正则（不是黑名单字面量——「不是……而是……」中间有内容）。
# 「既……又……」故意不收：它是正常中文（"既怕又恨"），收了会误杀。
SYMMETRIC_PATTERNS = [
    ("不是……而是……", re.compile(r"不是[^。！？]{0,20}而是")),
    ("与其说……不如说……", re.compile(r"与其说[^。！？]{0,20}不如说")),
    ("一方面……另一方面……", re.compile(r"一方面[^。！？]{0,30}另一方面")),
    ("仿佛……一般", re.compile(r"仿佛[^。！？]{1,20}一般")),
    ("像是……似的", re.compile(r"像是[^。！？]{1,20}似的")),
]
SOFT_EMOTION = 3
MAX_WEAK_PER_1K = 8.0
MAX_CONNECTIVE_PER_1K = 3.0
WINDOW_CHARS = 200
WINDOW_SHORT = 5
WINDOW_LONG = 20
MATTR_WINDOW = 500
TRI_WINDOW = 500
FINGERPRINT_MIN = 2            # 每章标志性语言/动作至少出现次数

# 结构类硬门槛（与文本长度无关，违反就是"必须改"）
HARD_PARA_CHARS = 400          # 单段字数硬上限，超过必须拆
HARD_DIALOGUE_RUN = 12         # 连续纯对话行上限
HARD_SAME_LEN_RUN = 4          # 连续同长度（±2 字）句子上限
# 报告口径（阈值里用到的句子长度定义）
SHORT_SENT_CHARS = 5           # "极短句"判定长度
LONG_SENT_CHARS = 40           # "长句"判定长度

BASELINE_HIGHER_BETTER = [
    "var_len", "burst_cv", "para_var", "para_cv", "mattr",
    "punct_cv", "window_pass", "mean_len",
]
BASELINE_LOWER_BETTER = [
    "tri_rep", "le_per_para", "connectives_per_1k", "weak_adverbs_per_1k",
]
DRIFT_TOLERANCE = 0.15
DRIFT_LENGTH_TOLERANCE = 0.40   # 字数差异超过此比例则漂移判断仅供参考
BASELINE_MIN_CHAPTERS = 3

# 漂移的**绝对**下限（相对容差必须和它同时满足才算漂移）。
# 为什么需要：相对容差用在小量级指标上会把"多一个词"报成风格漂移 ——
# 实测本书基线 weak_adverbs_per_1k=0.373、connectives_per_1k=0.109，
# ±15% 意味着"一章多一个「似乎」/「然而」"就报，13 章里刷出 11 条假漂移。
# 而这些指标本来就有绝对门槛（弱化副词 ≤8/千字、连接词 ≤3/千字），等于同一件事报两遍。
DRIFT_MIN_ABS = {
    "weak_adverbs_per_1k": 0.8,     # ≈2200 字里多 2 处弱化副词才算漂移
    "connectives_per_1k": 0.8,
    "le_per_para": 0.30,
    "tri_rep": 0.010,
    "punct_cv": 0.40,               # 标点分布天生高方差（本书跨章 0.31-1.32）
}


# ---------------------------------------------------------------- 基础解析

SENT_SPLIT_RE = re.compile(r"[^。！？!?…]+[。！？!?…]*")

# 章节号元叙述：模型把"结构信息"漏进正文的痕迹（正文里冒出「第33章」「Chapter 33」）。
# 口径借鉴 InkOS 的后写规则（他们会拦正文里的 Chapter 33）。标题行不算——
# content_lines 已经跳过 `#` 开头的行，所以这里扫到的都是正文内部。
META_CHAPTER_RE = re.compile(r"第\s*\d{1,4}\s*章|[Cc]hapter\s*\d{1,4}")


def has_content(s: str) -> bool:
    """片段里有没有实际内容（不是纯标点/空白）。"""
    return any(
        ch not in SENT_END and ch not in PUNCT_SET and not ch.isspace() for ch in s
    )


def split_sentences(text: str) -> list[str]:
    """按句末标点切句；跳过标题、引用与表格行。

    两个刻意处理的边界（都踩过坑）：
    1. **连续的句末标点算同一个句号**。"……" 曾经被切成两个空片段，
       于是 `“你……”` 变成 `“你` / `…` / `…` / `”` 四个"句子"。
    2. **纯标点片段不算句子**。1 字碎片会污染句长统计，也会在
       "重复开头"里聚成一堆无意义的报警（实际发生过：报告里冒出 `”`×4、`“你…`×3）。
    """
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#>|":
            continue
        for m in SENT_SPLIT_RE.finditer(line):
            piece = m.group(0).strip()
            if has_content(piece):
                out.append(piece)
    return out


def content_lines(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#>|":
            continue
        out.append(s)
    return out


def body_text(text: str) -> str:
    return "\n".join(content_lines(text))


def char_stream(body: str) -> str:
    """统计用字符流：去掉空白与标点。"""
    return PUNCT_STRIP_RE.sub("", body)


def load_blacklist(path: Path | None) -> list[str]:
    """从黑名单 markdown 里提取所有反引号包裹的词。"""
    words = list(DEFAULT_BLACKLIST)
    if path is None:
        return sorted(set(words))
    if not path.is_file():
        print(f"警告：黑名单文件不存在，改用内置清单：{path}", file=sys.stderr)
        return sorted(set(words))
    text = path.read_text(encoding="utf-8-sig")
    words.extend(re.findall(r"`([^`\n]{1,20})`", text))
    return sorted({w.strip() for w in words if w.strip()})


def load_forbidden(path: Path | None) -> list[str]:
    """从世界观文件的「禁止矛盾清单」小节提取反引号包裹的短句。"""
    if path is None or not path.is_file():
        return []
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    # 只认「## 禁止矛盾清单」或「## 禁止矛盾清单（…）」这种小标题，避免误抓"清单怎么写"那节
    m = re.search(
        r"^##\s*禁止矛盾清单(?:[（(][^\n]*)?\s*$.*?(?=^##\s|\Z)", text, re.S | re.M
    )
    section = m.group(0) if m else ""
    return sorted({w.strip() for w in re.findall(r"`([^`\n]{1,40})`", section) if w.strip()})


def char_name_from_path(path: Path) -> str:
    """从角色档案文件名推角色名：角色-配角丙.md → 配角丙；主角-主角甲.md → 主角甲。"""
    stem = path.stem
    for pre in ("主角-", "角色-", "主角", "角色"):
        if stem.startswith(pre) and len(stem) > len(pre):
            return stem[len(pre):]
    return stem


def load_fingerprints(paths: list[Path]) -> dict[str, list[str]]:
    """从角色档案的 fingerprint 块里提取口头禅与标志性动作。

    返回 **{角色名: [短语...]}**。按角色分组是刻意的，不是随意选择：

    合并成一个词表再求总数，会让"多传几个档案"自动凑够次数，从而掩盖某个角色
    一次都没落地的情况——传 3 个档案时，每个角色平均只要命中 0.67 次就算达标。
    这个坑真实发生过：一个女主 8 章出场、3 条口头禅一条没用上，检查却一直是绿的。

    **契约：传几个档案 = 声明这几个角色本章出场，每一个都要各自达标。**
    某角色本章不出场，就不要传他的档案。
    """
    out: dict[str, list[str]] = {}
    for path in paths:
        if not path.is_file():
            print(f"警告：角色档案不存在，忽略：{path}", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        m = re.search(
            r"<!--\s*fingerprint:start\s*-->(.*?)<!--\s*fingerprint:end\s*-->", text, re.S
        )
        block = m.group(1) if m else text
        phrases: list[str] = []
        for line in block.splitlines():
            if not re.search(r"[:：]", line):
                continue
            key, val = re.split(r"[:：]", line, maxsplit=1)
            if any(k in key for k in ("口头禅", "口癖", "标志性动作", "标志动作", "语言指纹")):
                phrases += [p.strip() for p in val.split("|") if p.strip()]
        out[char_name_from_path(path)] = sorted(set(phrases))
    return out


def cv(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = statistics.fmean(values)
    return statistics.pstdev(values) / m if m else 0.0


def max_run(flags: list[bool]) -> int:
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best


def longest_same_len_run(lengths: list[int]) -> tuple[int, int]:
    """最长的"相邻句长度差 ≤2"连续段，返回 (句数, 起始下标)。

    与 `max_run(...)+1` 等价（回归测试锁着等价性），但额外给出位置，
    好让报告能落到具体那一句——只报"连续 4 句"是不够用的（B013 的教训）。
    """
    n = len(lengths)
    if n == 0:
        return 0, 0
    best_len, best_start = 1, 0
    run, start = 1, 0
    for i in range(1, n):
        if abs(lengths[i] - lengths[i - 1]) <= 2:
            run += 1
        else:
            if run > best_len:
                best_len, best_start = run, start
            run, start = 1, i
    if run > best_len:
        best_len, best_start = run, start
    return best_len, best_start


def seg_windows(seq: str, window: int, step: int) -> list[str]:
    if not seq:
        return []
    if len(seq) <= window:
        return [seq]
    return [seq[i:i + window] for i in range(0, len(seq) - window + 1, step)]


def mattr(stream: str, window: int = MATTR_WINDOW) -> float:
    """滑动窗口平均 TTR —— 长度归一化的词汇丰富度。"""
    if not stream:
        return 0.0
    segs = seg_windows(stream, window, window // 2)
    return statistics.fmean(len(set(s)) / len(s) for s in segs if s)


def windowed_tri_rep(stream: str, window: int = TRI_WINDOW) -> tuple[float, float]:
    """定长窗口内的 3-gram 重复率与唯一率；长度归一化，跨章节可比。"""
    reps, uniqs = [], []
    for seg in seg_windows(stream, window, window // 2):
        tri = Counter(seg[i:i + 3] for i in range(max(0, len(seg) - 2)))
        total = sum(tri.values())
        if not total:
            continue
        reps.append((total - len(tri)) / total)
        uniqs.append(len(tri) / total)
    if not reps:
        return 0.0, 0.0
    return statistics.fmean(reps), statistics.fmean(uniqs)


def window_pass(sentences: list[str]) -> tuple[float, int, list[dict]]:
    """每 200 字为一个窗口，要求窗内至少 1 个 ≤5 字短句和 1 个 ≥20 字长句。

    返回 `(通过率, 窗口数, 不合格窗口清单)`。第三个返回值是**给作者用的**：
    只报一个百分比，作者不知道该改哪一段——真实踩过一次，为了一个 82% 的数字
    临时写了两版诊断脚本才定位到「缺短句」的是哪两窗。检查必须能落到具体位置。
    """
    windows = ok = 0
    buf_len = 0
    has_short = has_long = False
    buf_head = ""
    fails: list[dict] = []
    for s in sentences:
        if not buf_head:
            buf_head = s
        buf_len += len(s)
        if len(s) <= WINDOW_SHORT:
            has_short = True
        if len(s) >= WINDOW_LONG:
            has_long = True
        if buf_len >= WINDOW_CHARS:
            windows += 1
            if has_short and has_long:
                ok += 1
            else:
                fails.append({
                    "no": windows,
                    "chars": buf_len,
                    "need": ("短句" if not has_short else "")
                            + ("＋长句" if not has_long else ""),
                    "head": buf_head[:24],
                })
            buf_len = 0
            has_short = has_long = False
            buf_head = ""
    if windows == 0:
        return 0.0, 0, []
    return ok / windows, windows, fails


# ---------------------------------------------------------------- 统计

def analyse(text: str, blacklist: list[str],
            forbidden: list[str] | None = None,
            fingerprints: dict[str, list[str]] | None = None) -> dict:
    body = body_text(text)
    stream = char_stream(body)
    total_chars = len(stream)
    per1k = (1000.0 / total_chars) if total_chars else 0.0

    sentences = split_sentences(text)
    lengths = [len(s) for s in sentences]
    n = len(lengths)
    mean_len = statistics.fmean(lengths) if n else 0.0
    stdev_len = statistics.pstdev(lengths) if n > 1 else 0.0
    var_len = statistics.pvariance(lengths) if n > 1 else 0.0
    burst_cv = (stdev_len / mean_len) if mean_len else 0.0

    short_n = sum(1 for x in lengths if x <= SHORT_SENT_CHARS)
    long_n = sum(1 for x in lengths if x >= LONG_SENT_CHARS)
    win_pass, win_n, win_fails = window_pass(sentences)

    paras = content_lines(text)
    para_lens = [len(p) for p in paras]
    para_mean = statistics.fmean(para_lens) if para_lens else 0.0
    para_stdev = statistics.pstdev(para_lens) if len(para_lens) > 1 else 0.0
    para_var = statistics.pvariance(para_lens) if len(para_lens) > 1 else 0.0
    para_cv = (para_stdev / para_mean) if para_mean else 0.0
    single_para = sum(1 for p in paras if len(split_sentences(p)) <= 1)
    single_para_ratio = single_para / len(paras) if paras else 0.0
    para_over_soft = sum(1 for x in para_lens if x > SOFT_PARA_CHARS)
    para_over_hard = sum(1 for x in para_lens if x > HARD_PARA_CHARS)

    quote_styles = {q: body.count(q) for q in OPENERS if body.count(q)}
    dialogue_lines = sum(
        1 for p in paras
        if any(o in p for o in OPENERS) and any(c in p for c in CLOSERS)
    )
    tags = len(TAG_AFTER.findall(body)) + len(TAG_BEFORE.findall(body))
    tag_ratio = tags / dialogue_lines if dialogue_lines else 0.0
    dialogue_only = [
        p.startswith(tuple(OPENERS)) and p.endswith(tuple(CLOSERS)) for p in paras
    ]
    dialogue_run = max_run(dialogue_only)

    m = mattr(stream)
    tri_rep, tri_uniq = windowed_tri_rep(stream)
    ttr_raw = len(set(stream)) / total_chars if total_chars else 0.0

    punct_cv = cv([c for c in (body.count(p) for p in PUNCT_MARKS) if c > 0])

    le_counts = [p.count("了") for p in paras]
    le_per_para = statistics.fmean(le_counts) if le_counts else 0.0
    le_over_3 = sum(1 for c in le_counts if c > 3)

    # 连续带「了」的句子（变量一律用长名：这个函数里短名踩过遮蔽的坑）
    le_run_max = 0
    le_run_start = 0
    _le_run = 0
    for _idx, _sent in enumerate(sentences):
        if "了" in _sent:
            _le_run += 1
            if _le_run > le_run_max:
                le_run_max = _le_run
                le_run_start = _idx - _le_run + 1
        else:
            _le_run = 0

    symmetric_hits: list[str] = []
    for _label, _pat in SYMMETRIC_PATTERNS:
        _ms = list(_pat.finditer(body))
        if _ms:
            _start = _ms[0].start()
            _ctx = body[max(0, _start - 6): _start + 28].replace("\n", " ")
            symmetric_hits.append(f"{_label}（{len(_ms)} 处）：…{_ctx}…")

    hits = {w: body.count(w) for w in blacklist if body.count(w) > 0}
    quota_bad = {
        w: (count_word(body, w), cap)
        for w, cap in QUOTA_CHAPTER.items() if count_word(body, w) > cap
    }
    quota_500_bad = {}
    for w, cap in QUOTA_PER_500.items():
        allowed = cap * max(1.0, total_chars / 500.0)
        c = body.count(w)
        if c > allowed:
            quota_500_bad[w] = (c, round(allowed, 1))

    forbidden_hits = {w: body.count(w) for w in (forbidden or []) if body.count(w) > 0}

    # 声纹按角色分别计数，不做合并（合并会让多传档案自动凑数，掩盖整章缺席）
    fp_by_char: dict[str, dict] = {}
    for who, phrases in (fingerprints or {}).items():
        h = {w: body.count(w) for w in phrases if body.count(w) > 0}
        fp_by_char[who] = {"total": sum(h.values()), "hits": h}
    fp_hits = {w: c for v in fp_by_char.values() for w, c in v["hits"].items()}
    fp_total = sum(v["total"] for v in fp_by_char.values())

    emotion_n = sum(body.count(w) for w in EMOTION_WORDS)
    weak_n = sum(body.count(w) for w in WEAK_ADVERBS)
    connective_n = sum(body.count(w) for w in CONNECTIVES)

    openers: dict[str, int] = {}
    for s in sentences:
        key = OPENER_RE.sub("", s)[:3]
        if len(key) >= 2:
            openers[key] = openers.get(key, 0) + 1
    repeated = sorted(((k, v) for k, v in openers.items() if v >= 3), key=lambda kv: -kv[1])

    same_len_run, same_len_start = longest_same_len_run(lengths)

    # ---- 位置信息：检查的输出必须可执行（B013 定的规矩，这里推到其余硬门槛）
    # 句 → 段落 的对应：split_sentences 是逐行处理的，所以按段落分别切句结果一致。
    sent_para: list[int] = []
    for i, p in enumerate(paras, 1):
        sent_para.extend([i] * len(split_sentences(p)))

    def where(word: str, limit: int = 3) -> list[str]:
        """这个词落在哪几段（带上下文，便于直接搜到）"""
        found: list[str] = []
        for i, p in enumerate(paras, 1):
            j = p.find(word)
            if j >= 0:
                ctx = p[max(0, j - 8): j + len(word) + 8].strip()
                found.append(f"第{i}段「…{ctx}…」")
                if len(found) >= limit:
                    break
        return found

    loc_blacklist = {w: where(w) for w in hits}
    loc_quota = {w: where(w) for w in quota_bad}
    loc_quota_500 = {w: where(w) for w in quota_500_bad}
    loc_openers: dict[str, str] = {}
    for k, _cnt in repeated:
        segs = [
            f"第{sent_para[i]}段"
            for i, s in enumerate(sentences)
            if i < len(sent_para) and OPENER_RE.sub("", s)[:3] == k
        ]
        loc_openers[k] = "、".join(dict.fromkeys(segs))
    loc_long_paras = [
        f"第{i}段（{len(p)} 字）「{p[:14]}…」"
        for i, p in enumerate(paras, 1) if len(p) > HARD_PARA_CHARS
    ][:5]
    same_len_where = (
        f"从第{sent_para[same_len_start]}段「{sentences[same_len_start][:16]}…」开始"
        if same_len_run >= HARD_SAME_LEN_RUN
        and same_len_start < len(sentences)
        and same_len_start < len(sent_para)
        else ""
    )

    # 注意：这一段**不要用短变量名**（m/j/p 之类）——analyse() 里 m 已经是 MATTR 的值，
    # 曾经因为这里写了 m = ...search(p) 而把 MATTR 覆盖成 None，round(m,3) 当场崩。
    meta_chapter_hits: list[str] = []
    for para_no, para_text in enumerate(paras, 1):
        mc = META_CHAPTER_RE.search(para_text)
        if mc and len(meta_chapter_hits) < 5:
            mc_start = mc.start()
            mc_ctx = para_text[max(0, mc_start - 8): mc_start + len(mc.group(0)) + 8].strip()
            meta_chapter_hits.append(f"第{para_no}段「…{mc_ctx}…」")

    return {
        "chars": total_chars,
        "sentences": n,
        "paragraphs": len(paras),
        "mean_len": round(mean_len, 1),
        "var_len": round(var_len, 1),
        "burst_cv": round(burst_cv, 2),
        "short_sentences": short_n,
        "long_sentences": long_n,
        "window_pass": round(win_pass, 2),
        "windows": win_n,
        "window_fails": win_fails,
        "para_var": round(para_var, 1),
        "para_cv": round(para_cv, 2),
        "single_para_ratio": round(single_para_ratio, 2),
        "para_over_soft": para_over_soft,
        "para_over_hard": para_over_hard,
        "dialogue_lines": dialogue_lines,
        "dialogue_tag_ratio": round(tag_ratio, 2),
        "dialogue_run": dialogue_run,
        "quote_styles": quote_styles,
        "mattr": round(m, 3),
        "ttr_raw": round(ttr_raw, 3),
        "tri_rep": round(tri_rep, 3),
        "tri_uniq": round(tri_uniq, 3),
        "punct_cv": round(punct_cv, 2),
        "le_per_para": round(le_per_para, 2),
        "le_over_3": le_over_3,
        "le_run_max": le_run_max,
        "le_run_where": (
            f"从第{sent_para[le_run_start]}段「{sentences[le_run_start][:16]}…」开始"
            if le_run_max >= LE_RUN_SOFT
            and le_run_start < len(sentences) and le_run_start < len(sent_para)
            else ""
        ),
        "symmetric_hits": symmetric_hits,
        "same_len_run": same_len_run,
        "emotion_words": emotion_n,
        "weak_adverbs_per_1k": round(weak_n * per1k, 1),
        "connectives_per_1k": round(connective_n * per1k, 1),
        "blacklist_hits": hits,
        "forbidden_hits": forbidden_hits,
        "fingerprint_hits": fp_hits,
        "fingerprint_total": fp_total,
        "fp_by_char": fp_by_char,
        "quota_exceeded": quota_bad,
        "quota_per_500_exceeded": quota_500_bad,
        "repeated_openers": [f"{k}×{v}" for k, v in repeated[:6]],
        "loc_blacklist": loc_blacklist,
        "loc_quota": loc_quota,
        "loc_quota_500": loc_quota_500,
        "loc_openers": loc_openers,
        "loc_long_paras": loc_long_paras,
        "same_len_where": same_len_where,
        "meta_chapter_hits": meta_chapter_hits,
    }


def judge(r: dict, baseline: dict | None = None,
          check_fingerprint: bool = False,
          bands: dict | None = None,
          explain_drift: bool = False) -> tuple[list[str], list[str], list[str]]:
    hard: list[str] = []
    soft: list[str] = []
    drift: list[str] = []

    def band(key: str, default: float) -> float:
        """取区间值：本书若在基线文件里声明了 bands，就用本书的，否则用脚本默认。

        为什么要有这个：模板里的区间是别人家的快照。本书实测长期落在区间外时，
        会每章都刷一条"参考区间外"——而**一条你学会忽略的告警，就是下一个真 bug 的藏身处**
        （真实教训：声纹告警连报六章"0~1 次"，被当成"按需改"忽略掉了）。
        所以：确认是本书刻意的风格，就写进 bands 明确豁免，别让它一直刷。
        bands 只放宽，不收紧——收紧请直接改脚本常量，那才是全局规则。
        """
        try:
            return float((bands or {})[key])
        except (KeyError, TypeError, ValueError):
            return default


    if r["blacklist_hits"]:
        hard.append("黑名单命中：" + "、".join(f"{w}×{c}" for w, c in sorted(r["blacklist_hits"].items())))
        for w in sorted(r["blacklist_hits"])[:4]:
            for loc in (r.get("loc_blacklist") or {}).get(w, [])[:1]:
                hard.append(f"　└ {w} → {loc}")
        if len(r["blacklist_hits"]) > 4:
            hard.append(f"　└ 其余 {len(r['blacklist_hits']) - 4} 个词的落点省略")
    if r["forbidden_hits"]:
        hard.append("禁止矛盾命中（世界观红线）：" + "、".join(
            f"{w}×{c}" for w, c in sorted(r["forbidden_hits"].items())))
    if r["quota_exceeded"]:
        hard.append("用词配额超标：" + "、".join(
            f"{w}×{c}(上限{cap})" for w, (c, cap) in sorted(r["quota_exceeded"].items())))
        for w in sorted(r["quota_exceeded"])[:3]:
            for loc in (r.get("loc_quota") or {}).get(w, [])[:1]:
                hard.append(f"　└ {w} → {loc}")
    if r["quota_per_500_exceeded"]:
        hard.append("每500字配额超标：" + "、".join(
            f"{w}×{c}(上限{cap})" for w, (c, cap) in sorted(r["quota_per_500_exceeded"].items())))
        for w in sorted(r["quota_per_500_exceeded"])[:3]:
            for loc in (r.get("loc_quota_500") or {}).get(w, [])[:1]:
                hard.append(f"　└ {w} → {loc}")
    if r["repeated_openers"]:
        hard.append("重复开头：" + "、".join(r["repeated_openers"]))
        for k, v in [(x.split("×")[0], x) for x in r["repeated_openers"][:3]]:
            loc = (r.get("loc_openers") or {}).get(k)
            if loc:
                hard.append(f"　└ {v} 落在 {loc}")
    if r["dialogue_run"] > HARD_DIALOGUE_RUN:
        soft.append(
            f"连续纯对话 {r['dialogue_run']} 行 > {HARD_DIALOGUE_RUN}，中间插沉默/动作/环境音")
    if r["same_len_run"] >= HARD_SAME_LEN_RUN:
        hard.append(
            f"连续 {r['same_len_run']} 句长度几乎相同（±2字，门槛 {HARD_SAME_LEN_RUN} 句），"
            "是排比/均匀段，打断其中一句")
        if r.get("same_len_where"):
            hard.append(f"　└ {r['same_len_where']}")
    if r.get("meta_chapter_hits"):
        soft.append(
            f"正文里出现章节号元叙述 {len(r['meta_chapter_hits'])} 处（第N章 / Chapter N）"
            "——这是把结构信息漏进正文的痕迹，改成故事内的说法（只提示，因为引用文献标题时可能是有意的）"
        )
        for loc in r["meta_chapter_hits"][:3]:
            soft.append(f"　└ {loc}")
    if r["para_over_hard"]:
        hard.append(f"{r['para_over_hard']} 段超过 {HARD_PARA_CHARS} 字，拆开")
        for loc in (r.get("loc_long_paras") or [])[:3]:
            hard.append(f"　└ {loc}")
        if len(r.get("loc_long_paras") or []) > 3:
            hard.append(f"　└ 另有 {len(r['loc_long_paras']) - 3} 段同上")

    if check_fingerprint:
        # 声纹是确定性计数，不是统计量，所以放在字数门槛之前，短文本也照报。
        # 逐个角色判，不合并——见 load_fingerprints 的说明。
        if not r["fp_by_char"]:
            soft.append("角色声纹：本章没传 --fingerprint，未做声纹核对")
        for who, v in sorted(r["fp_by_char"].items()):
            if v["total"] == 0:
                hard.append(
                    f"角色声纹【{who}】0 次：已声明该角色本章出场，但档案里登记的口头禅"
                    f"／标志动作一个都没落地，读者认不出他是他。"
                    f"两条路选一条——把标志性语言或动作写进正文，或本章不传这个档案"
                    f"（登记了不用，等于档案在撒谎）。登记项：{list(v['hits'].keys()) or '（全部缺席）'}"
                )
            elif v["total"] < FINGERPRINT_MIN:
                soft.append(
                    f"角色声纹【{who}】{v['total']} 次 < {FINGERPRINT_MIN}，命中 {v['hits']}"
                )
            else:
                soft.append(f"角色声纹【{who}】{v['total']} 次（达标）")

    if r["chars"] < MIN_CHARS_FOR_STATS:
        soft.append(
            f"正文只有 {r['chars']} 字（<{MIN_CHARS_FOR_STATS}），统计类阈值与基线漂移已跳过；"
            "本次只查了黑名单、配额、禁止矛盾、结构与段落项"
        )
        return hard, soft, drift

    if not r["quote_styles"]:
        soft.append("没找到成对引号（“”/「」/『』），对话统计口径可能不准")
    elif "“" not in r["quote_styles"]:
        soft.append(
            f"正文用的是非弯引号（{list(r['quote_styles'])}）—— 脚本两种都统计，"
            "但如果正文混用两种引号，标签率与对话占比会失真"
        )

    if r["tri_rep"] > band("tri_rep", REF_TRI_REP):
        soft.append(
            f"3-gram 重复率 {r['tri_rep']} > 参考值 {band('tri_rep', REF_TRI_REP)}"
            "（窗口化口径；先用 --write-baseline 建本书基线，看漂移比看绝对值准）"
        )
    if r["var_len"] < band("var_len", MIN_VAR_LEN):
        soft.append(f"句长方差 σ²={r['var_len']} < {band('var_len', MIN_VAR_LEN)}（句子太匀）")
    if r["burst_cv"] < band("burst_cv", MIN_BURST_CV):
        soft.append(f"句长波动不足 BurstCV={r['burst_cv']} < {band('burst_cv', MIN_BURST_CV)}（插 3-5 字短句）")
    if r["para_cv"] < band("para_cv", MIN_PARA_CV):
        soft.append(f"段长波动不足 ParaCV={r['para_cv']} < {band('para_cv', MIN_PARA_CV)}")
    if r["para_var"] < band("para_var", MIN_PARA_VAR):
        soft.append(f"段长方差 Pσ²={r['para_var']} < {band('para_var', MIN_PARA_VAR)}")
    if r["mattr"] < band("mattr", MIN_MATTR):
        soft.append(f"词汇丰富度 MATTR={r['mattr']} < {band('mattr', MIN_MATTR)}（补具体细节）")
    if r["tri_uniq"] < REF_TRI_UNIQ:
        soft.append(f"3-gram 唯一率 {r['tri_uniq']} < {REF_TRI_UNIQ}")
    if r["punct_cv"] < band("punct_cv", MIN_PUNCT_CV):
        soft.append(f"标点分布太齐 标点CV={r['punct_cv']} < {band('punct_cv', MIN_PUNCT_CV)}")
    if r["window_pass"] < band("window_pass", MIN_WINDOW_PASS) and r["windows"]:
        soft.append(
            f"局部节奏不达标：{r['window_pass']:.0%} 的 200 字窗口同时有短句和长句"
            f"（目标 ≥{band('window_pass', MIN_WINDOW_PASS):.0%}）"
        )
        for f in r.get("window_fails", [])[:3]:
            soft.append(
                f"　└ 第 {f['no']} 个窗口（约 {f['chars']} 字）缺{f['need']}："
                f"从「{f['head']}…」这一段附近补"
            )
        if len(r.get("window_fails", [])) > 3:
            soft.append(f"　└ 另有 {len(r['window_fails']) - 3} 个窗口不合格，同上处理")
    if r["dialogue_tag_ratio"] > band("tag_ratio", MAX_TAG_RATIO):
        soft.append(
            f"对话标签过多：占对话行 {r['dialogue_tag_ratio']:.0%} > {band('tag_ratio', MAX_TAG_RATIO):.0%}"
            "（删到 30% 以下，标签用 说/道/问）"
        )
    sp_lo, sp_hi = band("short_para_min", MIN_SHORT_PARA), band("short_para_max", MAX_SHORT_PARA)
    if r["single_para_ratio"] < sp_lo or r["single_para_ratio"] > sp_hi:
        soft.append(f"单句段占比 {r['single_para_ratio']:.0%} 不在 {sp_lo:.0%}-{sp_hi:.0%} 区间")
    if r["para_over_soft"]:
        soft.append(f"{r['para_over_soft']} 段超过 {SOFT_PARA_CHARS} 字（建议上限）")
    if r["le_per_para"] > band("le_per_para", MAX_LE_PER_PARA):
        soft.append(f"每段平均 {r['le_per_para']} 个「了」 > {band('le_per_para', MAX_LE_PER_PARA)}（另 {r['le_over_3']} 段单段超 3 个）")
    if r.get("le_run_max", 0) >= LE_RUN_SOFT:
        soft.append(
            f"连续 {r['le_run_max']} 句都带「了」（门槛 {LE_RUN_SOFT} 句）——读起来黏，"
            "挑其中几句去掉「了」或换说法"
        )
        if r.get("le_run_where"):
            soft.append(f"　└ {r['le_run_where']}")
    if r.get("symmetric_hits"):
        soft.append(
            "公式化对称句式：" + "；".join(r["symmetric_hits"][:3])
            + " → 中文 AI 腔的强特征，改成直陈（只提示：偶尔用一次是修辞）"
        )
    if r["emotion_words"] > band("emotion", SOFT_EMOTION):
        soft.append(f"情绪词 {r['emotion_words']} 次 > {band('emotion', SOFT_EMOTION)}（改成行为）")
    if r["weak_adverbs_per_1k"] > band("weak_adverbs_per_1k", MAX_WEAK_PER_1K):
        soft.append(f"弱化副词 {r['weak_adverbs_per_1k']}/千字 > {band('weak_adverbs_per_1k', MAX_WEAK_PER_1K)}（似乎/好像/大概…要么确定要么删）")
    if r["connectives_per_1k"] > band("connectives_per_1k", MAX_CONNECTIVE_PER_1K):
        soft.append(f"连接词 {r['connectives_per_1k']}/千字 > {band('connectives_per_1k', MAX_CONNECTIVE_PER_1K)}")

    if baseline:
        base_chars = baseline.get("chars")
        if isinstance(base_chars, (int, float)) and base_chars:
            if abs(r["chars"] - base_chars) / base_chars > DRIFT_LENGTH_TOLERANCE:
                drift.append(
                    f"本章 {r['chars']} 字与基线均值 {round(base_chars)} 字差异较大，"
                    "漂移结论仅供参考"
                )
        suppressed: list[str] = []
        for k in BASELINE_HIGHER_BETTER:
            b = baseline.get(k)
            if not (isinstance(b, (int, float)) and b):
                continue
            cur = r.get(k, 0)
            floor = DRIFT_MIN_ABS.get(k, 0.0)
            delta = round(b - cur, 6)      # 先取整再比，避免 0.01 < 0.01 这种浮点自相矛盾
            if cur < b * (1 - DRIFT_TOLERANCE):
                if delta >= floor:
                    drift.append(f"{k} 从基线 {b} 掉到 {cur}")
                elif explain_drift:
                    suppressed.append(f"{k} 掉了 {round(delta, 3)}（绝对下限 {floor}）")
        for k in BASELINE_LOWER_BETTER:
            b = baseline.get(k)
            if not (isinstance(b, (int, float)) and b):
                continue
            cur = r.get(k, 0)
            floor = DRIFT_MIN_ABS.get(k, 0.0)
            delta = round(cur - b, 6)
            if cur > b * (1 + DRIFT_TOLERANCE):
                if delta >= floor:
                    drift.append(f"{k} 从基线 {b} 升到 {cur}")
                elif explain_drift:
                    suppressed.append(f"{k} 升了 {round(delta, 3)}（绝对下限 {floor}）")
        # 被下限挡住的波动默认不打印：每章都印一行说明，本身就是新的噪音
        # （B012 的教训——一条你学会忽略的告警会掩盖下一个真 bug）。要排查时加 --explain-drift。
        if suppressed and explain_drift:
            drift.append(
                "　└ 以下波动在绝对下限内、不算漂移：" + "；".join(suppressed[:3])
                + ("…" if len(suppressed) > 3 else "")
            )

    return hard, soft, drift


BASELINE_KEYS = BASELINE_HIGHER_BETTER + BASELINE_LOWER_BETTER + ["chars", "sentences"]

# 风格贴合（--style-from）：拿一段参照文本当「要像它」的目标，逐项比对本章。
# 与漂移的区别是**单边 vs 双边**：漂移只看"比基线变差了没有"，贴合是"像不像它" ——
# 写得比参照长/密/花，一样是不像。口径借鉴 InkOS 的 style import：
# 它把参照文本编译成**可操作的**风格指南，而不是一段形容词描述。
STYLE_MATCH_KEYS = [
    "mean_len", "burst_cv", "para_cv", "mattr", "tri_rep", "punct_cv",
    "single_para_ratio", "window_pass", "dialogue_tag_ratio",
    "le_per_para", "connectives_per_1k", "weak_adverbs_per_1k",
]
STYLE_MATCH_LABELS = {
    "mean_len": "平均句长", "burst_cv": "句长波动", "para_cv": "段长波动",
    "mattr": "词汇丰富度", "tri_rep": "3-gram 重复", "punct_cv": "标点分布",
    "single_para_ratio": "单句段占比", "window_pass": "局部节奏达标率",
    "dialogue_tag_ratio": "对话标签率", "le_per_para": "每段「了」",
    "connectives_per_1k": "连接词/千字", "weak_adverbs_per_1k": "弱化副词/千字",
}
STYLE_MATCH_TOL = 0.20   # 双边容差：偏离参照 20% 就算不像

# 改稿对照（--compare 旧稿）：改稿护栏。
# 起因（口径借鉴 InkOS 的实测教训）：**整段重写不一定变好**——他们实测 rewrite 一次，
# AI 标记词涨了 6 倍，于是定了硬规矩：改稿后比对标记数，**变多就弃稿留原文**。
# 我们原本只有"最小化改动百分比"（删除量上限），缺这道"改完回头看"的闸门。
REVISION_COMPARE_KEYS = [
    "mean_len", "burst_cv", "mattr", "tri_rep", "para_cv",
    "weak_adverbs_per_1k", "connectives_per_1k", "single_para_ratio",
]
REVISION_CHAR_TOL = 0.10   # 与 anti-ai-revision.md 的"字数 ≤±10%"一致
REVISION_COMPARE_LABELS = {
    "mean_len": "平均句长", "burst_cv": "句长波动", "mattr": "词汇丰富度",
    "tri_rep": "3-gram 重复", "para_cv": "段长波动", "weak_adverbs_per_1k": "弱化副词/千字",
    "connectives_per_1k": "连接词/千字", "single_para_ratio": "单句段占比",
}


def render(name: str, r: dict, hard: list[str], soft: list[str], drift: list[str]) -> str:
    lines = [
        f"=== {name} ===",
        f"字数 {r['chars']}｜句 {r['sentences']}｜段 {r['paragraphs']}"
        f"｜平均句长 {r['mean_len']}｜σ² {r['var_len']}｜BurstCV {r['burst_cv']}",
        f"短句(≤5) {r['short_sentences']}｜长句(≥40) {r['long_sentences']}"
        f"｜200字窗口达标 {r['window_pass']:.0%}（{r['windows']} 窗）"
        f"｜最长同长度连句 {r['same_len_run']}",
        f"Pσ² {r['para_var']}｜ParaCV {r['para_cv']}｜单句段 {r['single_para_ratio']:.0%}"
        f"｜MATTR {r['mattr']}｜3gram重复 {r['tri_rep']}｜标点CV {r['punct_cv']}",
        f"对话行 {r['dialogue_lines']}｜标签率 {r['dialogue_tag_ratio']:.0%}"
        f"｜最长纯对话连行 {r['dialogue_run']}｜情绪词 {r['emotion_words']}"
        f"｜连接词 {r['connectives_per_1k']}/千字｜弱化副词 {r['weak_adverbs_per_1k']}/千字",
    ]
    if r.get("forbidden_hits"):
        lines.append("禁止矛盾：" + "、".join(f"{w}×{c}" for w, c in sorted(r["forbidden_hits"].items())))
    if hard:
        lines.append("必须改：")
        lines += [f"  - {x}" for x in hard]
    else:
        lines.append("必须改：无")
    if soft:
        lines.append("参考区间外（按需改）：")
        lines += [f"  - {x}" for x in soft]
    if drift:
        lines.append("与本书基线漂移：")
        lines += [f"  - {x}" for x in drift]
    return "\n".join(lines)


def write_atomic(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件，再 `os.replace` 覆盖过去。

    **为什么必须有**：这些产物（`风格基线.json`、白名单、`--json` 报告）**写坏会污染之后每一次检查**
    —— 比如基线 JSON 断在半截，之后所有漂移判断要么崩、要么给出假结论，而且很难查。
    口径借鉴 InkOS 的 `commitAtomicFileSet`（它们做的是"原子文件集"，我们单文件原子就够）。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_declared_characters(node_path: Path | None) -> list[str]:
    """读章节点文件的 `characters:` 字段——"谁在本章出现"（给 series_audit 算支线出勤用）。"""
    if not node_path or not node_path.is_file():
        return []
    text = node_path.read_text(encoding="utf-8-sig", errors="replace")
    m = re.search(r"^\s*characters\s*[:：]\s*(.+)$", text, re.M)
    if not m:
        return []
    return [x.strip() for x in re.split(r"[,，、;；/]", m.group(1)) if x.strip()]


def check_fingerprint_coverage(declared: list[str],
                              passed: dict[str, list[str]]) -> list[str]:
    """节点声明本章出场、却没传档案的角色 —— 声纹层会静默漏掉他。

    这是 B003 的同类风险：声纹只有在"传了档案"时才判，忘传就只是一条软提示，
    整个"读者认不认得出他是他"这层就没了。反过来传了没出场的人，通常是节点漏填。
    只提示，不判必须改 —— 因为"出场"不等于"需要声纹"（纯叙述路过不必传）。
    """
    if not declared:
        return []
    out: list[str] = []
    missing = [n for n in declared if n not in passed]
    if missing:
        out.append(
            "声纹覆盖：节点 characters 里列了 " + "、".join(missing)
            + " 却没传他的档案 → 这章他有台词/有戏就该传，纯叙述路过则不用"
        )
    extra = [n for n in passed if n not in declared]
    if extra:
        out.append(
            "声纹覆盖：传了 " + "、".join(extra)
            + " 的档案，但节点 characters 里没列他出场 → 要么节点漏填，要么本章没他"
        )
    return out


def compare_style(target: dict, current: dict) -> list[dict]:
    """逐项比对本章与参照文本的风格指标。

    **双边**容差：比参照高或低同样算不像（这与"漂移"的单边判断是两回事）。
    """
    rows: list[dict] = []
    for k in STYLE_MATCH_KEYS:
        t, c = target.get(k), current.get(k)
        if not isinstance(t, (int, float)) or not isinstance(c, (int, float)) or t == 0:
            continue
        rel = (c - t) / t
        rows.append({
            "key": k, "label": STYLE_MATCH_LABELS.get(k, k),
            "target": t, "current": c, "rel": round(rel, 3),
            "ok": abs(rel) <= STYLE_MATCH_TOL,
        })
    return rows


def render_style_match(ref_name: str, rows: list[dict]) -> str:
    if not rows:
        return ""
    hit = sum(1 for r in rows if r["ok"])
    lines = [
        f"=== 风格贴合（参照 {ref_name}）===",
        f"贴合度 {hit}/{len(rows)} 项在 ±{STYLE_MATCH_TOL:.0%} 内（双边：偏高偏低都算不像）",
    ]
    off = sorted((r for r in rows if not r["ok"]), key=lambda x: -abs(x["rel"]))
    if off:
        lines.append("偏离项（按幅度排序，照这个改）：")
        for r in off[:6]:
            lines.append(
                f"  - {r['label']}：参照 {r['target']} ｜ 本章 {r['current']}"
                f"（{'高' if r['rel'] > 0 else '低'} {abs(r['rel']):.0%}）"
            )
        if len(off) > 6:
            lines.append(f"  - 另有 {len(off) - 6} 项偏离，同上处理")
    else:
        lines.append("偏离项：无")
    return "\n".join(lines)


def ai_mark_total(r: dict) -> int:
    """"AI 标记"总数：黑名单命中词次 + 超额配额词次。

    这是改稿护栏的尺子——它只看"像 AI 的痕迹有多少"，不看好坏、不看文笔。
    """
    marks = sum((r.get("blacklist_hits") or {}).values())
    marks += sum(c - cap for c, cap in (r.get("quota_exceeded") or {}).values())
    marks += sum(round(c - cap) for c, cap in (r.get("quota_per_500_exceeded") or {}).values())
    return marks


def render_revision_compare(old_name: str, old_r: dict, new_r: dict,
                            old_hard: list[str], new_hard: list[str]) -> str:
    """改稿前后的对照 + 护栏结论（变差就明确说"保留原文"）。"""
    om, nm = ai_mark_total(old_r), ai_mark_total(new_r)
    oh, nh = len(old_hard), len(new_hard)
    oc, nc = old_r.get("chars", 0), new_r.get("chars", 0)
    delta = (nc - oc) / oc if oc else 0.0

    lines = [f"=== 改稿对照（旧稿 {old_name} → 本章）==="]
    lines.append(f"AI 标记 {om} → {nm}｜必须改条目 {oh} → {nh}")
    lines.append(
        f"字数 {oc} → {nc}（{delta:+.1%}{'，超出 ±10% 的最小化改动范围' if abs(delta) > REVISION_CHAR_TOL else ''}）"
    )
    moved = []
    for k in REVISION_COMPARE_KEYS:
        o, n = old_r.get(k), new_r.get(k)
        if not isinstance(o, (int, float)) or not isinstance(n, (int, float)) or not o:
            continue
        rel = (n - o) / o
        if abs(rel) >= 0.10:
            moved.append(f"{REVISION_COMPARE_LABELS.get(k, k)} {o} → {n}（{rel:+.0%}）")
    if moved:
        lines.append("变动 ≥10% 的指标：" + "；".join(moved))
    else:
        lines.append("变动 ≥10% 的指标：无（这一轮改得很轻）")

    if nm > om or nh > oh:
        lines.append(
            "结论：**改稿变差了**（AI 标记或必须改条目增加）→ 按护栏规矩："
            "**保留旧稿**，不要在这条路上继续改；换手法（见 `references/anti-ai-revision.md` 的优先级）"
        )
    elif nm < om or nh < oh:
        lines.append("结论：**改稿有改善**，保留新稿")
    else:
        lines.append(
            "结论：**净变化为零** —— 若这一轮的目标是降 AI 味，说明手法没起作用；"
            "别在同一处反复微调，直接整段重写（换切入点/语序/观察点）"
        )
    return "\n".join(lines)


def collect_targets(paths: list[str], dirs: list[str], recursive: bool) -> list[Path]:
    targets = [Path(p) for p in paths]
    for d in dirs:
        dp = Path(d)
        if not dp.is_dir():
            print(f"错误：目录不存在：{dp}", file=sys.stderr)
            continue
        targets += sorted(p for p in (dp.rglob("*.md") if recursive else dp.glob("*.md")) if p.is_file())
    return [p for p in targets if p.is_file()]


def find_forbidden_near(start_dirs: list[Path]) -> Path | None:
    """按项目布局找回 world/世界观.md：从待审章节所在目录逐级往上找。

    为什么需要它：默认路径 `novel/world/世界观.md` 是 **CWD 相对**的。
    在 `novel/` 里跑（很自然的用法）就会找不到，于是**红线扫描整段跳过**——
    而报告里那句"必须改：无"读起来像"全都查过了"。实测踩过一次：
    从 novel/ 跑三章，三章都少查了 11 条禁止矛盾，输出里只有一行不显眼的提示。
    """
    seen: set[Path] = set()
    for d in start_dirs:
        try:
            d = d.resolve()
        except OSError:
            continue
        for base in [d, *d.parents]:
            if base in seen:
                continue
            seen.add(base)
            for rel in (("world", "世界观.md"), ("novel", "world", "世界观.md")):
                cand = base.joinpath(*rel)
                if cand.is_file():
                    return cand
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本地 AI 味预筛（代理指标，不是朱雀）")
    ap.add_argument("paths", nargs="*", help="要检查的章节文件")
    ap.add_argument("--dir", action="append", default=[], help="检查该目录下的 .md（可重复）")
    ap.add_argument("--recursive", action="store_true", help="--dir 递归子目录")
    ap.add_argument("--blacklist", help="黑名单 markdown（提取反引号内的词）")
    ap.add_argument("--forbidden", help="世界观文件，提取「禁止矛盾清单」里的反引号短句；"
                                        "默认读 novel/world/世界观.md（存在时）")
    ap.add_argument("--no-forbidden", action="store_true", help="不做禁止矛盾扫描")
    ap.add_argument("--fingerprint", action="append", default=[],
                    help="角色档案，读 fingerprint 块（可重复）。"
                         "契约：传几个 = 声明这几个角色本章出场，每个都要各自命中 ≥%d 次；"
                         "某角色本章不出场就不要传他。传了却 0 次命中判为必须改" % FINGERPRINT_MIN)
    ap.add_argument("--json", dest="json_out", help="把完整结果写入该 JSON 文件")
    ap.add_argument("--write-baseline", dest="baseline_out",
                    help=f"把本次结果写成风格基线（要求 ≥{BASELINE_MIN_CHAPTERS} 章）")
    ap.add_argument("--calibrate", action="store_true",
                    help="等同 --write-baseline novel/state/风格基线.json")
    ap.add_argument("--force-baseline", action="store_true", help="章数不足也写基线")
    ap.add_argument("--baseline", help="与之前写出的基线 JSON 比较，报告漂移")
    ap.add_argument("--node", help="本章节点文件：读 characters 字段，核对声纹该传谁没传")
    ap.add_argument("--style-from", dest="style_from",
                    help="参照文本：拿它当「要像它」的风格目标，逐项比对本章（双边容差）")
    ap.add_argument("--compare", dest="compare",
                    help="旧稿：对比改稿前后（AI 标记/必须改条目/关键指标），变差就提示保留原文")
    ap.add_argument("--explain-drift", action="store_true",
                    help="漂移里也被打印『被绝对下限挡住的小波动』，排查用；默认不打印以免刷噪音")
    ap.add_argument("--strict", action="store_true", help="存在必须改的问题时以退出码 1 结束")
    args = ap.parse_args(argv)

    targets = collect_targets(args.paths, args.dir, args.recursive)
    if not targets:
        print("错误：没有可检查的文件。给出文件路径或 --dir <目录>。", file=sys.stderr)
        return 2

    blacklist = load_blacklist(Path(args.blacklist) if args.blacklist else None)
    fingerprints = load_fingerprints([Path(p) for p in args.fingerprint])
    declared_chars = load_declared_characters(Path(args.node) if args.node else None)

    style_ref = None
    style_ref_name = ""
    if args.style_from:
        rp = Path(args.style_from)
        if not rp.is_file():
            print(f"错误：参照文本不存在：{rp}", file=sys.stderr)
            return 2
        style_ref_name = rp.name
        style_ref = analyse(rp.read_text(encoding="utf-8-sig", errors="replace"), blacklist)
        if style_ref["chars"] < MIN_CHARS_FOR_STATS:
            print(
                f"警告：参照文本只有 {style_ref['chars']} 字（<{MIN_CHARS_FOR_STATS}），"
                "统计指纹不可靠 —— 建议拿整章作参照\n",
                file=sys.stderr,
            )
    forbidden: list[str] = []
    if not args.no_forbidden:
        if args.forbidden:
            fp_path = Path(args.forbidden)
        else:
            fp_path = Path("novel/world/世界观.md")
            if not fp_path.is_file():
                # 默认路径找不到时按项目布局回退，别让红线扫描悄悄消失
                starts = [Path(p).parent for p in args.paths]
                starts += [Path(d) for d in args.dir]
                found = find_forbidden_near(starts or [Path.cwd()])
                if found is not None:
                    print(f"提示：默认路径 {fp_path} 不在当前目录（CWD 相对），"
                          f"已按项目布局改用 {found}\n")
                    fp_path = found
        if fp_path.is_file():
            forbidden = load_forbidden(fp_path)
            if forbidden:
                print(f"禁止矛盾清单：从 {fp_path} 读到 {len(forbidden)} 条\n")
            else:
                print(
                    f"提示：{fp_path} 里的「禁止矛盾清单」还是空的，这一项等于没查"
                    "——照模板填几条（反引号包起来的短句）\n"
                )
        else:
            # 静默跳过会给人"红线已经查过了"的错觉，所以明确说一声
            print(
                f"提示：没找到 {fp_path}，禁止矛盾扫描已跳过"
                f"（换个目录跑时用 --forbidden 指定路径，或加 --no-forbidden 关掉这条提示）\n"
            )
    check_fp = bool(fingerprints)

    baseline = None
    bands = None
    if args.baseline:
        bp = Path(args.baseline)
        if bp.is_file():
            data = json.loads(bp.read_text(encoding="utf-8-sig"))
            baseline = data.get("baseline")
            bands = data.get("bands")
        else:
            print(f"警告：基线文件不存在，忽略：{bp}", file=sys.stderr)

    compare_ref = None
    compare_name = ""
    compare_old_hard: list[str] = []
    if args.compare:
        cp = Path(args.compare)
        if not cp.is_file():
            print(f"错误：旧稿不存在：{cp}", file=sys.stderr)
            return 2
        compare_name = cp.name
        compare_ref = analyse(cp.read_text(encoding="utf-8-sig", errors="replace"),
                              blacklist, forbidden, fingerprints)
        compare_old_hard, _, _ = judge(compare_ref, baseline, False, bands)

    results = {}
    failed = False
    for path in targets:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        r = analyse(text, blacklist, forbidden, fingerprints)
        hard, soft, drift = judge(r, baseline, check_fp, bands, args.explain_drift)
        soft += check_fingerprint_coverage(declared_chars, fingerprints)
        results[str(path)] = {"metrics": r, "hard": hard, "soft": soft, "drift": drift}
        print(render(str(path), r, hard, soft, drift))
        if style_ref is not None:
            style_rows = compare_style(style_ref, r)
            results[str(path)]["style_match"] = style_rows
            print()
            print(render_style_match(style_ref_name, style_rows))
        if compare_ref is not None:
            results[str(path)]["revision_compare"] = {
                "old_name": compare_name,
                "old_ai_marks": ai_mark_total(compare_ref),
                "new_ai_marks": ai_mark_total(r),
                "old_hard": len(compare_old_hard),
                "new_hard": len(hard),
            }
            print()
            print(render_revision_compare(compare_name, compare_ref, r, compare_old_hard, hard))
        print()
        failed = failed or bool(hard)

    if args.calibrate or args.baseline_out:
        out = args.baseline_out or "novel/state/风格基线.json"
        if len(targets) < BASELINE_MIN_CHAPTERS and not args.force_baseline:
            print(
                f"拒绝写基线：只给了 {len(targets)} 章，至少需要 {BASELINE_MIN_CHAPTERS} 章"
                f"（要强行写加 --force-baseline）",
                file=sys.stderr,
            )
            return 2
        base = {}
        for k in BASELINE_KEYS:
            mean = statistics.fmean([results[str(p)]["metrics"][k] for p in targets])
            base[k] = int(round(mean)) if k in ("chars", "sentences") else round(mean, 3)
        # 重建基线时保留作者手写的本书区间豁免（bands），别把它冲掉
        kept = {}
        outp = Path(out)
        if outp.is_file():
            try:
                kept = json.loads(outp.read_text(encoding="utf-8-sig")).get("bands") or {}
            except Exception:
                kept = {}
        payload = {"baseline": base, "chapters": len(targets)}
        if kept:
            payload["bands"] = kept
        outp.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(outp, json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"已建立本书基线（{len(targets)} 章）：{out}"
              + (f"；保留了 bands 豁免 {sorted(kept)}" if kept else ""))
        print("以后用 --baseline 该文件检查风格漂移。")

    print("说明：以上是本地代理指标，不是朱雀结果。")
    print("朱雀实测：python zhuque_check.py <同一个文件>　（官方接口；过线标准 人工率 ≥95%）")
    print("没有密钥时用网页版：https://matrix.tencent.com/ai-detect")

    if args.json_out:
        write_atomic(
            Path(args.json_out),
            json.dumps({"files": results, "note": "proxy metrics, not Zhuque"},
                       ensure_ascii=False, indent=2),
        )
        print(f"已写入 {args.json_out}")

    return 1 if (failed and args.strict) else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
