# 变更历史

本文件记录灵枢的版本变更。版本号跟随 `SKILL.md` 的 frontmatter 版本。

## v2.18 — 检查更新只走远程拉取（Gitee 优先、GitHub 兜底）

- **删除本地开发仓库同步路径**：更新链路不再读取 `lingshu_source_dir`，灵枢
  本体更新源只走远程拉取。先试 Gitee 仓库（main 分支），失败自动兜底 GitHub
  仓库（main 分支）。纯标准库实现，走 REST 匿名接口（目录树 + 逐文件内容，
  30 秒下载超时）——归档下载端点被风控拦截对脚本不可用，API 接口稳定。
- **缓存复用**：远程源解压到系统 temp 下带仓库标识的目录，Gitee/GitHub 共用
  同一份会话缓存；点「更新」时强制重新拉取，确保拿到最新代码。
- **降级安全**：两个远端都失败时降级为「源即副本」（显示已最新），不崩溃。
- **来源标识**：更新检查结果 `source` 字段取值改为 gitee / github / none，
  面板更新弹窗显示「来源：Gitee 远程 / GitHub 远程」。

## v2.17 — 全量代码审查修复：28 项（6 严重 + 11 中等 + 11 建议）

### brain.py
- **人格字段写入校验**：traits/drives/state.energy/closeness 强制 float 转换并
  clamp 到 0-100，非数值直接拒绝——七情六欲上限不再可被脏数据突破。
- **atomic_json 原子写补强**：rename 失败重试（Windows 文件占用场景）。
- **欲望满足下限**改用 DESIRE_FLOOR 常量；**情绪冷却 factor** clamp [0.01,1]。
- **同步到 APP**（sync_app）改临时文件 + 原子替换，不再半途留下损坏副本。
- **日志级别过滤语义修正**：改为"指定级别及以上"。

### growth.py
- **update_check 兜底**：更新说明拿不到 CHANGELOG 时给通用文案，不再把
  changed/added/removed 文件清单下发（更新说明只此一份，不显示文件清单）。
- **create_skill** 名字归一化统一走 `_require_slug()` 校验。
- **install_skill** 补目标在源祖先目录内的双向防呆，防自覆盖。
- **note_stats / tidy_knowledge** 对 body 为 null 的记录不再崩。
- **skill use --auto** 改用解析结果里的真实 path；删除死代码 GLOBAL_SKILLS；
  ext_injections 的 BaseException 收窄为 Exception。

### panel_server.py
- **修复 Handler.timeout 类属性在 Python 3.13 完全无效的问题**：改 setup() 中
  socket settimeout(30)，超时真正生效。
- **异常分类修正**：do_POST 中 TypeError/AttributeError 从 400 分支移到 500——
  修掉 /api/skill 莫名 400 的堆栈遮蔽。
- **路径泄露防护**：FileExistsError/PermissionError 回显改通用文案，细节进 stderr。
- 清理：无效死配置（_WRITE_ENDPOINTS 永不命中项）、hmac import 归位顶部。

### panel.html
- 搜索结果字段兜底（parts/score）；dream/apply 结果兜底；评分/语气过 esc 转义。
- 风险确认标志用完重置；api() 加 15 秒超时，不再无限等待。

### plugin.js（Koishi 启动插件）
- stderr 管道 resume 防堵死；超时改 taskkill /T /F 杀整棵进程树；兜底 timer
  正确清理；删除死参数。

## v2.16 — 自更新体验打磨：changelog 人话说明 + 弹窗渲染修复

（a2bd6eb + 30f35f4 合入）

### 更新弹窗显示"修复了什么/完善了什么/新增了什么"

- **`growth._changelog_between()`**：后端从**源的 CHANGELOG.md** 自动抽取
  「已装版本 → 最新版本」区间的正文（跨多个版本正确合并，去掉合并哈希注释行），
  作为 `changelog` 字段随 `/api/self-update` 返回——更新说明只此一份（CHANGELOG），
  不用维护第二份。
- **前端人话渲染**：有 changelog 就优先显示（只把 `**加粗**` 标记的内容渲染成
  加粗，其余星号类标记原样隐藏；全部先 `esc()` 转义防 XSS）；CHANGELOG 拿不到才
  回退文件清单。弹窗标题升级为「灵枢本体有更新（v旧 → v新）」。
- **弹窗限高滚动**：长更新说明限高 60vh 内部滚动，不再撑爆弹窗。
- **更新后自动复查**：点完「更新」成功后自动再查一次，弹"已是最新"确认真装上；
  风险告知标志用完重置，下次更新重新走告知。
- **修复"永远显示一致"**：面板跑在全局副本里时 `update_check` 是"自己比自己"，
  永远 outdated=False 且版本号读旧的。改为源==已装目录时读配置
  `lingshu_source_dir`（开发仓库路径）做对比基准。

### 检测/更新目标跟随面板位置（不定死目录）

- **`growth._panel_root()`**：取当前进程实际所在的技能根（growth.py 上一级）。
  面板从哪个目录跑，检测与更新就针对哪份副本——多根目录（APP/Trae）或
  环境变量覆盖的场景不再找错位置。
- **`_find_installed()`**：第一优先返回面板所在根下的同名技能，再轮询全局根目录。
- **`install_skill()` 安装目标跟随**：已装在哪儿就更新哪儿（`_find_installed`
  优先），不再写死主目录——修掉"更新成功了、跑的还是旧副本"的错位。

### 修"面板点更新报 400：没有这个技能：lingshu"

- **`install_skill()` 补灵枢本体特判**：灵枢本体的源不在 `~/.brain-memory/skills/`
  技能区（那是自建技能的家），就在技能包自身目录。此前只有 `update_check()`
  做了这个特判，安装没做——表现为「检查更新明明提示有新版，点更新却 400」。
  现在安装与检查走同一套源解析：自身目录 == 已装目录时，读配置
  `lingshu_source_dir` 指向开发仓库。

### 修"检查更新误报有新版"（双保险）+ 插件 brain.py 探测加环境变量兜底

- **`update_check()` 源==已装且没配 `lingshu_source_dir` 时直接返回
  `outdated: false`**：不再拿副本目录自己比——副本一旦有残缺文件就会误报
  "outdated: true + 全量 changed/removed"（换盘符/非常规安装场景坐实过）。
- **第二层保险：签名有差但版本号相同不算过期**——mtime 抖动/临时文件残留
  不该触发更新弹窗；只有版本号真变了才提示更新。技能页的「有更新」徽标
  （`list_skills`）同步加上这层版本闸门。
- **版本闸门补缺口（双保险 2.0）：忘了升版本号也不会卡死**——纯 mtime 抖动
  仍须版本号变化才提示，但两类"真差异"不依赖版本号就能认定：
  ① 副本缺文件/多文件（残缺，换盘符场景）；
  ② SKILL.md / scripts/ / assets/ 下同名文件大小不同（关键内容真变了）。
  其余零散文件留给版本号闸门。新增 `_real_drift()`，`update_check` 与
  `list_skills` 两处闸门同步升级。
- **早退分支补齐版本字段**：源==已装且无配置的 `outdated: false` 返回里
  补上 `version`/`installed_version`（都读副本 frontmatter）——面板"已是
  最新（vX.Y.Z）"提示不再显示 undefined。
- **启动注入插件 `findBrainPy()` 加 `LINGSHU_SKILL_DIR` 环境变量兜底**：
  灵枢装在非常规位置（换盘符/自定义目录）时，设该环境变量指向技能根目录
  即可被插件找到，不用改 profile 的 `brainPy` 配置。
- **插件优先问 APP 要路径（skills registry）**：探测顺序升级为
  profile `brainPy` → `ctx.skills` registry 的 `list()`（官方文档证实条目带
  SKILL.md 的 location 且可为绝对路径；字段名各版本可能不同，path/location/
  dir/baseDir/directory 逐个试，同步/异步返回都支持）→ `LINGSHU_SKILL_DIR` 环境变量 →
  默认目录。APP 能发现技能，插件就能拿到，不再依赖目录名猜测。

### 六欲自动变动（不再只等手动"满足"）
- **修"六欲一直停在默认 60"**：此前六欲唯一写入口是面板/CLI 的手动"满足"，
  日常对话完全不牵动它们。现在日常事件会自动微调：
  - **七情牵动六欲（`feel()`）**：按情绪大类映射——喜/爱→成就欲、连接欲升；
    怒→掌控欲升（连接欲小降）；哀→连接欲升、成就欲小降；惧→安全欲升；
    欲→求知欲升。幅度按情绪强度缩放，1~3 点上下，微小自然。
  - **记事（`remember()`）**：记东西本身就是表达 → 表达欲 +1；问题/洞见类
    → 求知欲 +2；重要记忆（≥0.7）→ 成就欲 +1.5。
  - **回忆命中（`recall()`）**：真想起点什么，求知欲 +1.5（startup 注入不算）。
  - **daily 日记低谷自我调节**：写日记本身是自我觉察，低谷欲望回升 3 点
    （每项约每天最多一次，dry_run 预览不写盘）。
- **新增 `desire_nudge()`**：六欲自动微调的统一入口。每个欲望带 2 小时冷却
  （节流，避免每次对话都写盘；冷却记录存 `desire_history.jsonl`，限长 200 条，
  不污染 desires.json 结构）；数值 clamp 在 [8,100]；异常静默吞掉，
  绝不拖垮记忆/情绪主流程。`desires.json` 格式不变，CLI `emotion desire`
  与面板"满足"按钮行为完全兼容。

## v2.15 — 面板自更新：一键检查 + 自动弹窗 + 差异明细

（bb35b5c + 55a7e3f 合入）

### 面板本体自更新（概览右上角）

- **「检查更新」按钮 + 开面板自动查**：概览右上角新增按钮，开面板 1.2s 后自动比对
  "本地源 ↔ 全局已装副本"，有更新自动弹窗；也可随时点右上角按钮手动查。
  有更新时按钮旁亮橙色圆点。
- **弹窗显示"更新了什么"**：优先展示人话版更新说明——后端从源的 CHANGELOG.md 自动
  抽取「已装版本 → 最新版本」区间的正文（修复了什么/完善了什么/新增了什么，轻量
  markdown 渲染，先转义防 XSS）；CHANGELOG 拿不到时回退文件清单（changed/added/
  removed），并注明"记忆数据不受影响"；已是最新时弹"已是最新（vX.Y.Z）"。
- **底部「更新 / 关闭」两键**：更新 = force 镜像同步到全局技能目录，成功后圆点熄灭 +
  刷新技能区。首次更新走风险告知二次确认（弹窗展示灵枢能做什么，看过再点一次才带
  `yes` 真正执行）——不让风险确认被静默跳过，也不假成功。
- **后端**：`growth.update_check()` 内容签名比对返回差异明细（灵枢本体源自动回退到
  技能包自身目录，不依赖 `skills/` 技能区）；只读接口 `GET /api/self-update`。
- **代码审查修复三处**：① 更新圆点内联 `display:inline-block` 压过 `.hidden` 导致
  藏不住，改直接操作 `style.display`；② 「更新」不检查 `state`——`risk_required`
  也 toast"已更新"的假成功，改两段式确认 + `failed` 兜底；③ 差异比对 O(n²) 全表扫
  改为集合查表。

## v2.14 — 注入包实测修正（刚刚发生/内驱兜底）+ 面板按钮常显

（f661b96 + 701065f 合入）

### 注入包两处修正

- **「刚刚发生」与「最近的记忆」互斥分段**：两段原来都调 `recent()`，前者是后者的
  子集，记忆少时原样重复一遍。改为按 `last_startup.ts`（`>=`，容忍同秒写入）先切出
  本会话新增记忆进「刚刚发生」，再按 id 从「最近的记忆」剔除——两段互斥不重复。
- **内驱自相矛盾消除**：traits 已有 好奇85/理智78 而 drives 还空时，注入一边写
  「性格：好奇85」一边写「内驱：还没有数值」。`mind_state()` / `persona_brief()` /
  `build_injection()` 三处统一加 traits→drives 兜底。
- **面板更新按钮常显**：原来是 `installed && outdated` 条件渲染，刚重装后源副本一致
  → outdated=False → 按钮永不出现。改为 installed 即显示 ↻，「有更新」徽标仍由
  outdated 驱动。
- 隔离实测：startup→remember→startup 场景（新记忆进「刚刚发生」且不重复）、
  traits/drives 矛盾场景、`install --force` ×2 + `uninstall` 后记忆数据逐文件快照
  对比（config.json 仅新增预期内的 `risk_accepted`）——全部通过。

## v2.13 — 从"自觉"到"机制"：cordis 启动注入插件 + 更新按钮 + 数据完整性证明

（合并 35e0473 起 第五轮/面板轮/第六轮及插件三轮实测修复）

### cordis 启动注入插件（`cordis-plugin/lingshu-startup/`，一个插件一个子目录）

DSH catalog 只把 name+description 给模型，正文读不读靠模型"自觉"，没有任何机制保证。
本插件把"自觉"变成"机制"——每个新会话自动跑灵枢 `startup` 并注入上下文：

- **事件对齐 DSH 真实 API**（三轮实测反馈逐项修正）：监听 serial 事件 **`agent/created`**
  （payload `{ agent, source }`），只对 `startup` / `resume` 注入，`clear` / `compact`
  （同会话上下文重建）跳过；`WeakSet` 按 agent 去重。
- **python 探测容错**：spawn 不解析 PATHEXT（PATH 里只有 python.cmd 的机器
  `spawn('python')` 直接 ENOENT），探测优先 DSH 自带运行时
  `<DSH_HOME|~/.dsh>/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe`，
  落空退回 `ComSpec` 让 cmd 解析 `.cmd`（命令行带 python 命令本身 +
  `windowsVerbatimArguments` 防二次转义）。
- **投递通道实测修正**：`agent.inject()` 固定写 `next-step` 队列，而 `next-step` 在
  `turn/start` 时被清空——注入发生了却被"排错队丢掉"，模型永远看不到（事件日志
  坐实：`agent/inbox/spliced removed=1`）。改用 **`agent.send(msg, 'next-turn',
  false)`** 显式投递到 `next-turn`（step/start 构建 messages 只消费它），
  `wakeup=false` 避免无用户输入时空跑一轮；`source.form: 'instructions'` 标明
  语义，不被后续 runtime-context 快照的"取代"逻辑清掉。
- **绝不拖垮会话**：serial 监听器 reject 会回滚会话创建——整个监听器套顶层 try/catch
  兜底；超时默认 3s（串行事件不宜久等）+ 手动 kill 双保险；配置类失败
  `ctx.logger.warn` 一次（不刷屏），不再静默吞错。
- 失败宽容：灵枢没装/Python 不在/超时全部降级跳过，会话照常开始；只读语义
  （startup 内部仅更新 last_startup），与技能更新解耦（不复制 brain.py）。

### 面板：技能「更新」按钮 + 有更新徽标

- **`list_skills` 新增 `outdated` 检测**：对比本地源目录与全局已装副本的内容签名
  （相对路径+大小+mtime，排除 `__pycache__`/临时文件/安装时重新生成的 AGENTS.md），
  不一致即"有更新"。之前 `scripts/` 改了但 SKILL.md 没变时用户毫无感知。
- **技能卡片**：有更新时显示橙色「有更新」徽标，⇩ 安装按钮变 ↻ **更新按钮**——
  一键 force 镜像同步（源里删掉的文件也会清掉），确认弹窗注明"记忆数据不受影响"。
- **隔离实测 5 项**：未安装/安装后一致/改 scripts 检出有更新/force 更新后一致/
  更新后记忆数据完好——全部通过。

### 数据完整性证明 — 技能更新永不"忘事"

用户核心关切：技能更新/重装/卸载会不会把记忆/人格/笔记/知识库清空。结论（架构保证 +
8 场景 19 项隔离实测证明）：

- **数据/代码双目录物理隔离**：用户数据在 `BRAIN_MEMORY_HOME`（默认 `~/.brain-memory`），
  技能代码在 `~/.trae-cn/skills`（多根），两路径互不相交——测试专门断言。
- **`install --force` 重装 ×3 / `uninstall_skill(managed_only=True)` / 重新 install /
  手动 rmtree 整个技能目录**：每次全量快照（海马/皮层/尘埃/归档/人格/笔记/知识/情感/
  关联的条数 + id 集）逐字段对比，全部相等，零丢失。
- **"删除"类操作全是打标不真删**：forget 只挪 dust 层（`forgotten_at` + detail 减半），
  archive 是 append-only 且 restore 先写回热层再删归档（持双锁），panel 删知识是显式确认操作。
- **写盘原子 + 自愈**：`atomic_json`/`write_jsonl`/`save_all` 均 tmp + `os.replace`；
  坏行注入后 `read_jsonl` 自愈，好行全保留、坏块进 `.corrupt` 备份。
- **加固**：`_replace_retry` 写盘重试 6 → 12 次，缓解 Windows 高频读写下 `os.replace`
  被占用句柄误报（配合读-改-写一律持锁的既有约定，并发压测 100 次零异常）。

### 六项体验修复（第六轮）

- **SKILL.md description 精简**：frontmatter 从约 400 字砍到两句——catalog 占预算且
  模型扫一眼抓不到重点，只留"是什么 + 何时用"。
- **路径摩擦**：SKILL.md 加「路径约定」区块——命令 cwd 不一定是技能目录，
  明确"先 cd 或用绝对路径"两种写法。
- **Windows 编码**：brain.py / growth.py 入口无条件
  `reconfigure(encoding="utf-8", errors="replace")` stdout/stderr（只捕获预期
  `AttributeError`），用户不再需要手动设 `PYTHONIOENCODING`（此前 GBK 控制台中文
  变 鐏垫灑）。
- **高重要性记忆强制待审**：`remember()` 中重要性 ≥0.8（10 分制 ≥8）且来源非用户的
  记忆自动 `review=pending`——约定/承诺/重要决定一旦记错就是长期错误，与做梦产出同级
  对待；CLI 写入时明示"已进待审池"。

## v2.12 — 隐藏问题清剿：锁更稳、快照不回流、坏体有去重

### v2.12 第四轮：入口统一过滤畸形行 + 裸字段假设根除

再查一遍数据安全与面板交互的边角，修了一类"平时不炸、炸了才懂"的问题：

- **写锁只认自己**：`_locked` 锁文件里记录持有者 pid，解锁时只删自己的锁。此前赶上 30s 陈旧锁被另一进程接管后，旧持有者退出会把新持有者的锁删掉，导致第三个进程趁乱一起写盘。旧锁阈值也调到 30s（正常写盘 <2s），避免把还在做梦巩固的进程误判为死锁。
- **recall 强化不再回滚审核结果**：`_strengthen_write` 只 patch 强化字段（访问次数/稳定度/末次访问/细节），不再拿旧快照整条写回。之前面板上刚点了「确认」的记忆，被一次回忆一碰又弹回待审核。
- **`capture --raw-only` 语义修正**：此前 `--raw-only` 反而走完整逐段入库、普通 capture 却只归档不逐段进库，完全反了。现在原文快照无条件归档，`--raw-only` 只留原文、不进记忆库（不污染）。
- **做梦应用必须对号**：`apply_dream` 缺 `task_id` 直接拒绝应用（不再兜底沿用上一场），防止上一场的整理结果静默套到这一场记忆上。
- **人格增量 0 不再当 50**：`update_persona` 做梦微调时 `base = 50 if old is None else int(old)`，此前 `0 or 50` 会把 0 值人格一次性拉到 50。
- **`work_flush` 不自锁**：整体加锁后直接 `write_jsonl` 清空暂存，不再调用会重复抢同一把锁的 `work_clear`（否则多刷几次就"等不到写锁"）。
- **`dream --dry-run` 只读不抢锁**：只读目录下预览不再因建锁失败报错。
- **AGENTS.md 区块自愈**：有 `<!-- lingshu:start -->` 没 `<end>` 时补闭合、用户手写保留，不再叠出两段重复区块；扩展模块 `_ext_path` 认得下划线文件名（`flow_helper.py`），不再"列表里有、一跑就报没有"。
- **面板**：非法 JSON 请求体返回 400（此前静默当空操作）；`FileExistsError` 归入 409；只读模式下 daily 预览、自我总结这两个"只读按钮"放行；面板卸载加白名单（只能卸灵枢装过的技能）；安装灵枢时先弹风险告知再装；删除失败会提示而不是假成功；记忆搜索防抖 + 丢弃乱序旧响应。
- **数据落盘**：`save_all` 三文件先全写临时文件再一次替换（把"存一半"窗口压到最小）；`.corrupt` 坏块备份去重（修不动时不再每读一次就追加一遍）；`cluster_memories` 聚类池上限 400，防止记忆库变大后每晚巩固被 O(n²) 卡住。

### v2.12 第四轮：入口统一过滤畸形行 + 裸字段假设根除

第三轮数值防御之后，入口函数仍放行畸形行，所有调用方（brain.py 24 处 + growth.py 23 处）各自防御成本太高且漏防。本轮从公共入口统一拦住，所有调用方自动受益：

- **入口统一过滤**（5 个入口一次到位，后续零维护）：
  - `brain.load_all()` / `load_archive()` — 缺 `id` 或不是 dict 的行直接跳过，log_event 记条数
  - `brain.load_assoc()` — 缺 `src` / `dst` 的边跳过
  - `growth.load_knowledge()` / `load_notes()` — 缺 `id` 的条目跳过
  - 项目里散落的 47+ 处裸 `m["id"]` / `r["id"]` / `r["src"]` 全部因此变成安全的。
- **真问题补丁**（第三轮数值防御没覆盖到的裸字段）：
  - `review_report` — 缺 `id` 的 `review=pending` 畸形记录不再 KeyError（之前 panel 的"待审核"页面能直接崩）
  - `_strengthen_write`（记忆强化）— `touched` 集合里的畸形记录不再踩空
  - `assoc_of` / `assoc_auto` — 缺 `id` 的记忆、缺 `src/dst` 的关联边一律跳过
  - `link_memory` 校验 id 集、`assoc_auto` 实体桶、`assoc_of` 的 `by_id` 构建
- **growth.tidy_knowledge 整体加锁**：之前只在开头锁了 load_knowledge，后面的并查集构建、pending.json 写入都在锁外。现在整个函数体包进 KNOWLEDGE_FILE 锁（pending.json 天然不互相覆盖）；入口过滤畸形条目后 `r["id"]` 安全。
- **growth.create_skill / edit_skill** 的 registry 写：已在第三轮改成"合并式 save_registry"（持锁 load 再 update），但 SKILL.md 文件本身的 write_text 仍无锁——两个并发 write 是原子替换窗口极小，暂不处理（会在技能目录加一把新锁，对单技能语义过重）。

### v2.12 第三轮：脏数据不再把整条链路拉崩 + 归档/恢复原子化

从审计里挖出的都是"手改/旧版本/外部工具写了一条 null 字段"就让整条链路崩掉的问题——注入/面板/daily 一个都跑不了：

- **数值防御全域覆盖**：情绪链路（`emotion_events`/`mood_now`/`emotion_arc`）、欲望（`desire_state`）、日常（`daily`/`display_text`/`forget`）、展示（`display_text`/`recent`/`list_memories`）、成长素材（`growth.digest`）里所有裸 `float(x.get(...))` / `>= floor` 比较全部换成 `_num(...)`。键存在但值为 null/字符串时不再 TypeError——"脏行跳过、好行继续"。
- **归档层防御**：`cluster_memories` 过滤缺 id 记录（并查集映射不再 KeyError）；`_is_archivable` 首行直接拒绝缺 id 记录（归档候选/梦巩固的 dict 推导不再崩）。
- **归档/恢复原子化**：`archive restore` 顺序反过来——先 `save_all(hot)` 成功再从 archive.jsonl 删除，中途失败 hit 始终留在 archive；`cmd_restore`（zip 恢复）解压到临时目录 → 整包 `os.replace` 原子替换；全程持 hippocampus.jsonl + persona.json 锁，与面板并发不再互相踩写；前缀比较换成 `is_relative_to()` 防 zip-slip。
- **growth.digest 数值防御**：最近 N 天筛选里 `created`/`updated`/`ts` 用 `brain._num`，脏数据不崩。
- **面板异常分类**：`do_POST` 的 except 分支加入 TypeError/AttributeError（此前客户端传错类型会进 500，现在统一 400 带消息）。
- **面板死配置清理**：`_WRITE_ENDPOINTS` 里删去 `/api/config`、`/api/sync`、`/api/user-rules`、`/api/work`、`/api/assoc`——它们在 do_POST 里根本没有处理分支。
- **面板 workspace_errors 脱敏**：异常原文带本机绝对路径，现在只回 `{label, type}`，完整栈进 log_event。
- **面板并发请求乱序丢弃**：`loadWS` / `load` 各加一个自增序号，旧响应不会再把新快照覆盖掉。
- **面板事件委托 find() 判 undefined**：记忆置顶/编辑、知识置顶/编辑、笔记编辑前先判 `undefined`，"数据已变化"时 toast 提醒而不是抛 TypeError。
- **面板只读按钮恢复**：daily/dream handler 里用 `finally { btn.disabled = !!(S.ov && S.ov.readonly) }`，面板出错一次也不会把只读禁用状态冲掉。
- **面板技能安装错误分类**：`install catch` 里先正则识别"已存在/冲突"再弹覆盖确认，403 readonly / 文件占用 / "没这个技能"不再被误导成覆盖提示。

### v2.12 第二轮：并发写不再互相吞、面板不再假成功

再往下一层查并发与面板交互，补的都是"平时看不出来、真撞上就丢数据"的点：

- **写锁支持同线程重入**：`_locked` 发现本线程已持同一把锁时直接进入，不再傻等 10s 后超时。此前给"读-改-写"补锁时，`update_persona → save_persona` 这类嵌套会把自己锁死。
- **人格读-改-写整体加锁**：`update_persona` / `set_persona_field` / `persona_rollback` / `edit_persona` / `persona_interview` / `persona_self_author` / `mind_set` / `mind_tune` / `persona_restore` / `save_emotion_cfg` 与面板 `persona/state`、CLI `persona lock/unlock` 统一走 `persona.json` 写锁。此前两个改动各自 load 旧档，后写者会把先写者改的字段覆盖回旧值。
- **联想边与配置/欲望加锁**：`link_memory` / `unlink_memory`（`assoc.jsonl`）、`config_set` / `config_unset`（`config.json`）、`desire_satisfy`（`desires.json`）都改成锁内读-改-写，权重累加与配置项不再丢更新。
- **`apply_dream` 原子化**：pending 的读取、task_id 校验、记忆写入与消费现在都在同一把锁里。并发/重复 apply 第二进锁就会因 pending 已删而拒绝，不会把同一场梦沉淀两遍；`memories` 非对象、`importance` 非数字、`source_ids` 非字符串一律防御性跳过；梦境笔记写失败不再把已成功的应用报成错误。
- **`add_knowledge` / `write_note` / `update_note` / `delete_note` 加锁**：并发写入不再各自用旧快照覆盖，相似知识合并与同日日记追加都能稳定命中。
- **`skill install --force` 镜像式更新**：源里已删除的文件会从全局副本一并清掉（AGENTS.md 除外），不再留着旧脚本/旧资源被宿主继续扫描加载；清理动作记入运行日志。
- **面板 `workspace()` 防御化**：任一块数据读取失败只降级成空值并记日志，不再让"写已经成功"的接口整体报 400 诱发重复写。
- **面板空请求体不再触发真实写**：`/api/daily` 真跑与 `/api/dream` 都必须显式带 `confirm: true`（前端已同步），空 body / 误触 / 重放不会直接整理或做梦。
- **面板参数校验**：`/api/knowledge` 的 `points/tags/entities`、`/api/note` 的 `tags/memory_ids` 必须是数组，传字符串会被逐字拆碎的问题被挡住。
- **只读模式按 action 放行**：`/api/skill` 的 list/check/show、`/api/emotion` 的 now/check 在只读目录下不再被整端点 403。
- **`/api/skill` 补上 `list`**：该端点默认 action 就是 `list`，但此前没有对应分支，不带 action 的请求直接 400「未知操作」。
- **面板前端**：技能版本号、知识版本号、情绪分组名全部 `esc()` 转义（存储型 XSS）；淡忘、知识废弃/删除、笔记删除都会检查 `ok`，不再"假成功"；慢连接 30s 超时 + `daemon_threads`，坏客户端挂不住面板进程。

## v2.11.4 — 更新不丢东西：重置前先留档、安装源防呆、删目录前核路径

围绕"更新会不会把人格/记忆/技能弄丢"做的加固，都是"少一步手滑、多一层兜底"：

- **`init --force` 覆盖人格前自动留档**：此前直接写默认档案，人格、初识、演化历史当场没了。现在先复制一份 `persona.json.pre-init-<时间戳>`，并打印备份路径。
- **`user-rules clear` 清空前自动留档**：长期规则（如"不要动 DSH 数据目录"）清空后不再不可逆，先存 `lingshu.user.md.bak-<时间戳>`。
- **`skill install --from` 防呆**：三种会出事的指法直接拒绝——① 源就是目标目录（或在它里面，会把自己反复复制进去）；② 源是记忆数据目录 `~/.brain-memory` 或它的上级（会把整个记忆库塞进技能目录）；③ 源文件数 >3000 或体积 >200MB（多半指错目录了，防爆盘）。
- **`skill uninstall` / `skill rm` 删前核路径**：新增 `_safe_rmtree()`，只删「根目录的直接子目录」，绝不删根本身或根外的路径（防手滑、也防将来放宽名字规则后出事）。
- **面板删技能记日志**：面板上卸载/删除技能会写一条 `lingshu.log`，事后能查。
- **安装中文名提示**：装名字含中文的技能时提示"DSH 只认 kebab-case，可能不显示"。
- **文档**：更新流程补"覆盖前先 `backup`"，并明确列出更新后仍然保留的东西（人格、记忆、知识、笔记、自建技能 `skills/`、扩展 `extensions/`、`user-rules`、`assoc`、`dreams/`、日志）。

## v2.11.3 — 认出 DSH 的技能目录：装上就生效，卸载连旧中文目录一起清

v2.11.2 把名字改成 kebab-case，但发现两个更隐蔽的问题，本版本一并解决：

- **全局技能目录原来一直指错地方**：`skill install` 一直写到 `~/.trae-cn/skills`（Trae 的技能目录），而灵枢本体主要跑在 **DSH** 上，DSH 扫的是 `~/.dsh/skills`——这就是"装了不出现 / 更新了没生效"的又一来源。现在按优先级自动识别：有 `~/.dsh/skills` 就装到那，否则回落 `~/.trae-cn/skills`；两个目录都会扫描、都会清理。仍嫌不对可用环境变量 `LINGSHU_GLOBAL_SKILLS=<目录>` 硬指定。
- **卸载/安装自动收拾旧中文目录**：`skill uninstall lingshu` 现在会把**新名目录 + 旧版中文目录 `灵枢/`** 一起删（多全局目录都清），此前只删新名目录，旧空壳仍在会被 DSH 扫到而跳过；`skill install` 检测到旧目录、或另一个全局目录里还有同名副本时，打印提示与清理命令。
- **`skill list --global` 显示根目录**：列出 `~/.dsh/skills` / `~/.trae-cn/skills` 各自的扫描结果，旧中文目录仍标 `✗` 并附收拾提示。
- 注意：**更新技能本身不会改名/换目录**——更新只覆盖仓库文件，机器上的全局副本要重装一次（`skill uninstall lingshu` → `skill install lingshu --from <仓库> --yes`）。

## v2.11.2 — 名字改 kebab-case：DSH 终于认得出、bat 也不再乱码

用户反馈：装完技能在 DSH 里不出现（"改名"）、`start-panel.bat` 一跑满屏乱码命令报错。根因两个，都修：

- **frontmatter `name` 从中文改 kebab-case**：`name: 灵枢` → `name: lingshu`（另两者 `lingshu-lite` / `lingshu-tiny`）。DSH 的文件系统技能发现只认 kebab-case 名字，中文名会被静默跳过——这就是"装完像改名/消失"的原因。中文名保留到新增的 `title: 灵枢` 字段作展示用。
- **安装器跟随**：`_require_slug` 增加中文别名折算（`灵枢`→`lingshu`），装/查/删仍可用中文名或英文名，目录与 frontmatter 统一写 kebab；本体识别、全局副本路径、风险确认判定同时认新旧两个名字。
- **新增 `skill list --global`**：列出全局技能目录里已装的技能，并核对「目录名 == name(kebab)」，旧版中文目录会被标 `✗`，一眼看出为什么不出现（以前命令只能列自己写的技能，装了啥无从自查）。
- **修复 `skill list` 遇语义化版本号崩溃**：`int('2.11.2')` 直接 ValueError 整列表崩掉，改为容错（自建技能整数版本照常，semver 原样展示）。
- **`start-panel.bat` 重写**：原文件是 UTF-8 + LF 换行，cmd 按 GBK 解析产生乱码并把 `if` 语句拆成裸命令执行。改为**纯 ASCII + CRLF**，任何代码页下解析都稳定，面板启动提示改用英文。

## v2.11.1 — 稳定性加固：坏行自愈不再与并发写打架

v2.11 后的自查加固，不改功能只堵隐患：

- **坏行自愈的并发覆盖隐患**（本次唯一实质风险）：`read_jsonl` 的坏块自愈（好记录写回、坏块挪 `.corrupt`）此前是**无锁整文件替换**——若自愈恰与另一个进程的写盘并发，可能把对方刚落盘的新数据覆盖丢。现在自愈先看**本进程是否已持该文件写锁**（持锁直接修），否则快速抢锁（0.5s），抢不到就这轮跳过；坏块此时已进 `.corrupt` 留证、原文件未动，下次读取再修。
- `_locked()` 增加 `timeout` 参数（默认仍 10s），供上述快速抢锁使用。
- 面板 `overview` 的 pending 改用 `brain.load_dream_pending()`，去掉与 CLI 并行的第二套解析逻辑，避免两处漂移。
- `_log_summary` 注释与实现对齐（读最近 200 条）。
- `restore` 确认语改为"写回合并式恢复，备份之后新增的文件不被删除"，与真实行为一致（此前写"覆盖现有数据"，易误解）。

## v2.11 — 判定透明 + 面板批量操作：为什么切模式、批量审核、网页改梦草稿、运行日志都看得见

六个"用起来差一口气"的地方，本版本补齐：

**auto 话题判定升级为打分制 + 依据透出**

- 不再"命中任一关键词就归 work"：`_WORK_SIGNALS`（编程/数据/工程三组 26 词）与 `_HUMAN_SIGNALS`（情绪/关系/寒暄三组 30 词）分组信号词，`analyze_topic()` 统计**命中数打分**，`work_score >= human_score` 工程优先（写代码时情绪隔离更安全）。
- 面板概览新增「会话模式判定」卡：显示当前模式、置信度、工程/情绪两侧分数、命中词依据；可输入一句话**试判**，看 auto 会怎么切、为什么切。
- CLI 新增 `mode explain "一句话话题"`：打印判定结果、置信度、工程信号命中、情绪信号命中、mixed 依据。

**面板批量操作**

- 记忆页新增批量条：复选框多选（含全选）后，一次**批量 confirm / discard 待审核条目**、**批量设重要性**、**批量加/减标签**。
- 底层 `bulk_memory_action()` 单次写锁内完成全部更新（一次读-改-写整库），N 条不再逐条反复覆写文件。

**dream 梦草稿网页编辑**

- 梦页在 apply 区新增草稿态：pending 草稿会自动落盘为 `dreams/apply_draft.json`，面板可直接**网页编辑草稿 JSON** 再 apply，不再必须命令行改文件。
- 草稿按 `task_id` 与当前 pending 匹配才回填，防旧草稿套新任务；apply 成功自动清草稿。
- 新增 `save_dream_draft/load_dream_draft/clear_dream_draft`（面板 `/api/dream/draft`）。

**情绪-输出一致性告警**

- 新增后置轻量校验 `emotion_consistency_check()`：对回复文本做正/负词极性估计（感叹号放大），当内部情绪强度足够、回复语气与内部数值**严重背离**时给出告警。
- 告警落盘 `emotion_checks.jsonl`（滚动保留 200 条）；面板心情页新增「情绪-输出一致性核对」卡（输入回复试查 + 最近告警列表）。
- CLI 新增 `emotion check "回复文本"`：打印内部情绪、回复极性、是否告警、建议。

**运行日志（面板可看）**

- 新增轻量运行日志 `lingshu.log`（JSONL，>512KB 滚动保留后半）：锁冲突、内部异常、解析报错等打点（read_jsonl 坏块、write_jsonl 失败、`_locked` 超时、load_loose_json 失败、面板接口异常、CLI 崩溃兜底）。
- 日志函数**绝不抛异常、不进写锁**——日志本身不当崩溃源。
- 面板新增「日志」页：按级别（warn/error/全部）过滤、刷新查看；概览显示近期 warn/error 计数。
- CLI 新增 `logs [--limit N] [--level warn|error] [--all]`。

**记忆关联（assoc）补齐教程与手动建关联**

- 面板联想页新增「手动建立关联」卡：输入 A/B 记忆 id、关系名、为什么，一键 link。
- README 新增「记忆关联（联想图谱）」教程章节：从建记忆 → 手动 link → assoc auto 自动补边 → 面板 3D 图查看，带完整示例。

**文档同步**

- 版本号 2.10→2.11（SKILL / lite / tiny / README）；README 更新模式判定、面板、命令速查；docs/commands.md 同步新命令与新面板功能；本 CHANGELOG 新增本条。

## v2.10 — 冷热归档 + 万条性能：回忆不再随记忆增长变慢

记忆上万条后，逐个 JSONL 全量 read + 正则分词的 recall/remember 会线性变慢；且冷数据只能靠手动 `forget` / `backup` 处理。本版本引入**冷热归档层** + **候选剪枝预筛**，让"记忆越多越卡"变成"记忆越多越有条理"。

**冷热归档（新的 archive 层）**

- 新增第 4 层 `archive`（`archive.jsonl`）：把**沉眠记忆**（保留度 < 0.20、重要度 ≤ 0.5、创建满 30 天、从未被想起或静置超 180 天，且非置顶/封存/身份/待审核）雪藏出热查询集。与 `dust`（淡忘：细节被模糊）不同，归档**原文一字不丢**、永不删，`recall` 默认不查、需要时一键找回。
- 自动触发：`dream` 每次做梦时顺带执行（本来就全量遍历，零额外成本），在淡忘（dust）判断**之前**，让"久到想不起的"先雪藏而不是被模糊掉。
- 手动命令：`archive run`（立即归档，`--dry-run` 预览）/ `archive show` / `archive search <词>` / `archive restore <id>`（回热到 cortex）/ `archive count`。
- `recall --include-archive` 连归档层一起模糊回忆；`audit` / `stats` / 面板层筛选与统计都认识归档层。
- 归档记忆被 recall 命中时只读不强化，防止悄悄改写入库文件。

**recall / remember 候选剪枝（性能主解）**

- 带查询词的 `recall` 先做**廉价子串包含预筛**（term 必然是其来源文本的连续子串，数学上不丢召回），只对命中候选做正则分词 + IDF + 打分；`remember` 的近重复检测同样先预筛再算 jaccard。
- 无查询词（startup 注入）不再构建全量 terms/idf（relevance 恒为 0），只跑轻量打分。
- recall 强化写回从"整库 merge + 全量重写 3 文件"改为**只重写命中记忆所在层**（`_strengthen_write`）。
- 实测（1 万条记忆）：`recall` 3.5s → **76ms（约 47 倍）**；`recall` 无查询词 3.5s → **68ms**；`remember` ∈ 100ms。

**并发稳定性根治（Windows read-replace 竞态）**

- 顺带修掉一个 v2.9 就存在的隐性竞态：`remember` 此前**锁外先 `load_all()` 读、锁内 `save_all` 原子替换**，并发写的另一个进程覆盖"正在被读"的文件会在 Windows 上抛 `PermissionError`（rc=2，偶发丢条目）。现在 `remember` 的读-改-写**整体入锁**。
- `write_jsonl` 的 `os.replace` 加短暂重试：其他无锁读进程（stats/面板）短暂持有句柄时不再一次失败。
- 实测：16 remember + dream + forget + archive run **混合并发**，全部成功、记忆数不丢、无锁残留；24 并发 remember 全通过。

**文档同步**

- 版本号 2.9→2.10（SKILL / lite / tiny / README）；README 增加 `archive` 命令表与性能说明；本 CHANGELOG 新增本条。

## v2.9 — 第三轮审查：数据安全与一致性修复

对全部脚本与面板做第三轮只读审查 + 实测回归，按"真影响使用"修复残留问题：

**人格数据安全**

- **`persona rollback` 不再误删整段**：此前回滚 `from=None` 的增项（锁定、喜好/习惯、style/values、state）会整个字段被删光——回滚一条锁把全部锁清空、回滚一条喜好把整份清单删掉。现在只撤那一条；无旧值可还原的结构段（state）保守不动；"之后又改过"的覆盖警告只对真正的更新发出，不再对列表增项误报。
- **`persona set` 裸根防呆**：`persona set identity=abc` 这类不带子字段的值会把整段结构覆盖成字符串、导致注入与面板崩溃；现在非列表根字段必须写成 `根.子字段`，非法输入直接报用法。
- **初识不再卡死**：用户完整回答名字+性格后，AI 补喜好/习惯即可完成初识（此前 `onboarding` 永远停在 awaiting_self，注入包每会话都催"初识还没完成"）。

**配置与注入健壮性**

- **配置数值校验**：`config set budget_tokens/review_threshold/panel_port` 等数值项现在做类型与范围校验，坏值直接拒绝；注入侧读取全部改走容错解析——即便 config.json 被手改成坏值，`startup`/`inject`/`check-context` 也只回退默认值，不再崩掉或被误报成"未初始化"。

**备份 / 同步**

- **备份自包含修复**：`backup --out` 指向记忆库自身时不再把正在写的 zip 打包进自己（此前 Windows 上直接失败）。
- **DSH 同步 `--force` 只覆盖当天块**：改用 `rfind` 定位当天同步块，不再把同文件里历史块一起删掉。

**成长子系统（growth.py）**

- **只读门控修正**：`learn ask/loop`（纯读）只读时不再被误挡；`skill use` 仅 `--auto`（要写盘安装）时才算写命令。
- **知识/笔记字段容错**：`_kdoc`、`knowledge add` 合并、`apply_tidy` 合并、`search_notes` 对 `null` 字段（LLM/手改产出）统一转字符串，不再裸 TypeError 崩溃。
- **只读不碰索引**：`growth status` 等读取命令不再往磁盘写知识索引（答应"只读=能读不能写"）。
- **`note list --days 0`** 现在指"今天零点起"，不再恒为空。
- **`skill edit/borrow` 名字归一化**：大写/空格的名字不再导致 frontmatter 名字漂移、installed 误判、或退化成 hash 名。
- **`apply_tidy` 删除计数**：改为真实从库消失的条数（含合并吃掉的那 X-1 条）；`log_evolution` 补写锁；`self-review` 只读时不谎报"已刷新"。

**面板**

- **GET 接口兜底**：任一接口（如坏编码的梦日志、类型异常的记忆字段）不再断连无响应，统一回 400/500 与通用文案（与 POST 对齐）。
- **只读时前端禁用写按钮**：横幅之外，写按钮全部置灰（tab 切换与检索/注入预览保留），不再"能点但全部失败"。
- **情绪 top 列表补转义**（与同块 peak/end 对齐）；`/api/emotion` 未知 action 明确 400 不再假成功；`tags/entities` 非数组传参直接 400 防污染；token 比较改 `hmac.compare_digest`。

**并发写锁补齐（记忆整库覆写路径）**

- 此前 `forget` / `dream` / `dream --apply` / `work add|done|clear` / `review confirm|discard` 以及面板 `/api/memory` 编辑，都是「先无锁读全量 → 改 → `save_all` 整库覆写」，锁只包了最后一步写入，两个进程并发时会用各自的旧快照互相覆盖、丢更新。现在把这些路径的**读-改-写整体**放进 `_locked(hippocampus.jsonl)`（`work_*` 用 `WORKING_FILE` 锁）；`set_memory_review` 同步纳入。
- 实测：24 个进程并发 `remember` 后记忆数精确 24（无丢失）；6 个 `forget` + 2 个 `dream --auto` 并发混合写入，无 `TimeoutError`、无残留 `.lock`，数据分层与计数一致。

**文档同步**

- 版本号 2.8→2.9（SKILL / lite / tiny / README）；本 CHANGELOG 新增本条。

## v2.8 — 第二轮深度审查：安全漏洞 + 人格灵魂机制 + 逻辑数据一致性

沿用「真影响使用才改」的原则，三路并行审查（安全 / 人格灵魂 / 逻辑一致性）后修复：

**安全**

- **存储型 XSS 修复**：面板记忆卡片的 `kind` / `layer` 未经转义直接进 HTML；现在统一 `esc()` 输出、`cssCls()` 清洗 class；`/api/memory` 增加 `kind` 白名单（只允许标准七类）+ `importance` 强制 clamp 到 0-1。
- **备份/恢复加固**：`restore` 增加防 zip 炸弹限额（条目 ≤200、解压后 ≤512MB），并对"不像灵枢备份"提前拒绝；`backup` 自动跳过敏感文件（`.env`、`*.token/*.key/*.pem`、`id_rsa*`、含 secret/credential 的文件名）。
- **面板安装收敛**：`skill install` 从面板调用时忽略 `from_dir`（只有本地 CLI 能指定外部目录），防远程/恶意页面乱指路径。
- **面板响应加固**：统一加 `X-Content-Type-Options: nosniff` + `Referrer-Policy` + CSP（同源资源、放开内联脚本样式）；`_origin_ok` 支持 IPv6 Host（`[::1]:8765`）；负 `Content-Length` 视为空体；未捕获异常不再回显本机路径（日志留全栈，响应给通用文案）。
- **跨进程写锁补齐**：`persona.json` 保存、知识的 `save_knowledge`、笔记的 `save_notes` 全部加锁（此前只有记忆文件有锁，并发时可能互相覆盖）。

**人格灵魂**

- **初识不再空手打勾**：`persona self` 名字和性格都空着时不再标记初识完成；AI 自写的溯源从 `source=自我总结` 改为 `source=初识`，漂移统计不误报。
- **锁定区覆盖 self/interview**：`persona self` / `persona interview` 写名字、自称、性格前都查 `_is_locked`，整组锁（`traits`）与子键锁都生效。
- **AI 变更判定改看 source**：`ai_persona_changes` 优先按 `source` 字段判定（dream/初识），不再只靠 `why` 字符串猜，面板编辑/用户回答不再误报为 AI 私改。
- **persona set 加白名单**：根路径只认 identity/traits/drives/state/style/values/background/likes/dislikes/relationship；列表字段强制转 list（此前 `set likes=xxx` 会把 list 打成 str，后续注入崩溃）。
- **快照克制**：快照保留上限 40 张自动修剪；`restore` 支持按 label/名字片段模糊匹配，且恢复时合并当前历史（此前恢复会把之后的审计轨迹整个丢掉）；`drift` 基准跳过"restore前自动备份"、`state.updated` 不再计入漂移。
- **回滚显式覆盖**：`persona rollback` 加锁检查（锁定字段拒绝回滚），返回值带 `overwrote_later`，CLI 明确提示"之后的改动已被一并回滚"。
- **traits 语义修正**：更新性格时，做梦整理 = 增量微调（±），面板/CLI = 绝对值；都 clamp 到 0-100。

**逻辑数据一致性**

- **`dream --apply` 校验任务号**：apply.json 的 `task_id` 必须和 `pending.json` 一致才应用（防止把上一场/乱写的结果落到现在的记忆上），不一致直接拒绝，不再无脑删 pending。
- **JSONL 读写重构**：支持跨行格式化 JSON 块（按括号配对识别，坏块整块进 `.corrupt`）；写入改临时文件原子替换（pid+纳秒文件名，不再互踩 `.tmp`）。
- **缺失字段容错**：`dream` / `cluster_memories` / `build_injection` / `_print_mem` 对缺 `created`/`kind`/`importance` 的记录不再崩溃。
- **日常一致性**：`daily --force` 不再把内容重复追加进同一天笔记（`append_same_day=not force`）；梦报告同秒重复生成时自动加序号不覆盖。
- **知识整理**：`tidy_knowledge` 过滤已废弃条目，不再把 discarded 复活进整理清单；`list_notes` 支持 `days=0` 语义。
- **修一个 `check-context` 崩溃**：该命令里局部变量 `dream` 遮蔽了全局 `dream()` 函数，导致 `brain.py dream` 命令直接 UnboundLocalError——改为 `dream_pct`。

**文档同步**

- 版本号 2.7→2.8（SKILL / lite / tiny / README）；本 CHANGELOG 新增本条。

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