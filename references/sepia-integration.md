# sepia 接入说明（de-AI 叙事架构诊断）

> 本文件说明 vendored 的 `sepia/` 目录（来源 https://github.com/Nanako0129/sepia，MIT，
> 版本 0.12.3，见 `sepia/VENDORED.md`）如何进入本技能的改稿流程。
> **sepia 是提示词型技能，没有脚本；执行者是代理本身**——读它的 references、按协议诊断正文。

## 它补哪一层（分工）

现有工具查不到的层，正是 sepia 的主场：

| 层 | 谁管 |
|---|---|
| 句子级结构（黑名单、配额、句长、段落、对话占比、重复开头） | `style_audit.py` |
| 跨章重复、称呼契约、档案细节落地 | `continuity_check.py` |
| 伏笔、钩子轮换、爽点断档、字数下限 | `series_audit.py` |
| 外部 AI 味实测（AIGC 概率） | `zhuque_check.py`（朱雀） |
| **叙事架构（主题过度确定、因果太顺、时间太线性、结局模式、情感模式、人物网络、现实锚点）** | **sepia（本目录）** |
| **话语流（段落 QUD、中段塌陷、页面上位置模式）** | **sepia（本目录）** |

一句话：**本地脚本管"句子和连载"，朱雀管"AI 味"，sepia 管"故事长没长成 AI 的形状"。**
三者不冲突，都过了才算整章干净。

## 何时用（接入点）

- **`QC_FULL` 档（卷首卷末、大兑现、转折章）必须跑** sepia review。
- **常规章**：不强制，但推荐每 3-5 章至少一次（和 `continuity_check` 同频）。
- **改稿循环内层第 1 轮之前**：先出 sepia 诊断报告，再把架构层缺陷并入定向改稿清单。
  修复顺序照 sepia 的规则：**最深的一层先修**——叙事架构 → 话语流 → 表面风格。
  这和本技能"整段重写 > 换词"的既有优先级一致。
- 写完一章、跑完 `style_audit` 全绿、还没过朱雀时，用 sepia 抓脚本查不出的架构问题，
  改完再跑朱雀，避免"结构全对但读着还是 AI"的章被放过去。

## 怎么跑（小说路由，中文）

1. **加载**（按顺序，读 `sepia/SKILL.md` 的路由表）：
   `sepia/references/narrative-pass.md` → `sepia/references/discourse-pass.md` →
   `sepia/references/style-pass.md`；诊断用 `sepia/references/rubric.md`。
   **目标是中文**：在 style-pass 步骤同时加载 `sepia/references/languages/zh.md`
   （中文校准，含中文专属的 hunt/restore 表与"不是信号"清单）。
2. **模型身份**：小说路由上 review 时，作者模型与执行模型各按
   `sepia/references/model-fingerprints.md` 加载（家族+版本；未知就报 `none`）。
   网文连载场景通常两者都未知或已知本书所用模型，照协议如实报，别猜。
3. **诊断协议（rubric.md 强制）**：
   - **一次只读一组**，分五遍过 Group A→E，禁止一次读全表（模型自评会塌缩到一两个维度）。
   - **每条信号必须引正文短句**，无引文 = 无信号。
   - 数值行只记观测值并与 Human/AI 语料参照做**定性**比较，**不设单篇阈值**；
     没有触发机会的特征标 **n/a**（短章大量 n/a 是正常的，不是缺陷）。
   - 远偏离 AI 方向的极端值（如时间断裂拉满）记为 **over-correction 建议**，单独报告，
     不算 AI 信号。
4. **产出 `SEPIA DIAGNOSIS` 报告**（格式照 `rubric.md` 末尾的模板），并入本章改稿清单。
5. **修**：按 deepest-first 逐条修，每条绑定一处引文。改完重跑被改的 rubric 组做验证。

## 校准铁律（sepia 的"总纲"，违反就是新指纹）

1. **瞄准人类中间带，不是反转 AI 极值**：人类各项多为中等值（如时间断裂 2.4/5）。
   把每个 AI tell 都反转 = 造一个新的机器指纹。
2. **选择，不累积**：每章只做 **3-5 个 human-leaning move + 恰好 1 个 rarity move**，
   不要全表都修。修太多和修太少一样假。
3. **留 slack**：允许普通句、未展开的想法、平铺直叙的段落。别把每行都打磨出"辨识度"。
4. **白名单（style-pass.md §7）**：干净语法、单个词命中、正式语气、引号内原文、
   作者自己的习惯、普通句——都不是 AI 证据，不许"修"。

## 和本技能既有规则的衔接

- **字数口径**：sepia 不改字数；字数下限仍归 `series_audit` 管。sepia 的修复
  （替换 74% / 删除 18% / 插入 8%）天然倾向删减，与 P004"补写要有功能"一致——
  被 sepia 判定为架构缺陷而删掉的段落，如果本技能判定为"该演的戏"，两者冲突时
  先问作者，别让 sepia 的删除冲动覆盖场景预算。
- **人工判断项**：sepia 报告的语气类信号（语气像不像那个人）仍是人工复核项——
  sepia 只数模式，不判语气好坏；`manual-review-checklist.md` 的 ★ 必过项照旧要过。
- **汇报纪律**：报告 sepia 诊断时，固定句式补充为
  `必须改 0 ｜ 脚本不查：声纹落点、跨章重复、档案对齐、知识边界、对话占比、平台合规 ｜ sepia 不查：跨章因果、设定物理、伏笔语义`。

## 裁剪与更新

- 本目录只 vendored 核心 skill（`skills/sepia/`），未复制平台 wrapper
  （sepia-write/review/refactor/recreate/hemingway 等平台命令包装）——它们与网文连载无关。
- 上游更新：重克隆 https://github.com/Nanako0129/sepia，整体替换 `sepia/`，
  更新 `sepia/VENDORED.md` 的版本号与日期，跑一遍 selftest（有目录完整性回归），记 changelog。
- 上游 LICENSE（MIT）保留在 `sepia/LICENSE`，未经许可不得改动。
