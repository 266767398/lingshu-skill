// 灵枢 · DSH/Cordis 启动注入插件
//
// 为什么需要它：DSH 的技能 catalog 只把 name+description 给模型，正文要不要读
// 全凭模型自觉。本插件把"自觉"变成"机制"——每个新会话（agent/created，
// source ∈ startup/resume）时自动跑灵枢的 `brain.py startup`，把人格、记忆、
// 待办、行为指引注入会话上下文，模型想忘都忘不掉。
//
// 事件与注入（对齐 DSH 真实 API）：
// - 监听 `agent/created`（serial 事件，监听器会被 await），payload 为
//   { agent, source: 'startup'|'resume'|'clear'|'compact', signal? }。
//   只对 startup/resume 注入；clear/compact 是同会话重建上下文，不重复注入。
// - 注入走 UserMessage 形态（agent.inject 签名是 inject(message: UserMessage)），
//   不是裸字符串。
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
  timeoutMs: Schema.number().default(3000).description('startup 子进程超时（毫秒）；agent/created 是串行事件，超时太久会拖慢新会话可用时间'),
  injectResume: Schema.boolean().default(true).description('resume 来源（续聊）是否也注入'),
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
  // 进程内缓存：同一 agent 不重复跑 startup（clear/compact 重建 agent 时缓存自然失效，
  // 因为那两种 source 本来就不在白名单里；同一 agent 的监听器只会触发一次）
  const injected = new WeakSet()

  ctx.on('agent/created', async (payload) => {
    const { agent, source } = payload || {}
    if (!agent) return
    // 白名单：只有新开会话（startup）与续聊（resume）注入；
    // clear / compact 是同会话上下文重建，重复注入只会浪费 token。
    if (source !== 'startup' && source !== 'resume') return
    if (source === 'resume' && !config.injectResume) return
    if (injected.has(agent)) return
    injected.add(agent)

    const brainPy = findBrainPy(config.brainPy)
    if (!brainPy) return // 没装灵枢：静默跳过，本插件无副作用
    const text = await runStartup(config.python, brainPy, config.budget, config.timeoutMs)
    if (!text) return
    try {
      // DSH 签名：inject(message: UserMessage)——UserMessage 是 { role: 'user', content }
      // 的对象，不是裸字符串。塞裸字符串轻则被吞、重则写成畸形消息。
      await agent.inject?.({ role: 'user', content: text })
    } catch (e) {
      ctx.logger?.('lingshu')?.warn?.('startup 注入失败', e)
    }
  })
}
