#!/usr/bin/env python3
"""开发期压测：在**百万字规模**上验证三个脚本不崩、不变慢。

**这不是每章要跑的检查**——它是改完脚本之后自己跑一次的回归闸门
（口径依据：政策 P005 把单本规模定为 ≤100 万字 ≈ 455 章）。

做三件事：
1. **造一个合成项目**（默认 455 章 / 每章 2200 字 ≈ 100 万字，"脚本口径"字数）；
2. **在进程内计时**跑完整流程（不 spawn 子进程，所以不依赖管道/沙箱）；
   每段超过 `--max-seconds` 就判失败（默认 20 秒，留足余量）；
3. **跑边界模糊测试**：空文件、重复表头、缺列行、None 章号、空目录、target=0…
   任何一个用例抛异常就判失败。

为什么值得留着：它是**唯一能验证 P005 那条规模假设**的东西。
别的测试都在小样本上跑，跑不出"455 章时某处变成 O(n²) 或每章正文被读两遍"这类问题
（B032 就是这么发现 series_audit 慢 3.5 倍的）。

用法：
    python stress_test.py                      # 默认 455 章
    python stress_test.py --chapters 50        # 快速自检用
    python stress_test.py --keep               # 保留合成项目，便于手工排查

退出码：0 = 全部通过；1 = 有失败（计时超限或有用例崩溃）。
"""

from __future__ import annotations

import argparse
import pathlib
import random
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import continuity_check  # noqa: E402
import quote_check  # noqa: E402
import series_audit  # noqa: E402
import style_audit  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

VOCAB = ["柜子", "名单", "值班员", "照片", "走廊", "纸人", "签字", "冷", "门", "灯",
         "手", "雨", "账", "单子", "名字", "烟", "钥匙", "抽屉", "钟", "脚印"]
STRIP_RE = re.compile(r"[^\w]", re.UNICODE)


def build_project(root: pathlib.Path, chapters: int, per_chapter: int) -> int:
    """造一个结构完整的合成项目，返回总字数（脚本口径）。"""
    rnd = random.Random(7)          # 固定种子：每次跑的数字可比

    def sentence() -> str:
        return "".join(rnd.choice(VOCAB) for _ in range(rnd.randint(2, 7))) + rnd.choice("。！？")

    for d in ("chapters", "plot", "outline", "characters", "state", "foreshadowing"):
        (root / d).mkdir(parents=True, exist_ok=True)

    total = 0
    for i in range(1, chapters + 1):
        body, n = [f"# 第{i}章-第{i}夜"], 0
        while n < per_chapter:
            s = sentence()
            body.append(s)
            n += len(STRIP_RE.sub("", s))
        (root / "chapters" / f"第{i}章-第{i}夜.md").write_text(
            "\n\n".join(body) + "\n", encoding="utf-8")
        total += n
        (root / "plot" / f"节点-第{i:04d}章.md").write_text(
            f"---\nchapter: {i}\ntitle: 第{i}夜\ndate: 第{i}天\nhook_type: 悬念\n"
            f"opening_type: 对话\npayoff: 小爽点\npayoff_strength: 3\nemotion: 紧张\n"
            f"strand: 主线\naxes: 剧情, 关系\ncharacters: 甲, 乙\nsubplots: 支线甲\n"
            f"words: {per_chapter}\n---\n\n## 场景卡（2-4 张）\n\n"
            f"| # | 场景目标 | 冲突 | 结果类型 | 后续 |\n| --- | --- | --- | --- | --- |\n"
            f"| 1 | 目标 | 冲突 | `yes_but` |  |\n\n"
            f"## 本章节点（写完后填）\n\n- 事件：第{i}夜的事\n- 人物状态变化：无\n"
            f"- 结尾最后 200 字：占位\n- 下一章必须记住的事：占位\n", encoding="utf-8")

    (root / "characters" / "主角-甲.md").write_text("# 甲\n", encoding="utf-8")
    (root / "outline" / "卷纲-第1卷.md").write_text(
        "# 卷纲\n\n## 卷节拍表（六段）\n\n| # | 节拍 | 内容 | 章号 |\n| --- | --- | --- | --- |\n"
        + "".join(f"| {k} | 节拍{k} | x | 第 {k * (chapters // 6 + 1)} 章 |\n"
                  for k in range(1, 7)), encoding="utf-8")
    (root / "foreshadowing" / "伏笔表.md").write_text(
        "| 编号 | 内容 | 类型 | 层级 | 标签 | 埋设章 | 计划回收章 | 状态 | 最近推进章 | 备注 |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        + "".join(f"| F{k:03d} | 伏笔{k} | 主线 | 核心 | 身份 | 第1章 | 第999章 | 推进中 | 第{k}章 |  |\n"
                  for k in range(1, 6))
        + "\n## 回收记录\n\n| 编号 | 内容 | 埋设章 | 回收章 | 回收方式 |\n"
          "| --- | --- | --- | --- | --- |\n|  |  |  |  |  |\n", encoding="utf-8")
    (root / "state" / "连续性白名单.md").write_text("# 白名单\n\n甲说了那句话\n", encoding="utf-8")
    return total


def time_stages(root: pathlib.Path, blacklist: pathlib.Path, target_words: int):
    """在进程内跑完整流程并计时。返回 [(阶段名, 秒数)]。"""
    out: list[tuple[str, float]] = []

    def stage(name, fn):
        t0 = time.perf_counter()
        fn()
        out.append((name, time.perf_counter() - t0))

    plot, chdir, fs = root / "plot", root / "chapters", root / "foreshadowing" / "伏笔表.md"

    nodes = series_audit.load_nodes(plot)
    cur = max((n["_chapter"] for n in nodes if n["_chapter"] is not None), default=0)
    rows = series_audit.parse_foreshadowing(fs)
    measured: dict = {}

    stage("series_audit：节点与结构检查",
          lambda: series_audit.check_nodes(nodes))
    stage("series_audit：伏笔全部检查",
          lambda: (series_audit.check_foreshadowing(rows, cur, {n["_chapter"] for n in nodes}),
                   series_audit.foreshadowing_table_info(fs),
                   series_audit.check_foreshadow_consistency(
                       rows, series_audit.parse_foreshadow_archive(fs)),
                   series_audit.check_recovery_declared(rows, fs)))
    stage("series_audit：字数下限 + 遥测（共用缓存）",
          lambda: (series_audit.check_word_floor(nodes, chdir, target_words, 0.9, measured),
                   series_audit.word_length_stats(nodes, chdir, target_words, 0.9, measured)))
    stage("series_audit：方向跑偏三项 + 未知字段",
          lambda: (series_audit.check_direction_drift(nodes, root),
                   series_audit.check_volume_beats(nodes, root, cur),
                   series_audit.check_outline_reconciliation(nodes),
                   series_audit.check_unknown_fields(nodes, fs)))

    chapters = continuity_check.load_chapters(chdir)
    nd = continuity_check.load_nodes(plot)
    sents = {c: continuity_check.split_sentences(chapters[c][1]) for c in chapters}
    allow = continuity_check.load_allowlist(root / "state" / "连续性白名单.md")

    stage("continuity_check：跨章/微动作/称呼/档案",
          lambda: (continuity_check.check_file_inventory(root),
                   continuity_check.check_cross_chapter(sents, allow),
                   continuity_check.check_micro_repeat(chapters),
                   continuity_check.check_address_contract(root, chapters),
                   continuity_check.check_appearance_landed(root, chapters),
                   continuity_check.check_nodes(nd),
                   continuity_check.check_prewrite_trace(nd),
                   continuity_check.check_volume_outline(root)))

    bl = style_audit.load_blacklist(blacklist)

    def style_all():
        for p in sorted(chdir.glob("*.md")):
            r = style_audit.analyse(p.read_text(encoding="utf-8-sig", errors="replace"), bl)
            style_audit.judge(r, None, False, None)

    stage("style_audit：全章 analyse + judge", style_all)
    return out


def fuzz() -> list[str]:
    """边界用例：任何一个抛异常都算失败。返回失败描述列表。"""
    t = pathlib.Path(tempfile.mkdtemp(prefix="stress_fuzz_"))
    try:
        empty, only_head = t / "e.md", t / "h.md"
        empty.write_text("", encoding="utf-8")
        only_head.write_text("| 编号 | 状态 |\n| --- | --- |\n", encoding="utf-8")
        dup = t / "d.md"
        dup.write_text("| 编号 | 内容 | 埋设章 | 计划回收章 | 状态 |\n| --- | --- | --- | --- | --- |\n"
                       "| F1 | a | 第1章 | 第9章 | 已埋 |\n"
                       "| 编号 | 内容 | 埋设章 | 计划回收章 | 状态 |\n"
                       "| F2 | b | 第2章 | 第9章 | 已埋 |\n", encoding="utf-8")
        short = t / "s.md"
        short.write_text("| 编号 | 内容 | 埋设章 | 计划回收章 | 状态 |\n"
                         "| --- | --- | --- | --- | --- |\n| F1\n| F2 |\n", encoding="utf-8")
        no_ch = [{"_chapter": None, "strand": "支线", "characters": ""}] * 3
        cases = [
            ("parse_foreshadow_archive(缺失)", lambda: series_audit.parse_foreshadow_archive(t / "no.md")),
            ("parse_foreshadow_archive(空)", lambda: series_audit.parse_foreshadow_archive(empty)),
            ("parse_foreshadow_archive(只有表头)", lambda: series_audit.parse_foreshadow_archive(only_head)),
            ("foreshadowing_table_info(重复表头)", lambda: series_audit.foreshadowing_table_info(dup)),
            ("foreshadowing_table_info(缺列行)", lambda: series_audit.foreshadowing_table_info(short)),
            ("foreshadowing_table_info(空)", lambda: series_audit.foreshadowing_table_info(empty)),
            ("check_foreshadow_consistency(空)", lambda: series_audit.check_foreshadow_consistency([], [])),
            ("check_recovery_declared(空)", lambda: series_audit.check_recovery_declared([], empty)),
            ("check_unknown_fields(缺失文件)", lambda: series_audit.check_unknown_fields([], t / "no.md")),
            ("check_unknown_fields(空文件)", lambda: series_audit.check_unknown_fields([], empty)),
            ("check_direction_drift(空)", lambda: series_audit.check_direction_drift([], t)),
            ("check_direction_drift(章号为 None)", lambda: series_audit.check_direction_drift(no_ch, t)),
            ("check_volume_beats(无节点)", lambda: series_audit.check_volume_beats([], t, 0)),
            ("check_volume_beats(无卷纲)", lambda: series_audit.check_volume_beats([{"_chapter": 1}], t, 1)),
            ("check_outline_reconciliation(空)", lambda: series_audit.check_outline_reconciliation([])),
            ("check_outline_reconciliation(无 _text)", lambda: series_audit.check_outline_reconciliation(
                [{"_chapter": 10}])),
            ("word_length_stats(空)", lambda: series_audit.word_length_stats([], t, 0, 0.9)),
            ("word_length_stats(target=0)", lambda: series_audit.word_length_stats(
                [{"_chapter": 1}], t, 0, 0.9)),
            ("check_word_floor(target=0)", lambda: series_audit.check_word_floor(
                [{"_chapter": 1}], t, 0, 0.9)),
            ("parse_scene_cards(空)", lambda: series_audit.parse_scene_cards("")),
            ("parse_scene_cards(只有表头)", lambda: series_audit.parse_scene_cards(
                "## 场景卡\n\n| # | 结果类型 |\n| --- | --- |\n")),
            ("check_prewrite_trace(空)", lambda: continuity_check.check_prewrite_trace({})),
            ("check_prewrite_trace(别名结尾)", lambda: continuity_check.check_prewrite_trace(
                {1: ("n.md", "---\nchapter: 1\n---\n\n## 写前加载")})),
            ("load_allowlist(缺失)", lambda: continuity_check.load_allowlist(t / "no.md")),
            ("compare_style(空)", lambda: style_audit.compare_style({}, {})),
            ("ai_mark_total(空)", lambda: style_audit.ai_mark_total({})),
            ("render_revision_compare(chars=0)", lambda: style_audit.render_revision_compare(
                "旧", {"chars": 0}, {"chars": 0}, [], [])),
            ("render_style_match(空)", lambda: style_audit.render_style_match("r", [])),
            ("extract_quotes(空)", lambda: quote_check.extract_quotes("")),
            ("check_quotes(空)", lambda: quote_check.check_quotes([], "")),
        ]
        bad = []
        for name, fn in cases:
            try:
                fn()
            except Exception as exc:                      # noqa: BLE001
                bad.append(f"{name} → {type(exc).__name__}: {exc}")
        return bad
    finally:
        shutil.rmtree(t, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="开发期压测：百万字规模下验证脚本不崩不变慢")
    ap.add_argument("--chapters", type=int, default=455, help="合成章数（默认 455 ≈ 百万字）")
    ap.add_argument("--chars-per-chapter", type=int, default=2200, help="每章目标字数（默认 2200）")
    ap.add_argument("--target-words", type=int, default=2160, help="字数下限判定用的目标（默认 2160）")
    ap.add_argument("--max-seconds", type=float, default=20.0,
                    help="单阶段耗时上限，超过判失败（默认 20 秒，正常都在 3 秒内）")
    ap.add_argument("--keep", action="store_true", help="保留合成项目，便于手工排查")
    args = ap.parse_args(argv)

    root = pathlib.Path(tempfile.mkdtemp(prefix="stress_project_"))
    blacklist = pathlib.Path(__file__).resolve().parent.parent / "references" / "ai-flavor-blacklist.md"
    ok = True
    try:
        print("=== 压测：造合成项目 ===")
        total = build_project(root, args.chapters, args.chars_per_chapter)
        print(f"  {args.chapters} 章 / {total} 字（脚本口径）｜目录 {root}")

        print("=== 计时（进程内，不 spawn 子进程）===")
        for name, sec in time_stages(root, blacklist, args.target_words):
            flag = ""
            if sec > args.max_seconds:
                flag, ok = "  ★ 超限", False
            print(f"  {name:<38} {sec:6.2f} 秒{flag}")

        print("=== 边界模糊测试 ===")
        bad = fuzz()
        if bad:
            ok = False
            for x in bad:
                print(f"  ★ 崩了：{x}")
        else:
            print("  全部用例无异常 ✓")
    finally:
        if args.keep:
            print(f"（--keep：合成项目保留在 {root}）")
        else:
            shutil.rmtree(root, ignore_errors=True)

    print()
    print("压测结果：" + ("通过 ✓" if ok else "**失败**（见上面的 ★）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
