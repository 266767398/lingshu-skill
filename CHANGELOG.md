# 变更历史

本文件记录灵枢的版本变更。版本号跟随 `SKILL.md` 的 frontmatter 版本。

## v2.7 — 全面审查完善：健壮性 + 只读门控统一 + 错误修复

对全部脚本与文档做了一次系统的自我审查，按"真影响使用"筛选修复：

**换库与档案安全**

- `--home` 换数据目录时，`assoc` / 人格快照 / `daily` 状态随新目录重算（此前会写错旧库）。
- 本地 JSON 统一用 `utf-8-sig` 兼容 BOM；`persona.json` 损坏时先备份 `.corrupt-{时间戳}` 再重建，绝不静默覆盖。
- `persona rollback` 对 onboarding 这类字典字段做类型保护（防注入崩溃）；`persona restore` 快照名清洗统一、恢复前自动备份当前人格。
- `home move` 提前拒绝与当前目录相同 / 搬到当前目录内部的请求（防边复制边膨胀）。

**只读门控（readonly）三处统一**

- brain.py 写命令列表补全：`work done`、`config set/unset`、`sync`（非 dry-run）、带名字的 `emotion desire`、带参数的 `emotion config`。
- growth.py 加统一只读门控：knowledge / note / skill / ext / evolve / learn 的写命令在只读时统一返回 3（此前只有 ext 单独挡）。
- panel_server.py 22 个写端点加只读 403 拦截；启动时探测只读 + 端口占用友好报错。

**行为修复**

- `_is_lingshu` 只认名字叫「灵枢」的技能才是本体（不再因 `origin` 含"灵枢"误判），AI 自建技能安装不再被风险确认卡死。
- `dream --apply` 只读时返回 3、`--dry-run` 只预览不落盘。
- work 模式情绪隔离修复：只读身份注入不再带情绪段。
- `add_knowledge` 合并时跳过已废弃条目；`tidy` 去重防误删基础记录。
- `emotion adjust/del/cool` 统一走并发锁；非法情绪名报错不静默。
- `save_all` 未知层归入海马体；`user-rules add` 末尾补换行防粘连。
- `skill use --auto` 只装一次（此前重复二次 install）；`skill/knowledge rm` 缺失时错误码统一。
- `startup` 等提取最近条目按时间倒序（此前显示最旧）；成长指标同理。

**扩展模块**

- 扩展模块注册进 `sys.modules`（@dataclass/pickle 可跨模块用）；语法检查改用内置 `compile`，不再落 `__pycache__`；`inject` 输出截断 800 字符；捕获 `BaseException` 防 `sys.exit` 杀进程。
- `ext new` 模板的 DESC 引号转义（不再生成坏 Python）。

**文档同步**

- `docs/commands.md`：删幽灵命令 `persona edit` / `config get`；补 `user-rules` / `ext` / `assoc unlink` / `work status|list` / `note list|show`。
- `README.md`：更新 persona 行、补数据目录树（assoc/desires/快照等）、标注版本。
- `SKILL-lite.md` / `SKILL-tiny.md`：`persona edit`→`set`、补审核闸提示、版本同步、修正漏字。
- `docs/mechanics.md`：删掉不存在的"新增记忆 ≥15 条"做梦触发条件、TOC 补扩展模块。
- `SKILL.md`：版本号 2.6→2.7、数据位置清单补全、`startup --budget` 注明。

## v2.6 — 扩展模块：AI 自己写进灵枢的可执行钩子

技能（`skill`）= 说明书（给别人复用）；扩展模块（`ext`）= 代码（AI 自己写进灵枢引擎的 Python 钩子），更自由：

- **`ext` 命令**：`ext new <名字>` 生成可跑模板 → `ext edit --body/--append/--file` 改 → `ext show/list/run/check/rm`。
- **钩子**：`DESC`（列表说明）、`inject(ctx)`（返回文本并入**每会话注入包**，长期提醒）、`run(argv)`（`ext run <名字> [参数]` 自定义命令）。
- **更新永不覆盖**：扩展放在数据目录 `~/.brain-memory/extensions/`，和 `lingshu.user.md`、`skills/` 一样，更新技能只动技能目录。
- **坏模块不致命**：某扩展语法错/抛异常时被静默跳过，灵枢照常工作；`ext check <名字>` 提前查语法。
- **只读门控**：数据目录不可写时 `ext new/edit/rm` 被挡（返回 3），`list/show/run/check` 放行。
- SKILL.md 新增「扩展模块」节、README 数据目录树与命令速查、docs/mechanics.md 扩展模块小节。

## v2.5 — 更新不再覆盖用户内容

升级会覆盖技能目录文件，把用户手动补的规则覆盖掉（DSH 只读保护、全局目录指回等），每次都要重写一遍。v2.5 从机制上解决：

- **`lingshu.user.md`（数据目录）**：新增 `user-rules` 命令（`user-rules show / add <规则> / clear`）。你的长期规则（如"不动 DSH 数据目录""提到全局时指回 `~/.trae-cn/skills`"）写进 `~/.brain-memory/lingshu.user.md`——更新技能只动技能目录、不碰数据目录，**升级后永远还在**；且每个会话的注入包自动带一段「**用户附加规则**」，灵枢每次都会看到并遵守。
- **AGENTS.md 区块化维护**：生成的常驻入口改用 `<!-- lingshu:start -->` … `<!-- lingshu:end -->` 包裹。更新时只替换标记之间的规则区块，**标记之外你手写的内容原样保留**；旧版无标记文件若整文件都是生成内容则整体替换，若是用户手写则把灵枢区块插到最前面、原内容保留。
- SKILL.md 新增「用户附加规则」节 + 数据位置说明；`docs/models.md` 更新流程注明不会覆盖；`skills/` 列表与命令索引同步更新。

## v2.4 — 上下文瘦身 + 喜好/讨厌标签修复

- **主 SKILL.md 瘦身**：808 行 → 190 行（约 1.35 万 token → 约 3500 token，-75%）。主文件只留每会话必做动作（startup / 即时落盘 / 水位三档 / 会话模式 / 审核闸 / 自检清单）+ 高频命令索引；细节拆到 `docs/` 按需 Read：
  - `docs/mechanics.md`：理智与好奇心、七情六欲、记忆关联、做梦三步、capture、知识库、笔记、学习闭环、技能自编辑、成长、自我进化、灵魂与自我、人格规则、备份迁移、DSH 同步、设计依据
  - `docs/commands.md`：完整命令表、可视化面板、故障排查 FAQ
  - `docs/models.md`：能力边界、模型分级降档、幻觉防护三道闸、如何更新灵枢技能
  - 信息零丢失：原 SKILL.md 各节原文搬入对应 docs，主文件只加索引。
- **喜好/讨厌标签修复**：面板人格页"喜欢/讨厌"标签之前只读顶层 `likes/dislikes`，而实际数据在 `evolved.learned_likes/dislikes`，导致标签空白。现在合并展示（去重），并给"自我设定"区加了「喜欢 / 讨厌」输入框，可直接编辑保存（写入人格历史）。

## v2.3 — 基线复盘：审核闸 · 会话模式 · 人格快照与漂移 · 情绪微调

针对七条基线问题的修复（"软提示可无视 / 幻觉逐层沉淀 / 人格变更无溯源 / 情绪错判只能整删 / 裸数值缺行为映射 / 拟人模式与工程模式冲突 / 人格漂移静默"）：

1. **审核闸（对抗幻觉逐层沉淀污染人格）**
   - AI 整理的记忆（`dream --apply` 等）标 `review="pending"`，确认前不算事实；`review` / `review confirm <id>` / `review discard <id>` 统一过目（记忆 `m_*` 与知识 `k_*` 通用）。
   - AI 发起的人格变更写入 `source`（dream/自我总结/…）与 `evidence`（依据的记忆/事件 id），`persona history` 可溯源。
   - 新增 `audit` 整体自查：待审计数 + 冲突记忆 + 该做什么；注入包主动提醒 `[待审核]`。
2. **会话模式与情绪隔离（工程时自动隔离情绪）**
   - `config.mode`（默认 `auto`）+ `mode show/work/human/auto` 命令。
   - work 模式：情绪与拟人语气不进注入包，明确"旧情绪与当前任务无关"；human 模式：情绪照常注入。
   - auto 模式按话题关键词自动判（代码/报错/接口/数据 → work，日常 → human）。
3. **人格快照与漂移告警（微小偏移也看得见）**
   - `persona snapshot [--label]` 拍完整快照（`persona_snapshots/`），`persona snapshots` 列表、`persona restore <名字>` 一键还原。
   - `persona drift [--days]` 与最近基准快照逐字段对比（结构差异 + AI 改动），变化偏大时注入包亮 `[人格漂移]` 告警。
4. **情绪微调（错判不用整条删）**
   - `emotion events` 看事件流（带序号）；`emotion adjust <序号>` 只改判错的项（强度/情绪名/原因/余波时长）；`emotion del <序号>` 删误记的整条；`emotion cool` 24 小时内强情绪整体降温（写代码前清余波）。
   - 修正：`adjust / del` 的序号与 `events` 展示一致（按时间降序编号）。
5. **行为映射（裸数值 → 可直接照做的约束）**
   - 注入包 human 模式块把好奇心/理智/心情翻成行为约束（`_behavior_map`），不再只有裸数字。
6. **健壮性**
   - `read_jsonl` 改 `utf-8-sig`：Windows 脚本/编辑器写出的带 BOM 文件首行不再被误当"坏行"丢进 .corrupt。

## v2.2 — 技能自更新流程

- 新增「更新灵枢技能」：SKILL.md 内置一键更新流程（gitee 仓库拉取 → 版本对照 → 覆盖技能文件 → 自检），对 AI 说「更新灵枢技能」即可执行。
- 主仓库：<https://gitee.com/htmcwy_0/lingshu-skill>　镜像：<https://github.com/266767398/lingshu-skill>。
- 更新只替换技能本体文件，记忆数据（`~/.brain-memory/`）不受影响。

## v2.0 — 类人机制 + 工程化补强

### 新的核心机制（"更像人"）

- **蔡格尼克效应**：工作记忆条目带「未完成/已完成」状态，没做完的事在注入包被点名，做完 `work done` 放下。
- **峰终定律**：情绪总结按"最强烈一刻 + 结尾"出，不看平均值。
- **首因效应**：最早留下的印象占权更高，首次被想起的记忆强化更深。
- **情绪传染 / 心情惯性**：新情绪被当下心情染色，同向放大、反向衰减，不瞬间反转。
- **习惯化 / 享乐适应**：同一件事反复出现，情绪反应与重要性不再无限抬升（保留下限）。
- **杏仁核劫持上限 40%**：再怎么上头，理智权重被压不能过半，给人留余地。

### 对抗"AI 幻觉落盘"（可靠性）

- **容错 JSON 解析**：`dream --apply` / `knowledge tidy --apply` 先标准解析，失败去 ```json 围栏，再失败截取配平片段；实在失败**明确报错 + 退出码 2**，不再静默吞掉。
- **知识库审核状态**：AI 动过的知识条目一律标 `pending`（待审核）；`knowledge review` 查看，`confirm / discard` 后才算数。检索、统计自动排除废弃项。
- **小模型档位**：`config set model_tier small` 后，`startup` 注入包追加「小模型模式」约束——不自动做梦合并、不自动提炼知识（须先审核）、情绪评估改手动。
- **记忆冲突检测**：`detect-conflicts` 按实体启发式扫"改口/否定"信号，标出可能过时或矛盾的记忆对（只提醒不下结论）。
- **人格回滚**：`persona history` 查看（序号从最新往前数），`persona rollback <序号>` 一键撤销，回滚本身也留历史。

### 可视化面板体验

- **组合筛选**：层 + 重要性区间 + 时间范围 + 标签 + 情绪 + 排序一键组合。
- **注入包预览**：直接看 `startup` 会注入的完整记忆包（只读）。
- **冲突检查**：面板内一键扫描并平铺展示可能矛盾的记忆对。
- **知识库审核操作**：待审核条目在列表中直接 ✓ 确认 / ✕ 废弃。
- **人格历史一键回滚**：历史表每行带「回滚到此」按钮。
- **只读横幅**：目录不可写时页面顶部显眼提示"修改不会保存"，侧栏同步显示只读与模型档位。

### 其他

- `recall` 只强化命中过的记忆，不再连带整个检索对象被强化。
- 情绪事件写入增加字段校验，损坏数据不再静默产生。
- `startup` 返回码规范：0 正常 / 10 只读降级 / 2 未初始化 / 64 命令写错。
- 新增 `CHANGELOG.md`、`SKILL-lite.md`（精简版）、`SKILL-tiny.md`（最小试用版）。
- README 顶部新增：幻觉固化风险警告、脚本 vs 模型职责表、模型适配表、Ollama 接入示例、规模上限与归档说明。

## v1.0 — 基础版

- 分层记忆（海马体 / 皮层 / 淡忘区）+ 艾宾浩斯遗忘曲线 + 访问加固 + 置顶永不遗忘。
- 做梦：`dream --trigger` → 人工 `--apply`，`dream --auto` 内置自愈。
- 上下文水位三级防线 + `capture` 原样封存（verbatim 保险柜）。
- 模糊联想回忆（中文字符二元组 / 英文数字词元加权）。
- 人格不预写：初识引导 + 演化历史 + 好奇心 / 理智双驱。
- 成长路径：知识库、笔记日志、技能自编辑与全局化、进化日志。
- 本地面板（token 鉴权 + Host/Origin 校验）+ 备份恢复 + 只读降级。