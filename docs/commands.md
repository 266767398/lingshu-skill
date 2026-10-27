# 灵枢 · 命令速查与排障（按需查阅）

> 这是 [SKILL.md](../SKILL.md) 的完整命令表 + 故障排查。主文件只留高频命令索引；**不确定某命令的确切参数时，用 Read 读这里。**

## 完整命令速查

所有命令前缀认准 `python scripts/brain.py`（Windows 双击 `start-panel.bat` 只启动面板）。

| 目的 | 命令 |
|---|---|
| 初始化 | `init` |
| 会话开始钩子 | `startup --query "关键词"`（返回码 0/10/2/64） |
| 注入记忆 | `inject "关键词" [--budget 1400]` |
| 只读降级 | `--ro inject "关键词"`（或 `BRAIN_MEMORY_READONLY=1`） |
| 换数据目录 | `--home <目录> ...` / `home move <目录>` / `home verify` |
| 备份 / 恢复 | `backup [--out 目录] [--keep 20]` / `restore <zip> [--yes]` |
| 工作记忆 | `work [status\|list]`（看一眼）→ `work add "..."` → `work done <序号>` / `work flush [--skip-done]` / `work clear` |
| 写入 | `remember "内容" --kind fact --importance 7 --tags a,b` |
| 回忆 | `recall "关键词" --k 8` |
| 遇到不会的 | `learn ask "问题"` / `learn loop` |
| 做梦 | `dream` / `dream --apply apply.json` / `dream --auto [--dry-run]` |
| 与 DSH 同步 | `sync dsh [--dir 目录] [--days 1] [--force] [--dry-run]` |
| 上下文水位 | `check-context --used 68`（60 清点 / 75 做梦 / 90 封存，可加 `--unsaved 3000`） |
| 原样封存 | `capture "<原文>" [--tag 逃生] [--raw-only]` |
| 人格 | `persona [show\|sync\|path\|set traits.幽默=70\|lock 名字\|unlock 名字]` |
| 人格历史/回滚 | `persona history [--limit N]` → 看来源与依据 → `persona rollback <序号>` |
| 人格快照/漂移 | `persona snapshot --label "基准"` / `persona snapshots` / `persona restore <名字>` / `persona drift [--days 7] [--json]` |
| 人格编辑 | `persona set traits.幽默=70`（逐字段改，写进历史可回滚） |
| 初识 | `persona interview [--name ... --traits ...\|--unsure]` → `persona self [--name ... --traits ... --likes ... --dislikes ... --habits ...]` |
| 理智/好奇 | `mind [state\|weigh "事" --benefit 7 --cost 3\|tune --curiosity +5 --reason -3 --why "..."]` |
| 会话模式 | `mode show\|work\|human\|auto`（工程会话自动隔离情绪） |
| 审核闸 / 自查 | `review [--json]` → `review confirm <id>` / `review discard <id>`（记忆 m_* 与知识 k_* 通用）；`audit [--json]` |
| 心情（七情六欲） | `emotion [now\|arc\|list\|feel 情绪名 --intensity 0.7 --why "..."\|desire 欲望名 --amount 20]` |
| 情绪事件/微调 | `emotion events`（带序号）→ `emotion adjust <序号> [--intensity 0.3] [--emotion-name 委屈] [--why "..."]` / `emotion del <序号>` / `emotion cool` |
| 事件→情绪（认知评价） | `emotion evaluate "事件" [--polarity auto\|positive\|negative\|neutral] [--intensity 0.8]` |
| 共情 / 情感设置 | `emotion mirror "用户情绪" [--intensity 0.7]` / `emotion config [--empathy mirror\|mild\|off] [--gain 0.5] [--max-intensity 0.8] [--suppress "怒,恶"] [--notes "..."]` |
| 记忆关联 | `assoc [link <idA> <idB> --relation 因果 --weight 1\|unlink <idA> <idB>\|of <id>\|auto [--dry-run]\|graph --limit 300]` |
| 每日例行 | `daily [--dry-run] [--force]`（写日记 + 沉淀知识 + 过期的梦自动做掉） |
| 自我总结（每2-3天） | `skill self-review [--force] [--days 3]` |
| 知识库 | `knowledge add\|list\|search\|show\|pin\|rm` |
| 整理知识 | `knowledge tidy` → 填 apply.json → `knowledge tidy --apply apply.json` |
| 知识审核 | `knowledge review` / `knowledge confirm <id>` / `knowledge discard <id>` |
| 笔记 | `note write "标题" --type journal\|log\|growth --body ...` / `note list [--type ...]` / `note show <id>` |
| 笔记检索 / 素材 | `note search "词"` / `note digest --days 7` |
| 技能 | `skill new\|edit\|list\|show\|check\|install\|uninstall\|rm\|find\|use\|borrow` |
| 技能安装 | `skill install <名字> [--force] [--from 外部目录]`（整目录 + 自动生成 AGENTS.md；装灵枢本体需先过风险确认 `--yes`） |
| 技能自动全局化 | `skill use <名字> --auto` |
| DSH 插件（只读） | `skill plugins [--json]` |
| 用户附加规则 | `user-rules show\|add <规则>\|clear`（数据目录 lingshu.user.md，更新永不覆盖，每会话自动注入） |
| 扩展模块 | `ext new\|edit\|show\|list\|run\|check\|rm`（可执行钩子，数据目录，更新永不覆盖） |
| 成长 | `growth status` / `growth review --days 7` |
| 进化日志 | `evolve log "标题" --kind persona\|skill\|knowledge\|thinking\|self` / `evolve list` |
| 统计 | `stats` |
| 列表 / 忘记 | `list` / `forget <id>` |
| 配置 | `config show` / `config set <key> <value>` / `config unset <key>` |
| 记忆冲突检查 | `detect-conflicts [--json] [--limit 50]` |
| 可视化面板 | `serve` → http://127.0.0.1:8765 |

## 可视化面板

```bash
python scripts/brain.py serve
```

浏览器打开 http://127.0.0.1:8765 ：记忆时间线与保留率、人格档案与好奇心/理智、**此刻理智状态（被情绪压住多少）**、知识库、笔记日志、技能自编辑、成长指标与复盘素材、**每日例行与自我总结**、进化轨迹、手动做梦与梦报告。仅监听本机。v2.3 起侧栏还显示**会话模式**（可点击切换）、**待审核项计数**、**人格漂移告警**；记忆列表里的待审核条目可直接 ✓/✗ 确认或废弃。

新增两块：

- **心情页**：七情占比、六欲、"事件 → 情绪"认知评价（输入一句话，看它怎么判断、冒出什么情绪、理智被压多少、大事是否交给你）、共情试一下、情感设置（共情等级/比例/上限/压下去的情绪，随时改）。
- **联想页（3D 空间）**：每条记忆一个节点、连线是关联，拖拽旋转、滚轮缩放、点节点看详情与邻居；"自动联想"一键按共享实体/标签补边。

Windows 可双击 `<本技能目录>/start-panel.bat` 启动。

## 故障排查 FAQ

**Q：模型总是忘走 `startup`，怎么办？**
把「每会话固定动作」第一条抄到宿主（Claude Code / Cursor / OpenCode / Trae）的项目级规则或开场指令里，让宿主每次会话开始时先强制跑一次。`startup` 能把"跑没跑"变成返回码，但"跑不跑"取决于宿主与模型。小模型环境直接改用 `SKILL-tiny.md`（只留硬约束）。

**Q：说了 `remember` 却没写进去？**
先看输出是不是只读降级（返回码 10 / 提示只读）——数据目录不可写时脚本不会写。再用 `stats` 对总数；正常情况不会丢（原子写 + 跨进程锁 + 损坏行自愈），写不进多半是 `--home` 指错了目录或沙箱拒写。

**Q：`startup` 返回码 64？**
命令写错了：`startup` 只接受 `--query "..."`，不接位置参数。见返回码表。

**Q：回忆后记忆层数变少？**
不应该。`recall` 的强化只写回被"想起"的那条，不碰其它层（淡忘区被清空的 bug 已修复并有回归验证）。若异常先 `stats` 对比各层数量。

**Q：情绪曲线和实际感觉对不上？**
"此刻心情"是按强度 × 半衰期（默认 6h）叠加出来的，峰值过了自然回落；要看每天的真实最高点用 `emotion arc`（峰终口径）。方向反了，多半是 `emotion feel` 的情绪名选错了——`emotion list` 看全表。判错还能 `emotion adjust <序号>` 微调，不用整条删。

**Q：哪些场景别用灵枢？**
纯一次性聊天、只想要临时算力不需要记忆、宿主不支持执行脚本、用户明确只想要"记一句话"但不想建库——这些场合灵枢是负担，直接不用。