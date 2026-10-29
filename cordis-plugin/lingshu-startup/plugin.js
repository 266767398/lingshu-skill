// 灵枢 · DSH/Cordis 启动注入插件
//
// 为什么需要它：DSH 的技能 catalog 只把 name+description 给模型，正文要不要读
// 全凭模型自觉。本插件把"自觉"变成"机制"——每个会话启动（startup/resume）
// 时自动跑灵枢的 `brain.py startup`，把人格、记忆、待办、行为指引直接注入
// 会话上下文，模型想忘都忘不掉。
//
// 数据安全：本插件只读记忆库（startup 内部只读+写 last_startup），不碰
// 技能目录；brain.py 自身保证只读降级（HOME 不可写时照样注入）。

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import Schema from '@deepseek-ai/schemastery'

export const name = 'dsh-lingshu-startup'

export const Config = Schema.object({
  brainPy: Schema.string().default('')
    .description('brain.py 绝对路径；留空自动探测 ~/.dsh/skills/lingshu、~/.trae-cn/skills/lingshu'),
  python: Schema.string().default('python').description('Python 解释器'),
  budget: Schema.number().default(1400).description('startup 注入 token 预算'),
  timeoutMs: Schema.number().default(8000).description('startup 子进程超时（毫秒）'),
  injectResume: Schema.boolean().default(true).description('resume 来源的会话是否也注入'),
})

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

/** 跑 brain.py startup，返回注入文本；失败返回 null（绝不阻塞会话）。 */
function runStartup(python, brainPy, budget, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false
    const done = (v) => { if (!settled) { settled = true; resolve(v) } }
    let child
    try {
      child = spawn(python, [brainPy, 'startup', '--budget', String(budget)], {
        encoding: 'utf8',
        windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
        timeout: timeoutMs, // Node>=15 自带超时，超时触发 error/kill
      })
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
  ctx.on('agent/session-start', async (payload) => {
    const { agent, source } = payload || {}
    if (!agent) return
    if (source === 'resume' && !config.injectResume) return
    const brainPy = findBrainPy(config.brainPy)
    if (!brainPy) return // 没装灵枢：静默跳过，本插件无副作用
    const text = await runStartup(config.python, brainPy, config.budget, config.timeoutMs)
    if (!text) return
    try {
      // agent.inject：把外部消息种进会话上下文（不产生用户可见的回复轮）
      await agent.inject?.(text)
    } catch (e) {
      ctx.logger?.('lingshu')?.warn?.('startup 注入失败', e)
    }
  })
}
