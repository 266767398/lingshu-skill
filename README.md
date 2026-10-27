# 灵枢 · 类脑记忆、人格与成长

> 名字取自《黄帝内经》：**算力给思考，人格给灵魂，记忆给生命。**

> ⚠️ **能力天花板（先读）**：灵枢是「提示词 + 脚本」的机制，**不能从底层强制约束模型**。当模型发生强幻觉时依然会无视全部规则；效果上限取决于底层大模型本身的推理、记忆与指令遵循能力。中强模型（7B+、云端）可完整遵守；**3B 以下本地小模型会出现大量规则漏执行**（忘走 startup、跳过写盘、纪律打折）——请据此降低预期，或改用 `SKILL-lite.md`（精简版）/ `SKILL-tiny.md`（最小试用版）。

> 🚨 **风险警告：幻觉会被固化落盘**。本地保存 ≠ 内容真实。`dream`、`knowledge tidy`、`emotion evaluate` 都要大模型产出结构化结果；模型若把无关记忆强行合并、编造内容、误判情绪，这些错误会被**持久写进本地记忆库 / 知识库**，不会自动消失。防护措施：AI 动过的记忆 / 知识一律先标为**待审核**（`review` → `confirm` / `discard`；知识也可 `knowledge review`），人格变更带**来源与依据**（`persona history`），另有全局自查 `audit`。小模型请用 `config set model_tier small` 关掉高风险自动能力。**落盘前请人工过目。**

一套纯本地、零依赖（只用 Python 标准库）的 AI 记忆系统。它把"上下文窗口"变成"会生长的记忆"——短期记忆像海马体一样快速写入、自然衰减；上下文将满时像人做梦一样重放、压缩、巩固到长期皮层；想不起来时靠模糊联想而不是精确匹配。

## 目录

- [核心机制](#核心机制)
  - [分层记忆与遗忘曲线](#分层记忆与遗忘曲线)
  - [做梦：上下文将满时的整理](#做梦上下文将满时的整理)
  - [上下文水位三级防线](#上下文水位三级防线)
  - [工作记忆](#工作记忆)
  - [会话模式与情绪隔离](#会话模式与情绪隔离)
  - [审核闸：AI 产物先过目](#审核闸ai-产物先过目)
- [情绪与类人机制](#情绪与类人机制)
- [模糊联想回忆](#模糊联想回忆)
- [人格](#人格)
- [成长路径](#成长路径)
  - [知识库](#知识库)
  - [笔记日志](#笔记日志)
  - [技能自编辑与全局化](#技能自编辑与全局化)
  - [进化日志](#进化日志)
- [DeepSeek 生态兼容](#deepseek-生态兼容)
- [可视化面板](#可视化面板)
- [可靠性设计](#可靠性设计)
- [脚本 vs 模型：谁在真正干活](#脚本-vs-模型谁在真正干活)
- [模型适配与档位](#模型适配与档位)
- [接入大模型（Ollama 示例）](#接入大模型ollama-示例)
- [规模上限与归档](#规模上限与归档)
- [如何更新灵枢](#如何更新灵枢)
- [快速开始](#快速开始)
- [命令速查](#命令速查)
- [项目结构](#项目结构)

## 核心机制

### 分层记忆与遗忘曲线

记忆分三层，模拟大脑：

| 层 | 作用 | 衰减 |
|---|---|---|
| **海马体**（hippo） | 短期：新写入的记忆，快速衰减 | 快（按访问衰减） |
| **皮层**（cortex） | 长期：做梦/多次访问后巩固上来 | 慢 |
| **淡忘区**（dust） | 久未访问、重要性低，自然遗忘 | 只留残余 |

每条记忆按艾宾浩斯遗忘曲线衰减，但**访问会加固**（`recall` 命中即强化），重要性高的记忆衰减更慢，被置顶（`pinned`）的记忆永不遗忘。

### 做梦：上下文将满时的整理

上下文超过 75% 时触发"做梦"——灵枢重放短期记忆、按主题合并碎片、巩固到长期皮层，并把不重要的淡忘。梦完成后输出一份报告，告诉你巩固了什么、淡忘了什么、哪些还没想明白。

- `dream --auto`：内置自愈——重放 → 启发式合并 → 沉淀，不依赖任何外部脚本。
- 手动三步：`dream --trigger context` → 读 pending 填 apply.json → `dream --apply apply.json`。

### 上下文水位三级防线

**会话中即记即存，不攒到做梦。** 约定、情绪高点、里程碑当时就 `remember`；上下文快满时原文封存：

| 水位 | 动作 | 命令 |
|---|---|---|
| **随时** | 即记即存 | `remember` / `capture` |
| **60%** | 主动清点：本轮还有哪些该记没记，先补记 | `check-context --used 60` |
| **75%** | 做梦：重放→压缩→巩固 | `dream --auto` |
| **90%** | 逃生：原样封存，先保命再压缩 | `capture "原文" --tag 逃生` |

**`capture` 保证 100% 不丢**：原文归档到 `verbatim/`（逐字不改，保险柜）+ 逐段进记忆库并打 `verbatim` 标记（做梦永不淡化、永不进淡忘区）。

### 工作记忆

当前上下文本身就是工作记忆，默认不落盘。只有需要留一手时才 `work add`（会话级暂存，最多 9 条，12 小时后自动丢）。每件带"未完成/已完成"状态（蔡格尼克效应）：没做完的事会在注入包里被点名"还惦记着"，做完用 `work done <序号>` 标记就放下；`work flush` 把值得留的转成真正记忆（`--skip-done` 只转没完成的，已 done 的直接丢），`work clear` 丢掉。

### 会话模式与情绪隔离

工程会话和日常聊天的需要是打架的：刚聊完情绪化话题转头写代码，旧情绪的余波还在注入包里，模型容易被带着走偏。灵枢用 `mode` 切三种会话模式（`config.mode`，默认 `auto`）：

- **work 工程模式**：情绪状态与拟人语气**不注入本会话**，注入包明确说"旧情绪与当前任务无关，当作没有"，先给可执行方案，重视正确性与工程契约。
- **human 拟人模式**：情绪照常注入，并把数值得翻成**可直接照着做的行为约束**（`emotion now` 不再只有裸数字）。
- **auto 自动模式**（默认）：按本轮话题关键词自动判——工程话题（代码/报错/接口/数据……）走 work，其余走 human。

切换：`mode work | human | auto`。写代码前想手动清掉余波，用 `emotion cool` 把 24 小时内的强情绪整体降温，或 `emotion adjust <序号>` 微调单条。

### 审核闸：AI 产物先过目

记忆→做梦→知识库→进化日志→人格，这条链路里 AI 既是整理者又可能编造者，幻觉会逐层沉淀污染人格。灵枢给每个 AI 动手的环节都加了审核闸：

- **AI 整理的记忆**（`dream --apply` 等）标 `review="pending"`，确认前不算事实。
- **AI 整理的知识**（`knowledge tidy`）标 `status="pending"`。
- **AI 发起的人格变更**记 `source`（dream/自我总结/…）与 `evidence`（依据的记忆/事件 id），`persona history` 可溯源。
- 一次看完所有待审项：`review` → `review confirm <id>` / `review discard <id>`（记忆 m_* 与知识 k_* 通用，后者等同 `knowledge confirm/discard`）；整体自查用 `audit`（待审计数 + 冲突记忆 + 该做什么）。

每个会话的注入包会主动提醒有 `[待审核]` 项、有 `[人格漂移]`，不让幻觉静默沉淀。

### 情绪与类人机制

情绪系统把"心情"做成一条会衰减的曲线，而不是一行写死的字：

- **两条轴**：valence（喜/哀，-1~1）+ arousal（强度，0~1），用**半衰期叠加**算出此刻心情（`emotion now`），看轨迹用 `emotion arc`，情绪共 7 大类（`emotion list`）。
- **怎么产生情绪**：`emotion evaluate` 认知评价自动产生（不是手动演出来的）、`emotion mirror` 共情跟入用户情绪、`emotion feel` 手动记一笔。共情强度与抑制清单是用户随时可改的设定（`emotion config`）。
- **错判了能微调**：`emotion events` 看事件流（带序号），`emotion adjust <序号>` 只改判错的那几项（强度/情绪名/原因），`emotion del <序号>` 删掉误记的整条，不用推倒重来。
- **情绪增强编码**：情绪强烈时记住的事更牢，记录带 `emotion` 标记（杏仁核打个标签）。
- **情绪传染 / 心情惯性**：新情绪会被当下心情"染色"——同向被点燃放大，反向被惯性往回拽，不会瞬间反转。
- **习惯化 / 享乐适应**：同一件事反复出现，情绪反应递减；反复记的同一件事重要性也不再步步抬升（但保留下限）。
- **杏仁核劫持**：情绪最强烈时理智权重被压下，但上限封在 40%——再上头也得给人留余地。
- **峰终定律**：对一段体验的记忆，几乎只由"最强烈的一刻"和"结尾"决定，情绪总结按这个口径出。
- **首因效应**：最早留下的印象占权更高，首次被想起的记忆强化更深。

### 模糊联想回忆

`recall` 不靠精确匹配：中文用字符二元组、英文/数字用词做模糊联想，按 **相关度 + 近期性 + 重要性 + 首因** 加权排序，并在命中后自动强化（越用越牢）。数字/编号被保留为独立词元，不会把"版本0"和"版本1"误当成同一条。

## 人格

人格**不预写**。第一次使用时问用户"你希望我叫什么、是什么性格"，用户说不知道就由灵枢自己定。喜好、厌恶、习惯由相处中自己写。

- 人格演化有完整历史，每次改动可回滚：`persona history` 看历史（序号从最新往前数，条目带**来源 source** 与**依据 evidence**，能看清这次改人格来自哪段对话/哪些记忆），`persona rollback <序号>` 一键撤销那次改动；回滚本身也记一条历史。
- **快照与漂移告警**：重要节点先 `persona snapshot --label "基准"` 拍一张完整快照；`persona snapshots` 列表、`persona restore <名字>` 一键还原。`persona drift` 把当前人格与最近基准快照逐字段对比（结构差异 + 最近 N 天 AI 改动，`--days` 可调），变化偏大时注入包会亮 `[人格漂移]` 告警——微小偏移也看得见，不是等跑偏了才发现。
- **好奇心与理智**是两条内驱数值，不是出厂设定——用户嫌你啰嗦，理智上调；用户让你多挖多试，好奇心上调（`mind tune`）。它们不是只有裸数值：注入包（`startup` 的 human 模式块）会把数值翻成行为约束（`_behavior_map`），模型照做即可。
- `persona self` 让你总结"我是谁"，`persona interview` 是初识引导。

## 成长路径

### 知识库

把经历提炼成可复用的**结论**（不是流水账）。每条有主题、观点、要点、相关记忆引用，支持置顶与版本更新。`knowledge tidy` 会提示你哪些经历值得提炼成知识——**AI 动过的条目一律先标为待审核**（面板里直接看，或 `knowledge review` 查看），人工 `knowledge confirm <id>` / `knowledge discard <id>` 确认后才算数，防止模型幻觉直接沉淀成知识库。

### 笔记日志

写给自己的日记（journal）、工作日志（log）、成长复盘（growth）。`note digest --days 7` 定期回看自己写了什么。

### 技能自编辑与全局化

技能是固化的做法：`skill new` 写 → `skill check` 自检 → `skill install` 装进全局技能目录（`~/.trae-cn/skills/`），新会话直接可调。`skill use --auto` 用技能时自动全局化；`skill borrow` 借鉴别人的技能变成自己的版本（诚实标注来源）。**支持中文技能名**。

### 进化日志

每次真正的改进（人格微调、技能新增、知识沉淀、思维变化）记进进化日志 `evolve log`，是灵枢的成长史。

## DeepSeek 生态兼容

- **整目录全局化**：带 `scripts/`、`assets/` 的技能整目录复制进全局目录，副本能直接跑。
- **自动生成 `AGENTS.md`**：DSH/DeepSeek 以 AGENTS.md 为常驻入口（每次会话必进上下文），安装时自动生成「何时用 / 怎么用 / 入口命令」。
- **DSH 插件只读可见**：`skill plugins` 列出 `~/.dsh/profiles` 下所有 DSH 插件（名字、版本、描述、AGENTS.md 标记），面板也有专属区块——只看不改。
- **frontmatter 解析健壮**：支持引号、多行块标量，各家写的 SKILL.md 都能读对。

## 可视化面板

`brain.py serve` 启动本地面板（仅监听 127.0.0.1，`start-panel.bat` 一键启动）：

- 记忆浏览：层 + 重要性区间 + 时间范围 + 标签 + 情绪 + 排序的**组合筛选**；置顶、编辑、忘记
- **注入包预览**：直接看 `startup` 会注入给模型的完整记忆包（只读，不落盘、不加强记忆）
- **冲突检查**：启发式扫出"可能矛盾/已过时"的记忆对，只提醒不下结论
- 人格、内驱数值实时查看与微调；**人格历史可一键回滚**
- 知识库（含 **⏳待审核 / 已确认 / 已废弃** 状态，可 confirm/discard）、笔记、技能（含 DSH 插件区块）、成长、进化日志
- 做梦报告、备份恢复
- **只读降级有醒目横幅**：目录不可写时页面顶部直接提示"修改不会保存"

**安全**：面板自带随机 token 鉴权（页面加载时注入，所有 API 请求必须携带），并校验 Host/Origin，挡住 DNS rebinding 与无来源直连偷读记忆。

## 可靠性设计

- **跨进程写锁**：并发写入用独占锁互斥，不丢更新
- **损坏行自愈**：JSONL 里出现坏行自动分拣——好行写回，坏行挪到 `.corrupt`，绝不静默丢失
- **备份/恢复**：`backup` 打包整个记忆库（含 verbatim/ 与 skills/），自动修剪旧备份；`restore` 恢复前先留现状备份，校验 zip 合法性并防 zip-slip 路径穿越
- **只读降级**：沙箱写盘被拒时自动降级为只读注入，已有的记忆照样带回；`--home` 支持把数据目录迁到工作区
- **原子写入**：所有文件写入走临时文件 + `os.replace`，不会出现写一半的脏文件

## 脚本 vs 模型：谁在真正干活

**先打消一个误会：装上灵枢不等于自动拥有类人脑智能。** 这套脚本是「存储、调度、数学计算」；真正的「智能」——合并记忆、理解情绪、提炼知识——仍然靠你接入的大模型。谁干什么，边界如下：

| 工作 | 谁干 | 说明 |
|---|---|---|
| 存储 / 衰减 / 强化 / 分层搬运 | **脚本** | 艾宾浩斯曲线、蔡格尼克、峰终、杏仁核劫持上限等全是公式，模型只负责触发命令 |
| 模糊回忆 | **脚本** | 字符二元组 / 词元加权，不需要模型参与（也因此是字面匹配，不是向量语义检索） |
| 上下文注入包（startup 输出） | **脚本** | 纯拼装，模型照抄即可 |
| 重放 → 碎片分组（dream） | **脚本** | pending 分组是启发式完成 |
| 碎片合并成新记忆、判断要不要 | **模型** | 这一段是幻觉高发区：合并错误、编造就发生在这里 |
| 情绪事件的解读（emotion evaluate） | **模型** | 小模型会把中性对话判成愤怒/狂喜，或看不出反话 |
| 提炼知识库（knowledge tidy） | **模型** | 小模型容易编造结论，直接沉淀成知识 = 幻觉落盘 |
| 技能自检（skill check） | **模型** | 全靠模型读正文挑毛病 |

所以：**脚本保证"记下来了、想得起来、删得掉、片段不丢"；模型决定"记得准不准、合并对不对、情绪认不认"。** 想提高"记得准"，就换更强的模型或动 `config`。

## 模型适配与档位

灵枢不在脚本里硬堵模型能力，而是提供一个**档位开关**，让高风险自动能力按模型强弱开合：

| 能力 | 7B+ / 云端 | 1.5B–3B 小模型 |
|---|---|---|
| 记忆增删查（remember / recall / list） | ✅ 完整 | ✅ 完整（脚本本身零模型依赖） |
| capture 原样封存 | ✅ | ✅ |
| dream 重放分组 | ✅ | ✅（启发式部分可用） |
| dream --apply 合并结果 | ✅ 完整用 | ⚠️ 建议关：合并错误 = 幻觉记忆落盘 |
| knowledge tidy | ✅ 完整用 | ⚠️ 建议关（或强制走待审核） |
| emotion evaluate / mirror | ✅ | ⚠️ 易判错情绪，改用手动 `emotion feel` |
| skill check | ✅ | ⚠️ 结果可信度低 |

**小模型模式**（`config set model_tier small`）做的事：`startup` 注入包里追加「小模型模式」约束——不要自动 `dream --apply`、不要自动 `knowledge tidy`（产出须先 `knowledge review` 过目）、情绪评估改用手动 `emotion feel`。这只是**注入提示**，约束力取决于模型听话程度；3B 以下仍会漏执行，别指望它当保险丝。恢复：`config unset model_tier`（回到 auto）。

**输出容错**：所有需要模型产 JSON 的入口（`dream --apply`、`knowledge tidy --apply`）都做了**容错解析**——先按标准 JSON 读，失败再去掉 ```json 代码围栏，再失败截取第一个配平的 `{...}`，实在读不出来会**明确报错并返回退出码 2**，不再静默吞掉。解析失败时报错里带文件路径与内容开头，方便直接排查。

## 接入大模型（Ollama 示例）

灵枢是命令行工具，接入哪家模型由你决定，它只负责被调用。以本地 Ollama + qwen2.5 7b 为例：

```bash
# 1) 你家 Agent 是一次会话的调度者，为每次会话先跑三条
python scripts/brain.py startup --query "$USER_MSG"        # 把记忆注入提示前面
python scripts/brain.py remember "$USER_MSG" --source user # 用户说的入模记忆
# 2) 之后正常对话；水位到了再触发整理
python scripts/brain.py check-context --used 68            # 60 清点 / 75 做梦 / 90 封存
python scripts/brain.py dream --auto
```

Ollama 侧只需要一份 `Modelfile`，把"调用脚本"写进 system prompt 让它自己看情况执行：

```dockerfile
FROM qwen2.5:7b
SYSTEM """你运行在本地，有记忆系统「灵枢」。每轮先按需执行：
- 新会话第一轮：运行 startup --query "本轮主题"，把输出原样作为你的记忆前缀。
- 用户说了值得记的事：运行 remember "<原话>" --kind fact|preference|emotion --importance 1-10。
- 需要回忆：运行 recall "<关键词>"。
- 上下文快满：运行 check-context --used <已用百分比>，并按提示配合。
如果命令没有可执行环境，就按 SKILL.md 的规则口头执行，绝不乱编记忆。
"""
```

更省事的替代：直接把这套框架装成 Agent 技能（`skill install` 全局化后，DSH / 本地 Agent 每会话自动读到 SKILL.md，按里面的机制走）。

## 规模上限与归档

记忆以 JSONL 纯文本存储（hippocampus/cortex/dust 等），**没有数据库索引**：

- `recall` 模糊匹配要遍历全部记录，条数上万后每次命中会变慢（本地为毫秒级→百毫秒级，仍在可用区间）。
- `dream` 会自动把一批短期记忆打包喂给模型，短期记忆越多，当次 token 消耗越大。

**建议的养生方案**：每周一次 `backup`（默认保留最近 20 份，自动修剪）；长期不用的旧记忆用 `forget` 移入淡忘区（只留残余轮廓，不占活跃匹配）；知识库定期 `knowledge review` 确认。真到数据膨胀影响手感的层级，把 `--home` 指向新目录再 `restore` 最近备份，旧目录留档。

## 如何更新灵枢

**主仓库：<https://gitee.com/htmcwy_0/lingshu-skill>**　镜像：<https://github.com/266767398/lingshu-skill>

对 AI 说一句「**更新灵枢技能**」即可，它会按 `SKILL.md` 的「更新灵枢技能」流程执行。手动更新也简单：

```bash
DIR="$HOME/.cache/灵枢-skill"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull; else git clone --depth 1 https://gitee.com/htmcwy_0/lingshu-skill.git "$DIR"; fi
# 然后把这些覆盖到技能目录：SKILL.md SKILL-lite.md SKILL-tiny.md README.md CHANGELOG.md docs/ assets/ scripts/ start-panel.bat
```

- 更新只替换**技能本体文件**；记忆数据在 `~/.brain-memory/`，**不会动**。
- 你长期手动维护的规则（DSH 只读保护、全局目录指回等）写进 `~/.brain-memory/lingshu.user.md`（`user-rules add <规则>`），更新永不覆盖、每会话自动注入；全局 `AGENTS.md` 里 `<!-- lingshu:start/end -->` 之外的自己写内容，更新时也原样保留。
- 更新前可对照 `CHANGELOG.md` 看这次改了什么；更新后跑一次 `python scripts/brain.py stats` 自检。
- 没有 git 的机器：到仓库页面下载 ZIP，解压后手动覆盖同名文件即可。

## 快速开始

```bash
# 初始化记忆库
python scripts/brain.py init

# 写一条记忆
python scripts/brain.py remember "用户喜欢用 Python 写脚本" --kind preference --importance 8

# 会话开始注入记忆（返回码 0=正常 10=只读降级 2=未初始化 64=命令写错）
python scripts/brain.py startup --query "本次会话主题"

# 回忆
python scripts/brain.py recall "Python"

# 记一次心情，看此刻心情
python scripts/brain.py emotion feel 开心 --intensity 0.8 --about "搞定了 bug"
python scripts/brain.py emotion now

# 上下文 75% 了，做梦整理
python scripts/brain.py dream --auto

# 上下文 90% 了，先封存保命
python scripts/brain.py capture "把这一段原文粘进来" --tag 逃生

# 启动可视化面板
python scripts/brain.py serve   # 或直接双击 start-panel.bat
```

## 命令速查

| 领域 | 命令 |
|---|---|
| 记忆 | `remember` / `recall` / `inject` / `startup` / `list` / `forget` |
| 上下文水位 | `check-context --used 68`（60 清点 / 75 做梦 / 90 封存） |
| 原样封存 | `capture "<原文>" [--tag 逃生]` |
| 做梦 | `dream` / `dream --apply apply.json` / `dream --auto` |
| 人格 | `persona show\|self\|interview\|mind\|state\|edit\|history [--limit]\|rollback <序号>\|snapshot [--label]\|snapshots\|restore <名字>\|drift [--days 7] [--json]` |
| 情绪 | `emotion now\|arc\|list\|feel\|evaluate\|mirror\|desire\|config\|events\|adjust <序号>\|del <序号>\|cool` |
| 工作记忆 | `work status\|add\|list\|done <序号>\|flush [--skip-done]\|clear` |
| 会话模式 | `mode show\|work\|human\|auto`（工程会话自动隔离情绪） |
| 审核闸 | `review [--json]` / `review confirm <id>` / `review discard <id>`（记忆 m_* 与知识 k_* 通用） |
| 用户规则 | `user-rules show\|add <规则>\|clear`（写在数据目录 lingshu.user.md，更新永不覆盖，每会话自动注入） |
| 自查 | `audit [--json]`（待审计数 + 冲突记忆 + 该做什么） |
| 知识库 | `knowledge add\|list\|search\|show\|pin\|rm\|tidy\|review\|confirm <id>\|discard <id>` |
| 冲突检查 | `detect-conflicts [--json] [--limit 50]`（启发式，只提醒不下结论） |
| 模型档位 | `config show` / `config set model_tier small` / `config unset model_tier` |
| 笔记 | `note write\|search\|digest` |
| 技能 | `skill new\|edit\|list\|show\|check\|install\|uninstall\|rm\|find\|use\|borrow` |
| DSH 插件（只读） | `skill plugins [--json]` |
| 成长 | `growth status` / `growth review --days 7` |
| 进化日志 | `evolve log "标题" --kind persona\|skill\|knowledge\|thinking\|self` |
| 备份恢复 | `backup [--out 目录] [--keep 20]` / `restore <zip> [--yes]` |
| 数据目录 | `home show\|move\|verify` |
| 与 DSH 同步 | `sync dsh [--dir 目录] [--days 1] [--force]` |
| 统计 / 面板 | `stats` / `serve [--port] [--no-browser]` |

全局参数：`--home <目录>`（切换数据目录）、`--ro`（只读降级）。

## 项目结构

```
灵枢-skill/
├── SKILL.md                # 技能本体：AI 每会话必读的主文件（动作+高频命令+docs 索引）
├── SKILL-lite.md           # 精简版：小模型 / 新人用的降档说明书
├── SKILL-tiny.md           # 最小试用版：只剩记忆三件套的入门说明
├── docs/
│   ├── mechanics.md        # 机制详解（做梦/情绪/知识库/技能/人格规则…）按需 Read
│   ├── commands.md         # 完整命令速查 + 可视化面板 + 故障排查 FAQ
│   └── models.md           # 模型分级降档 + 幻觉防护三道闸 + 如何更新灵枢
├── CHANGELOG.md            # 变更历史
├── start-panel.bat         # 一键启动可视化面板
├── assets/
│   └── panel.html          # 可视化面板前端（单文件，无依赖）
└── scripts/
    ├── brain.py            # 核心引擎：记忆/人格/做梦/工作记忆/备份恢复/上下文防线/面板服务
    ├── growth.py           # 成长系统：知识库/笔记/技能/进化日志/DSH 插件扫描
    └── panel_server.py     # 本地面板 HTTP 服务（token 鉴权 + 来源校验）
```

运行时数据（默认 `~/.brain-memory/`，`--home` 可迁移）：

```
.brain-memory/
├── hippocampus.jsonl       # 短期记忆（海马体）
├── cortex.jsonl            # 长期记忆（皮层）
├── dust.jsonl              # 淡忘区
├── working.jsonl           # 工作记忆（会话暂存）
├── emotions.jsonl          # 情绪事件流（心情按半衰期从这里叠加出来）
├── persona.json            # 人格档案
├── config.json             # 配置（阈值/权重）
├── skills/                 # 自己写的技能
├── knowledge/              # 知识库
├── notes.jsonl             # 日记/日志/成长复盘
├── evolution.jsonl         # 进化日志
├── dreams/                 # 梦报告与 pending
├── verbatim/               # capture 原样封存的保险柜
└── backups/                # 备份
```

## 边界

灵枢管**人格/情绪/关系**，DSH 管**工作/项目**——每日用 `sync dsh` 把灵枢日志摘要自动进 DSH 当日日志，两边各记各的，不重复、不漂移。

---

纯本地、零依赖、你的数据永远是你的。隐私是默认，不是选项。
