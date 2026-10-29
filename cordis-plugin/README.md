# dsh-lingshu-startup · 灵枢启动注入插件

把"模型自觉读 SKILL.md"变成"机制自动注入"：每个 DSH 会话启动（`agent/session-start`，
startup / resume）时自动运行灵枢的 `brain.py startup`，把人格、记忆、工作记忆、
行为指引直接 `agent.inject()` 进会话上下文。

## 为什么要有这个插件

DSH 的技能 catalog 只把 name + description 给模型，正文要不要读完全凭模型自觉。
没有任何机制保证下一次会话的模型一定会加载灵枢。这个插件补上最后一环：
**不依赖模型自觉，会话一开始记忆就在上下文里。**

## 安装（本机）

```
# 1) 确认灵枢已全局化（~/.dsh/skills/lingshu/scripts/brain.py 存在）
# 2) 把 cordis-plugin 目录安装进 DSH profile：
cd cordis-plugin
pnpm pack          # 得到 dsh-lingshu-startup-0.1.0.tgz
# 在 DSH 插件市场/配置里安装该 tgz（或按 DSH 文档把包放进 profile 的 node_modules
# 并在 profile package.json 的 dsh.profile.bundles 里加 "dsh-lingshu-startup"）
```

## 配置项

| 项 | 默认 | 说明 |
|---|---|---|
| brainPy | 自动探测 | brain.py 绝对路径；留空则依次找 `~/.dsh/skills/lingshu`、`~/.trae-cn/skills/lingshu` |
| python | `python` | Python 解释器 |
| budget | `1400` | startup 注入 token 预算 |
| timeoutMs | `8000` | 子进程超时；超时/失败**静默放行**，绝不阻塞会话 |
| injectResume | `true` | 续聊（resume）的会话是否也注入 |

## 数据安全

- 只读语义：`brain.py startup` 本身只读记忆库（仅更新 last_startup 时间戳）；
  HOME 不可写时 brain.py 自动只读降级照样注入。
- 失败宽容：灵枢没装、Python 不在、超时——全部静默跳过，会话照常开始。
- 与技能更新解耦：本插件不复制 brain.py，永远调用全局技能目录里的最新版。
