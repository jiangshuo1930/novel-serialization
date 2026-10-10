#!/usr/bin/env python3
"""为 novel-serialization 生成 novel/ 目录骨架。

幂等：已存在的档案文件默认跳过，加 --force 才覆盖。
模板取自本技能的 references/ 目录。

用法:
  python init_novel.py
  python init_novel.py --root . --title "夜行账房" --protagonist "陆平"
  python init_novel.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "references"

DIRS = [
    "outline",
    "style",
    "world",
    "characters",
    "plot",
    "foreshadowing",
    "chapters",
    "archive",
    "state",
]

REDLINE = """# 世界观红线（速查版）

> 写每一章前扫一眼。改红线 = 全篇崩，改之前先问用户。

## 力量上限与代价

- 最强能到什么程度：
- 越界必须付出的代价：

## 禁止出现

- 现代词汇：
- 科技造物：
- 称谓 / 观念：

## 固定写法

- 货币：
- 时间单位：
- 距离：
- 等级 / 称谓：

## 死亡规则

- 谁能死：
- 能不能复活：
- 复活代价：
"""

RELATIONS = """# 人物关系表

> B 级路人不建档，只在这里记一行（具名角色超过 ~20 个后启用分级）。

| 人物 A | 人物 B | 关系 | 当前状态 | 变化章 |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |

## 称呼对照

| 谁 | 叫谁什么 | 从第几章开始 | 代表什么 |
| --- | --- | --- | --- |
|  |  |  |  |
"""


TIMELINE = """# 剧情时间线：第 1 卷

> **一卷一个文件**（`plot/时间线-第1卷.md`）：每写完一章在末尾追加一行，只记结果。
> 写前只读最近 3 行。详细节点写在 `plot/节点-第XXXX章.md`，本文件只做总表。
> 写完一卷，把本文件移进 `archive/`，再建下一卷的——否则写到几百章时这一份文件会大到读不动。

| 章号 | 故事内时间 | 地点 | 核心事件 | 状态变化 | 钩子类型 |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## 本卷时间基准

- 故事起点时间：
- 本卷跨度：
- 进行中的倒计时：
- 与上一章的时间衔接规则（跨夜 1-2 句 / 跨日 一个过渡段 / 超 3 天 过渡章）：
"""


def progress(title: str, protagonist: str) -> str:
    return f"""# 进度

- 书名：{title}
- 主角：{protagonist}
- 平台 / 档位（免费流 / 付费流）：
- 单章目标字数：
- 当前检查档位（QC_FULL / QC_CHECK / QC_SKIP）：
- 当前卷：第 1 卷
- 最新章号：0
- 累计字数：0
- 上次更新：

## 下一章 beat

- 核心冲突：
- 出场人物：
- 结尾钩子：
- 钩子类型（避开最近 2 章用过的）：

## 上一章交接（写下一章前必读）

- 上一章章号 / 标题：
- 上一章钩子类型 / 强度：
- 上一章结束情绪：
- next_chapter_must_remember：

## 节奏台账（据此决定这章要不要补兑现）

| 项 | 上次发生章号 | 距今 |
| --- | --- | --- |
| 小爽点 |  |  |
| 中爽点 |  |  |
| 大兑现（强度≥4） |  |  |
| 感情线推进 |  |  |
| 支线碰面 |  |  |

## 时间账（防倒计时错乱）

- 故事内当前时间：
- 与上一章时间差：
- 进行中的倒计时：

## 主线推进记录（每 10 章对照总纲）

| 章号区间 | 推进主线次数 | 是否连续 3 章未推进 | 大兑现有无 | 处理 |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |

## 本卷活跃清单（当前卷结束归档时用）

- 本卷新埋伏笔编号：
- 本卷应回收伏笔编号：
- 本卷出场 S 级角色：
"""


def read_template(name: str, fallback: str) -> str:
    path = TEMPLATES / name
    if path.is_file():
        return path.read_text(encoding="utf-8-sig")
    print(f"警告：模板缺失，使用占位内容：{path}", file=sys.stderr)
    return fallback


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 novel/ 目录骨架（幂等）")
    ap.add_argument("--root", default=".", help="novel/ 建在该目录下，默认当前目录")
    ap.add_argument("--title", default="未命名", help="书名")
    ap.add_argument("--protagonist", default="主角", help="主角名")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的档案文件")
    args = ap.parse_args(argv)

    base = Path(args.root).resolve()
    novel = base / "novel"

    for name in DIRS:
        (novel / name).mkdir(parents=True, exist_ok=True)

    char_template = read_template("character-template.md", "# 角色档案：<姓名>\n")
    files = {
        novel / "outline" / "总纲.md": read_template("outline-template.md", "# 总纲\n"),
        novel / "style" / "风格档案.md": read_template("style-template.md", "# 风格档案\n"),
        novel / "world" / "世界观.md": read_template("worldbuilding-template.md", "# 世界观\n"),
        novel / "world" / "世界观红线.md": REDLINE,
        novel / "world" / "关系表.md": RELATIONS,
        novel / "foreshadowing" / "伏笔表.md": read_template(
            "foreshadowing-template.md", "# 伏笔表\n"
        ),
        novel / "plot" / "时间线-第1卷.md": TIMELINE,
        novel / "outline" / "卷纲-第1卷.md": read_template(
            "volume-outline-template.md", "# 卷纲：第 X 卷\n"
        ),
        novel / "state" / "进度.md": progress(args.title, args.protagonist),
        novel / "characters" / f"主角-{args.protagonist}.md": char_template.replace(
            "<姓名>", args.protagonist
        ),
    }

    created, skipped = [], []
    for path, content in files.items():
        if path.exists() and not args.force:
            skipped.append(path)
            continue
        path.write_text(content, encoding="utf-8")
        created.append(path)

    print(f"根目录：{novel}")
    for path in created:
        print(f"  已生成 {path.relative_to(base)}")
    for path in skipped:
        print(f"  跳过（已存在）{path.relative_to(base)}")
    if skipped and not args.force:
        print("提示：要覆盖已存在的档案，加 --force。")
    print("下一步：")
    print("  1) 在 novel/ 下跑 git init 并提交第一次（静态档/动态档都是纯文本，靠 git 才能回滚）")
    print("  2) 填 outline/总纲.md（含创作宪法与结局）与 outline/卷纲-第1卷.md")
    print("  3) 填 style/风格档案.md（平台档位、单章字数、检查档位）")
    print("  4) 再写第一章；写前先建 plot/节点-第0001章.md")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
