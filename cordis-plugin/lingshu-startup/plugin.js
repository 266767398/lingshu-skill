// 灵枢 · DSH/Cordis 启动注入插件
//
// 为什么需要它：DSH 的技能 catalog 只把 name+description 给模型，正文要不要读
// 全凭模型自觉。本插件把"自觉"变成"机制"——每个新会话（agent/created，
// source ∈ startup/resume）时自动跑灵枢的 `brain.py startup`，把人格、记忆、
// 待办、行为指引注入会话上下文，模型想忘都忘不掉。
//
// 事件与注入（对齐 DSH 真实 API）：
// - 监听 `agent/created`（serial 事件，监听器会被 await，失败会回滚会话创建——
//   所以本插件任何路径都不允许抛异常），payload 为
//   { agent, source: 'startup'|'resume'|'clear'|'compact', signal? }。
//   只对 startup/resume 注入；clear/compact 是同会话重建上下文，不重复注入。
// - inject 载荷是 UserMessage：{ id, role:'user', content: TextBlock[], source }，
//   不是裸字符串也不是 { role, content: string }。source 用 'runtime-context'，
//   不冒充用户说的话。
//
// 数据安全：本插件只读记忆库（startup 内部只读+写 last_startup），不碰
// 技能目录；brain.py 自身保证只读降级（HOME 不可写时照样注入）。

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'

// 刻意零外部依赖：不 import @deepseek-ai/schemastery。原因——link: 安装时模块
// 真实路径留在技能目录，Node 从那里向上找 node_modules 一路到盘符根都找不到
// DSH 内置包，import 失败 → cordis 静默跳过整个插件（UI 里装了却不动）。
// 去掉依赖后 link 安装也能工作，改 plugin.js 即时生效，不必每次重打包。

export const name = 'dsh-lingshu-startup'

// 默认配置（纯普通对象，零依赖）。DSH 对导出 Config 的插件会喂用户配置，
// 不导出 Config 时喂默认空对象——两种情况本插件都能工作。
const DEFAULTS = {
  brainPy: '',     // brain.py 绝对路径；留空自动探测 ~/.dsh/skills/lingshu、~/.trae-cn/skills/lingshu
  python: '',      // python.exe 绝对路径；留空自动探测 DSH 自带运行时等位置（spawn 不解析 PATHEXT）
  budget: 1400,    // startup 注入 token 预算
  timeoutMs: 3000, // startup 子进程超时；agent/created 是串行事件，超时太久拖慢新会话
  injectResume: true, // resume 来源（续聊）是否也注入
}

/** 合并用户配置与默认值（用户值非空/存在才覆盖） */
function mergeConfig(user) {
  const merged = { ...DEFAULTS }
  if (user && typeof user === 'object') {
    for (const k of Object.keys(DEFAULTS)) {
      const v = user[k]
      if (v !== undefined && v !== null && v !== '') merged[k] = v
    }
  }
  return merged
}

/** 依次探测常见安装位置的 brain.py */
function findBrainPy(configured) {
  if (configured) return existsSync(configured) ? configured : null
  const home = homedir()
  for (const dir of [
    join(home, '.dsh', 'skills', 'lingshu'),
    join(home, '.trae-cn', 'skills', 'lingshu'),
  ]) {
    const p = join(dir, 'scripts', 'brain.py')
    if (existsSync(p)) return p
  }
  return null
}

/**
 * 探测可用的 python.exe。spawn 走 CreateProcess，不解析 PATHEXT——
 * PATH 里只有 python.cmd 的机器上 spawn('python') 直接 ENOENT。
 * 优先级：用户配置 > DSH 自带运行时（确定的 exe）> 常见安装位置 > 系统常见位置。
 * 都没命中返回 null（由调用方走 cmd 兜底）。
 */
function findPythonExe(configured) {
  if (configured) return existsSync(configured) ? configured : null
  const home = homedir()
  const candidates = []
  // DSH 自带运行时：DSH_HOME 环境变量优先，其次 ~/.dsh
  const dshHomes = [process.env.DSH_HOME, join(home, '.dsh')].filter(Boolean)
  for (const dsh of dshHomes) {
    candidates.push(
      join(dsh, 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
      join(dsh, 'runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
    )
  }
  // Python 官方安装器与其他常见落点
  candidates.push(
    join(home, 'AppData', 'Local', 'Programs', 'Python', 'python.exe'),
    'C:\\Python313\\python.exe',
    'C:\\Python312\\python.exe',
    'C:\\Program Files\\Python313\\python.exe',
  )
  for (const p of candidates) {
    if (existsSync(p)) return p
  }
  return null
}

/**
 * 跑 brain.py startup，返回注入文本；失败返回 null（绝不抛异常、绝不阻塞会话）。
 * 找不到 python.exe 时退回 ComSpec 让 cmd 解析 PATHEXT（python.cmd 也能跑）。
 */
function runStartup(pythonExe, pythonCmdFallback, brainPy, budget, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false
    const done = (v) => { if (!settled) { settled = true; resolve(v) } }
    const args = [brainPy, 'startup', '--budget', String(budget)]
    let child
    try {
      if (pythonExe) {
        child = spawn(pythonExe, args, {
          encoding: 'utf8',
          windowsHide: true,
          env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
          timeout: timeoutMs, // Node>=15 自带超时，超时触发 error/kill
        })
      } else {
        // 兜底：让 cmd 去解析 .cmd（/d 关 autorun，/s 规范引号解析，/c 执行后退出）。
        // 注意命令行必须带上 python 命令本身（pythonExe 没找到时用裸 'python'，
        // 由 cmd 按 PATHEXT 解析成 python.cmd）——只拼 brain.py 会依赖 .py 文件关联。
        const comspec = process.env.ComSpec ?? 'cmd.exe'
        const pythonCmd = pythonCmdFallback || 'python'
        const cmdline = [`"${pythonCmd}"`, ...args.map((a) => `"${a}"`)].join(' ')
        child = spawn(comspec, ['/d', '/s', '/c', cmdline], {
          encoding: 'utf8',
          windowsHide: true,
          windowsVerbatimArguments: true, // 命令行已手工加引号，阻止 Node 再转义一层
          env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
          timeout: timeoutMs,
        })
      }
    } catch {
      return done(null)
    }
    let out = ''
    child.stdout?.on('data', (d) => { out += d.toString('utf8') })
    child.on('error', () => done(null))
    child.on('close', (code) => {
      // 返回码 0=正常注入；10=只读降级注入（照样有效）；2=未初始化（跳过）
      done(code === 0 || code === 10 ? out.trim() : null)
    })
    // 双保险：timeout 选项个别版本不生效时手动兜底
    setTimeout(() => { try { child.kill() } catch {} ; done(null) }, timeoutMs + 500)
      .unref?.()
  })
}

export function apply(ctx, config) {
  const cfg = mergeConfig(config)
  // 进程内缓存：同一 agent 不重复跑 startup
  const injected = new WeakSet()
  // 配置类失败只告警一次：静默吞掉配置错误会让插件"看似在工作、实际没注入"
  let warnedOnce = false

  ctx.on('agent/created', async (payload) => {
    try { // 顶层兜底：serial 监听器 reject 会回滚会话创建，这里绝不允许冒泡
      const { agent, source } = payload || {}
      if (!agent) return
      // 白名单：只有新开会话（startup）与续聊（resume）注入；
      // clear / compact 是同会话上下文重建，重复注入只会浪费 token。
      if (source !== 'startup' && source !== 'resume') return
      if (source === 'resume' && !cfg.injectResume) return
      if (injected.has(agent)) return
      injected.add(agent)

      const brainPy = findBrainPy(cfg.brainPy)
      if (!brainPy) {
        if (!warnedOnce) { warnedOnce = true; try { ctx.logger?.warn?.('lingshu-startup: 未找到 brain.py（未安装灵枢？），跳过注入') } catch {} }
        return
      }
      const pythonExe = findPythonExe(cfg.python)
      const text = await runStartup(pythonExe, cfg.python, brainPy, cfg.budget, cfg.timeoutMs)
      if (!text) {
        if (!warnedOnce) { warnedOnce = true; try { ctx.logger?.warn?.('lingshu-startup: brain.py startup 未产出注入文本（python 不在/超时/未初始化），跳过注入') } catch {} }
        return
      }
      try {
        // UserMessage 完整形状：content 是 TextBlock[]，source 用 runtime-context
        //（不冒充用户说的话）；MessageId 用随机 UUID。
        await agent.inject?.({
          id: randomUUID(),
          role: 'user',
          content: [{ type: 'text', text }],
          source: { kind: 'runtime-context' },
        })
      } catch (e) {
        try { ctx.logger?.warn?.('lingshu-startup: 注入失败', e) } catch {}
      }
    } catch (e) {
      // 任何意外都不能让监听器 reject（agent/created 失败会回滚会话创建）
      try { ctx.logger?.warn?.('lingshu-startup: 未捕获异常', e) } catch {}
    }
  })
}
