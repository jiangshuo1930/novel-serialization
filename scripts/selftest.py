#!/usr/bin/env python3
"""自检：确认两个审计脚本的核心判断没有坏掉，并且示例项目的雷会照常响。

不需要参数，不联网，不改你的项目文件（只在自己的临时目录里写几份夹具）。

    python selftest.py

全部通过 → 退出码 0；有失败 → 退出码 1，并打印失败原因。
设计原则：**只对"确定性规则"下断言**（黑名单、配额、解析、轮换、超期、倒计时），
统计类指标与我手算的句子长度只报告、不判定，避免夹具问题造成假 FAIL。

自检项数是**文档承诺**：`README.md` 两处写着"N 项自检"。这个数字由本文件末尾自动核对，
数字变了就报 FAIL 并告诉你要改哪两处——不要让文档悄悄过期。
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import continuity_check  # noqa: E402
import index_search  # noqa: E402
import quote_check  # noqa: E402
import series_audit  # noqa: E402
import stress_test  # noqa: E402  （只为"能导入"这件事把关；压测本身很慢，不在自检里跑）
import style_audit  # noqa: E402
import zhuque_check  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
BLACKLIST_PATH = SKILL_DIR / "references" / "ai-flavor-blacklist.md"
EXAMPLES = SKILL_DIR / "examples"

# 故意写得很像 AI：句长均匀、黑名单命中多、程度副词堆叠
AI_SAMPLE = """夜色降下来，青石巷口的路灯一盏一盏地亮了。林默缓缓地走在回家的路上，心中暗道今天的事情实在有些奇怪。他不由得想起白天在衙门里看到的那一幕，瞳孔骤然收缩。他深吸一口气，努力让自己镇定下来。他意识到自己可能已经被人盯上了。与此同时，身后的脚步声也停了下来。他缓缓地转过身，看着空无一人的巷子。他缓缓地抬起手，摸了摸自己的后颈。他缓缓地放下手，继续往前走。他缓缓地推开院门，走进了院子。他缓缓地关上院门，站在了院子当中。"""

# 干净样本：句长大幅错落（4 字到 60 多字）、只写具体小事、没有黑名单词
HUMAN_SAMPLE = """巷口的灯泡闪了两下，才亮起来。

主角甲把车停在路边，没下车。计价器还在跳，他看着那个数字，想起早上出门时妻子说的话——她说今天晚上想吃鱼，让他别忘了。

他忘了。

雨开始下。不是很大。落在挡风玻璃上，一小团一小团地散开，像被人用指头按过的印子。他伸手把雨刮器关了，又打开，再关上，来回三次，直到他自己也说不清为什么要这么干。后排座位上放着一袋橘子，是同事下午塞给他的，袋子上还沾着水印，橘子皮的味道在车里散开，酸，而且有点闷。

“多少钱？”他问。

司机没回头，报了个数字。

他扫码，付钱，下车。伞在后备箱里，他懒得拿。走到楼下的时候，头发已经湿了，他站在单元门口抖了抖袖子上的水，抬头看了一眼自己家的窗户——灯还亮着。

他掏出钥匙，在手里捏了半分钟，才插进锁孔。

电梯停在六楼。他按了三次，那个数字一动不动。他转身去走楼梯，走到三楼的时候停了一下，扶着栏杆喘了几口气，忽然发现楼道里有人在小声说话，声音是从四楼传下来的，其中一个声音他认得。

楼道里的声控灯没亮。他在黑里站了一会儿，听见自己的呼吸，也听见四楼那两个人还在说。他数了数台阶，一级一级往上走，走到第四级的时候，鞋底踩到了什么东西，软软的，他弯腰摸了一下——是一只袜子，湿的。他把手在裤子上蹭了两下，继续往上。

四楼的门虚掩着，门缝里漏出一条光。他没有推门，先站在门外听。里面的人说话很慢，一句和我爱你之类的都没有，说的全是钱。他听懂了其中一句：下周三之前，必须把钱凑齐。他退后半步，后腰撞到了楼梯扶手，铁皮发出一声闷响。屋里的说话声停了。

那是他妻子的声音。"""

WORLD_SAMPLE = """# 世界观

## 清单怎么写（示例，脚本不扫这一节）

- `掏手机` —— 这是示例，不该被脚本提取成规则

## 禁止矛盾清单（forbidden_contradictions）

| 禁止出现的写法（反引号内） | 为什么 |
| --- | --- |
| `主角用左手拔剑` | 设定里他左手已废 |
"""

CHAR_SAMPLE = """# 角色档案：老周

## 语言指纹

<!-- fingerprint:start -->
口头禅: 你就说是不是 | 行吧行吧
标志性动作: 敲桌沿 | 摸后颈
<!-- fingerprint:end -->
"""

# 第二个角色，词表刻意与老周完全不同，用来验证"按角色分别计数"
CHAR_SAMPLE2 = """# 角色档案：老李

## 语言指纹

<!-- fingerprint:start -->
口头禅: 你行你上 | 讲不讲道理
标志性动作: 拧瓶盖 | 把外套扣子扣上
<!-- fingerprint:end -->
"""

FORESHADOW_TABLE = """| 编号 | 内容 | 类型 | 层级 | 标签 | 埋设章 | 计划回收章 | 状态 | 最近推进章 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F001 | 断指 | 主线 | 核心 | 怪癖 | 第2章 | 第5章 | 已埋 | 第3章 |  |
| F002 | 玉佩 | 道具 | 装饰 | 物件 | 第4章 | 第30章 | 推进中 | 第6章 |  |

## 回收记录

| 编号 | 内容 | 埋设章 | 回收章 | 回收方式 |
| --- | --- | --- | --- | --- |
| F000 | 旧事 | 第1章 | 第4章 | 一句话了结 |
"""

# 朱雀官方接口的**实测返回摘录**（只保留当时打印出来的字段），用来做解析回归测试。
# 不去调用网络，所以断网也能跑。
ZQ_HUMAN = {
    "status": "success", "softmax_confidence": 0.0019, "ratio_confidence": 0,
    "labels_ratio": {"0": 1, "1": 0, "2": 0},
    "segment_labels": [{"text": "hello world", "label": 0, "conf": 0.0019,
                        "order": 1, "position": [0, 11]}],
    "usage": {"total_tokens": 11}, "msg": "",
    "makers_models_usage": {"total_tokens": 19},
}
# 关键教训：这段明显是 AI 味的文字，标签分布是"不确定 100%"，但置信度 0.94。
# 如果拿 labels_ratio["1"]（AI 段占比 = 0）当 AI 率，就会把假通过放出去。
ZQ_AI_UNCERTAIN = {
    "status": "success", "softmax_confidence": 0.9401,
    "labels_ratio": {"0": 0, "1": 0, "2": 1},
    "segment_labels": [], "usage": {"total_tokens": 278}, "msg": "",
}
ZQ_AI = {
    "status": "success", "softmax_confidence": 0.9969,
    "labels_ratio": {"0": 0, "1": 1, "2": 0},
    "segment_labels": [
        {"text": "外面的雨声渐渐小了。", "label": 1, "conf": 0.9975, "order": 1},
        {"text": "空气中弥漫着陈旧的纸张气味。", "label": 1, "conf": 0.9972, "order": 2},
        {"text": "夜色降临在青石巷口。", "label": 1, "conf": 0.9969, "order": 3},
    ],
    "usage": {"total_tokens": 702}, "msg": "",
}
ZQ_NO_SCORE = {"status": "success", "labels_ratio": {"0": 0, "1": 0, "2": 1}, "msg": ""}

# 干净样本里绝不允许因为"确定性规则"报错的项
BLOCKING_KEYS = ("黑名单", "配额", "禁止矛盾", "段超过", "纯对话")


def node(ch: int, **kw) -> dict:
    d = {"_chapter": ch, "_path": f"节点-第{ch:04d}章.md"}
    d.update(kw)
    return d


def stat_fixture(text: str) -> str:
    """把夹具重复到统计门槛（MIN_CHARS_FOR_STATS）以上再返回。

    **为什么需要**：统计类判断（句长波动、连续「了」句、对称句式、漂移…）在
    `chars < MIN_CHARS_FOR_STATS` 时会被 `judge()` 直接跳过——于是夹具太短会造成
    **假通过**（"不提示"）或**假失败**（"该提示但没提示"）。这个坑在自检里踩过六次，
    所以现在统一用它造夹具，而不是手写 `text * N`。
    """
    if not text.strip():
        # 空文本会在这里**死循环**（out 永远不增长，长度检查永不触发）——先挡住（B032）
        raise AssertionError("stat_fixture 需要非空文本，否则会死循环")
    out = text
    while style_audit.analyse(out, [], None, {})["chars"] < style_audit.MIN_CHARS_FOR_STATS:
        out += text
        if len(out) > 500000:
            raise AssertionError("夹具造不出来——检查 text 是不是空的")
    return out


def main() -> int:
    failures: list[str] = []
    checks = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        if condition:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}" + (f" —— {detail}" if detail else ""))
            failures.append(label)

    print("[1] 黑名单文件可加载，且死条目已清除")
    blacklist = style_audit.load_blacklist(BLACKLIST_PATH)
    check("黑名单加载出 ≥30 个词", len(blacklist) >= 30, f"实际 {len(blacklist)}")
    for bad in ("随着……", "不是……而是……", "最后", "一方面……另一方面……", "微微"):
        check(f"不误杀/不失效：{bad}", bad not in blacklist)

    print("[2] 世界观「禁止矛盾清单」只扫该节（示例节不能被抓）")
    with tempfile.TemporaryDirectory() as tmp:
        wp = Path(tmp) / "世界观.md"
        wp.write_text(WORLD_SAMPLE, encoding="utf-8")
        forbidden = style_audit.load_forbidden(wp)
        check("提取到清单里的规则", "主角用左手拔剑" in forbidden, f"{forbidden}")
        check("没把示例节的词当规则", "掏手机" not in forbidden, f"{forbidden}")
        r = style_audit.analyse("他左手还废着，却主角用左手拔剑了。", blacklist, forbidden)
        f_hard, _, _ = style_audit.judge(r)
        check("正文命中禁止矛盾 → 必须改", any("禁止矛盾" in x for x in f_hard), f"{f_hard}")

    print("[3] AI 味样本应被判必须改")
    ai = style_audit.analyse(AI_SAMPLE, blacklist)
    ai_hard, _, _ = style_audit.judge(ai)
    check("AI 样本命中黑名单", bool(ai["blacklist_hits"]), f"hits={ai['blacklist_hits']}")
    check("AI 样本产生 HARD 项", bool(ai_hard), f"hard={ai_hard}")

    print("[4] 干净样本不应误报确定性硬错误")
    human = style_audit.analyse(HUMAN_SAMPLE, blacklist)
    human_hard, human_soft, _ = style_audit.judge(human)
    check("干净样本无黑名单命中", not human["blacklist_hits"], f"hits={human['blacklist_hits']}")
    check("干净样本无配额超标", not human["quota_exceeded"], f"{human['quota_exceeded']}")
    check("干净样本识别出对话行", human["dialogue_lines"] >= 1, f"引号 {human['quote_styles']}")
    blocking = [x for x in human_hard if any(k in x for k in BLOCKING_KEYS)]
    check("没有确定性硬错误（黑名单/配额/禁止矛盾/段落/对话）", not blocking, f"{blocking}")
    if human_hard:
        print(f"       另有可调项触发（不影响判定，供参考）：{human_hard}")
    print(
        f"       实测：{human['chars']} 字｜σ²={human['var_len']}｜BurstCV={human['burst_cv']}"
        f"｜ParaCV={human['para_cv']}｜MATTR={human['mattr']}｜3gram重复={human['tri_rep']}"
        f"｜窗口达标 {human['window_pass']:.0%}｜参考区间外 {len(human_soft)} 项"
    )

    print("[5] 引号样式：「」也要能统计")
    jp = "「你走不走？」\n\n他没答。\n\n「我问你走不走。」\n"
    r_jp = style_audit.analyse(jp, blacklist)
    check("「」识别为对话行", r_jp["dialogue_lines"] >= 2, f"{r_jp['quote_styles']}")

    print("[6] 短文本应跳过统计类判断")
    short = style_audit.analyse("他走了。", blacklist)
    _, short_soft, _ = style_audit.judge(short)
    check("短文本给出跳过提示", any("跳过" in s for s in short_soft), f"{short_soft}")

    print("[7] 角色声纹（fingerprint 块，按角色分别判）")
    with tempfile.TemporaryDirectory() as tmp:
        cp = Path(tmp) / "角色-老周.md"
        cp.write_text(CHAR_SAMPLE, encoding="utf-8")
        fps = style_audit.load_fingerprints([cp])
        check("按角色名分组返回", list(fps.keys()) == ["老周"], f"{list(fps.keys())}")
        check("读到 4 条声纹", len(fps["老周"]) == 4, f"{fps}")

        hit = style_audit.analyse(HUMAN_SAMPLE + "\n你就说是不是。他敲桌沿。", blacklist, None, fps)
        hit_hard, hit_soft, _ = style_audit.judge(hit, None, True)
        check("声纹达标时不进必须改",
              not any("老周" in s and "声纹" in s for s in hit_hard), f"hard={hit_hard}")

        miss = style_audit.analyse(HUMAN_SAMPLE, blacklist, None, fps)
        miss_hard, _, _ = style_audit.judge(miss, None, True)
        check("声纹为 0 时报必须改并点名角色",
              any("声纹" in s and "老周" in s and "0 次" in s for s in miss_hard), f"{miss_hard}")

        # 回归测试：这是真实踩过的坑——合并计数会让"多传几个档案"自动凑够次数，
        # 从而掩盖某个角色整章没落地。两个角色时，足量的那个不能把为 0 的那个盖过去。
        cp2 = Path(tmp) / "角色-老李.md"
        cp2.write_text(CHAR_SAMPLE2, encoding="utf-8")
        two = style_audit.load_fingerprints([cp, cp2])
        check("两个角色分别成组", set(two) == {"老李", "老周"}, f"{sorted(two)}")
        mixed = style_audit.analyse(HUMAN_SAMPLE + "\n你就说是不是。他敲桌沿。", blacklist, None, two)
        mixed_hard, _, _ = style_audit.judge(mixed, None, True)
        check("足量者不得掩盖缺席者：老李（0 次）仍被判必须改",
              any("老李" in s and "0 次" in s for s in mixed_hard), f"{mixed_hard}")
        check("同一轮里达标的老周不被误判",
              not any("老周" in s and "声纹" in s for s in mixed_hard), f"{mixed_hard}")

        # 回归（B040）：档案里**一条声纹都没登记**（空块）时，这一项根本没法查。
        # 以前它和"登记了却零命中"走同一分支 → 报必须改，可正文怎么改都清不掉
        # （空清单永远满足不了），提示还写着"登记项：（全部缺席）"——把"档案没写"
        # 说成了"本章没写"。那一刀该落在档案上，且必须明说"无法核对"。
        empty_fps = {"葛瘸子": []}
        r_empty = style_audit.analyse(HUMAN_SAMPLE + "\n是个瘸子。", blacklist, None, empty_fps)
        e_hard, e_soft, _ = style_audit.judge(r_empty, None, True)
        check("空声纹块不进必须改（该改的是档案，不是正文）",
              not any("葛瘸子" in s for s in e_hard), f"{e_hard}")
        check("但也不静默：明确报「无法核对」",
              any("葛瘸子" in s and "无法核对" in s for s in e_soft), f"{e_soft}")
        check("有登记却零命中仍判必须改（这次放宽没动到那条守卫）",
              any("老周" in s and "0 次" in s for s in miss_hard), f"{miss_hard}")

        nopass = style_audit.analyse(HUMAN_SAMPLE, blacklist, None, {})
        _, nopass_soft, _ = style_audit.judge(nopass, None, True)
        check("没传档案时明确提示未做核对",
              any("没传 --fingerprint" in s for s in nopass_soft), f"{nopass_soft}")

    print("[8] 连载体检：钩子轮换 / 倒计时 / 爽点间隔 / 词表")
    nodes = [
        node(1, hook_type="反转", opening_type="对话", payoff="小爽点", payoff_strength=3,
             emotion="压抑", subplots="账房线", countdown="D-9"),
        node(2, hook_type="反转", opening_type="动作", payoff="小爽点", payoff_strength=3,
             emotion="压抑", subplots="账房线", countdown="D-8"),
        node(3, hook_type="反转", opening_type="悬念", payoff="无", payoff_strength=0,
             emotion="压抑", subplots="账房线", countdown="D-7"),
        node(4, hook_type="危机", opening_type="环境", payoff="无", payoff_strength=0,
             emotion="紧张", subplots="", countdown="D-9"),
        node(5, hook_type="悬念", opening_type="动作", payoff="无", payoff_strength=0,
             emotion="压抑", subplots="", countdown="D-5"),
        node(6, hook_type="信息", opening_type="对话", payoff="无", payoff_strength=0,
             emotion="紧张", subplots="", countdown="D-4"),
        node(7, hook_type="关系", opening_type="内心", payoff="无", payoff_strength=0,
             emotion="无奈", subplots="", countdown="D-3"),
        node(8, hook_type="反转", opening_type="声音", payoff="无", payoff_strength=0,
             emotion="紧张", subplots="", countdown="D-2"),
        node(9, hook_type="危机", opening_type="动作", payoff="无", payoff_strength=0,
             emotion="压抑", subplots="", countdown="D-1"),
        node(10, hook_type="升级", opening_type="对话", payoff="中爽点", payoff_strength=5,
             emotion="爽", subplots="", countdown=""),
        node(11, hook_type="悬念", opening_type="悬念", payoff="中爽点", payoff_strength=5,
             emotion="爽", subplots="", countdown=""),
        node(12, hook_type="信息", opening_type="环境", payoff="小爽点", payoff_strength=3,
             emotion="期待", subplots="", countdown=""),
    ]
    n_hard, n_soft = series_audit.check_nodes(nodes)
    check("抓到连续 3 章同钩子", any("章末钩子重复" in x for x in n_hard), f"{n_hard}")
    check("抓到连续 3 章同情绪", any("情绪连续同向" in x for x in n_hard), f"{n_hard}")
    check("抓到倒计时倒退", any("倒计时倒退" in x for x in n_hard), f"{n_hard}")
    check("抓到爽点断档", any("爽点" in x for x in n_hard), f"{n_hard}")
    check("抓到支线久未出场", any("支线" in x for x in n_soft), f"{n_soft}")
    bad = series_audit.check_vocab([node(1, hook_type="翻转", opening_type="环境",
                                        payoff_strength=9)])
    check("抓到词表外的钩子类型", any("不在标准清单" in x for x in bad), f"{bad}")
    check("抓到超量纲的强度值", any("量纲" in x for x in bad), f"{bad}")

    print("[9] 伏笔表解析（只读活跃表）与超期判定")
    with tempfile.TemporaryDirectory() as tmp:
        fs = Path(tmp) / "伏笔表.md"
        fs.write_text(FORESHADOW_TABLE, encoding="utf-8")
        rows = series_audit.parse_foreshadowing(fs)
        check("只解析活跃表（2 条，回收记录被跳过）", len(rows) == 2, f"实际 {len(rows)}")
        check("埋设/回收章号解析正确",
              bool(rows) and rows[0]["planted"] == 2 and rows[0]["target"] == 5,
              f"{rows[0] if rows else None}")
        fs_hard, _ = series_audit.check_foreshadowing(rows, current=20)
        check("主线伏笔超期判为必须改", any("F001" in x for x in fs_hard), f"{fs_hard}")

    print("[10] 示例项目集成（示例的雷必须照常响）")
    if not (EXAMPLES / "plot").is_dir():
        check("示例目录存在", False, str(EXAMPLES))
    else:
        ex_nodes = series_audit.load_nodes(EXAMPLES / "plot")
        check("示例节点读到 7 章", len(ex_nodes) == 7, f"实际 {len(ex_nodes)}")
        ex_hard, ex_soft = series_audit.check_nodes(ex_nodes)
        check("示例：钩子重复", any("章末钩子重复" in x for x in ex_hard), f"{ex_hard}")
        check("示例：情绪连续同向", any("情绪连续同向" in x for x in ex_hard), f"{ex_hard}")
        check("示例：倒计时倒退", any("倒计时倒退" in x for x in ex_hard), f"{ex_hard}")
        check("示例：爽点断档", any("爽点" in x for x in ex_hard), f"{ex_hard}")
        check("示例：词表外的钩子类型被提示",
              any("不在标准清单" in x for x in ex_soft), f"{ex_soft}")
        ex_rows = series_audit.parse_foreshadowing(EXAMPLES / "foreshadowing" / "伏笔表.md")
        check("示例：活跃伏笔 2 条", len(ex_rows) == 2, f"实际 {len(ex_rows)}")
        ex_fs_hard, _ = series_audit.check_foreshadowing(ex_rows, current=7)
        check("示例：主线伏笔超期", any("F001" in x for x in ex_fs_hard), f"{ex_fs_hard}")
        words = series_audit.count_chapter_chars(EXAMPLES / "chapters", 1)
        check("示例：能实测正文字数", isinstance(words, int) and words > 300, f"{words}")

    print("[11] 朱雀接口解析（用实测返回做的回归测试，不联网）")
    h = zhuque_check.extract_scores(ZQ_HUMAN)
    check("真人样本：AIGC = 0.0019", abs((h["aigc"] or -1) - 0.0019) < 1e-9, f"{h['aigc']}")
    check("真人样本：人工率 99.81% 判通过（线是 人工率 ≥0.95）",
          h["human"] is not None and h["human"] >= zhuque_check.DEFAULT_MIN_HUMAN,
          f"{h['human']}")
    check("默认线就是人工率 0.95（对应 AIGC ≤0.05）",
          zhuque_check.DEFAULT_MIN_HUMAN == 0.95 and abs(1 - 0.95 - 0.05) < 1e-9,
          f"{zhuque_check.DEFAULT_MIN_HUMAN}")
    check("人工率与 AIGC 互补（1 - aigc）", abs(h["human"] - (1 - h["aigc"])) < 1e-9,
          f"{h['human']} vs {h['aigc']}")
    _txt = zhuque_check.render("t", h, zhuque_check.DEFAULT_MIN_HUMAN, 0)
    check("渲染出的判定文案用的是人工率口径", "人工率 ≥" in _txt, "文案没跟着改")
    check("渲染文案里同时给出对应的 AIGC 线", "AIGC ≤0.05" in _txt, _txt.splitlines()[-2:])
    check("文本 token 与计费 token 分开取（11 / 19）",
          h["tokens"] == 11 and h["billed_tokens"] == 19,
          f"tokens={h['tokens']} billed={h['billed_tokens']}")
    u = zhuque_check.extract_scores(ZQ_AI_UNCERTAIN)
    check("标签是『不确定』时仍用置信度判定（不回退 labels_ratio）",
          abs((u["aigc"] or -1) - 0.9401) < 1e-9, f"aigc={u['aigc']}")
    check("上一条判为未通过（人工率 5.99% < 0.95）",
          (u["human"] or 1) < zhuque_check.DEFAULT_MIN_HUMAN, f"{u['human']}")
    a = zhuque_check.extract_scores(ZQ_AI)
    check("AI 样本：AIGC = 0.9969", abs((a["aigc"] or -1) - 0.9969) < 1e-9, f"{a['aigc']}")
    ranked = sorted(a["segments"], key=lambda x: float(x.get("conf") or 0), reverse=True)
    check("分段按 conf 降序，最重的一段排第一",
          bool(ranked) and abs(ranked[0]["conf"] - 0.9975) < 1e-9, f"{ranked[:1]}")
    n = zhuque_check.extract_scores(ZQ_NO_SCORE)
    check("缺置信度时报『无法判定』，不猜一个数", n["aigc"] is None, f"{n['aigc']}")
    # 用形状相同的假密钥测打码，绝不要把真实密钥写进这个文件
    secret = "sk-EXAMPLE0123456789abcdefghijklmnopqrstuvwxyzABCDEF"
    masked = zhuque_check.mask_key(secret)
    check("密钥打码后不泄露中段", masked != secret and secret[8:20] not in masked, f"{masked}")

    print("[12] 跨章一致性核对（continuity_check 的雷必须响）")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "novel"
        for d in ("chapters", "plot", "world", "characters", "outline", "state"):
            (root / d).mkdir(parents=True, exist_ok=True)

        # 第 1、2 章逐字重复同一句（相邻 → 必须改）；第 4 章有一句章内刷 3 次。
        # 第 1 章刻意放进「老孙」（称呼契约提前）与「黑眼圈」（外貌已落地）各一次。
        (root / "chapters" / "第1章-甲.md").write_text(
            "老孙把杯子放下。\n他看了我一眼。\n他黑眼圈很重，像是很久没睡了。\n"
            "那时候天已经黑了，路上没有一个人。\n",
            encoding="utf-8")
        (root / "chapters" / "第2章-乙.md").write_text(
            "他看了我一眼。\n他又把杯子放下了，杯子底在桌面上蹭出一圈白印。\n"
            "低头。低头。低头。\n",
            encoding="utf-8")
        (root / "chapters" / "第4章-丁.md").write_text(
            "这一段只是把字数凑够，好让检测有东西可查，句子长度也刻意做得不一样。\n",
            encoding="utf-8")

        # 第 1 章缺「事件」段 → 必须改
        (root / "plot" / "节点-第0001章.md").write_text(
            "---\nchapter: 1\ntitle: 甲\nhook_type: 悬念\nopening_type: 对话\n"
            "payoff: 小爽点\nemotion: 压抑\ncharacters: 甲\n---\n\n"
            "- **人物状态变化**：有内容\n- **结尾最后 200 字原文**：有内容\n"
            "- **下一章必须记住的事（`next_chapter_must_remember`）**：有内容\n",
            encoding="utf-8")

        # 卷纲：状态列写了带注释的非法值，且章号 2 出现两次
        (root / "outline" / "卷纲-第1卷.md").write_text(
            "## 本卷章表\n\n| 章号 | 标题 | beat | 钩子类型 | 状态 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 1 | 甲 | x | 悬念 | done |\n"
            "| 2 | 乙 | x | 危机 | draft（第 1 章 done） |\n"
            "| 2 | 乙 | x | 危机 | outline |\n",
            encoding="utf-8")

        # 关系表契约：规定「老孙」从第 12 章起，但正文第 1 章就用了
        (root / "world" / "关系表.md").write_text(
            "## 称呼对照\n\n| 谁 | 叫谁什么 | 从第几章开始 | 代表什么 |\n"
            "| --- | --- | --- | --- |\n"
            "| 甲 | 乙 → \"老孙\" | 第 12 章起 | 混熟了 |\n",
            encoding="utf-8")

        # 档案：appearance 块里登记了一个正文从未出现的细节
        (root / "characters" / "角色-甲.md").write_text(
            "<!-- appearance:start -->\n黑眼圈 | 小指缺一节\n<!-- appearance:end -->\n",
            encoding="utf-8")

        chapters = continuity_check.load_chapters(root / "chapters")
        nodes = continuity_check.load_nodes(root / "plot")
        sents = {c: continuity_check.split_sentences(chapters[c][1]) for c in chapters}

        h, _ = continuity_check.check_cross_chapter(sents, set())
        check("抓到相邻章逐字重复", any("高个" in x or "他看了我一眼" in x for x in h), f"{h}")

        h2, s2 = continuity_check.check_cross_chapter(sents, {"他看了我一眼"})
        check("白名单能豁免刻意回声", not any("他看了我一眼" in x for x in h2), f"{h2}")

        _, s3 = continuity_check.check_micro_repeat(chapters)
        check("抓到章内微动作刷 3 次", any("低头" in x for x in s3), f"{s3}")

        h4, _ = continuity_check.check_nodes(nodes)
        check("抓到节点缺必填段", any("缺「事件」" in x for x in h4), f"{h4}")

        h5, _ = continuity_check.check_volume_outline(root)
        check("抓到卷纲状态列非法值", any("不在允许取值" in x for x in h5), f"{h5}")
        check("抓到卷纲章号重复行", any("出现 2 行" in x for x in h5), f"{h5}")

        _, s6 = continuity_check.check_address_contract(root, chapters)
        check("抓到称呼契约对不上", any("老孙" in x for x in s6), f"{s6}")

        h7, _ = continuity_check.check_appearance_landed(root, chapters)
        check("抓到档案登记了却没落地的外貌细节",
              any("小指缺一节" in x for x in h7), f"{h7}")
        check("已落地的那条不误报", not any("黑眼圈" in x for x in h7), f"{h7}")

        # 回归（技能 bug）：卷纲**模板自带**的 markdown 表头分隔行 `| --- | --- |`
        # 的第一格 "---" 会命中章号正则 `[\d\-]+`（那是为了容忍 "1-3" 这类章号区间），
        # 于是整行被当成数据行，每份照模板写的卷纲都会报一条假的
        # 「状态列写了「---」」必须改。实测：《本侯想告老还乡》第 1 卷卷纲中招。
        (root / "outline" / "卷纲-第1卷.md").write_text(
            "## 本卷章表\n\n| 章号 | 标题 | beat | 钩子类型 | 状态 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 1 | 甲 | x | 悬念 | done |\n"
            "| 2 | 乙 | x | 危机 | draft |\n",
            encoding="utf-8")
        hv_sep, _ = continuity_check.check_volume_outline(root)
        check("卷纲表头分隔行不再被当成数据行（照模板写 = 零误报）",
              not any("状态列" in x or "不在允许取值" in x for x in hv_sep), f"{hv_sep}")
        check("合法状态值一个都不报（done/draft 均放行）", hv_sep == [], f"{hv_sep}")
        # 反向：非法值仍要抓到，别把分隔行过滤做成了"整列不查"
        (root / "outline" / "卷纲-第1卷.md").write_text(
            "## 本卷章表\n\n| 章号 | 标题 | beat | 钩子类型 | 状态 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 1 | 甲 | x | 悬念 | done（第 2 章也 done） |\n",
            encoding="utf-8")
        hv_bad, _ = continuity_check.check_volume_outline(root)
        check("带括号注释的非法状态值照旧要抓到",
              any("不在允许取值" in x for x in hv_bad), f"{hv_bad}")

    print("[13] 字数只判下限：超上限不再报警（作者决定不做字数上限）")
    with tempfile.TemporaryDirectory() as tmp:
        cd = Path(tmp) / "chapters"
        cd.mkdir(parents=True, exist_ok=True)
        (cd / "第1章-超长.md").write_text("字" * 5000, encoding="utf-8")
        (cd / "第2章-偏短.md").write_text("字" * 100, encoding="utf-8")
        (cd / "第3章-正常.md").write_text("字" * 1000, encoding="utf-8")
        nd = [node(1), node(2), node(3)]
        msgs = series_audit.check_word_floor(nd, cd, 1000)
        check("偏短章（100 < 目标×0.9）报骨架感",
              any("第 2 章" in m and "骨架感" in m for m in msgs), f"{msgs}")
        check("超长章（5000 > 目标×1.1）完全不出现在告警里",
              not any("第 1 章" in m for m in msgs), f"{msgs}")
        check("正常章（1000）不报警", not any("第 3 章" in m for m in msgs), f"{msgs}")
        check("上限比例常量已移除，脚本只剩 WORD_LOW_RATIO",
              hasattr(series_audit, "WORD_LOW_RATIO"), "常量缺失")
        check("不传 --target-words 时不做任何字数判断",
              series_audit.check_word_floor(nd, cd, None) == [], "应返回空")

    print("[14] 判断逻辑加固的回归测试（四处真实踩过的坑）")
    # 坑 0（影响最大）：对话段末尾的收尾引号曾被当成一个独立"句子"。
    # 每一个"“话。”"结尾的段落都会多出一个 `”` 幽灵句 —— 实测某一章多出 42 个，
    # 直接把 句数/平均句长/σ²/BurstCV/单句段占比 全部算歪。
    sents0 = style_audit.split_sentences("“赵经理。”他笑了一下。\n“来了。”\n")
    check("段落末尾的收尾引号不再变成独立句子（无 `”` 幽灵句）",
          "”" not in sents0, f"{sents0}")
    check("该有的句子一个不少", len(sents0) == 3, f"{sents0}")

    # 坑 1：`“你……”` 曾被切成 4 个"句子"，1 字碎片污染句长统计与"重复开头"
    sents = style_audit.split_sentences("“你……”\n他走了。\n“来了。坐。”\n")
    check("连续句末标点算同一个句号（不切出 … 空片段）",
          not any(len(style_audit.OPENER_RE.sub("", s)[:3]) < 2 for s in sents),
          f"{sents}")
    check("“你……”整体算一句，不是四句", len(sents) == 4, f"{sents}")
    # 坑 2："不过"（连接词，上限 4）被"再好不过""不过分"刷爆
    check("守卫词：'再好不过'不计入连接词'不过'",
          style_audit.count_word("这再好不过。他也不过问。", "不过") == 0,
          f"{style_audit.count_word('这再好不过。他也不过问。', '不过')}")
    check("守卫词：真正的连接词'不过'照样算",
          style_audit.count_word("不过，他还是来了。不过我没说。", "不过") == 2,
          f"{style_audit.count_word('不过，他还是来了。不过我没说。', '不过')}")
    # 坑 3：可延期的死线被误判成"倒计时倒退"
    renew = [node(1, hook_type="悬念", opening_type="对话", payoff="无",
                  payoff_strength=0, emotion="压抑", subplots="",
                  countdown="D-0（可延期）"),
             node(2, hook_type="危机", opening_type="动作", payoff="无",
                  payoff_strength=0, emotion="紧张", subplots="",
                  countdown="D-7（可延期）")]
    r_hard, _ = series_audit.check_nodes(renew)
    check("带『可延期』标记的死线不报倒计时倒退",
          not any("倒计时" in x for x in r_hard), f"{r_hard}")
    strict = [node(3, hook_type="悬念", opening_type="对话", payoff="无",
                   payoff_strength=0, emotion="压抑", subplots="", countdown="D-0"),
              node(4, hook_type="危机", opening_type="动作", payoff="无",
                   payoff_strength=0, emotion="紧张", subplots="", countdown="D-7")]
    s_hard, _ = series_audit.check_nodes(strict)
    check("不带标记时倒计时倒退照样报（守卫没把功能关掉）",
          any("倒计时倒退" in x for x in s_hard), f"{s_hard}")

    # 坑 4：一条死线结束、另一条全新死线开始 —— 连载里的常态，不该报"倒流"
    reset = [node(5, hook_type="悬念", opening_type="对话", payoff="无",
                  payoff_strength=0, emotion="压抑", subplots="", countdown="D-0"),
             node(6, hook_type="危机", opening_type="物件", payoff="无",
                  payoff_strength=0, emotion="紧张", subplots="",
                  countdown="D-1（重置）")]
    r2_hard, _ = series_audit.check_nodes(reset)
    check("带『重置』标记的新死线不报倒计时倒退",
          not any("倒计时倒退" in x for x in r2_hard), f"{r2_hard}")
    # 重置之后，新序列内部照样要单调
    reset_bad = [node(7, hook_type="悬念", opening_type="对话", payoff="无",
                      payoff_strength=0, emotion="压抑", subplots="",
                      countdown="D-3（重置）"),
                 node(8, hook_type="危机", opening_type="物件", payoff="无",
                      payoff_strength=0, emotion="紧张", subplots="", countdown="D-5")]
    rb_hard, _ = series_audit.check_nodes(reset_bad)
    check("重置之后，新序列内部倒退照样报",
          any("倒计时倒退" in x for x in rb_hard), f"{rb_hard}")

    # 坑 5：一跳跨了 10 天，正文里已经写了「出京第十日」这样的时间标记，
    # 但脚本只看得见 front-matter 的数字 —— 默认阈值 1 天，于是会一直提醒"需要过渡"。
    # 本书的规矩比它宽（超 3 天给一个明确时间标记即可），用 bands 按本书放宽。
    jump = [node(9, hook_type="悬念", opening_type="对话", payoff="无",
                 payoff_strength=0, emotion="压抑", subplots="", countdown="D-90"),
            node(10, hook_type="危机", opening_type="物件", payoff="无",
                 payoff_strength=0, emotion="紧张", subplots="", countdown="D-80")]
    _, j_soft = series_audit.check_nodes(jump)
    check("默认阈值下，一跳 10 天照样提醒需要过渡（豁免前行为不变）",
          any("倒计时跳跃" in x for x in j_soft), f"{j_soft}")
    _, j2_soft = series_audit.check_nodes(jump, {"countdown_jump_days_max": 10})
    check("bands.countdown_jump_days_max=10 时不再提醒",
          not any("倒计时跳跃" in x for x in j2_soft), f"{j2_soft}")
    _, j3_soft = series_audit.check_nodes(
        [node(11, hook_type="悬念", opening_type="对话", payoff="无",
              payoff_strength=0, emotion="压抑", subplots="", countdown="D-20"),
         node(12, hook_type="危机", opening_type="物件", payoff="无",
              payoff_strength=0, emotion="紧张", subplots="", countdown="D-8")],
        {"countdown_jump_days_max": 3})
    check("豁免只放宽到设定值：一跳 12 天（>3）仍然报",
          any("倒计时跳跃" in x for x in j3_soft), f"{j3_soft}")
    j4_hard, _ = series_audit.check_nodes(
        [node(13, hook_type="悬念", opening_type="对话", payoff="无",
              payoff_strength=0, emotion="压抑", subplots="", countdown="D-5"),
         node(14, hook_type="危机", opening_type="物件", payoff="无",
              payoff_strength=0, emotion="紧张", subplots="", countdown="D-9")],
        {"countdown_jump_days_max": 10})
    check("豁免只放宽「跳跃」，不放宽「倒退」（D-5→D-9 照样必须改）",
          any("倒计时倒退" in x for x in j4_hard), f"{j4_hard}")

    print("[15] 本书区间豁免（bands）：确认刻意风格就明确豁免，别让告警一直刷")
    # 夹具必须 ≥500 字，否则统计类判断被跳过（校验器自己的门槛）
    sample = "\n\n".join(["他站在门口，看了我一会儿，什么也没说就走了。"] * 30)
    r = style_audit.analyse(sample, [], None, {})
    check("夹具够长，统计类判断没有被跳过",
          r["chars"] >= style_audit.MIN_CHARS_FOR_STATS, f"chars={r['chars']}")
    check("夹具确实是高单句段占比（>默认上限 45%）",
          r["single_para_ratio"] > style_audit.MAX_SHORT_PARA,
          f"{r['single_para_ratio']:.0%}")
    _, soft_default, _ = style_audit.judge(r, None, False, None)
    check("默认区间下会报单句段占比",
          any("单句段占比" in s for s in soft_default), f"{soft_default}")
    _, soft_band, _ = style_audit.judge(r, None, False, {"short_para_max": 1.0})
    check("声明 bands 后不再报（豁免生效）",
          not any("单句段占比" in s for s in soft_band), f"{soft_band}")
    _, soft_bad, _ = style_audit.judge(r, None, False, {"short_para_max": "不是数字"})
    check("bands 值非法时安全回落到默认（不崩、不缺检查）",
          any("单句段占比" in s for s in soft_bad), f"{soft_bad}")

    print("[16] window_pass 要能落到具体位置（只报百分比等于没报）")
    # 全是 12 字的句子 → 既无短句也无长句，必不合格（要凑够 ≥200 字才会封窗）
    flat = ["他站在门口看了我一会儿。"] * 20
    rate, n, fails = style_audit.window_pass(flat)
    check("全匀长句时通过率为 0", rate == 0.0 and n >= 1, f"rate={rate} n={n}")
    check("报出不合格窗口的数量与明细",
          len(fails) == n and all("need" in f for f in fails), f"{fails}")
    check("明细里指明缺的是短句还是长句",
          bool(fails) and fails[0]["need"] == "短句＋长句", f"{fails[:1]}")
    check("明细带上了窗口开头的原文，便于定位",
          bool(fails) and fails[0]["head"].startswith("他站在门口"), f"{fails[:1]}")
    good = ["他走了。", "他站在门口看了我一会儿，最后还是什么都没说清楚就走了。"] * 8
    rate2, n2, fails2 = style_audit.window_pass(good)
    check("长短句都有时通过率为 100%", rate2 == 1.0 and not fails2, f"{rate2} {fails2}")

    print("[17] BOM 回归：带 UTF-8 BOM 的文件不能被静默漏读")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "novel"
        (root / "foreshadowing").mkdir(parents=True)
        table = (
            "| 编号 | 内容 | 类型 | 层级 | 标签 | 埋设章 | 计划回收章 | 状态 | 最近推进章 | 备注 |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| F001 | 断指 | 主线 | 核心 | 怪癖 | 第1章 | 第2章 | 已埋 | 第1章 |  |\n"
        )
        fs = root / "foreshadowing" / "伏笔表.md"
        fs.write_bytes(b"\xef\xbb\xbf" + table.encode("utf-8"))
        rows = series_audit.parse_foreshadowing(fs)
        check("带 BOM 的伏笔表仍解析出条目（修前是 0 条 → 全部伏笔检查静默失效）",
              len(rows) == 1, f"解析到 {len(rows)} 条")
        check("带 BOM 时列也读对（计划回收章 = 2）",
              bool(rows) and rows[0]["target"] == 2, f"{rows[0] if rows else None}")

        keyf = Path(tmp) / "key.txt"
        keyf.write_bytes(b"\xef\xbb\xbf" + b"sk-REGRESSION0123456789")

        class _KeyArgs:  # resolve_key 只读这两个属性
            key_file = str(keyf)
            key_env = "ZHUQUE_KEY_ENV_MUST_NOT_EXIST"

        got = zhuque_check.resolve_key(_KeyArgs())
        check("带 BOM 的密钥文件读出干净密钥（否则 Bearer 里带 U+FEFF 必然 401）",
              got == "sk-REGRESSION0123456789", repr(got))

        # 写侧必须仍是 utf-8（不带 BOM），否则会把 BOM 写进所有生成文件
        with contextlib.redirect_stdout(io.StringIO()):
            continuity_check.main(["--novel", str(root), "--write-allowlist"])
        wf = root / "state" / "连续性白名单.md"
        check("脚本生成的文件不带 BOM（写入口径没被带偏）",
              wf.is_file() and not wf.read_bytes().startswith(b"\xef\xbb\xbf"),
              f"文件存在={wf.is_file()}")

    print("[18] 目录递归与章号重复：不许静默少看，也不许两个脚本给出不同答案")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "novel"
        (root / "chapters" / "卷1").mkdir(parents=True)
        (root / "plot").mkdir(parents=True)
        dup = "他推开门。屋里没人。窗台上放着一只旧碗。"
        for n, t in ((1, "灯下"), (2, "旧账")):
            (root / "chapters" / "卷1" / f"第{n}章-{t}.md").write_text(
                f"# 第{n}章\n\n{dup}\n", encoding="utf-8")
        (root / "chapters" / "卷1" / "没有章号.md").write_text("# x\n\n占位\n", encoding="utf-8")

        chapters = continuity_check.load_chapters(root / "chapters")
        check("章节放在按卷子目录里也看得见（修前是 0 章）",
              len(chapters) == 2, f"看到 {len(chapters)} 章")
        sents = {c: continuity_check.split_sentences(chapters[c][1]) for c in chapters}
        xhard, _ = continuity_check.check_cross_chapter(sents, set())
        check("子目录布局下跨章重复照样报", bool(xhard), f"{xhard}")

        _, inv_soft = continuity_check.check_file_inventory(root)
        check("文件名没有章号会被提示（否则整份被静默漏掉）",
              any("没有章号" in x for x in inv_soft), f"{inv_soft}")

        # 两个节点文件都声称是第 3 章
        for name in ("节点-第0003章.md", "节点-第03章.md"):
            (root / "plot" / name).write_text(
                "---\nchapter: 3\ntitle: t\nhook_type: 反转\nopening_type: 对话\n"
                "payoff: 小爽点\npayoff_strength: 3\nemotion: 压抑\ncountdown: D-5\n---\n",
                encoding="utf-8")
        inv_hard, _ = continuity_check.check_file_inventory(root)
        check("continuity 报出节点章号重复",
              any("章号重复" in x for x in inv_hard), f"{inv_hard}")
        se_hard, _ = series_audit.check_nodes(series_audit.load_nodes(root / "plot"))
        check("series_audit 同样报出章号重复（两边不能给出不同答案）",
              any("章号重复" in x for x in se_hard), f"{se_hard}")

    print("[19] 漂移绝对下限 / 位置信息 / 声纹覆盖")
    # 夹具必须 ≥500 字，否则 judge() 在统计类判断前就 return 了（这个坑踩过三次）
    sample = "\n\n".join(["他站在门口看了我一会儿，最后还是什么都没说就走了。"] * 26)
    r = style_audit.analyse(sample, [], None, {})
    check("夹具够长，漂移块真的会跑",
          r["chars"] >= style_audit.MIN_CHARS_FOR_STATS, f"chars={r['chars']}")
    base = {"weak_adverbs_per_1k": 0.373, "chars": r["chars"]}

    small = dict(r)
    small["weak_adverbs_per_1k"] = 0.45          # 相对 +20%，绝对只差 0.077
    _, _, d_small = style_audit.judge(small, base, False, None)
    check("小量级指标只差一点 → 不算漂移（修前会报，13 章刷出 11 条假漂移）",
          not any("weak_adverbs" in x and "升到" in x for x in d_small), f"{d_small}")
    check("默认不打印被下限挡住的波动（否则又是新噪音）",
          not any("绝对下限" in x for x in d_small), f"{d_small}")
    _, _, d_explain = style_audit.judge(small, base, False, None, True)
    check("加 --explain-drift 才说明被挡住的波动",
          any("绝对下限" in x for x in d_explain), f"{d_explain}")
    big = dict(r)
    big["weak_adverbs_per_1k"] = 2.0             # 相对 +436%，绝对差 1.63
    _, _, d_big = style_audit.judge(big, base, False, None)
    check("明显变化照样报漂移（守卫没把功能关掉）",
          any("weak_adverbs" in x and "升到" in x for x in d_big), f"{d_big}")
    edge = dict(r)
    edge["tri_rep"] = 0.037                      # 与基线 0.027 恰好差 0.010 = 绝对下限
    _, _, d_edge = style_audit.judge(edge, {"tri_rep": 0.027, "chars": r["chars"]}, False, None)
    check("差值恰好等于绝对下限时应算漂移（浮点边界）",
          any("tri_rep" in x and "升到" in x for x in d_edge), f"{d_edge}")

    def _old_same_len(lengths):
        n = len(lengths)
        if not n:
            return 0
        return style_audit.max_run(
            [abs(lengths[i] - lengths[i - 1]) <= 2 for i in range(1, n)]) + 1

    for case in ([10, 10, 10, 40, 41, 42, 43, 90], [10, 20, 30], [7]):
        check(f"最长同长度段与旧算法等价 {case}",
              style_audit.longest_same_len_run(case)[0] == _old_same_len(case))

    r_black = style_audit.analyse("他走进来。\n\n他嘴角勾起，说了句什么。\n", ["嘴角勾起"], None, {})
    h_black, _, _ = style_audit.judge(r_black, None, False, None)
    check("黑名单命中带段落落点（不再只说『几处』）",
          any("嘴角勾起" in x and "第2段" in x for x in h_black), f"{h_black}")

    r_long = style_audit.analyse("他" * 420 + "。\n", [], None, {})
    h_long, _, _ = style_audit.judge(r_long, None, False, None)
    check("超长段落报出是第几段、多少字",
          any("段超过" in x for x in h_long)
          and any(x.strip().startswith("└ 第1段") for x in h_long), f"{h_long}")

    r_flat = style_audit.analyse("他站在门口看了我一会儿。" * 8, [], None, {})
    h_flat, _, _ = style_audit.judge(r_flat, None, False, None)
    check("连续同长度句报出从哪一段开始",
          any("长度几乎相同" in x for x in h_flat)
          and any("开始" in x for x in h_flat), f"{h_flat}")

    with tempfile.TemporaryDirectory() as tmp:
        nf = Path(tmp) / "节点-第0009章.md"
        nf.write_text("---\nchapter: 9\ncharacters: 陆平, 老周\n---\n\n# 第9章\n", encoding="utf-8")
        dec = style_audit.load_declared_characters(nf)
        check("能读出节点声明的出场角色", dec == ["陆平", "老周"], f"{dec}")
        cov1 = style_audit.check_fingerprint_coverage(dec, {"陆平": ["x"]})
        check("该传没传的角色会被点名（B003 同类风险）",
              any("老周" in x for x in cov1), f"{cov1}")
        cov2 = style_audit.check_fingerprint_coverage(
            dec, {"陆平": ["x"], "老周": ["y"], "沈九": ["z"]})
        check("传了没出场的人也会提示（多半是节点漏填）",
              any("沈九" in x for x in cov2), f"{cov2}")
        cov3 = style_audit.check_fingerprint_coverage(dec, {"陆平": ["x"], "老周": ["y"]})
        check("覆盖完整时不吭声", cov3 == [], f"{cov3}")

    print("[20] 伏笔表的枚举与去重（口径借鉴自 InkOS 的 schema 校验）")
    table = (
        "| 编号 | 内容 | 类型 | 层级 | 标签 | 埋设章 | 计划回收章 | 状态 | 最近推进章 | 备注 |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| F001 | 樟木箱里的照片 | 主线 | 核心 | 物件 | 第3章 | 第40章 | 已埋 | 第5章 |  |\n"
        "| F001 | 老周的欠条 | 人物 | 支线 | 身份 | 第4章 | 第50章 | 推进中 | 第6章 |  |\n"
        "| F002 | 樟木箱里的旧照片 | 主线 | 核心 | 物件 | 第7章 | 第45章 | 已埋 | 第8章 |  |\n"
        "| F003 | 收件单被代签 | 世界观 | 装饰 | 物件 | 第9章 | 第30章 | 已回收了 | 第10章 |  |\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        fp = Path(tmp) / "伏笔表.md"
        fp.write_text(table, encoding="utf-8")
        frows = series_audit.parse_foreshadowing(fp)
        check("伏笔表读出 4 条", len(frows) == 4, f"{len(frows)}")
        check("顺便取到内容列（查重复承诺要用）",
              bool(frows) and frows[0]["content"] == "樟木箱里的照片",
              f"{frows[0] if frows else None}")
        fh, fs = series_audit.check_foreshadowing(frows, current=12)
        check("编号重复判必须改", any("编号重复" in x for x in fh), f"{fh}")
        check("状态不在白名单会提示（写错的状态会被当成仍在进行）",
              any("白名单" in x for x in fs), f"{fs}")
        check("改名造成的重复承诺会被抓到（差一个字也要抓）",
              any("疑似同一条承诺" in x for x in fs), f"{fs}")

    print("[21] 风格贴合（--style-from）：拿参照文本当目标，双边比对")
    same = style_audit.compare_style({"mean_len": 15.0}, {"mean_len": 15.0})
    check("指标一致时命中", bool(same) and all(r["ok"] for r in same), f"{same}")
    hi = style_audit.compare_style({"mean_len": 15.0}, {"mean_len": 27.0})
    check("双边：比参照高 80% 算偏离", bool(hi) and not hi[0]["ok"], f"{hi}")
    lo = style_audit.compare_style({"mean_len": 15.0}, {"mean_len": 6.0})
    check("双边：比参照低 60% 也算偏离", bool(lo) and not lo[0]["ok"], f"{lo}")
    block = style_audit.render_style_match("参照.md", hi)
    check("输出带中文标签、两边数值与方向",
          "平均句长" in block and "15.0" in block and "27.0" in block and "高" in block, block)

    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "参照.md"
        chap = Path(tmp) / "本章.md"
        ref.write_text("他站在门口看了我一会儿。" * 30, encoding="utf-8")
        chap.write_text(
            "\n\n".join(["他走了。", "他站在门口看了我一会儿，最后还是什么都没说就走了。"] * 12),
            encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = style_audit.main([str(chap), "--style-from", str(ref)])
        out = buf.getvalue()
        check("--style-from 会打印风格贴合块",
              "风格贴合" in out and "贴合度" in out, out[-200:])
        check("差异明显时不会报满分", "贴合度 12/12" not in out, out[-200:])
        check("风格贴合不参与硬门槛判定（不加 --strict 时退 0）", rc == 0, f"rc={rc}")

    print("[22] 文档交叉引用完整性（搬内容、改文件名时最容易搬坏的东西）")
    doc_root = Path(__file__).resolve().parent.parent
    ref_re = re.compile(r"(?<![\w/\-])(?:references|examples)/[^\s`），。；]+\.md")
    doc_files = [doc_root / "SKILL.md"] + sorted((doc_root / "references").glob("*.md"))
    dangling: list[str] = []
    scanned = 0
    for f in doc_files:
        if not f.is_file():
            continue
        scanned += 1
        for i, line in enumerate(f.read_text(encoding="utf-8-sig").splitlines(), 1):
            if "尚未创建" in line:          # 明确标注的"将来才建的文件"不算失效引用
                continue
            if "inkos" in line.lower():     # 台账里引用第三方项目的路径（如 InkOS 的 references/xxx.md）
                continue                    # 约定：引用外部项目的行必须写出项目名，否则会被误判
            for m in ref_re.finditer(line):
                target = m.group(0).rstrip(".。，）")
                if not (doc_root / target).is_file():
                    dangling.append(f"{f.name}:{i}→{target}")
    check(f"扫描到 {scanned} 份文档", scanned >= 14, f"实际 {scanned}")
    check("references/ 与 examples/ 里被引用的 .md 都真实存在",
          not dangling, "；".join(dangling[:5]))
    check("台账里引用的脚本文件名也都存在",
          all((doc_root / "scripts" / n).is_file()
              for n in ("style_audit.py", "series_audit.py", "continuity_check.py",
                        "zhuque_check.py", "init_novel.py", "selftest.py")),
          "缺脚本文件")
    # 完结清单是最容易被写成"孤儿文档"的一份：文件在仓库里，主干却不指向它。
    # 那样它等于不存在——而完结是唯一不可回滚的一步。
    skill_md = (doc_root / "SKILL.md").read_text(encoding="utf-8")
    check("SKILL.md 必须指向完结清单（完结阶段的主干入口）",
          "completion-checklist.md" in skill_md, "SKILL.md 里没提到它")
    check("SKILL.md 的完结阶段必须写明「钩子轮换在完结章不适用」",
          "钩子" in skill_md and "完结章" in skill_md and "不适用" in skill_md,
          "缺少这条已知例外，会有人为了消告警给完结章硬加钩子")
    # README 里还写死了两个数字：黑名单条数、references 份数。都是文档承诺，都会过期。
    n_black = len(style_audit.load_blacklist(BLACKLIST_PATH))
    ridx = (SKILL_DIR / "README.md").read_text(encoding="utf-8")
    claimed_black = {int(m) for m in re.findall(r"AI 味黑名单（(\d+) 条）", ridx)}
    check(f"README 写的黑名单条数与实际一致（{n_black} 条）",
          claimed_black == {n_black}, f"文档写 {sorted(claimed_black)}，实际 {n_black}")
    n_refs = len(list((SKILL_DIR / "references").glob("*.md")))
    claimed_refs = {int(m) for m in re.findall(r"(\d+)\s*份参考", ridx)}
    check(f"README 写的 references 份数与实际一致（{n_refs} 份）",
          claimed_refs == {n_refs}, f"文档写 {sorted(claimed_refs)}，实际 {n_refs}")

    print("[23] 改稿护栏 / 章节号污染 / 伏笔「最近推进章」（口径借鉴 InkOS）")

    old_r = style_audit.analyse(
        "他嘴角勾起，心中暗道不好。\n\n" + "他站在门口看了我一会儿。" * 12,
        ["嘴角勾起", "心中暗道"], None, {})
    check("AI 标记计数把黑名单命中算进去",
          style_audit.ai_mark_total(old_r) >= 2, str(style_audit.ai_mark_total(old_r)))
    better = dict(old_r)
    better["blacklist_hits"] = {}
    txt_better = style_audit.render_revision_compare("旧.md", old_r, better, ["黑名单命中：x"], [])
    check("标记变少 → 判改善", "改善" in txt_better, txt_better)
    worse = dict(better)
    worse["blacklist_hits"] = {"不禁": 5}
    txt_worse = style_audit.render_revision_compare("旧.md", better, worse, [], ["黑名单命中：不禁×5"])
    check("标记变多 → 判变差并要求保留旧稿",
          "变差" in txt_worse and "保留旧稿" in txt_worse, txt_worse)
    txt_same = style_audit.render_revision_compare("旧.md", old_r, old_r, [], [])
    check("无净变化 → 明说这一轮没起作用", "净变化" in txt_same, txt_same)
    grown = dict(old_r)
    grown["chars"] = int(old_r["chars"] * 1.5)
    check("字数超出 ±10% 会被点出来（最小化改动）",
          "超出" in style_audit.render_revision_compare("旧.md", old_r, grown, [], []), "")

    meta = style_audit.analyse(
        "# 第13章-标题\n\n他说第33章还没写完。\n", [], None, {})
    check("抓到正文里的「第33章」",
          any("第33章" in x for x in meta["meta_chapter_hits"]), str(meta["meta_chapter_hits"]))
    check("标题行（# 开头）不算元叙述",
          not any("第13章" in x for x in meta["meta_chapter_hits"]), str(meta["meta_chapter_hits"]))
    h_meta, s_meta, _ = style_audit.judge(meta, None, False, None)
    check("章节号污染只提示、不判必须改（引用文献标题时可能是有意的）",
          not any("元叙述" in x for x in h_meta) and any("元叙述" in x for x in s_meta),
          f"h={h_meta} s={s_meta}")

    with tempfile.TemporaryDirectory() as tmp:
        fp2 = Path(tmp) / "伏笔表.md"
        fp2.write_text(
            "| 编号 | 内容 | 类型 | 层级 | 标签 | 埋设章 | 计划回收章 | 状态 | 最近推进章 | 备注 |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| F010 | 收件单被代签 | 世界观 | 装饰 | 物件 | 第1章 | 第60章 | 推进中 | 第2章 |  |\n"
            "| F011 | 老周的欠条 | 人物 | 支线 | 身份 | 第5章 | 第70章 | 推进中 | 第12章 |  |\n",
            encoding="utf-8")
        rows2 = series_audit.parse_foreshadowing(fp2)
        check("解析出「最近推进章」（这一列以前收了却没用）",
              [r.get("last_advanced") for r in rows2] == [2, 12], str(rows2))
        _, s2 = series_audit.check_foreshadowing(rows2, current=20)
        check("久未推进的伏笔会被点名（被提到不算推进）",
              any("F010" in x and "没推进" in x for x in s2), str(s2))
        check("刚推进过的不会误报",
              not any("F011" in x and "没推进" in x for x in s2), str(s2))

    print("[24] 变化轴：把铁律「每章至少推进一项」变成可查的（借鉴 InkOS 双轴规则）")
    with tempfile.TemporaryDirectory() as tmp:
        pd = Path(tmp)
        fm = "hook_type: 悬念\nopening_type: 对话\npayoff: 小爽点\npayoff_strength: 3\n"
        (pd / "节点-第0001章.md").write_text(
            f"---\nchapter: 1\n{fm}axes: 剧情, 关系\n---\n", encoding="utf-8")
        (pd / "节点-第0002章.md").write_text(
            f"---\nchapter: 2\n{fm}axes: 剧情\n---\n", encoding="utf-8")
        (pd / "节点-第0003章.md").write_text(
            f"---\nchapter: 3\n{fm}---\n", encoding="utf-8")
        ax_nodes = series_audit.load_nodes(pd)
        check("节点能读到 axes 字段",
              str(ax_nodes[0].get("axes", "")) == "剧情, 关系", str(ax_nodes[0].get("axes")))
        _, s_ax = series_audit.check_nodes(ax_nodes)
        check("只写主轴、缺支撑轴 → 报写法不对",
              any("变化轴写法不对" in x for x in s_ax), str(s_ax))
        check("完全没写 axes → 只提示，不当错误（旧章不用回改）",
              any("没声明变化轴" in x for x in s_ax), str(s_ax))
        check("轴写对的节点不被点名", not any("第1章「" in x for x in s_ax), str(s_ax))

    print("[25] 引文核验闸门 + 伏笔表算术一致性（口径借鉴 InkOS 的 sourceRefs 硬门槛）")
    with tempfile.TemporaryDirectory() as tmp:
        ch = Path(tmp) / "第13章-洗出来的照片.md"
        ch.write_text("# 第13章\n\n他把照片翻过来，背面用铅笔写着一个日期。\n\n老周没说话。\n",
                      encoding="utf-8")
        good = Path(tmp) / "好报告.md"
        good.write_text("证据：「他把照片翻过来，背面用铅笔写着一个日期」——这句没解释。\n",
                        encoding="utf-8")
        bad_rp = Path(tmp) / "坏报告.md"
        bad_rp.write_text("证据：「他把照片翻过来，背面用钢笔写着一行字」——编造的引文。\n",
                          encoding="utf-8")
        q_ok = quote_check.extract_quotes(good.read_text(encoding="utf-8"))
        q_bad = quote_check.extract_quotes(bad_rp.read_text(encoding="utf-8"))
        src = quote_check.load_source([ch])
        check("抽出报告里的引文", len(q_ok) == 1, f"{q_ok}")
        ok1, bad1 = quote_check.check_quotes(q_ok, src)
        check("真引文能搜到", len(ok1) == 1 and not bad1, f"{ok1} / {bad1}")
        ok2, bad2 = quote_check.check_quotes(q_bad, src)
        check("编造的引文会被抓出来", len(bad2) == 1, f"{ok2} / {bad2}")
        check("太短的引文不判（可能只是泛指）",
              quote_check.extract_quotes("「他不知道」这句话很短。", min_len=6) == [], "")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_good = quote_check.main([str(good), "--chapter", str(ch)])
            rc_bad = quote_check.main([str(bad_rp), "--chapter", str(ch)])
        check("退出码：好报告 0、坏报告 1（能当闸门用）",
              rc_good == 0 and rc_bad == 1, f"{rc_good}/{rc_bad}")

    fs_arith = [
        {"id": "F020", "content": "照片背面的日期", "type": "主线", "planted": 13, "target": 5,
         "last_advanced": 13, "status": "已埋"},
        {"id": "F021", "content": "老周的沉默", "type": "人物", "planted": 40, "target": 50,
         "last_advanced": 1, "status": "已埋"},
    ]
    ah, asx = series_audit.check_foreshadowing(fs_arith, current=13, known_chapters={1, 13})
    check("计划回收章早于埋设章 → 算术不可能，必须改",
          any("早于埋设章" in x for x in ah), f"{ah}")
    check("埋设章在当前章之后 → 提示章号或状态写错",
          any("还没写到就登记" in x for x in asx), f"{asx}")
    check("埋设章没有对应节点 → 悬空引用",
          any("悬空引用" in x for x in asx), f"{asx}")
    _, asx2 = series_audit.check_foreshadowing(
        [{"id": "F030", "content": "正常条目", "type": "主线", "planted": 1, "target": 30,
          "last_advanced": 12, "status": "推进中"}], current=13, known_chapters={1})
    check("正常条目不被算术检查误报",
          not any("早于埋设章" in x or "悬空引用" in x for x in asx2), f"{asx2}")

    print("[26] 阈值裁决的落地：连续「了」句 / 公式化对称句式 / 段落软门槛 / 新增黑名单")
    # 夹具必须 ≥500 字：连续「了」句属于统计类判断，短文本会被直接跳过（这个坑踩过五次）
    le_txt = stat_fixture("他看了我一眼。")
    r_le = style_audit.analyse(le_txt, [], None, {})
    check("夹具够长，统计类判断真的会跑",
          r_le["chars"] >= style_audit.MIN_CHARS_FOR_STATS, f"chars={r_le['chars']}")
    check("算出最长连续带「了」句数", r_le["le_run_max"] >= 8, str(r_le["le_run_max"]))
    _, s_le, _ = style_audit.judge(r_le, None, False, None)
    check("超过门槛（8 句）会提示",
          any("都带「了」" in x for x in s_le), f"{s_le}")
    check("提示带落点（第几段、哪句起）",
          any(x.strip().startswith("└ 从第") for x in s_le), f"{s_le}")
    ok_le = style_audit.analyse(stat_fixture("他走了。天亮了。他坐下。风停了。"), [], None, {})
    check("对照组夹具也够长（否则'不提示'是假通过）",
          ok_le["chars"] >= style_audit.MIN_CHARS_FOR_STATS, f"chars={ok_le['chars']}")
    _, s_ok_le, _ = style_audit.judge(ok_le, None, False, None)
    check("没超过门槛（实测这本书的上限是 6）不提示",
          not any("都带「了」" in x for x in s_ok_le), f"{s_ok_le}")

    sym = style_audit.analyse(stat_fixture("这不是害怕，而是一种更冷的东西。"), [], None, {})
    check("抓到「不是……而是……」", bool(sym["symmetric_hits"]), str(sym["symmetric_hits"]))
    _, s_sym, _ = style_audit.judge(sym, None, False, None)
    check("对称句式只提示、不判必须改", any("公式化对称句式" in x for x in s_sym), f"{s_sym}")
    sym_ok = style_audit.analyse("他既怕又恨，可还是去了。", [], None, {})
    check("「既……又……」不误杀（正常中文，故意不收）",
          not sym_ok["symmetric_hits"], str(sym_ok["symmetric_hits"]))

    check("段落建议上限已按实测校准到 200", style_audit.SOFT_PARA_CHARS == 200,
          str(style_audit.SOFT_PARA_CHARS))
    check("段落硬门槛保持 400（这本书最长段 130，降它只会增加误伤）",
          style_audit.HARD_PARA_CHARS == 400, str(style_audit.HARD_PARA_CHARS))

    bl2 = style_audit.load_blacklist(doc_root / "references" / "ai-flavor-blacklist.md")
    for w in ("全场震惊", "众人哗然", "鸦雀无声", "面面相觑"):
        check(f"黑名单新增「{w}」（实测全书 0 处 → 零误报）", w in bl2, "")
    check("「所有人都」没被误加进黑名单", "所有人都" not in bl2, "")
    check("黑名单里没有脚本/文档文件名（反引号写文件名会被当禁用词）",
          not any(w.endswith((".py", ".md", ".json")) for w in bl2),
          str([w for w in bl2 if w.endswith((".py", ".md", ".json"))]))
    check("黑名单里没有含省略号的「句式模板」（字面量匹配不到，只会误伤）",
          not any("……" in w for w in bl2),
          str([w for w in bl2 if "……" in w]))
    check("黑名单里没有方括号（说明性文字被反引号带进来了）",
          not any("[" in w or "]" in w for w in bl2),
          str([w for w in bl2 if "[" in w or "]" in w]))

    print("[27] 场景卡结果 / 白名单加载与覆盖保护 / 伏笔废弃依据（B028 修的三处）")
    from itertools import chain as _chain

    with tempfile.TemporaryDirectory() as tmp:
        nd = Path(tmp) / "plot"
        nd.mkdir(parents=True)
        _fm = ("---\nchapter: {ch}\nhook_type: 悬念\nopening_type: 对话\npayoff: 小爽点\n"
               "payoff_strength: 3\naxes: 剧情, 关系\n---\n\n## 场景卡（2-4 张）\n\n"
               "| # | 场景目标 | 冲突 | 结果类型 | 后续 |\n| --- | --- | --- | --- | --- |\n")
        (nd / "节点-第0001章.md").write_text(
            _fm.format(ch=1) + "| 1 | 要账 | 他不认 | `yes_but` |  |\n"
            "| 2 | 逼问 | 他反咬 | `flat_no`（守第七条失败） |  |\n", encoding="utf-8")
        sc_nodes = series_audit.load_nodes(nd)
        check("解析出 2 张场景卡", len(sc_nodes[0].get("_scenes") or []) == 2,
              str(sc_nodes[0].get("_scenes")))
        _, s_sc = series_audit.check_nodes(sc_nodes)
        check("带反引号/括号说明的合法结果类型不误报（真实节点就是这么写的）",
              not any("场景卡" in x for x in s_sc), str(s_sc))

        (nd / "节点-第0002章.md").write_text(
            _fm.format(ch=2) + "| 1 | 谈判 | 无 |  |  |\n| 2 | 收尾 | 无 | 顺利解决了 |  |\n",
            encoding="utf-8")
        _, s_bad = series_audit.check_nodes(series_audit.load_nodes(nd))
        check("空结果类型会被抓", "空" in str(s_bad) and "场景卡" in str(s_bad), str(s_bad))
        check("「顺利解决了」会被抓", "顺利解决了" in str(s_bad), str(s_bad))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "state").mkdir(parents=True)
        wl = root / "state" / "连续性白名单.md"
        wl.write_text(
            "# 标题注释\n\n> 一行一条，写**归一化后的句子**；`#` 起注释。\n"
            "> 别拿它当消音器。\n\n# —— 某书的白名单 ——\n\n# 注释\n示例核心台词\n\n"
            "# 说明\n示例重复句\n", encoding="utf-8")
        loaded = continuity_check.load_allowlist(wl)
        check("白名单只加载真条目（跳过 # 注释行与 > 引用行）",
              len(loaded) == 2, f"{sorted(loaded)}")
        check("真条目确实加载了（而且不是说明行）",
              len(loaded) == 2 and not any("消音器" in x or "注释" in x for x in loaded),
              f"{sorted(loaded)}")
        check("引用行没被当成条目", not any("消音器" in x for x in loaded), f"{sorted(loaded)}")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_refuse = continuity_check.main(["--novel", str(root), "--write-allowlist"])
        check("已有白名单时拒绝覆盖（退出码 2）", rc_refuse == 2, f"rc={rc_refuse}")
        check("拒绝之后文件内容没被动过",
              "示例重复句" in wl.read_text(encoding="utf-8"), "")
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc_force = continuity_check.main(
                ["--novel", str(root), "--write-allowlist", "--force"])
        check("加 --force 才允许重建，且确实重建了",
              rc_force != 2 and "示例重复句" not in wl.read_text(encoding="utf-8"),
              f"rc={rc_force}")
        check("原子写没有留下 .tmp 残file",
              not list((root / "state").glob("*.tmp")),
              str(list((root / "state").glob("*.tmp"))))

    _, s_dep = series_audit.check_foreshadowing(
        [{"id": "F040", "content": "作废的线索", "type": "主线", "planted": 1, "target": 30,
          "last_advanced": 2, "status": "已废弃", "notes": ""}], current=13)
    check("伏笔标了已废弃却没写依据 → 提示",
          any("撤回必须有依据" in x for x in s_dep), str(s_dep))
    _, s_dep2 = series_audit.check_foreshadowing(
        [{"id": "F041", "content": "作废但有据", "type": "主线", "planted": 1, "target": 30,
          "last_advanced": 2, "status": "已废弃", "notes": "作者撤回了前提"}], current=13)
    check("写了依据就不再提示",
          not any("撤回必须有依据" in x for x in s_dep2), str(s_dep2))

    print("[28] 四个缺口的回归：伏笔表静默失效 / 字段名被改 / 字数比例可配 / 写前留痕")
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        # (a) 伏笔表：有「状态」列却解析出 0 条（B015 的 BOM 就是这类静默失效）
        broken = t / "坏伏笔表.md"
        broken.write_text("| 编号 | 内容 | 类型 | 埋设章 | 计划回收章 | 状态 |\n"
                          "| --- | --- | --- | --- | --- | --- |\n", encoding="utf-8")
        info_bad = series_audit.foreshadowing_table_info(broken)
        check("体检能看出「有状态列但 0 条」",
              info_bad["with_status"] == 1 and info_bad["rows"] == 0, str(info_bad))
        # (b) 列名被改（状态 -> 进度）
        renamed = t / "改名列.md"
        renamed.write_text("| 编号 | 内容 | 埋设章 | 计划回收章 | 进度 |\n"
                           "| --- | --- | --- | --- | --- |\n| F1 | 某伏笔 | 第1章 | 第9章 | 已埋 |\n",
                           encoding="utf-8")
        info_rn = series_audit.foreshadowing_table_info(renamed)
        check("体检能看出「没有任何表带状态列」（列名被改）",
              info_rn["headers"] and info_rn["with_status"] == 0, str(info_rn))
        # 正常表不该报警
        okfs = t / "好伏笔表.md"
        okfs.write_text("| 编号 | 内容 | 类型 | 埋设章 | 计划回收章 | 状态 |\n"
                        "| --- | --- | --- | --- | --- | --- |\n"
                        "| F1 | 某伏笔 | 主线 | 第1章 | 第9章 | 已埋 |\n", encoding="utf-8")
        info_ok = series_audit.foreshadowing_table_info(okfs)
        check("正常伏笔表体检通过",
              info_ok["rows"] == 1 and info_ok["with_status"] == 1
              and not info_ok["missing_columns"], str(info_ok))

        # (b2) 节点 front-matter 字段名被改 → 所有节点都缺它
        pd = t / "plot"
        pd.mkdir()
        (pd / "节点-第0001章.md").write_text(
            "---\nchapter: 1\n钩子类型: 悬念\nopening_type: 对话\npayoff: 小爽点\n"
            "payoff_strength: 3\nemotion: 紧张\naxes: 剧情, 关系\n---\n", encoding="utf-8")
        fm_nodes = series_audit.load_nodes(pd)
        fm_hard, fm_soft = series_audit.check_nodes(fm_nodes)
        check("字段名被改（hook_type→钩子类型）→ 判必须改",
              any("hook_type" in x for x in fm_hard), f"{fm_hard}")

        # (c) 字数比例可配 + 长度遥测
        cd2 = t / "chapters"
        cd2.mkdir()
        (cd2 / "第1章-甲.md").write_text("字" * 800, encoding="utf-8")
        (cd2 / "第2章-乙.md").write_text("字" * 990, encoding="utf-8")
        (pd / "节点-第0001章.md").write_text(
            "---\nchapter: 1\nhook_type: 悬念\nopening_type: 对话\npayoff: 小爽点\n"
            "payoff_strength: 3\nemotion: 紧张\naxes: 剧情, 关系\n---\n", encoding="utf-8")
        (pd / "节点-第0002章.md").write_text(
            "---\nchapter: 2\nhook_type: 反转\nopening_type: 动作\npayoff: 中爽点\n"
            "payoff_strength: 4\nemotion: 压抑\naxes: 剧情, 关系\n---\n", encoding="utf-8")
        two = series_audit.load_nodes(pd)
        lo90 = series_audit.check_word_floor(two, cd2, 1000, 0.9)
        lo80 = series_audit.check_word_floor(two, cd2, 1000, 0.8)
        check("比例 0.9 时 800 字那章低于下限", any("第 1 章" in x for x in lo90), f"{lo90}")
        check("比例改成 0.8 后同一章不再报警（比例真的生效了）",
              not any("第 1 章" in x for x in lo80), f"{lo80}")
        stats = series_audit.word_length_stats(two, cd2, 1000, 0.9)
        check("长度遥测给出均值与低于下限的章号",
              "均值" in stats and "低于下限 1 章" in stats, stats)

        # (d) 写前加载留痕
        nodes_d: dict = {
            1: ("节点-第0001章.md", "---\nchapter: 1\n---\n\n## 写前加载（留痕）\n\n"
                "- 读了：总纲、卷纲、节点-第0000章、角色-主角甲、伏笔表、风格档案\n"
                "- 硬约束：他不说守则\n"),
            2: ("节点-第0002章.md", "---\nchapter: 2\n---\n\n## 写前加载（留痕）\n\n- 读了：总纲\n"),
        }
        _, pw_soft = continuity_check.check_prewrite_trace(nodes_d)
        check("留痕太短会被提示", any("留痕太空" in x for x in pw_soft), f"{pw_soft}")
        check("留痕齐全的不被提示",
              not any("第0001章" in x or "节点-第0001章" in x for x in pw_soft), f"{pw_soft}")
        _, pw_soft2 = continuity_check.check_prewrite_trace(
            {3: ("节点-第0003章.md", "---\nchapter: 3\n---\n\n## 本章节点\n\n- 事件：x\n")})
        check("最新一章缺这一节 → 提醒一次",
              any("最新一章" in x and "写前加载" in x for x in pw_soft2), f"{pw_soft2}")
        _, pw_soft3 = continuity_check.check_prewrite_trace(
            {1: ("节点-第0001章.md", "---\nchapter: 1\n---\n\n## 写前加载\n\n"
                 "- 读了：一些东西\n- 约束：没有特别的\n")})
        check("留痕没提任何必读项 → 提示",
              any("没提到任何必读项" in x for x in pw_soft3), f"{pw_soft3}")

    print("[29] 方向跑偏的三个机器信号：主线断档 / 主角消失 / 卷节拍对账 + 总纲对账留痕")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "plot").mkdir(parents=True)
        (root / "outline").mkdir()
        (root / "characters").mkdir()
        (root / "characters" / "主角-甲.md").write_text("# 甲\n", encoding="utf-8")

        def _node(ch: int, strand: str, chars: str, extra: str = "") -> str:
            return (f"---\nchapter: {ch}\nstrand: {strand}\ncharacters: {chars}\n"
                    f"hook_type: 悬念\nopening_type: 对话\npayoff: 小爽点\n"
                    f"payoff_strength: 3\nemotion: 紧张\naxes: 剧情, 关系\n---\n\n{extra}")

        # 1) 主线断档：连续 3 章 strand=支线
        for ch in (1, 2, 3):
            (root / "plot" / f"节点-第{ch:04d}章.md").write_text(
                _node(ch, "支线", "甲"), encoding="utf-8")
        nd = series_audit.load_nodes(root / "plot")
        h_dr, s_dr = series_audit.check_direction_drift(nd, root)
        check("连续 3 章 strand 不是主线 → 主线断档提示",
              any("主线断档" in x for x in s_dr), f"{s_dr}")
        check("但它是提示不是必须改（自报字段 + 支线弧可能刻意）", not h_dr, f"{h_dr}")
        (root / "plot" / "节点-第0004章.md").write_text(
            _node(4, "主线", "甲"), encoding="utf-8")
        # 阈值边界：只有 2 章断档不提示（第 1-3 章那 3 章是真的断过，仍然要报）
        (root / "plot" / f"节点-第0020章.md").write_text(_node(20, "支线", "甲"), encoding="utf-8")
        (root / "plot" / f"节点-第0021章.md").write_text(_node(21, "支线", "甲"), encoding="utf-8")
        (root / "plot" / f"节点-第0022章.md").write_text(_node(22, "主线", "甲"), encoding="utf-8")
        _, s_dr2 = series_audit.check_direction_drift(
            series_audit.load_nodes(root / "plot"), root)
        check("只有 2 章断档时不提示（阈值是 3 章）",
              not any("第 20-21 章" in x for x in s_dr2), f"{s_dr2}")
        check("真断过 3 章的仍然要报（回到主线也不抹掉历史）",
              any("第 1-3 章" in x for x in s_dr2), f"{s_dr2}")

        # 2) 主角消失：连续 3 章 characters 里没有主角
        for ch in (5, 6, 7):
            (root / "plot" / f"节点-第{ch:04d}章.md").write_text(
                _node(ch, "主线", "乙, 丙"), encoding="utf-8")
        _, s_pg = series_audit.check_direction_drift(
            series_audit.load_nodes(root / "plot"), root)
        check("主角连续 3 章不出场 → 提示", any("连续 3 章没出场" in x for x in s_pg), f"{s_pg}")
        check("主角出场时不提示", not any("第 5-7 章" in x and "没出场" in x
                                        for x in s_pg if "第 8" in x), f"{s_pg}")

        # 3) 卷节拍对账
        (root / "outline" / "卷纲-第1卷.md").write_text(
            "# 卷纲\n\n## 卷节拍表（六段）\n\n| # | 节拍 | 内容 | 章号 |\n"
            "| --- | --- | --- | --- |\n| 1 | 开局钩子 | x | 第 1 章 |\n"
            "| 2 | 升级危机链 | y | 第 4 章 |\n| 3 | 中段反转 | z | 第 41 章 |\n",
            encoding="utf-8")
        _, s_vb = series_audit.check_volume_beats(
            series_audit.load_nodes(root / "plot"), root, current=13)
        check("对账出「最近一个已过的节拍」",
              any("最近一个已过" in x and "升级危机链" in x for x in s_vb), f"{s_vb}")
        check("对账出「下一个节拍还差几章」",
              any("中段反转" in x and "还差 28 章" in x for x in s_vb), f"{s_vb}")
        (root / "outline" / "卷纲-第2卷.md").write_text("# 没有节拍表的卷纲\n", encoding="utf-8")
        _, s_vb2 = series_audit.check_volume_beats(
            series_audit.load_nodes(root / "plot"), root, current=13)
        check("卷纲缺「卷节拍表」段 → 明说跳过（不静默）",
              any("找不到「卷节拍表」段" in x for x in s_vb2), f"{s_vb2}")

        # 4) 总纲对账留痕：第 10/20 章都是 10 的倍数，都缺这一节
        (root / "plot" / "节点-第0010章.md").write_text(
            _node(10, "主线", "甲"), encoding="utf-8")
        _, s_rc = series_audit.check_outline_reconciliation(
            series_audit.load_nodes(root / "plot"))
        check("缺「总纲对账」段 → 只报最近一个（第 20 章），一次提一个",
              len(s_rc) == 1 and "第 20 章" in s_rc[0], f"{s_rc}")
        (root / "plot" / "节点-第0020章.md").write_text(
            _node(20, "主线", "甲", "## 总纲对账\n\n- 主线在轨\n- 无该拉回的支线\n"),
            encoding="utf-8")
        _, s_rc2 = series_audit.check_outline_reconciliation(
            series_audit.load_nodes(root / "plot"))
        check("补上最近的之后，改报剩下那个（第 10 章）",
              len(s_rc2) == 1 and "第 10 章" in s_rc2[0], f"{s_rc2}")
        (root / "plot" / "节点-第0010章.md").write_text(
            _node(10, "主线", "甲", "## 总纲对账\n\n- 主线在轨\n"), encoding="utf-8")
        _, s_rc3 = series_audit.check_outline_reconciliation(
            series_audit.load_nodes(root / "plot"))
        check("都补上之后不再提示", not s_rc3, f"{s_rc3}")

    print("[30] 伏笔表三处对账：活跃表 × 回收记录 ×「本章伏笔操作」")
    fs_text = (
        "| 编号 | 内容 | 类型 | 层级 | 标签 | 埋设章 | 计划回收章 | 状态 | 最近推进章 | 备注 |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| F001 | 还在推进的 | 主线 | 核心 | 身份 | 第1章 | 第99章 | 推进中 | 第3章 |  |\n"
        "| F002 | 说收了没收的 | 道具 | 支线 | 物件 | 第1章 | 第9章 | 推进中 | 第3章 |  |\n"
        "| F003 | 已回收但还留在这 | 人物 | 支线 | 怪癖 | 第1章 | 第9章 | 已回收 | 第5章 |  |\n"
        "\n## 本章伏笔操作（每次写章后填）\n\n- **第 5 章**：\n"
        "  - 新埋：—\n  - 推进：F001\n  - 回收：F002、F009\n\n"
        "## 回收记录（已回收）\n\n"
        "| 编号 | 内容 | 埋设章 | 回收章 | 回收方式 |\n| --- | --- | --- | --- | --- |\n"
        "| F003 | 已回收的 | 第1章 | 第5章 | 正面回应 |\n|  |  |  |  |  |\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        fp3 = Path(tmp) / "伏笔表.md"
        fp3.write_text(fs_text, encoding="utf-8")
        r3 = series_audit.parse_foreshadowing(fp3)
        arch3 = series_audit.parse_foreshadow_archive(fp3)
        check("活跃表解析出 3 条（回收记录表被正确跳过）", len(r3) == 3, f"{len(r3)}")
        check("回收记录表解析出 1 条（空行被跳过）", len(arch3) == 1, f"{arch3}")
        h_c, s_c = series_audit.check_foreshadow_consistency(r3, arch3)
        check("同时出现在两张表里 → 不报（F003 状态已是已回收，不算矛盾）",
              not any("同时出现" in x for x in h_c), f"{h_c}")
        check("活跃表里留着状态=已回收的行 → 提示移出",
              any("还留着状态" in x for x in s_c), f"{s_c}")
        h_r, s_r = series_audit.check_recovery_declared(r3, fp3)
        check("「说收了没收」→ 必须改",
              any("F002" in x and "说收了没收" in x for x in h_r), f"{h_r}")
        check("回收了不存在的编号 → 提示",
              any("F009" in x for x in s_r), f"{s_r}")
        # 一致的情形：活跃表还在推进、回收记录另有一条，两者无交集
        r4 = [x for x in r3 if x["id"] != "F003"]
        h_c2, s_c2 = series_audit.check_foreshadow_consistency(
            [dict(x, status="推进中") for x in r4], arch3)
        check("活跃表与回收记录无交集时不报矛盾",
              not any("同时出现" in x for x in h_c2), f"{h_c2}")

    print("[37] 伏笔回收后移出活跃表 → 检查器必须认得「回收记录」（B041）")
    # 按技能自己的规矩：伏笔回收后要记进「回收记录」并从活跃表移出
    # （见 references/foreshadowing-template.md 的回收规则 6）。可检查器原来只认活跃表，
    # 于是"照规矩做"被判成"回收了不存在的编号"——**正确做法被报成错误**。
    with tempfile.TemporaryDirectory() as tmp:
        fp4 = Path(tmp) / "伏笔表.md"
        fp4.write_text(
            "# 伏笔表\n\n## 活跃伏笔（已埋 / 推进中）\n\n"
            "| 编号 | 内容 | 类型 | 层级 | 标签 | 埋设章 | 计划回收章 | 状态 | 最近推进章 | 备注 |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| F001 | 甲 | 主线 | 核心 | 身份 | 第1章 | 第420章 | 已埋 | 第3章 | —— |\n\n"
            "## 本章伏笔操作（每次写章后填）\n\n"
            "**第20章**\n\n- 新埋：无\n- 推进：无\n- 回收：#F003（在交账那一段回应）\n- 碰一下：无\n\n"
            "## 回收记录（已回收，一卷满后移入 novel/archive/）\n\n"
            "| 编号 | 内容 | 埋设章 | 回收章 | 回收方式 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| F003 | 乙 | 第1章 | 第20章 | 交账时掀开 |\n",
            encoding="utf-8")
        r5 = series_audit.parse_foreshadowing(fp4)
        h5b, s5b = series_audit.check_recovery_declared(r5, fp4)
        check("回收后移出活跃表、记进回收记录 → 不再报「回收了不存在的编号」",
              not any("没有的编号" in x for x in h5b + s5b), f"{h5b} {s5b}")

        # 反向保护：既不在活跃表、也不在回收记录里 → 照旧报
        fp5 = Path(tmp) / "伏笔表5.md"
        fp5.write_text(fp4.read_text(encoding="utf-8").replace(
            "| F003 | 乙 | 第1章 | 第20章 | 交账时掀开 |\n", ""), encoding="utf-8")
        r6 = series_audit.parse_foreshadowing(fp5)
        h6b, s6b = series_audit.check_recovery_declared(r6, fp5)
        check("两处都没有的编号照旧报出来（放宽没变成关掉）",
              any("没有的编号" in x for x in h6b + s6b), f"{h6b} {s6b}")

    print("[31] 未知字段/列名（zod .strict() 的等价物）")
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        pd = t / "plot"
        pd.mkdir()
        (pd / "节点-第0001章.md").write_text(
            "---\nchapter: 1\nhook_type: 悬念\nopening_type: 对话\npayoff: 小爽点\n"
            "payoff_strength: 3\nemotion: 紧张\naxes: 剧情, 关系\n"
            "hooktype: 拼错了的字段\n---\n", encoding="utf-8")
        unk = series_audit.check_unknown_fields(series_audit.load_nodes(pd), t / "无.md")
        check("节点里的拼错字段会被报出来",
              any("hooktype" in x and "未知字段" in x for x in unk), f"{unk}")

        (pd / "节点-第0002章.md").write_text(
            "---\nchapter: 2\nhook_type: 悬念\nopening_type: 对话\npayoff: 小爽点\n"
            "payoff_strength: 3\nemotion: 紧张\naxes: 剧情, 关系\n---\n", encoding="utf-8")
        unk2 = series_audit.check_unknown_fields(
            [n for n in series_audit.load_nodes(pd) if n["_chapter"] == 2], t / "无.md")
        check("合法字段一个都不报（零误报）", not unk2, f"{unk2}")

        fs = t / "伏笔表.md"
        fs.write_text("| 编号 | 内容 | 状态 | 推进章 |\n| --- | --- | --- | --- |\n"
                      "| F1 | 某条 | 已埋 | 第2章 |\n", encoding="utf-8")
        unk3 = series_audit.check_unknown_fields([], fs)
        check("伏笔表的未知列会被报出来（「推进章」应为「最近推进章」）",
              any("推进章" in x for x in unk3), f"{unk3}")
        fs.write_text("| 编号 | 内容 | 状态 | 最近推进章 |\n| --- | --- | --- | --- |\n"
                      "| F1 | 某条 | 已埋 | 第2章 |\n", encoding="utf-8")
        check("伏笔表合法列不报", not series_audit.check_unknown_fields([], fs), "")

        # 缺列的行不能让脚本崩（B032：原来在 cells[i_status] 直接 IndexError，
        # 而这一层的存在意义恰恰是把畸形表格报出来）
        short_rows = t / "短行.md"
        short_rows.write_text(
            "| 编号 | 内容 | 埋设章 | 计划回收章 | 状态 |\n"
            "| --- | --- | --- | --- | --- |\n| F1\n| F2 |\n", encoding="utf-8")
        info_s = series_audit.foreshadowing_table_info(short_rows)
        check("少写一个 `|` 的行不崩（回归 B032）", isinstance(info_s, dict), str(info_s))
        rows_s = series_audit.parse_foreshadowing(short_rows)
        check("缺列的行不会抛异常", isinstance(rows_s, list), str(rows_s))
        h_s, s_s = series_audit.check_foreshadowing(rows_s, current=3)
        check("缺列的行会以「（空）」状态被报出来，而不是静默",
              any("（空）" in x for x in s_s) or any("计划回收章" in x for x in s_s), f"{s_s}")

        # 长度缓存与原行为一致（优化不能改结果）
        cd3 = t / "ch3"
        cd3.mkdir()
        (cd3 / "第1章-a.md").write_text("字" * 100, encoding="utf-8")
        nodes3 = [{"_chapter": 1}]
        no_cache = series_audit.check_word_floor(nodes3, cd3, 200, 0.9)
        cache: dict = {}
        with_cache = series_audit.check_word_floor(nodes3, cd3, 200, 0.9, cache)
        check("带缓存与不带缓存的字数判定完全一致（优化不改结果）",
              no_cache == with_cache, f"{no_cache} vs {with_cache}")
        check("缓存确实被填上了", cache.get(1) == 100, str(cache))
        st_c = series_audit.word_length_stats(nodes3, cd3, 200, 0.9, cache)
        st_n = series_audit.word_length_stats(nodes3, cd3, 200, 0.9)
        check("长度遥测带缓存也不变", st_c == st_n, f"{st_c} vs {st_n}")

    print("[32] 章节索引（SQLite + FTS5 trigram）：只是缓存，原文永远权威")
    with tempfile.TemporaryDirectory() as tmp:
        iroot = Path(tmp)
        for d in ("chapters", "plot", "characters", "state"):
            (iroot / d).mkdir(parents=True)
        (iroot / "chapters" / "第1章-甲.md").write_text(
            "# 第1章\n\n他把手在裤腿上擦了一下。编号-07在床头，编号-07。\n", encoding="utf-8")
        (iroot / "chapters" / "第2章-乙.md").write_text(
            "# 第2章\n\n老周没说话。老周走了。\n", encoding="utf-8")
        (iroot / "characters" / "主角-甲.md").write_text("# 甲\n", encoding="utf-8")
        (iroot / "characters" / "角色-老周.md").write_text("# 老周\n", encoding="utf-8")
        (iroot / "plot" / "节点-第0001章.md").write_text(
            "---\nchapter: 1\ncharacters: 甲\n---\n", encoding="utf-8")
        (iroot / "plot" / "节点-第0002章.md").write_text(
            "---\nchapter: 2\ncharacters: 老周\n---\n", encoding="utf-8")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_b = index_search.main(["build", "--novel", str(iroot)])
            con = index_search.open_db(iroot)
            long_hits = index_search.search(con, "编号-07", 10) if con else []
            short_hits = index_search.search(con, "老周", 10) if con else []
            if con:
                con.close()
            rc_v = index_search.main(["verify", "--novel", str(iroot)])
            (iroot / "plot" / "节点-第0002章.md").write_text(
                "---\nchapter: 2\ncharacters: 甲\n---\n", encoding="utf-8")
            rc_v2 = index_search.main(["verify", "--novel", str(iroot)])
            rc_s = index_search.main(["stats", "--novel", str(iroot)])
        check("build 成功且产出索引文件",
              rc_b == 0 and (iroot / "state" / "索引.db").is_file(), f"rc={rc_b}")
        check("原子替换不留 .tmp", not list((iroot / "state").glob("*.tmp")), "")
        check("≥3 字短语能查到（FTS5 trigram 路径）",
              any("编号-07" in t for _c, _s, t in long_hits), str(long_hits))
        check("2 字人名也能查到（短词必须退回 LIKE，否则 trigram 查不到）",
              any("老周" in t for _c, _s, t in short_hits), str(short_hits))
        check("节点已列时 verify 不报", rc_v == 0, f"rc={rc_v}")
        check("节点漏列老周 → verify 报出来", rc_v2 == 1, f"rc={rc_v2}")
        check("stats 正常", rc_s == 0, f"rc={rc_s}")
        with contextlib.redirect_stdout(io.StringIO()):
            rc_br = index_search.main(["brief", "2", "--novel", str(iroot)])
        check("brief 能跑（写前简报）", rc_br == 0, f"rc={rc_br}")
        with contextlib.redirect_stdout(io.StringIO()):
            rc_br2 = index_search.main(["brief", "99", "--novel", str(iroot)])
        check("brief 给不存在的章号 → 退 2 而不是崩", rc_br2 == 2, f"rc={rc_br2}")

        # 回归：写前简报是**动笔前**用的，那时正文还不存在。
        # 早期版本一上来就 "没有第 N 章的正文" 退出，等于在唯一需要它的时刻用不了。
        (iroot / "plot" / "节点-第0003章.md").write_text(
            "---\nchapter: 3\ntitle: 丙\ncharacters: 老周\nhook_type: 升级\n"
            "axes: 剧情, 知识\nopening_type: 声音\n---\n", encoding="utf-8")
        assert not (iroot / "chapters" / "第3章-丙.md").exists()
        buf_br3 = io.StringIO()
        with contextlib.redirect_stdout(buf_br3):
            rc_br3 = index_search.main(["brief", "3", "--novel", str(iroot)])
        out3 = buf_br3.getvalue()
        check("只有节点、还没写正文时 brief 也能跑（写前工具的基本要求）",
              rc_br3 == 0, f"rc={rc_br3}")
        check("没正文时标题回落到节点 front-matter 的 title",
              "《丙》" in out3, out3.splitlines()[:3])
        check("并明确提示正文尚未写，不假装有正文",
              "正文尚未写" in out3, out3.splitlines()[:2])
        # 既无正文也无节点 → 仍然退 2（不能把"什么都没有"当成正常）
        with contextlib.redirect_stdout(io.StringIO()):
            rc_br4 = index_search.main(["brief", "98", "--novel", str(iroot)])
        check("既无正文也无节点 → 退 2 并提示先写节点", rc_br4 == 2, f"rc={rc_br4}")

    print("[33] 禁止矛盾扫描的路径回退（CWD 相对路径会让红线整段跳过）")
    # 默认路径 `novel/world/世界观.md` 是 CWD 相对的。在 novel/ 里跑（很自然的用法）
    # 就找不到 → 红线扫描整段跳过，而报告里那句"必须改：无"读起来像"全都查过了"。
    # 实测踩过一次：从 novel/ 跑三章，三章各少查 11 条禁止矛盾。
    with tempfile.TemporaryDirectory() as tmp:
        # 好夹具放在 tmp/proj/novel/ 下：这样从 tmp/nowhere 上溯时
        # 不会撞上它（否则"找不到"这个用例会被自己的夹具喂成假通过 —— 第一次就踩了）
        root = Path(tmp) / "proj" / "novel"
        (root / "world").mkdir(parents=True)
        wp_new = root / "world" / "世界观.md"
        wp_new.write_text("## 禁止矛盾清单\n\n| `手机` | 现代词 |\n| `公里` | 现代计量 |\n",
                          encoding="utf-8")
        (root / "chapters").mkdir()
        ch_new = root / "chapters" / "第1章.md"
        ch_new.write_text("正文正文正文\n", encoding="utf-8")

        found = style_audit.find_forbidden_near([ch_new.parent])
        check("从章节目录上溯能找到 world/世界观.md（不再整段跳过）",
              found is not None, f"{found}")
        check("找到的正是项目里那一个",
              found is not None and found.resolve() == wp_new.resolve(), f"{found}")
        deeper = root / "chapters" / "a" / "b"
        deeper.mkdir(parents=True)
        check("更深的子目录也能上溯找到",
              style_audit.find_forbidden_near([deeper]) is not None, "应能找到")
        check("找到之后真能提出禁用短句",
              "手机" in (style_audit.load_forbidden(found) if found else []), "应含手机")

        nowheere_tree = Path(tmp) / "nowhere"
        nowheere_tree.mkdir()
        check("这一层确实没有时返回 None（不假装查过）",
              style_audit.find_forbidden_near([nowheere_tree]) is None, "应返回 None")

        # 反向保护：显式 --forbidden 指错路径是"人的明确指令"，不许被回退逻辑悄悄救回来。
        # 走一次真正的 main()，看它打印的是哪一句。
        explicit_missing = str(Path(tmp) / "definitely_not_here" / "世界观.md")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            style_audit.main([str(ch_new), "--forbidden", explicit_missing])
        out_new = buf.getvalue()
        check("显式路径找不到时，照旧明说「已跳过」",
              "禁止矛盾扫描已跳过" in out_new, out_new.splitlines()[:3])
        check("显式路径不会触发路径回退",
              "已按项目布局改用" not in out_new, out_new.splitlines()[:3])

    print("[35] 白名单默认自动加载（约定路径上的豁免不能被静默无视）")
    # 以前不带 --allow 就静默无视 state/连续性白名单.md，于是刻意回声被报成"必须改"。
    # 约定路径上明明躺着作者的豁免记录，却要靠人每条命令都记得加 flag —— 与 B038 同一形状。
    with tempfile.TemporaryDirectory() as tmp:
        wroot = Path(tmp) / "novel"
        for sub in ("chapters", "plot", "state", "world", "characters", "outline"):
            (wroot / sub).mkdir(parents=True)
        echo = "他说这事得照章办"
        (wroot / "chapters" / "第1章-甲.md").write_text(
            f"他站在门口看了一眼。\n\n{echo}。\n\n他往前走了一步。\n", encoding="utf-8")
        (wroot / "chapters" / "第2章-乙.md").write_text(
            f"她又问了一遍。\n\n{echo}。\n\n她把门关上了。\n", encoding="utf-8")
        (wroot / "state" / "连续性白名单.md").write_text("# 白名单\n\n" + echo + "\n",
                                                         encoding="utf-8")

        buf_a = io.StringIO()
        with contextlib.redirect_stdout(buf_a):
            continuity_check.main(["--novel", str(wroot)])
        out_auto = buf_a.getvalue()
        check("不带 --allow 也会自动加载 state/连续性白名单.md",
              "已自动加载白名单" in out_auto, out_auto.splitlines()[:4])
        check("自动加载后刻意回声不再报必须改",
              "跨章逐字重复" not in out_auto, out_auto[-320:])

        buf_b = io.StringIO()
        with contextlib.redirect_stdout(buf_b):
            continuity_check.main(["--novel", str(wroot), "--no-allow"])
        out_no = buf_b.getvalue()
        check("--no-allow 时刻意回声照旧报出来（守卫没被关掉）",
              "跨章逐字重复" in out_no, out_no[-320:])

        err_c = io.StringIO()
        buf_c = io.StringIO()
        with contextlib.redirect_stdout(buf_c), contextlib.redirect_stderr(err_c):
            continuity_check.main(["--novel", str(wroot),
                                   "--allow", str(wroot / "state" / "不存在.md")])
        check("--allow 指了不存在的文件会明说警告",
              "白名单不存在" in err_c.getvalue(), err_c.getvalue()[:200])
        check("这一轮不许假装有豁免（该报的还要报）",
              "跨章逐字重复" in buf_c.getvalue(), buf_c.getvalue()[-320:])

    print("[36] 禁止矛盾扫描不许跨项目污染（拿别的项目的红线扫这一篇）")
    # 真实踩过：技能装在 `E:\小说\.dsh\skills\` 下，审技能自带的 `examples/chapters`
    # 时，上溯会一路走到盘符根、撞上 `E:\小说\novel\world\世界观.md`——那是**另一个项目**
    # 的红线，命中/不命中都是假的证据。修法：上溯止于被审文件自己的项目根；
    # CWD 的默认路径只在"确实属于被审项目"时才采用。
    with tempfile.TemporaryDirectory() as tmp:
        a_novel = Path(tmp) / "projA" / "novel"
        (a_novel / "world").mkdir(parents=True)
        (a_novel / "chapters").mkdir()
        a_world = a_novel / "world" / "世界观.md"
        a_world.write_text("## 禁止矛盾清单\n\n| `手机` | 现代词 |\n", encoding="utf-8")

        b_novel = Path(tmp) / "projB" / "novel"
        (b_novel / "world").mkdir(parents=True)
        (b_novel / "chapters").mkdir()
        b_world = b_novel / "world" / "世界观.md"
        b_world.write_text("## 禁止矛盾清单\n\n| `公里` | 现代计量 |\n", encoding="utf-8")
        ch_b = b_novel / "chapters" / "第1章.md"
        ch_b.write_text("正文正文正文\n", encoding="utf-8")

        check("novel_root_of 认得出 novel/ 段",
              style_audit.novel_root_of(ch_b.parent) == b_novel.resolve(),
              f"{style_audit.novel_root_of(ch_b.parent)}")
        check("没有项目骨架时 novel_root_of 返回 None",
              style_audit.novel_root_of(Path(tmp)) is None, "应返回 None")
        check("从章节目录命名回退：上溯只认自己项目的 world/世界观.md",
              style_audit.find_forbidden_near([ch_b.parent]) == b_world.resolve(),
              f"{style_audit.find_forbidden_near([ch_b.parent])}")

        old_cwd = os.getcwd()
        try:
            os.chdir(a_novel)                      # CWD 在 A 项目里，却要审 B 项目的章节
            buf36 = io.StringIO()
            with contextlib.redirect_stdout(buf36):
                rc36 = style_audit.main(["--dir", str(b_novel / "chapters")])
            out36 = buf36.getvalue()
            check("跨项目时读的是被审项目自己的世界观，不是 CWD 那份",
                  str(b_world) in out36 and str(a_world) not in out36,
                  out36.splitlines()[:3])
            check("跨项目时不会把 A 项目的红线拿出来用",
                  "手机" not in out36, out36.splitlines()[:3])
            check("跨项目时照旧退 0（不是用法错误）", rc36 == 0, f"rc={rc36}")

            # 反向保护：CWD 就是被审项目的根时，仍要照常读自己的世界观
            (a_novel / "chapters" / "第1章.md").write_text("正文正文正文\n", encoding="utf-8")
            buf36b = io.StringIO()
            with contextlib.redirect_stdout(buf36b):
                style_audit.main(["--dir", str(a_novel / "chapters")])
            check("CWD 就是被审项目时照常读自己的世界观",
                  "读到 1 条" in buf36b.getvalue(), buf36b.getvalue().splitlines()[:3])

            os.chdir(b_novel)                      # 项目内的三要素：章节、节点、白名单
            b_ch = b_novel / "chapters" / "第1章.md"
            b_ch.write_text("他站在门口看了一眼。\n\n他往前走了一步。\n", encoding="utf-8")
            (b_novel / "plot").mkdir()
            (b_novel / "plot" / "节点-第0001章.md").write_text(
                "---\nchapter: 1\n---\n\n## 本章节点\n\n- 事件：试\n", encoding="utf-8")
            buf36c = io.StringIO()
            with contextlib.redirect_stdout(buf36c):
                style_audit.main([str(b_ch)])
            check("在项目里跑时默认路径照旧命中",
                  "读到 1 条" in buf36c.getvalue(), buf36c.getvalue().splitlines()[:3])

            # 上溯撞上"CWD 所属项目"的那一种（原来最脏的形态）：
            # CWD 的 novel/ 里确实有世界观，被审文件却在别处、周围没有任何项目骨架。
            stray = Path(tmp) / "别处" / "第9章.md"
            stray.parent.mkdir()
            stray.write_text("正文正文正文\n", encoding="utf-8")
            os.chdir(a_novel)
            buf36d = io.StringIO()
            with contextlib.redirect_stdout(buf36d):
                style_audit.main([str(stray)])
            out36d = buf36d.getvalue()
            check("被审文件周围没有项目骨架时，不许拿 CWD 那份红线顶替",
                  "禁止矛盾清单：从" not in out36d, out36d.splitlines()[:3])
            check("这种情况要明说已跳过，而不是静默",
                  "已跳过" in out36d, out36d.splitlines()[:3])
        finally:
            os.chdir(old_cwd)

    print("[37] 引文核验：抽出 0 条引文必须报「无法核对」，不许报通过")
    # 空结果是合法结果，但不是合格结果。一条引文都没抽到时这道闸门什么都没核，
    # 以前会打印"全部引文都能在正文里字面搜到 ✓"——那句话是假绿灯。
    with tempfile.TemporaryDirectory() as tmp:
        qch = Path(tmp) / "第1章.md"
        qch.write_text("他把灯吹灭了。\n", encoding="utf-8")
        no_quote = Path(tmp) / "无引文报告.md"
        no_quote.write_text("# 审查报告\n\n这一版比上一版好，节奏更稳。\n", encoding="utf-8")
        quote_ok = Path(tmp) / "有引文报告.md"
        quote_ok.write_text("证据：「他把灯吹灭了」——收得干净。\n", encoding="utf-8")

        buf37 = io.StringIO()
        with contextlib.redirect_stdout(buf37):
            rc37 = quote_check.main([str(no_quote), "--chapter", str(qch)])
        out37 = buf37.getvalue()
        check("0 条引文时明说「无法核对」", "无法核对" in out37, out37.splitlines()[:3])
        check("0 条引文时不再打印「都能搜到 ✓」",
              "都能在正文里字面搜到" not in out37, out37.splitlines()[:3])
        check("0 条引文不算闸门失败（不是「搜不到」，是根本没核）", rc37 == 0, f"rc={rc37}")

        buf37b = io.StringIO()
        with contextlib.redirect_stdout(buf37b):
            rc37b = quote_check.main([str(quote_ok), "--chapter", str(qch)])
        check("有引文且都对时照旧打勾", "都能在正文里字面搜到" in buf37b.getvalue(),
              buf37b.getvalue()[-200:])
        check("有引文且都对时退 0", rc37b == 0, f"rc={rc37b}")

    print("[38] 三个检查器的退出码契约（跨章一致性默认设闸门，可 --no-strict 放宽）")
    with tempfile.TemporaryDirectory() as tmp:
        eroot = Path(tmp) / "novel"
        for sub in ("chapters", "plot", "world"):
            (eroot / sub).mkdir(parents=True)
        dup = "他说这事得照章办"
        (eroot / "chapters" / "第1章-甲.md").write_text(
            f"他站在门口看了一眼。\n\n{dup}。\n\n他往前走了一步。\n", encoding="utf-8")
        (eroot / "chapters" / "第2章-乙.md").write_text(
            f"她又问了一遍。\n\n{dup}。\n\n她把门关上了。\n", encoding="utf-8")
        buf38 = io.StringIO()
        with contextlib.redirect_stdout(buf38):
            rc_default = continuity_check.main(["--novel", str(eroot)])
        check("有必须改时默认退 1（硬闸门）", rc_default == 1, f"rc={rc_default}")
        check("默认这一轮确实报了跨章逐字重复",
              "跨章逐字重复" in buf38.getvalue(), buf38.getvalue()[-200:])
        buf38b = io.StringIO()
        with contextlib.redirect_stdout(buf38b):
            rc_lax = continuity_check.main(["--novel", str(eroot), "--no-strict"])
        check("--no-strict 时只报告不设闸门（退 0）", rc_lax == 0, f"rc={rc_lax}")
        check("--no-strict 不改变报告内容",
              "跨章逐字重复" in buf38b.getvalue(), buf38b.getvalue()[-200:])

    print("[39] 文档声明的示例输出必须和实测一致（防止文档漂移）")
    # 为什么需要它：`examples/README.md` 与 `README.md` 里写着"应该报出来什么"和自检项数，
    # 这些数字是**文档承诺**。脚本阈值一改、示例一改，承诺就会悄悄过期——
    # 本轮就是这么发现的：`examples/README.md` 声称建议改 2 条，实测是 3 条
    # （`axes:` 检查是后来加的，文档没跟上）。
    # 这一节直接在进程内跑示例，把"承诺"与"实测"对上。
    def _section_count(text: str, prefixes: tuple[str, ...]) -> int | None:
        """数出某个小节下的**顶层**条目数量；找不到该小节返回 None。

        顶层条目以 `  - ` 开头；`  - 　└ …` 是它的**落点子条目**（"从第几段哪一句开始"），
        不算独立条目——`examples/README.md` 承诺的条数也是按顶层算的。
        """
        lines = text.splitlines()
        start = None
        for i, ln in enumerate(lines):
            if any(ln.startswith(p) for p in prefixes):
                start = i + 1
                break
        if start is None:
            return None
        n = 0
        for ln in lines[start:]:
            if ln and not ln.startswith("  "):
                break
            if ln.startswith("  - ") and "└" not in ln:
                n += 1
        return n

    with tempfile.TemporaryDirectory() as tmp:
        ex_plot = EXAMPLES / "plot"
        ex_chapters = EXAMPLES / "chapters"

        buf_sa = io.StringIO()
        with contextlib.redirect_stdout(buf_sa):
            series_audit.main(["--plot", str(ex_plot), "--chapters-dir", str(ex_chapters),
                               "--target-words", "620"])
        out_sa = buf_sa.getvalue()
        n_hard_sa = _section_count(out_sa, ("必须改：",))
        n_soft_sa = _section_count(out_sa, ("建议改：",))
        check("示例：连载体检「必须改」= 5 条（examples/README.md 的承诺）",
              n_hard_sa == 5, f"实测 {n_hard_sa}")
        check("示例：连载体检「建议改」= 3 条（含新增的 axes 检查）",
              n_soft_sa == 3, f"实测 {n_soft_sa}")
        check("示例：钩子类型不在清单里的提示还在（第 7 章故意写「翻转」）",
              "翻转" in out_sa, out_sa[-200:])

        buf_st = io.StringIO()
        with contextlib.redirect_stdout(buf_st):
            style_audit.main(["--dir", str(ex_chapters),
                              "--blacklist", str(BLACKLIST_PATH)])
        out_st = buf_st.getvalue()
        n_hard_st = _section_count(out_st, ("必须改：",))
        n_soft_st = _section_count(out_st, ("参考区间外",))
        check("示例：单章预筛「必须改」= 3 条（examples/README.md 的承诺）",
              n_hard_st == 3, f"实测 {n_hard_st}")
        check("示例：单章预筛「参考区间外」= 4 条",
              n_soft_st == 4, f"实测 {n_soft_st}")
        check("示例：第 1 章字数 613（走正文实测，不是节点自报）",
              "字数 613" in out_st, out_st[:200])

    # 自检项数被 README 两处写死；数字一变文档就会过期。
    # 注意顺序：这一项必须在**所有检查都跑完之后**才算，因为它断言的正是最终总数
    # （`checks` 计数器是边跑边加的，写在上面的断言会把自己也数进去，永远差 1）。
    print()
    readme = (SKILL_DIR / "README.md").read_text(encoding="utf-8")
    claimed = sorted({int(m) for m in re.findall(r"(\d+)\s*项自检", readme)})
    if claimed != [checks]:
        failures.append(
            f"README 里的自检项数与实际不一致：文档写 {claimed}，实际 {checks} "
            f"（README.md 第 7 行与脚本表两处都要改成 {checks}）")
    if failures:
        print(f"自检失败：{len(failures)}/{checks} 项未通过")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"自检全部通过（{checks} 项）")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
