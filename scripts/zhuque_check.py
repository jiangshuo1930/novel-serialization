#!/usr/bin/env python3
"""朱雀（Zhuque）AIGC 文本检测 —— 走官方接口。

接口：腾讯云 EdgeOne Makers 内置模型 @makers/zhuque-text
  POST https://ai-gateway.edgeone.link/v1/providers/zhuque-text/classify
  Authorization: Bearer <API Key>
  {"text": "...", "is_merge": true}

文档：
  https://cloud.tencent.cn/document/product/1552/137539   （Makers，每月 50 万 token 免费额度）
  https://cloud.tencent.cn/document/product/1552/135974   （企业版：自建网关域名 + API Key）

响应里的关键字段（官方示例）：
  softmax_confidence  整篇被判为 AI 的置信度，0-1  —— 本脚本的 "AIGC 值"
  labels_ratio        {"0": 真人占比, "1": AI 生成占比, "2": 不确定占比}
  segment_labels[]    分段判定，每段一个 conf（用来定位"该重写哪几段"）
  usage.total_tokens           这段文本本身的 token（实测 ≈1.0-1.15/字）
  makers_models_usage.total_tokens  **计费口径**，比 usage 高约 70%（实测 ≈1.7/字）；
                                估算剩余额度要看这一项

标签口径 0=真人 / 1=AI生成 / 2=不确定：官方该页没有逐字段说明，这里按社区实现
（Sophomoresty/zhuque）的字段表处理；首次调用请加 --debug 核对原始返回。

密钥只从环境变量读，不落盘、不写进任何报告、不进 git：
  PowerShell:  $env:ZHUQUE_API_KEY = "sk-..."
  bash/zsh:    export ZHUQUE_API_KEY="sk-..."
（想永久设置用 setx ZHUQUE_API_KEY "sk-..."，或改用 --key-file 指向一个已加入 .gitignore 的文件）

用法:
  python zhuque_check.py --text "hello world"          # 冒烟测试：确认密钥能用
  python zhuque_check.py novel/chapters/第12章-雨夜收账.md
  python zhuque_check.py --dir novel/chapters --segments
  python zhuque_check.py <file> --json novel/state/朱雀报告.json
  python zhuque_check.py <file> --dry-run              # 只打印将要发送的请求，不真的调用

退出码：0 = 人工率达标；1 = 未达标；2 = 调用失败（密钥/网络/额度问题）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://ai-gateway.edgeone.link/v1/providers/zhuque-text/classify"
DEFAULT_KEY_ENV = "ZHUQUE_API_KEY"
DEFAULT_URL_ENV = "ZHUQUE_API_URL"
MIN_CHARS = 200          # 低于此长度检测结果不可靠
MAX_CHARS = 20000        # 超过此长度先拦一下，避免一次烧掉大量额度
DEFAULT_MIN_HUMAN = 0.95
# 判定口径：**人工率 ≥ 0.95**（对应 AIGC ≤ 0.05）。以人工率为准。
# 历史：2026 年曾按作者要求放宽成"AIGC < 0.5"，随后作者决定改回去——
# **别再把这条当成可以随手放宽的线**，它是作者明确要守的标准。
# `--max-aigc` 保留成一个换算别名（max_aigc = 1 - min_human），方便临时试别的线。
LABEL_NAMES = {"0": "真人", "1": "AI生成", "2": "不确定"}


def mask_key(key: str) -> str:
    if len(key) <= 12:
        return "***"
    return f"{key[:6]}…{key[-4:]}"


def resolve_key(args) -> str | None:
    if args.key_file:
        p = Path(args.key_file)
        if not p.is_file():
            print(f"错误：密钥文件不存在：{p}", file=sys.stderr)
            return None
        return p.read_text(encoding="utf-8-sig").strip()
    key = os.environ.get(args.key_env, "").strip()
    return key or None


def read_texts(paths: list[str], dirs: list[str], recursive: bool) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    targets = [Path(p) for p in paths]
    for d in dirs:
        dp = Path(d)
        if not dp.is_dir():
            print(f"错误：目录不存在：{dp}", file=sys.stderr)
            continue
        targets += sorted(p for p in (dp.rglob("*.md") if recursive else dp.glob("*.md")) if p.is_file())
    for p in targets:
        if p.is_file():
            out.append((str(p), p.read_text(encoding="utf-8-sig", errors="replace")))
    return out


def call_api(url: str, key: str, text: str, merge: bool, timeout: int,
             debug: bool, dry_run: bool) -> tuple[dict | None, str | None]:
    payload = json.dumps({"text": text, "is_merge": merge}, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if dry_run:
        print(f"  [dry-run] POST {url}")
        print(f"  [dry-run] Authorization: Bearer {mask_key(key)}")
        print(f"  [dry-run] body: {len(payload)} 字节，is_merge={merge}，正文 {len(text)} 字")
        return None, "dry-run"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        hint = {
            401: "密钥无效或已被删除（EdgeOne 控制台 → Makers → Models → API Key）",
            403: "密钥无权调用该模型，或未开通/额度用尽",
            404: "路由不对：企业版要用自己的网关域名，配 ZHUQUE_API_URL 或 --url",
            429: "触发限流，等一会儿再试",
        }.get(e.code, "见返回内容")
        return None, f"HTTP {e.code}：{hint}\n  返回：{body}"
    except urllib.error.URLError as e:
        return None, f"网络错误：{e.reason}"
    except TimeoutError:
        return None, f"超时（{timeout}s）：文本较长时同步模式可能要 10s 以上，可加大 --timeout"

    if debug:
        print("  [debug] 原始返回：")
        print("  " + raw.strip()[:2000])
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        return None, f"返回不是 JSON：{raw[:300]}"


def extract_scores(data: dict) -> dict:
    """从返回里取出 AIGC 值、人工率和标签分布。

    只用 softmax_confidence 当分数，**不做 labels_ratio 回退**。
    实测教训：一段明显 AI 味的文字返回过 labels_ratio={"2":1}（整篇判为"不确定"）
    而 softmax_confidence=0.9401。若拿 labels_ratio["1"]（AI 段占比 = 0）当 AI 率，
    会把这段判成 100% 人工 —— 一个静默的假通过。拿不到置信度就报"无法判定"。
    """
    raw = data.get("softmax_confidence")
    aigc = float(raw) if isinstance(raw, (int, float)) else None
    ratios = data.get("labels_ratio") or {}
    segs = data.get("segment_labels") or []
    return {
        "aigc": aigc,
        "aigc_source": "softmax_confidence" if aigc is not None else "缺失",
        "human": (1.0 - aigc) if aigc is not None else None,
        "labels_ratio": ratios,
        "ratio_confidence": data.get("ratio_confidence"),
        "segments": segs,
        "tokens": (data.get("usage") or {}).get("total_tokens"),
        "billed_tokens": (data.get("makers_models_usage") or {}).get("total_tokens"),
        "status": data.get("status"),
        "msg": data.get("msg"),
    }


def render(name: str, s: dict, min_human: float, show_segments: int) -> str:
    lines = [f"=== {name} ==="]
    if s["status"] and s["status"] != "success":
        lines.append(f"接口状态异常：{s['status']} {s['msg'] or ''}".rstrip())
    if s["aigc"] is None:
        lines.append(
            "没能从返回里解析出 softmax_confidence —— 不猜、不回退到 labels_ratio。"
            "加 --debug 看原始返回，把字段名告诉我，我来适配"
        )
        return "\n".join(lines)
    lines.append(
        f"AIGC 值 {s['aigc']:.4f}（来自 {s['aigc_source']}）"
        f"｜人工率 {s['human']:.2%}"
    )
    if s["labels_ratio"]:
        parts = [
            f"{LABEL_NAMES.get(k, k)} {float(v):.0%}"
            for k, v in sorted(s["labels_ratio"].items())
            if isinstance(v, (int, float))
        ]
        lines.append("标签分布：" + " / ".join(parts) + "　（0=真人 1=AI生成 2=不确定）")
        lines.append("　　※ 标签分布是分类占比，不是概率；与 AIGC 值不一致时**以 AIGC 值为准**")
    if s["tokens"] or s["billed_tokens"]:
        parts = []
        if s["tokens"]:
            parts.append(f"文本 {s['tokens']}")
        if s["billed_tokens"]:
            parts.append(f"计费口径 {s['billed_tokens']}")
        lines.append("本次消耗：" + "，".join(parts) + " token　（扣额度看计费口径那一项）")
    # 判定以 **人工率** 为准（作者定的线：人工率 ≥ 0.95，对应 AIGC ≤ 0.05）。
    # AIGC 值是同一个东西的另一种说法（= 1 - 人工率），两个都打出来，避免记混。
    passed = s["human"] >= min_human
    lines.append(
        f"判定：{'通过' if passed else '未通过'}"
        f"（过线标准 人工率 ≥{min_human:.0%}，对应 AIGC ≤{1 - min_human:.2f}）"
    )
    lines.append(
        f"　　※ 这是**下限**不是目标：人工率越高越好，"
        f"改稿时看的是「这一版比上一版升了多少」，不是「过没过」。"
    )

    if show_segments and s["segments"]:
        ranked = sorted(
            (x for x in s["segments"] if isinstance(x, dict)),
            key=lambda x: float(x.get("conf") or 0),
            reverse=True,
        )[:show_segments]
        lines.append(f"AI 味最重的 {len(ranked)} 段（按 conf 降序，优先重写这些）：")
        for i, seg in enumerate(ranked, 1):
            label = LABEL_NAMES.get(str(seg.get("label")), str(seg.get("label")))
            text = str(seg.get("text", "")).replace("\n", " ")[:60]
            lines.append(f"  {i}. conf={float(seg.get('conf') or 0):.4f} label={label}｜{text}…")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="朱雀 AIGC 文本检测（官方接口）")
    ap.add_argument("paths", nargs="*", help="要检测的章节文件")
    ap.add_argument("--dir", action="append", default=[], help="检测该目录下的 .md（可重复）")
    ap.add_argument("--recursive", action="store_true", help="--dir 递归子目录")
    ap.add_argument("--text", help="直接检测这段文本（冒烟测试用）")
    ap.add_argument("--url", default=os.environ.get(DEFAULT_URL_ENV, DEFAULT_URL),
                    help=f"接口地址，默认 {DEFAULT_URL}")
    ap.add_argument("--key-env", default=DEFAULT_KEY_ENV, help=f"读取密钥的环境变量名，默认 {DEFAULT_KEY_ENV}")
    ap.add_argument("--key-file", help="从文件读密钥（记得把该文件加进 .gitignore）")
    ap.add_argument("--min-human", type=float, default=DEFAULT_MIN_HUMAN,
                    help=f"人工率过线标准（大于等于它算通过），默认 {DEFAULT_MIN_HUMAN}")
    ap.add_argument("--max-aigc", type=float, default=None,
                    help="AIGC 口径的换算别名：等价于 --min-human (1-该值)。两个都给时以 --min-human 为准")
    ap.add_argument("--segments", type=int, nargs="?", const=5, default=0,
                    help="列出 AI 味最重的 N 段（默认 5），并改用逐段模式 is_merge=false")
    ap.add_argument("--merge", action="store_true", help="强制整篇合并检测（覆盖 --segments 的逐段模式）")
    ap.add_argument("--json", dest="json_out", help="把结果写入该 JSON 文件（不含密钥）")
    ap.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数，默认 30")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发送的请求，不真的调用")
    ap.add_argument("--debug", action="store_true", help="打印原始返回 JSON")
    ap.add_argument("--force", action="store_true", help=f"允许发送超过 {MAX_CHARS} 字的文本")
    args = ap.parse_args(argv)

    # --max-aigc 是换算别名；两个都给时以 --min-human 为准
    if args.max_aigc is not None and args.min_human == DEFAULT_MIN_HUMAN:
        args.min_human = 1.0 - args.max_aigc

    key = resolve_key(args)
    if not key:
        print(
            f"错误：没读到 API Key。\n"
            f"  设置环境变量（推荐，不落盘）：\n"
            f"    PowerShell:  $env:{args.key_env} = \"sk-...\"\n"
            f"    bash/zsh:    export {args.key_env}=\"sk-...\"\n"
            f"  或用 --key-file 指向一个已加入 .gitignore 的文件。\n"
            f"  密钥在 EdgeOne 控制台 → Makers → Models → API Key 创建。",
            file=sys.stderr,
        )
        return 2

    items: list[tuple[str, str]] = []
    if args.text:
        items.append(("[--text]", args.text))
    items += read_texts(args.paths, args.dir, args.recursive)
    if not items:
        print("错误：没有待检测内容。给出文件路径、--dir 或 --text。", file=sys.stderr)
        return 2

    merge = args.merge or not args.segments
    results: dict[str, dict] = {}
    worst_exit = 0
    for name, text in items:
        stripped = text.strip()
        print(f"=== {name} ===")
        if len(stripped) < MIN_CHARS:
            print(f"  警告：只有 {len(stripped)} 字，少于 {MIN_CHARS} 字，检测结果不可靠（朱雀对短文本判别力弱）")
        if len(stripped) > MAX_CHARS and not args.force:
            print(f"  跳过：{len(stripped)} 字超过 {MAX_CHARS} 字上限。确认要花这个额度就加 --force。")
            worst_exit = max(worst_exit, 2)
            print()
            continue

        data, err = call_api(args.url, key, stripped, merge, args.timeout, args.debug, args.dry_run)
        if args.dry_run:
            print("  （dry-run 结束，没有真的调用）")
            print()
            continue
        if err:
            print(f"  调用失败：{err}")
            worst_exit = max(worst_exit, 2)
            print()
            continue

        scores = extract_scores(data)
        results[name] = {k: v for k, v in scores.items() if k != "segments"}
        results[name]["segments"] = scores["segments"][:20]
        print(render(name, scores, args.min_human, args.segments))
        print()
        if scores["human"] is not None and scores["human"] < args.min_human:
            worst_exit = max(worst_exit, 1)

    if args.json_out and results:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        # 原子写（本脚本只有这一处写盘，就地内联，不引辅助函数）
        _tmp = Path(args.json_out).with_name(Path(args.json_out).name + ".tmp")
        _tmp.write_text(
            json.dumps({"min_human": args.min_human, "files": results},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(_tmp, Path(args.json_out))
        print(f"已写入 {args.json_out}（不含密钥）")

    if not args.dry_run:
        print("说明：朱雀是外部服务，这里的数值是它的实测结果；本地 style_audit.py 只是预筛，两者不冲突。")
        print(f"密钥来自环境变量 {args.key_env}（{mask_key(key)}），本脚本不会把它写进任何文件。")
    return worst_exit


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
