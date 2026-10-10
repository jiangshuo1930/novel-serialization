#!/usr/bin/env python3
"""章节索引（SQLite + FTS5 trigram）：即时回答"这个词／这个人出现在哪几章"。

**它只是缓存，原文永远权威。** 三条设计约束（照 InkOS 的规矩，也为了不坑自己）：
1. **其它检查脚本一律不读它**——门禁（style_audit / series_audit / continuity_check）
   必须直接读原文；一旦门禁依赖索引，索引一旧，门禁就开始骗人。
2. **每次 build 都是全量重建到临时文件再原子替换**——所以**索引不存在"变旧"这种状态**，
   要么是这次建出来的，要么没有。（这也是我不再担心"旧索引比没索引更危险"的原因。）
3. 版本与来源都写进 `meta` 表；`stats` 会报"建完之后又新增了几章"。

为什么值得有它（而不是只靠 grep）：
- **`verify` 是新增的一致性检查**：节点 front-matter 自报的 `characters:` 是**自报**，
  正文里到底有没有这个人，以前没人核对 → 现在能（"节点说他在，正文里没有"）。
- 道具／伏笔追踪（如 `find "编号-07"`）在 455 章、百万字时，一条 SQL 就给出全部落点。

用法：
    python index_search.py build  --novel novel          # 建/重建索引（原子替换）
    python index_search.py find   "编号-07" --novel novel  # 这个词出现在哪几章（带原文片段）
    python index_search.py who    主角甲   --novel novel   # 这个人的出场章号与次数
    python index_search.py verify --novel novel          # 节点自报 characters vs 正文实测
    python index_search.py stats  --novel novel

退出码：0 正常；1 = verify 发现问题；2 = 用法／索引错误。
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import series_audit  # noqa: E402  （复用它的伏笔表解析，别抄一份）

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_NAME = "索引.db"
SCHEMA_VERSION = "1"
SENT_SPLIT_RE = re.compile(r"[^。！？!?…\n]+[。！？!?…]*")
CH_RE = re.compile(r"第\s*(\d+)\s*章")
PUNCT = "，。！？；：、—…“”‘’「」（）《》"
PUNCT_STRIP_RE = re.compile(r"[\s" + re.escape(PUNCT) + "]")
EXCERPT_PAD = 12


def has_fts5(con: sqlite3.Connection) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE __probe USING fts5(x, tokenize='trigram')")
        con.execute("DROP TABLE __probe")
        return True
    except sqlite3.Error:
        return False


def content_lines(text: str) -> list[str]:
    """只要正文：跳过标题、引用、表格行（与其它脚本同口径）。"""
    return [s for s in (line.strip() for line in text.splitlines())
            if s and s[0] not in "#>|"]


def iter_chapters(novel_root: Path) -> list[tuple[int, str, Path]]:
    """[(章号, 标题, 路径)]，按章号升序。"""
    out = []
    cdir = novel_root / "chapters"
    if not cdir.is_dir():
        return out
    for p in sorted(cdir.rglob("*.md")):
        m = CH_RE.search(p.name)
        if not m:
            continue
        title = p.stem.split("-", 1)[1] if "-" in p.stem else p.stem
        out.append((int(m.group(1)), title, p))
    out.sort(key=lambda x: x[0])
    return out


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in content_lines(text):
        for m in SENT_SPLIT_RE.finditer(line):
            s = m.group(0).strip()
            if any(ch not in PUNCT and not ch.isspace() for ch in s):
                out.append(s)
    return out


def chapter_chars(text: str) -> int:
    return len(PUNCT_STRIP_RE.sub("", "\n".join(content_lines(text))))


def db_path(novel_root: Path) -> Path:
    return novel_root / "state" / DB_NAME


def build(novel_root: Path) -> int:
    chapters = iter_chapters(novel_root)
    if not chapters:
        print(f"找不到正文：{novel_root / 'chapters'}（约定 第N章-标题.md）", file=sys.stderr)
        return 2
    target = db_path(novel_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(str(tmp))
    try:
        fts = has_fts5(con)
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE chapters(chapter INTEGER PRIMARY KEY, title TEXT, path TEXT,"
                    " chars INTEGER, sentences INTEGER)")
        con.execute("CREATE TABLE sentences(chapter INTEGER, seq INTEGER, text TEXT)")
        if fts:
            con.execute("CREATE VIRTUAL TABLE sent_fts USING fts5("
                        "text, chapter UNINDEXED, seq UNINDEXED, tokenize='trigram')")
        total_chars = total_sent = 0
        for ch, title, path in chapters:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            chars = chapter_chars(text)
            sents = split_sentences(text)
            total_chars += chars
            total_sent += len(sents)
            con.execute("INSERT INTO chapters VALUES (?,?,?,?,?)",
                        (ch, title, str(path.relative_to(novel_root)), chars, len(sents)))
            con.executemany("INSERT INTO sentences VALUES (?,?,?)",
                            [(ch, i, s) for i, s in enumerate(sents)])
            if fts:
                con.executemany("INSERT INTO sent_fts(text, chapter, seq) VALUES (?,?,?)",
                                [(s, ch, i) for i, s in enumerate(sents)])
        con.executemany("INSERT INTO meta VALUES (?,?)", [
            ("schema_version", SCHEMA_VERSION),
            ("novel_root", str(novel_root)),
            ("fts", "trigram" if fts else "none"),
            ("chapters", str(len(chapters))),
            ("chars", str(total_chars)),
        ])
        con.commit()
    finally:
        con.close()
    os.replace(tmp, target)                      # 原子替换：不存在"半截索引"
    print(f"已建索引：{target}")
    print(f"  {len(chapters)} 章 / {total_chars} 字 / {total_sent} 句"
          f"｜检索模式：{'FTS5 trigram' if fts else '无 FTS5，`find` 自动退回 LIKE'}")
    if not fts:
        print("  提示：本机 sqlite3 没编 FTS5 —— 检索仍可用（LIKE 全表扫），只是慢一些。")
    return 0


def open_db(novel_root: Path) -> sqlite3.Connection | None:
    p = db_path(novel_root)
    if not p.is_file():
        print(f"索引不存在：{p}　先跑 build", file=sys.stderr)
        return None
    return sqlite3.connect(str(p))


def search(con: sqlite3.Connection, term: str, limit: int) -> list[tuple[int, int, str]]:
    """子串检索。**trigram 要求 ≥3 字**，短词（如 2 字人名）必须退回 LIKE，否则查不到。"""
    fts = False
    try:
        fts = any(r[0] == "fts" and r[1] == "trigram"
                  for r in con.execute("SELECT key, value FROM meta"))
    except sqlite3.Error:
        fts = False
    if fts and len(term) >= 3:
        try:
            q = '"' + term.replace('"', '""') + '"'
            return list(con.execute(
                "SELECT chapter, seq, text FROM sent_fts WHERE sent_fts MATCH ?"
                " ORDER BY chapter, seq LIMIT ?", (q, limit)))
        except sqlite3.Error:
            pass                                  # FTS 查询语法问题就退回 LIKE
    return list(con.execute(
        "SELECT chapter, seq, text FROM sentences WHERE text LIKE ?"
        " ORDER BY chapter, seq LIMIT ?", (f"%{term}%", limit)))


def excerpt(text: str, term: str) -> str:
    i = text.find(term)
    if i < 0:
        return text[:40]
    return "…" + text[max(0, i - EXCERPT_PAD): i + len(term) + EXCERPT_PAD] + "…"


def cmd_find(novel_root: Path, term: str, limit: int) -> int:
    con = open_db(novel_root)
    if con is None:
        return 2
    try:
        rows = search(con, term, limit)
    finally:
        con.close()          # 必须显式关：Windows 上不关会锁住 .db，临时目录都删不掉（B034）
    chs = sorted({r[0] for r in rows})
    print(f"=== 「{term}」===")
    print(f"命中 {len(rows)} 句｜出现在 {len(chs)} 章："
          + ("、".join(f"第{c}章" for c in chs) if chs else "（无）"))
    for ch, _seq, text in rows[:limit]:
        print(f"  - 第{ch}章｜{excerpt(text, term)}")
    if len(rows) >= limit:
        print(f"  （只显示前 {limit} 条，用 --limit 调大）")
    return 0


def cmd_who(novel_root: Path, name: str, limit: int) -> int:
    con = open_db(novel_root)
    if con is None:
        return 2
    try:
        rows = search(con, name, 100000)
    finally:
        con.close()          # 同上：不关会锁文件
    per: dict[int, int] = {}
    for ch, _seq, _t in rows:
        per[ch] = per.get(ch, 0) + 1
    print(f"=== 「{name}」的实测出场 ===")
    if not per:
        print("  正文里一次都没出现（如果节点说他在，那就是节点在撒谎）")
        return 0
    print(f"  出场 {len(per)} 章｜句级出现 {len(rows)} 次")
    gaps = [c for c in range(min(per), max(per) + 1) if c not in per]
    print("  章号：" + "、".join(f"第{c}章×{per[c]}" for c in sorted(per)))
    if gaps:
        print("  中间断档：" + "、".join(f"第{c}章" for c in gaps[:20])
              + ("…" if len(gaps) > 20 else ""))
    return 0


def load_known_names(novel_root: Path) -> list[str]:
    """已知人名：从 `characters/主角-<名>.md`、`角色-<名>.md` 的文件名取（开书约定）。"""
    cdir = novel_root / "characters"
    names: list[str] = []
    if cdir.is_dir():
        for p in sorted(cdir.glob("*.md")):
            stem = p.stem
            for pre in ("主角-", "角色-"):
                if stem.startswith(pre) and stem[len(pre):].strip():
                    names.append(stem[len(pre):].strip())
                    break
    return names


def cmd_verify(novel_root: Path, min_hits: int = 2) -> int:
    """**正文里出现了、但节点 characters 没列** —— 这是加索引才方便做的新检查。

    ⚠️ 反方向（节点列了、正文找不到名字）**不做**：第一人称叙事里主角永远叫"我"，
    别的角色多是"他/她"，那个方向会几乎每章都报（实测 14 章报 13 条），
    属于"每章都响、于是没人看"的告警（B012/B017 的教训）。
    这个方向是**可判定的**：名字在正文里出现是事实。
    """
    plot = novel_root / "plot"
    chapters = {ch: p for ch, _t, p in iter_chapters(novel_root)}
    names = load_known_names(novel_root)
    if not plot.is_dir() or not chapters or not names:
        print("缺 chapters/、plot/ 或 characters/，无法核对", file=sys.stderr)
        return 2
    problems: list[str] = []
    for p in sorted(plot.glob("节点-*.md")):
        m = CH_RE.search(p.name)
        if not m or int(m.group(1)) not in chapters:
            continue
        ch = int(m.group(1))
        head = p.read_text(encoding="utf-8-sig", errors="replace")
        fm = re.search(r"^---\n(.*?)\n---", head, re.S)
        cm = re.search(r"^\s*characters\s*[:：]\s*(.+)$", fm.group(1), re.M) if fm else None
        declared = ([x.strip() for x in re.split(r"[,，、;；/]", cm.group(1)) if x.strip()]
                    if cm else [])
        body = "\n".join(content_lines(chapters[ch].read_text(
            encoding="utf-8-sig", errors="replace")))
        missing = [(n, body.count(n)) for n in names
                   if n not in declared and body.count(n) >= min_hits]
        if missing:
            problems.append(
                f"第{ch}章：正文里出现但节点 characters 没列 → "
                + "、".join(f"{n}×{c}" for n, c in sorted(missing, key=lambda x: -x[1])))
    print(f"=== 正文实测出场 vs 节点自报（核对了 {len(chapters)} 章 / {len(names)} 个人名"
          f"，阈值 ≥{min_hits} 次）===")
    if not problems:
        print("  没有漏列的 ✓")
        return 0
    for x in problems:
        print("  - " + x)
    print("  （只提示：节点漏列会让声纹契约与支线出勤一起漏掉这个角色）")
    return 1


def cmd_brief(novel_root: Path, chapter: int) -> int:
    """**写前简报：这一章的人 / 伏笔 / 物件，在全书的落点**（索引最值钱的用法）。

    为什么这是最值的地方：写新章时最贵的错误是**与前文自相矛盾**——
    人还在别处 / 道具没交代 / 伏笔忘了推进。这些信息散在几百个文件里，
    而它们恰好都能从索引一次问出来（以前要靠搜文件、或靠记性）。
    """
    con = open_db(novel_root)
    if con is None:
        return 2
    chapters = {ch: t for ch, t, _p in iter_chapters(novel_root)}
    node = novel_root / "plot" / f"节点-第{chapter:04d}章.md"
    head = node.read_text(encoding="utf-8-sig", errors="replace") if node.is_file() else ""
    fm = re.search(r"^---\n(.*?)\n---", head, re.S)

    def field(name: str) -> str:
        m = re.search(rf"^\s*{name}\s*[:：]\s*(.+)$", fm.group(1), re.M) if fm else None
        return m.group(1).strip() if m else ""

    # **写前简报本来就是"动笔前"用的** —— 那时候正文还不存在。
    # 正文在下面只用来取标题，所以没有正文时回落到节点 front-matter 的 title。
    # 早期版本一上来就 "没有第 N 章的正文" 退出，等于**在唯一需要它的时刻用不了**。
    pending = chapter not in chapters
    if pending:
        if not node.is_file():
            con.close()
            print(f"第 {chapter} 章既没有正文也没有节点：先写 {node} 再跑简报",
                  file=sys.stderr)
            return 2
        title = field("title") or f"第 {chapter} 章"
        print("（正文尚未写，按节点 front-matter 出简报；标题取 title 字段）")
    else:
        title = chapters[chapter]

    declared = [x.strip() for x in re.split(r"[,，、;；/]", field("characters")) if x.strip()]
    print(f"=== 写前简报：第 {chapter} 章《{title}》 ===")
    print(f"钩子 {field('hook_type') or '（未填）'}｜轴 {field('axes') or '（未填）'}"
          f"｜开头 {field('opening_type') or '（未填）'}")
    try:
        print()
        if declared:
            print("**出场人物**（按索引回看全书落点，避免「人还在别处」）：")
            for nm in declared:
                per = sorted({c for c, _s, _t in search(con, nm, 100000)})
                prev = [c for c in per if c < chapter]
                if prev:
                    gap = chapter - max(prev)
                    note = "**隔了很久，先回看那一章**" if gap >= 8 else ""
                    print(f"  - {nm}：全书 {len(per)} 章｜最近 第{max(prev)}章（隔 {gap} 章）{note}")
                else:
                    print(f"  - {nm}：**本章首次出现**（全书其它地方 {len(per)} 章）")

        fs = novel_root / "foreshadowing" / "伏笔表.md"
        rows = series_audit.parse_foreshadowing(fs)
        active = [r for r in rows if r["status"] not in ("已回收", "已废弃", "回收")]
        if active:
            print()
            print("**活跃伏笔**（该碰一下的排在前面）：")
            def staleness(r):
                la = r.get("last_advanced")
                return chapter - la if isinstance(la, int) else 999
            for r in sorted(active, key=staleness, reverse=True)[:5]:
                la = r.get("last_advanced")
                gap = staleness(r)
                tag = "**该碰一下了**" if gap >= 10 else ""
                print(f"  - {r['id']}（{r['type'] or '?'}）最近推进 "
                      f"{'第' + str(la) + '章' if isinstance(la, int) else '（未填）'}"
                      f"（隔 {gap} 章）{tag}｜{str(r.get('content', ''))[:34]}")

        terms = sorted(set(re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", head))
                       | set(re.findall(r"[「『]([^」』]{2,8})[」』]", head)))
        hits = [(t, sorted({c for c, _s, _t in search(con, t, 100000)})) for t in terms[:8]]
        hits = [(t, cs) for t, cs in hits if len(cs) >= 2]
        if hits:
            print()
            print("**本章节点里提到的专有词**（全书落点，防止前后不一致）：")
            for t, cs in hits[:5]:
                print(f"  - {t}：出现在 " + "、".join(f"第{c}章" for c in cs))
    finally:
        con.close()
    return 0


def cmd_stats(novel_root: Path) -> int:
    con = open_db(novel_root)
    if con is None:
        return 2
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()          # 同上：不关会锁文件
    n_now = len(iter_chapters(novel_root))
    print("=== 索引状况 ===")
    print(f"  库文件：{db_path(novel_root)}")
    print(f"  建索引时：{meta.get('chapters', '?')} 章 / {meta.get('chars', '?')} 字"
          f"｜检索模式 {meta.get('fts', '?')}｜schema v{meta.get('schema_version', '?')}")
    print(f"  现在正文：{n_now} 章", end="")
    if n_now > int(meta.get("chapters", 0) or 0):
        print(f" → **索引落后了 {n_now - int(meta.get('chapters', 0) or 0)} 章，重新 build**")
    else:
        print(" → 与索引一致" if n_now == int(meta.get("chapters", 0) or 0) else " → 章数变少了？")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="章节索引（SQLite+FTS5 trigram）；只是缓存，原文权威")
    ap.add_argument("action", choices=["build", "find", "who", "verify", "stats", "brief"])
    ap.add_argument("term", nargs="?", help="find/who 要查的词或人名；brief 要给的章号")
    ap.add_argument("--novel", default="novel", help="项目目录，默认 novel")
    ap.add_argument("--limit", type=int, default=40, help="最多显示多少条（默认 40）")
    ap.add_argument("--min-hits", type=int, default=2,
                    help="verify 用的出现次数阈值（默认 2：路过提一次不算，出现两次才算真在场）")
    args = ap.parse_args(argv)
    root = Path(args.novel)
    if args.action == "build":
        return build(root)
    if args.action == "stats":
        return cmd_stats(root)
    if args.action == "verify":
        return cmd_verify(root, args.min_hits)
    if args.action == "brief":
        if not args.term or not args.term.isdigit():
            print("brief 需要章号，例如：index_search.py brief 14", file=sys.stderr)
            return 2
        return cmd_brief(root, int(args.term))
    if not args.term:
        print(f"{args.action} 需要给一个词或人名", file=sys.stderr)
        return 2
    return cmd_find(root, args.term, args.limit) if args.action == "find" \
        else cmd_who(root, args.term, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
