import { expect, test } from 'claude-code/testing'
import { kfmt } from './register.tsx'

// AgentRow 的输入是 AgentInfo 再加 rowIds / columns / body / isHidden。
function row(over: Record<string, unknown> = {}) {
  return {
    plugin: 'cc-pills-agent-row',
    component: 'AgentRow',
    requestId: 'a1',
    props: {
      id: 'a1',
      description: '审查这个模块的并发写与残锁恢复逻辑',
      type: 'Explore',
      status: 'running',
      taskStatus: 'running',
      tokenCount: 250_000,
      contextWindowSize: 1_000_000,
      model: 'claude-opus-5-5[1m]',
      effort: 'high',
      startTime: Date.now() - 125_000,
      rowIds: ['a1'],
      columns: 120,
      isHidden: false,
      ...over,
    },
  } as const
}

// 引擎那一层在测试里由下面这个钩子代替：它把 mod 写进行内容的 body 原样画成一格文字。
// 返回的是整个 body 字符串，断言按显示宽度看的就是它。
function harness($: any, on: any) {
  // on() 只能在第一次用到 $ 之前注册：一次注册，多次挂载
  on('clock.now', () => ({ value: Date.now() }))
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)

    return Text({ children: String(e.props.body ?? '') })
  })

  return async (over: Record<string, unknown> = {}) => {
    const ui = await $.ui.mount({ ...row(over), surface: 'terminal' })
    const text = (await ui.find({ type: 'Text' }))?.text ?? ''
    await ui.unmount()

    return text
  }
}

test('a running row carries model, tokens, context left and duration', async ($, on) => {
  const text = await harness($, on)()

  expect(text).toContain('opus-5-5 · high · tok 250k')
  expect(text).toContain('ctx 剩75%')
  expect(text).toMatch(/2m0\ds/)
  expect(text).not.toContain('[running]')
})

test('exactly twenty percent remaining stays outside the warning band', async ($, on) => {
  const text = await harness($, on)({ tokenCount: 800_000, contextWindowSize: 1_000_000 })
  expect(text).toContain('ctx 剩20%')
  expect(text).not.toContain('⚠')
})

test('a finished row shows its status and no duration', async ($, on) => {
  const text = await harness($, on)({ status: 'completed' })

  expect(text).toContain('[completed]')
  expect(text).not.toMatch(/\dm\d\ds/)
})

test('a Chinese description is cut to the measured columns', async ($, on) => {
  const body = harness($, on)
  const wide = await body()
  expect(wide).toContain('审查这个模块的并发写')

  // 70 列放不下整段描述，必须截断且带省略号
  const narrow = await body({ columns: 70 })
  expect(narrow).toContain('…')
  expect(narrow).not.toContain('审查这个模块的并发写与残锁恢复逻辑')

  // 40 列连描述都放不下：只剩行尾，且不以孤零零的分隔符开头
  const cramped = await body({ columns: 40 })
  expect(cramped.startsWith('opus-5-5')).toBe(true)
})

test('a task with no readings yet hides tok and context', async ($, on) => {
  const text = await harness($, on)({ tokenCount: 0, contextWindowSize: undefined })

  expect(text).not.toContain('tok 0')
  expect(text).not.toContain('ctx')
})

test('a missing columns falls back to 120', async ($, on) => {
  const text = await harness($, on)({ columns: undefined })

  expect(text).toContain('审查这个模块的并发写')
})

test('a pending row is tagged, a running one is not', async ($, on) => {
  const body = harness($, on)
  expect(await body({ status: 'pending' })).toContain('[pending]')
  expect(await body({ status: 'running' })).not.toContain('[running]')
})

test('a bare agent leaves the row to the engine instead of hiding it', async ($, on) => {
  // body === "" 按「隐藏该行」处理：什么字段都没有时必须原样放行，否则这一行整个消失
  let seen: unknown = 'unset'
  on('clock.now', () => ({ value: Date.now() }))
  on('ui.render', ($: any, e: any) => {
    seen = e.props.body
    const { Text } = $.ui.resolve(e)

    return Text({ children: 'engine' })
  })
  const ui = await $.ui.mount({
    ...row({ description: '', label: '', model: undefined, tokenCount: 0,
      contextWindowSize: undefined, effort: undefined, startTime: undefined }),
    surface: 'terminal',
  })
  await ui.unmount()

  expect(seen).toBeUndefined()
})

test('measured token rate appears after >=1s and warns when context < 20%', async ($, on) => {
  let now = 1_700_000_000_000
  on('clock.now', () => ({ value: now }))
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)

    return Text({ children: String(e.props.body ?? '') })
  })
  const render = async (over: Record<string, unknown>) => {
    const ui = await $.ui.mount({ ...row(over), surface: 'terminal' })
    const text = (await ui.find({ type: 'Text' }))?.text ?? ''
    await ui.unmount()

    return text
  }

  // 首帧记基线，无速率
  const first = await render({ id: 'rate1', tokenCount: 100_000, contextWindowSize: 1_000_000 })
  expect(first).not.toContain('↑')

  // 2 秒后增加 5000 tok → ↑2.5k/s，且剩余 < 20% 带 ⚠
  now += 2000
  const second = await render({ id: 'rate1', tokenCount: 105_000, contextWindowSize: 120_000 })
  expect(second).toContain('↑2.5k/s')
  expect(second).toContain('ctx 剩12%⚠')
})

test('kfmt never emits a four-digit mantissa and negative inputs are ignored', async ($, on) => {
  for (const n of [999_499, 999_500, 99_950_000, 999_450_000, 999_499_999, 999_500_000]) {
    expect(kfmt(n)).not.toMatch(/^1000/)
  }
  expect(kfmt(999_499_999)).toBe('1B')

  const text = await harness($, on)({ tokenCount: -50, columns: -10 })
  expect(text).not.toContain('tok')
  expect(text).not.toContain('ctx')
  expect(text).toContain('审查这个模块的并发写')
})

test('zero tokenCount hides context even with windowSize, strips controls and handles bad effort', async ($, on) => {
  const text = await harness($, on)({
    tokenCount: 0,
    contextWindowSize: 1_000_000,
    description: 'clean\x1b[31m\u009btext\nline2',
    effort: { bad: 1 },
  })
  expect(text).not.toContain('tok')
  expect(text).not.toContain('ctx')
  expect(text).not.toContain('\x1b')
  expect(text).not.toContain('[object Object]')
  expect(text).toContain('clean[31mtext line2')
})

test('clock rollback resets rate baseline and narrow CJK cut emits no bare ellipsis', async ($, on) => {
  let now = 1_700_000_010_000
  on('clock.now', () => ({ value: now }))
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)

    return Text({ children: String(e.props.body ?? '') })
  })
  const render = async (over: Record<string, unknown>) => {
    const ui = await $.ui.mount({ ...row(over), surface: 'terminal' })
    const text = (await ui.find({ type: 'Text' }))?.text ?? ''
    await ui.unmount()

    return text
  }

  await render({ id: 'rb1', tokenCount: 100_000 })
  // 时钟回拨后基线重置，之后推进 2s 能正常算出速率（不冻结）
  now -= 5000
  expect(await render({ id: 'rb1', tokenCount: 100_000 })).not.toContain('↑')
  now += 2000
  expect(await render({ id: 'rb1', tokenCount: 104_000 })).toContain('↑2.0k/s')

  now += 600_000
  expect(await render({ id: 'rb1', tokenCount: 800_000 })).not.toContain('↑')

  // 预算恰剩 2 列遇宽字符时，不得拼出孤零零的「…  — 」
  const tight = await render({
    id: 'rb2',
    description: '中文描述',
    model: 'opus-5',
    effort: undefined,
    tokenCount: 0,
    contextWindowSize: undefined,
    startTime: undefined,
    columns: 12,
  })
  expect(tight.startsWith('…')).toBe(false)
})

test('waiting status is treated as live and durations >=24h format as XdYh', async ($, on) => {
  let now = 1_700_000_100_000
  on('clock.now', () => ({ value: now }))
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)

    return Text({ children: String(e.props.body ?? '') })
  })
  const render = async (over: Record<string, unknown>) => {
    const ui = await $.ui.mount({ ...row(over), surface: 'terminal' })
    const text = (await ui.find({ type: 'Text' }))?.text ?? ''
    await ui.unmount()

    return text
  }

  await render({ id: 'wt1', status: 'waiting', tokenCount: 100_000 })
  now += 2000
  const text = await render({
    id: 'wt1',
    status: 'waiting',
    tokenCount: 104_000,
    startTime: now - 90_000_000,
  })
  expect(text).toContain('↑2.0k/s')
  expect(text).toContain('1d1h')
  expect(text).toContain('[waiting]')
})
