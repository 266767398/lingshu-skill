# dsh-lingshu-startup · 灵枢启动注入插件

把"模型自觉读 SKILL.md"变成"机制自动注入"：每个 DSH 新会话（`agent/created`，
source ∈ startup / resume）时自动运行灵枢的 `brain.py startup`，把人格、记忆、
工作记忆、行为指引以 UserMessage 形态 `agent.inject()` 进会话上下文。

## 为什么要有这个插件

DSH 的技能 catalog 只把 name + description 给模型，正文要不要读完全凭模型自觉。
没有任何机制保证下一次会话的模型一定会加载灵枢。这个插件补上最后一环：
**不依赖模型自觉，会话一开始记忆就在上下文里。**

## 目录约定

`cordis-plugin/` 下**一个插件一个子目录**——`lingshu-startup/` 就是本插件，
后续新增插件（如定时做梦、每日摘要）各自建子目录，互不干扰，方便单独
`pnpm pack` 与单独装进 profile。

## 安装（本机）

```
# 1) 确认灵枢已全局化（~/.dsh/skills/lingshu/scripts/brain.py 存在）
# 2) 把本插件子目录打包安装进 DSH profile：
cd cordis-plugin/lingshu-startup
pnpm pack          # 得到 dsh-lingshu-startup-0.1.0.tgz
# 在 DSH 插件市场/配置里安装该 tgz（或按 DSH 文档把包放进 profile 的 node_modules
# 并在 profile package.json 的 dsh.profile.bundles 里加 "dsh-lingshu-startup"）
```

## ⚠️ 开发调试别用 `link:` 安装（踩过的坑）

`link:` 安装会把模块的真实路径留在技能目录，Node 从那里向上找 `node_modules`
一路到盘符根——找不到 DSH 内置包，`import` 失败 → cordis **静默跳过整个插件**
（UI 里装了、bundles 里有、配置树里也有，就是不动）。实体 tgz 安装（如上）的
解析基准在 profile 下，没这个问题。

本插件因此**刻意零外部依赖**：不 import `@deepseek-ai/schemastery`，配置用普通
对象 `DEFAULTS` + `mergeConfig` 合并（默认值 budget 1400 / timeoutMs 3000 /
injectResume true）。这样 link 安装也能工作、改 `plugin.js` 即时生效，不必每次
重打包——只是别忘了 Host 仍不会热加载 bundle（见下节），改完还是要重启 DSH。

排错技巧：desktop profile 被 Electron 独占，无法直连时，可用
`dsh --profile probe` 开临时 profile 把静默失败变成 stderr 显式错误；
`--dump-config` 能看到插件在配置树里 → 配置没问题 → 问题必然在 import。

## ⚠️ 装完必须重启 DSH（Host 不会热加载 bundle）

**Host 的插件树（cordis profile bundles）是主进程启动时一次性组合的**——装完/改完
插件不重启，新开的会话跑的还是旧 Host，它压根不知道有这个插件。实测踩坑：
12:11 启动 Host，12:22 装插件，之后所有新会话都只有 skill catalog 摘要、没有注入。

对照记忆（为什么 skill 能热更新而插件不能）：

| | 生效时机 | 原因 |
|---|---|---|
| skill catalog | **实时热更新** | skill-filesystem 用 chokidar 监视技能目录，运行时感知增删 |
| cordis 插件 | **必须重启 Host** | bundle 组合在主进程启动时一次性完成 |

（`dshmarket` 不出现在设置里也是同一个原因——它是 bundle，不是 skill。）

### 装完后的验证清单

1. **完全退出并重启 DSH**（主进程重启，不是只关窗口/新开会话）。
2. 设置里能看见 `dsh-lingshu-startup`（看不见 = bundle 没挂上：检查 profile
   `package.json` 的 `dsh.profile.bundles` 是否含 `dsh-lingshu-startup`，以及
   node_modules 里包是否存在）。
3. 开一个新会话，随便说句话——上下文里应出现灵枢 startup 注入的人格/记忆段。
4. 没注入时按下面排错（插件日志会 warn 一次，不刷屏）：
   - `未找到 brain.py` → 灵枢没全局化，先 `growth.py install lingshu`；
   - `startup 未产出注入文本` → python 问题：本机 PATH 只有 python.cmd shim
     （如 ServBay）时 cmd 兜底也救不了，确认 DSH 自带运行时
     `~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe`
     存在（探测优先命中它），或在配置里手填 `python`（exe 全路径）；
   - 都没有 warn 也没注入 → 插件没挂上，回到第 2 步。

## 配置项

| 项 | 默认 | 说明 |
|---|---|---|
| brainPy | 自动探测 | brain.py 绝对路径；留空则依次找 `~/.dsh/skills/lingshu`、`~/.trae-cn/skills/lingshu` |
| python | 自动探测 | **python.exe 绝对路径**。spawn 不解析 PATHEXT（PATH 里只有 python.cmd 的机器上 `spawn('python')` 直接 ENOENT），留空则优先探测 DSH 自带运行时 `<DSH_HOME>/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe`，都没找到退回 `ComSpec` 让 cmd 解析 `.cmd` |
| budget | `1400` | startup 注入 token 预算 |
| timeoutMs | `3000` | 子进程超时；超时/失败**静默放行**，绝不阻塞会话（`agent/created` 是串行事件，默认值压到 3s 免拖慢新会话） |
| injectResume | `true` | 续聊（resume）的会话是否也注入；`clear` / `compact` 一律不注入（同会话上下文重建，重复注入浪费 token） |

## 数据安全

- 只读语义：`brain.py startup` 本身只读记忆库（仅更新 last_startup 时间戳）；
  HOME 不可写时 brain.py 自动只读降级照样注入。
- 失败宽容：灵枢没装、Python 不在、超时——降级跳过会话照常开始；配置类失败会
  `ctx.logger.warn` 打一次日志（不重复刷屏），不静默吞错。监听器有顶层兜底，
  任何异常都不冒泡（serial `agent/created` 失败会回滚会话创建，绝不允许）。
- 注入身份：消息 `source` 用 `runtime-context`，不冒充用户说的话。
- 与技能更新解耦：本插件不复制 brain.py，永远调用全局技能目录里的最新版。
