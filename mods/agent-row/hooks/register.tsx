// cc-pills 的 subagent 面板，写成 mod 的 AgentRow 渲染点（subagentStatusLine 已弃用）。
//
// 一行 = 描述 + 模型 / effort / tok（与实测增速）/ ctx 剩余 / 时长 + 非运行态的状态标签。
// 数据全部来自引擎的 AgentInfo，不再解析 transcript、不再有共享状态与锁。
//
// 宽度按显示宽度算（1、2 字节 UTF-8 字符各占 1 列，3/4 字节占 2 列，已知按 1 列显示的
// 3 字节符号除外）——与 statusline-command.sh 的 dw() 同一模型。`columns` 由面板实测。
//
// 速率不用 tokenSamples：文档只说「约五秒一次」，换算成每秒要假设间隔，违背「不显示
// 推算值」的原则。改为本模块自己记上次的（时刻, tokenCount），两次实测求差。

import type { Register } from 'claude-code'

const NARROW = new Set([0x2026, 0x2191, 0x2193, 0x21ba, 0x21bb, 0x21e1, 0x21e3])

function cw(cp: number): number {
  if (NARROW.has(cp)) return 1
  return cp > 0x7ff ? 2 : 1
}

export function dw(s: string): number {
  let n = 0
  for (const ch of s) n += cw(ch.codePointAt(0) as number)
  return n
}

export function cut(s: string, n: number): string {
  if (dw(s) <= n) return s
  if (n < 2) return ''
  let out = ''
  let w = 0
  for (const ch of s) {
    const c = cw(ch.codePointAt(0) as number)
    if (w + c > n - 1) break
    out += ch
    w += c
  }
  return out + '…'
}

// 与 statusline-tokens.py 的 fmt() 同口径：999.5k 起按 M，999.5M 起按 B，三位数 M 不留小数
export function kfmt(n: number): string {
  if (n >= 999_500_000 || Math.round((n / 1e6) * 10) / 10 >= 999.5) {
    const v = Math.round((n / 1e9) * 10) / 10
    return Number.isInteger(v) ? `${v}B` : `${v.toFixed(1)}B`
  }
  if (n >= 999_500) {
    const v = Math.round((n / 1e6) * 10) / 10
    if (v >= 100) return `${Math.round(v)}M`
    return Number.isInteger(v) ? `${v}M` : `${v.toFixed(1)}M`
  }
  if (n >= 1000) return `${Math.round(n / 1000)}k`
  return `${Math.round(n)}`
}

export function dfmt(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
}

// 非数字、负数、NaN 一律按「没有」——字段缺失就整段隐藏（与主栏同一原则）
function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null
}

function oneLine(v: unknown): string {
  return typeof v === 'string' ? v.replace(/\s+/g, ' ').trim() : ''
}

const LIVE = new Set(['pending', 'running', 'waiting'])

export const register: Register = on => {
  // 每行的速率基线：上次渲染的（时刻, tokenCount）。模块变量随重载清零，最多丢一帧速率。
  const seen = new Map<string, { t: number; k: number }>()

  on('ui.render', { component: 'AgentRow' }, async ($, e, next) => {
    const p = e.props
    const now = await $.clock.now()
    const st = oneLine(p.status)
    const live = st === '' || LIVE.has(st)

    const tokens = num(p.tokenCount)
    const windowSize = num(p.contextWindowSize)

    // ── 速率：两次实测求差（间隔 ≥1 s、增量非负、≥1 tok/s 才显示）──
    const id = typeof p.id === 'string' && p.id !== '' ? p.id : null
    let rate: number | null = null
    if (id !== null) {
      if (!live) {
        seen.delete(id)
      } else if (tokens !== null) {
        const prev = seen.get(id)
        if (prev && now - prev.t >= 1000 && tokens - prev.k >= 0) {
          rate = (tokens - prev.k) / ((now - prev.t) / 1000)
        }
        if (!prev || now - prev.t >= 2000) {
          seen.set(id, { t: now, k: tokens })
          if (seen.size > 256) {
            for (const [k, v] of seen) {
              if (now - v.t > 600_000) seen.delete(k)
            }
          }
        }
      }
    }

    const parts: string[] = []

    const model = oneLine(p.model).replace(/^claude-/, '').replace(/\[1m\]$/, '')
    if (model) parts.push(model)

    const effort = p.effort === undefined || p.effort === null ? '' : oneLine(String(p.effort))
    if (effort) parts.push(effort)

    // token 为 0 = 还没读到第一次用量，不显示「tok 0」
    if (tokens !== null && tokens > 0) {
      let s = `tok ${kfmt(tokens)}`
      if (rate !== null && rate >= 1) {
        s += rate >= 1000 ? ` ↑${(Math.floor(rate / 100) / 10).toFixed(1)}k/s` : ` ↑${Math.round(rate)}/s`
      }
      parts.push(s)
    }

    if (tokens !== null && windowSize !== null && windowSize > 0) {
      const left = Math.max(0, Math.min(100, Math.floor((1 - tokens / windowSize) * 100)))
      parts.push(left < 20 ? `ctx 剩${left}%⚠` : `ctx 剩${left}%`)
    }

    const started = num(p.startTime)
    if (live && started !== null && started > 1e12) {
      parts.push(dfmt((now - started) / 1000))
    }

    const tail = parts.join(' · ')
    // 与 bash 面板同口径：只有 running / 空状态不加标签，pending 也要标出来（它是排队，不是运行）
    const tag = st !== '' && st !== 'running' ? ` [${st}]` : ''

    // ── 描述按剩余显示宽度截断 ──
    const desc = oneLine(p.label) || oneLine(p.description)
    const rawCols = num(p.columns)
    const cols = rawCols !== null && rawCols > 0 ? rawCols : 120
    const sepTail = tail ? `  — ${tail}` : ''
    const desc2 = desc ? cut(desc, cols - dw(sepTail) - dw(tag)) : ''
    // 描述放不下时不留一个孤零零的分隔符
    const body = (desc2 ? desc2 + sepTail : tail) + tag

    // body 为空串等于「隐藏该行」：什么都没有可显示时原样放行，让引擎画自己的行
    if (!body) return next(e)

    // label 交给正文：否则引擎会在行首再画一遍同名摘要
    return next({ ...e, props: { ...p, body, label: '' } })
  })
}
