# 灵枢 · 模型适配与更新（按需查阅）

> 这是 [SKILL.md](../SKILL.md) 的补充手册：模型分级、降档策略、能力边界、如何更新技能本体。

## 能力边界（先读再看）

灵枢是「提示词 + 脚本」的机制，不是能强制约束模型的框架。脚本层能兜底的部分（只读降级、损坏行自愈、退出码）都做了，但 **"该记没记、该查没查、该走 startup" 只能靠模型自觉**。效果上限取决于底层模型：中强模型（7B+、云端）能完整遵守；3B 以下的本地小模型会漏读条款、跳过命令、忘记纪律——这是模型能力问题，不是灵枢坏了。

## 模型分级与降档

你的能力决定哪些功能能开。**记不清就按小模型处理，宁可少做、别做错。**

- **7B+ / 云端**：完整执行。做梦合并、情绪评估、知识提炼都能用。
- **1.5B–3B**：把 `config.json` 里 `model_tier` 设为 `small`（`config set model_tier small`）。此后 `startup` 注入包会追加「小模型模式」约束：
  - 不自动 `dream --apply`（合并错误 = 幻觉记忆永久落盘）；
  - 不自动 `knowledge tidy`（提炼产物须先 `knowledge review` 人工确认）；
  - 情绪评估改用手动 `emotion feel`，别替用户"打分"。
- **记不住规则**：改用 `SKILL-lite.md`（精简版）或 `SKILL-tiny.md`（三件套试用版）。

需要模型产 JSON 的入口（`dream --apply`、`knowledge tidy --apply`）都有容错解析：去掉说明文字 / ```json 围栏后还能读；真读不出来会明确报错并返回退出码 2，不会静默当没发生。

## 幻觉防护：三道闸

模型整理记忆/知识/人格时可能编造，灵枢用三道闸防止幻觉逐层沉淀：

1. **审核闸**：AI 整理出来的记忆标 `review="pending"`、知识标 `status="pending"`；`review` 查看 → `review confirm <id>` 转正 / `review discard <id>` 废弃（记忆 `m_*` 与知识 `k_*` 通用）。
2. **溯源**：AI 发起的人格变更带 `source`（dream/自我总结/…）与 `evidence`（依据的记忆/事件 id），`persona history` 可查。
3. **自查**：`audit` 一次看完待审计数 + 冲突记忆 + 该做什么；`detect-conflicts` 扫矛盾记忆。

每个会话的注入包会主动提醒 `[待审核]` 与 `[人格漂移]`，不让幻觉静默沉淀。

## 更新灵枢技能

用户说「**更新灵枢技能 / 升级灵枢 / 更新一下**」时，照下面做。更新只动**技能本体文件**，不动记忆数据——数据在 `~/.brain-memory/`，跟技能目录是两回事，覆盖技能不会丢任何记忆。

1. **看当前版本**：读 `<本技能目录>/SKILL.md` frontmatter 的 `version`。
2. **拉最新代码**（第一次 clone，之后每次 pull）：
   ```bash
   DIR="$HOME/.cache/灵枢-skill"
   if [ -d "$DIR/.git" ]; then git -C "$DIR" pull; else git clone --depth 1 https://gitee.com/htmcwy_0/lingshu-skill.git "$DIR"; fi
   ```
   主仓库：<https://gitee.com/htmcwy_0/lingshu-skill>　镜像：<https://github.com/266767398/lingshu-skill>
3. **对照版本**：读 `"$DIR"/SKILL.md` 的 `version` 和 `"$DIR"/CHANGELOG.md`。与本地一样 → 告诉用户"已是最新版本"，结束；更高 → 继续。
4. **覆盖更新**：把克隆目录里的这些文件/目录复制到 `<本技能目录>`（覆盖同名）：
   `SKILL.md`、`SKILL-lite.md`、`SKILL-tiny.md`、`README.md`、`CHANGELOG.md`、`docs/`、`assets/`、`scripts/`、`start-panel.bat`
   （Windows 上等价：把克隆目录里的这些项复制粘贴到技能目录，选"覆盖"。）
   覆盖前建议先备份一次数据目录（几秒，心理踏实）：`python "<本技能目录>/scripts/brain.py" backup`，产物在 `~/brain-backups/`。
   注意：**你的自定义内容不会被覆盖**——长期规则写在数据目录 `~/.brain-memory/lingshu.user.md`（更新只动技能目录，永远不碰它）；**你自己写的技能**在 `~/.brain-memory/skills/`、**扩展模块**在 `~/.brain-memory/extensions/`，都在数据目录里，更新技能目录不会碰。全局 `AGENTS.md` 里 `<!-- lingshu:start -->` … `<!-- lingshu:end -->` 区块之外的手写部分，更新时原样保留。
5. **自检**：`python "<本技能目录>/scripts/brain.py" stats` 能正常输出统计 = 数据没坏。
6. **汇报**：对照 `CHANGELOG.md` 把本次新增的关键点讲给用户听，并明确说"记忆数据没动，还是原来的"。

> 没有 git 的环境：让用户打开 gitee 仓库页面下载 ZIP，解压后手动覆盖同名文件，效果一样。