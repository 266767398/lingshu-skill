# 灵枢 · 类脑记忆、人格与成长

> 名字取自《黄帝内经》：**算力给思考，人格给灵魂，记忆给生命。**

一套纯本地、零依赖（只用 Python 标准库）的 AI 记忆系统。它把"上下文窗口"变成"会生长的记忆"——短期记忆像海马体一样快速写入、自然衰减；上下文将满时像人做梦一样重放、压缩、巩固到长期皮层；想不起来时靠模糊联想而不是精确匹配。

## 目录

- [核心机制](#核心机制)
  - [分层记忆与遗忘曲线](#分层记忆与遗忘曲线)
  - [做梦：上下文将满时的整理](#做梦上下文将满时的整理)
  - [上下文水位三级防线](#上下文水位三级防线)
  - [工作记忆](#工作记忆)
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

当前上下文本身就是工作记忆，默认不落盘。只有需要留一手时才 `work add`（会话级暂存，最多 9 条，12 小时后自动丢）。

### 模糊联想回忆

`recall` 不靠精确匹配：中文用字符二元组、英文/数字用词做模糊联想，按 **相关度 + 近期性 + 重要性** 加权排序，并在命中后自动强化（越用越牢）。数字/编号被保留为独立词元，不会把"版本0"和"版本1"误当成同一条。

## 人格

人格**不预写**。第一次使用时问用户"你希望我叫什么、是什么性格"，用户说不知道就由灵枢自己定。喜好、厌恶、习惯由相处中自己写。

- 人格演化有完整历史，每次改动可回滚。
- **好奇心与理智**是两条内驱数值，不是出厂设定——用户嫌你啰嗦，理智上调；用户让你多挖多试，好奇心上调（`mind tune`）。
- `persona self` 让你总结"我是谁"，`persona interview` 是初识引导。

## 成长路径

### 知识库

把经历提炼成可复用的**结论**（不是流水账）。每条有主题、观点、要点、相关记忆引用，支持置顶与版本更新。`knowledge tidy` 会提示你哪些经历值得提炼成知识。

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

- 记忆浏览：按层/类型/重要性筛选、置顶、编辑、忘记
- 人格、内驱数值实时查看与微调
- 知识库、笔记、技能（含 DSH 插件区块）、成长、进化日志
- 做梦报告、备份恢复

**安全**：面板自带随机 token 鉴权（页面加载时注入，所有 API 请求必须携带），并校验 Host/Origin，挡住 DNS rebinding 与无来源直连偷读记忆。

## 可靠性设计

- **跨进程写锁**：并发写入用独占锁互斥，不丢更新
- **损坏行自愈**：JSONL 里出现坏行自动分拣——好行写回，坏行挪到 `.corrupt`，绝不静默丢失
- **备份/恢复**：`backup` 打包整个记忆库（含 verbatim/ 与 skills/），自动修剪旧备份；`restore` 恢复前先留现状备份，校验 zip 合法性并防 zip-slip 路径穿越
- **只读降级**：沙箱写盘被拒时自动降级为只读注入，已有的记忆照样带回；`--home` 支持把数据目录迁到工作区
- **原子写入**：所有文件写入走临时文件 + `os.replace`，不会出现写一半的脏文件

## 快速开始

```bash
# 初始化记忆库
python scripts/brain.py init

# 写一条记忆
python scripts/brain.py remember "用户喜欢用 Python 写脚本" --kind preference --importance 8

# 会话开始注入记忆（返回码 0=正常 10=只读降级 2=未初始化）
python scripts/brain.py startup --query "本次会话主题"

# 回忆
python scripts/brain.py recall "Python"

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
| 人格 | `persona show\|self\|interview\|mind\|state\|edit` |
| 工作记忆 | `work status\|add\|list\|flush\|clear` |
| 知识库 | `knowledge add\|list\|search\|show\|pin\|rm\|tidy` |
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
├── SKILL.md                # 技能本体：给 AI 读的完整说明书（frontmatter + 全部机制）
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
