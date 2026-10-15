# novel-serialization · 长篇小说连载全流程管理（Agent Skill）

[![selftest](https://github.com/jiangshuo1930/novel-serialization/actions/workflows/selftest.yml/badge.svg)](https://github.com/jiangshuo1930/novel-serialization/actions/workflows/selftest.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一套给 AI Agent 用的**长篇网文连载**工作流技能：写前自动加载角色/大纲/伏笔，写后自动更新档案，
全程卡风格一致性与节奏（爽点、章末钩子、伏笔回收），并配合 AI 检测工具做去 AI 味。

**设计取向：可复现 > 覆盖广。** 所有检查都是确定性脚本（纯 Python 标准库，零依赖、离线可用），
因此每一项都能写回归测试——目前自带 **312 项自检**。语义层面的判断（人物是否走样、知识边界、
概述替代场景）明确交给**人工复核清单**，不假装机器能判。

> **自检不是靠自觉**：`.github/workflows/selftest.yml` 在每次推送时，于
> Python 3.10 / 3.11 / 3.12 三个版本上各跑一遍自检，再跑一次百万字压测。
> 任何一个红了，这次推送就是失败的。

## 安装

把整个目录放到项目的技能目录下即可（Agent 会按 `SKILL.md` 的 frontmatter 发现它）：

```bash
# 方式一：直接克隆到技能目录
git clone https://github.com/jiangshuo1930/novel-serialization.git \
  .agents/skills/novel-serialization

# 方式二：下载后手动复制
cp -r novel-serialization .agents/skills/
```

然后让 Agent 读 `SKILL.md` 开始工作。**不需要安装任何依赖**（只要 Python 3.10+）。

## 它包含什么

| 目录 | 内容 |
| --- | --- |
| `SKILL.md` | 工作流主干：目录结构 → 开书 → 写前必读 → 写正文 → 写后七步 → 超长篇机制 → 边界 |
| `references/` | 17 份参考：AI 味黑名单（63 条）、改稿方法论、人设/大纲/节点/卷纲模板、人工复核清单、**完结清单**、半本书接入规程、维护纪律、**sepia 接入说明**、完整台账 |
| `scripts/` | 9 个确定性脚本（见下） |
| `examples/` | 一个可运行的示例项目，用来预演脚本输出 |
| `sepia/` | vendored 的反 AI 叙事架构诊断技能（来源/版本见 `sepia/VENDORED.md`），改稿时加载其 pass + rubric 补脚本盲区 |

## 脚本

| 脚本 | 干什么 |
| --- | --- |
| `init_novel.py` | 开书：生成 `novel/` 骨架（10 个文件） |
| `style_audit.py` | 每章定稿：黑名单/配额/句长波动/词汇丰富度/节奏窗口/文风漂移/**参照文本双边比对**/**改稿前后护栏** |
| `series_audit.py` | 每章：伏笔超期与算术、钩子轮换、爽点断档、支线出勤、倒计时、**方向跑偏**（主线断档/主角消失/卷节拍对账）、字数遥测 |
| `continuity_check.py` | 每章：跨章一致性——逐字重复、微动作刷屏、称呼契约、档案细节落地、节点字段、**写前加载留痕** |
| `quote_check.py` | 审查报告的**引文核验闸门**：报告里引用的原文必须能在正文里字面搜到 |
| `index_search.py` | 章节索引（SQLite+FTS5 trigram）：`build` / `find` / `who` / `verify` / **`brief`（写前简报）** / `stats` |
| `selftest.py` | 312 项自检 |
| `stress_test.py` | 开发期压测：455 章/百万字规模下验证不崩不变慢 |
| `zhuque_check.py` | 接腾讯朱雀 AIGC 检测官方接口（需自备密钥，走环境变量） |

```bash
python scripts/selftest.py                      # 先确认脚本可用
python scripts/init_novel.py --root my-novel    # 开书
```

## 四条设计原则（踩出来的）

1. **检查的输出必须可执行**：只说"连续 4 句长度相同"没用，要给出"从第 3 段哪一句开始"。
2. **一条你学会忽略的告警，就是下一个真 bug 的藏身处**：阈值必须贴本书实测校准——
   一条永远不响的门槛等于没有门槛，一条每章都响的门槛会被学会忽略。
3. **"负责报畸形的层"自己必须对畸形免疫**：写检查的人最容易忘的就是检查器自身的健壮性。
4. **发现问题必须修到底，并推回本仓库**（硬性要求，见 `SKILL.md`「硬性要求」一节）：
   运行中任何阶段没达到理想状态——脚本报错、检查被静默跳过、空结果被说成通过、文档与代码不一致——
   都要**定位根因 → 是技能问题就改技能 → 补 `selftest.py` 回归 → 记 `skill-changelog.md` →
   自检全绿后 `git push`**。**不许置之不理**，也不许只调阈值把告警消掉。

## 边界（不做什么）

- **不做语义判断**：人物是否走样、知识边界、概述替代场景、节拍写得好不好，脚本给不了——
  它们在 `references/manual-review-checklist.md` 里，交给人。
- **不做平台合规扫描**：敏感词与三观雷区是作者的责任。
- **不注水**：字数只判下限（骨架预警）；低于下限的正确动作是回去看场景，而不是加句子。
- **不做唯一真源迁移**：`novel/` 保持纯 markdown（可读、可 diff、可手改），脚本只读不写正文。

## 许可

MIT，见 [`LICENSE`](LICENSE)。可自由使用、修改、分发（保留版权声明即可）。
