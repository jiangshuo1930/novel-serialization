#!/usr/bin/env python3
"""审查报告的引文核验闸门。

**为什么需要它**：`style_audit` / `series_audit` / `continuity_check` 报出的落点是脚本自己
从正文里算出来的，不会编造；但**审查报告是模型写的**。模型写报告时最容易出的错，
就是"引用了正文里根本没有的原文"——一条编造的引文会让作者按错的方向去改稿。

所以：报告里凡是 `「…」`、`『…』`、`“…”`、`"…"` 引起来的片段，都必须能在指定章节里字面搜到
（忽略空白差异）。搜不到就说明这条"证据"不存在。
**英文直引号只认成对的**（直引号兼作撇号与英寸符号，不成对就会把技术内容当引文）。

口径借鉴 InkOS 的审稿契约：每条观察都要带 sourceRefs，quote 必须精确命中源文本，
否则报 `REVIEW_SOURCE_REQUIRED` / `REVIEW_SOURCE_MISMATCH`（见 references/skill-maintenance.md）。

用法：
    python quote_check.py 报告.md --chapter novel/chapters/第13章-洗出来的照片.md
    python quote_check.py 报告.md --chapter-dir novel/chapters      # 只要有一篇命中就算过
    python quote_check.py 报告.md --chapter <章节> --min-len 8 --json out.json

退出码：0 = 全部引文都能在正文里搜到；1 = 有搜不到的（**报告不能作为改稿依据**）；2 = 用法错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 报告里常见的四种引号。
# 最后一种是英文直引号 `"…"`，**只认成对的**：直引号在英文与代码里兼作撇号、英寸符号
# （`can't`、`3"`），不要求成对就会把技术内容当引文，制造假阳性（B017 的教训）。
# 加了它之后，用 `"…"` 写报告的情况不再是盲区；而只出现一个直引号时依旧抽不到——
# 那种情况会落到「无法核对」分支，不会假装通过（B042）。
QUOTE_PATTERNS = [
    re.compile(r"「([^」]{2,200})」"),
    re.compile(r"『([^』]{2,200})』"),
    re.compile(r"“([^”]{2,200})”"),
    re.compile(r'"([^"\n]{2,200})"'),
]
MIN_LEN_DEFAULT = 6          # 太短的引文（"他不知道"）不判——它可能是泛指，不是引用
SKIP_MARKERS = ("http://", "https://", ".py", ".md", "--", "python ", "novel/")

WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """去掉所有空白：报告里的换行、缩进不该影响"能不能搜到"的判断。"""
    return WS_RE.sub("", text or "")


def extract_quotes(report_text: str, min_len: int = MIN_LEN_DEFAULT) -> list[str]:
    """抽出报告里所有"像在引用原文"的片段，按出现顺序、去重。"""
    found: list[str] = []
    seen: set[str] = set()
    for pat in QUOTE_PATTERNS:
        for m in pat.finditer(report_text):
            raw = m.group(1).strip()
            if len(normalize(raw)) < min_len:
                continue
            if any(marker in raw for marker in SKIP_MARKERS):
                continue                      # 命令行/路径/URL 不是引文
            key = normalize(raw)
            if key in seen:
                continue
            seen.add(key)
            found.append(raw)
    return found


def load_source(files: list[Path]) -> str:
    return normalize("".join(f.read_text(encoding="utf-8-sig", errors="replace") for f in files))


def check_quotes(quotes: list[str], source: str) -> tuple[list[str], list[str]]:
    """返回 (能搜到的, 搜不到的)。"""
    ok: list[str] = []
    bad: list[str] = []
    for q in quotes:
        (ok if normalize(q) in source else bad).append(q)
    return ok, bad


def write_atomic(path: Path, text: str) -> None:
    """原子写：先写临时文件再 `os.replace`（口径同其它脚本，见 skill-changelog 的原子写盘条目）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def collect_chapters(chapter: str | None, chapter_dir: str | None) -> list[Path]:
    files: list[Path] = []
    if chapter:
        p = Path(chapter)
        if p.is_file():
            files.append(p)
    if chapter_dir:
        d = Path(chapter_dir)
        if d.is_dir():
            files.extend(sorted(d.rglob("*.md")))
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="核验审查报告里的引文是否真的出自正文")
    ap.add_argument("report", help="审查报告（markdown）")
    ap.add_argument("--chapter", action="append", default=[], help="正文文件（可重复）")
    ap.add_argument("--chapter-dir", help="正文目录：任一章节命中即算通过")
    ap.add_argument("--min-len", type=int, default=MIN_LEN_DEFAULT,
                    help=f"引文短于这个长度不判（默认 {MIN_LEN_DEFAULT}；太短的可能是泛指）")
    ap.add_argument("--json", dest="json_out", help="把结果写成 JSON")
    args = ap.parse_args(argv)

    rp = Path(args.report)
    if not rp.is_file():
        print(f"错误：报告不存在：{rp}", file=sys.stderr)
        return 2
    chapters = collect_chapters(None, args.chapter_dir) if args.chapter_dir else []
    for c in args.chapter:
        p = Path(c)
        if not p.is_file():
            print(f"错误：章节不存在：{p}", file=sys.stderr)
            return 2
        chapters.append(p)
    if not chapters:
        print("错误：至少要给一个 --chapter 或一个 --chapter-dir", file=sys.stderr)
        return 2

    quotes = extract_quotes(rp.read_text(encoding="utf-8-sig", errors="replace"), args.min_len)
    source = load_source(chapters)
    ok, bad = check_quotes(quotes, source)

    print(f"=== 引文核验（报告 {rp.name}）===")
    print(f"核验章节 {len(chapters)} 篇｜抽出引文 {len(quotes)} 条（≥{args.min_len} 字）"
          f"｜搜到 {len(ok)}｜**搜不到 {len(bad)}**")
    if bad:
        print()
        print("搜不到的引文（**这些「证据」在正文里不存在，报告不能作为改稿依据**）：")
        for q in bad[:10]:
            print(f"  - 「{q}」")
        if len(bad) > 10:
            print(f"  - 另有 {len(bad) - 10} 条同上")
        print()
        print("怎么处理：要么回正文把原句抄准，要么删掉这条观察——**不要按一条不存在的引文去改稿**。")
    elif not quotes:
        # 空结果不等于合格。一条引文都没抽出来时，这道闸门**什么都没核**：
        # 报"全部引文都能搜到 ✓"会读成"引文都验过了"，而实际上报告可能通篇没有引文、
        # 或者用的是 `「」`/`“”` 之外的引号。技能自己的审查纪律要求这种情形报
        # 「无法核对」，不许报「通过」——所以这里必须单独说清楚。
        print()
        print("**无法核对：一条引文都没抽到，这道闸门什么都没验。**")
        print(f"  报告里需要出现用「」/『』/“”/成对英文直引号包起来、且不短于 {args.min_len} 字的原文片段。")
        print("  如果报告确实引了原文却抽不到，注意英文直引号必须**成对**"
              "（不成对的会被当成撇号/英寸符号，故意不认）——"
              "或者这份报告根本没有可核验的证据。")
        print("  **这不等于「引文都对」**，别把这一行读成通过。")
    else:
        print("全部引文都能在正文里字面搜到 ✓")

    if args.json_out:
        write_atomic(
            Path(args.json_out),
            json.dumps({"report": str(rp), "chapters": [str(c) for c in chapters],
                        "quotes": len(quotes), "ok": len(ok), "missing": bad},
                       ensure_ascii=False, indent=2))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
