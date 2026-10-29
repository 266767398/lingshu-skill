// 注意：不要在这里 re-export 不存在的名字（如已移除的 Config）——ESM 命名导入
// 在模块链接期就会 SyntaxError，整个插件加载失败且被 cordis 静默吞掉。
// plugin.js 刻意零外部依赖后不再导出 Config（配置走 DEFAULTS + mergeConfig）。
export { name, apply } from './plugin.js'
