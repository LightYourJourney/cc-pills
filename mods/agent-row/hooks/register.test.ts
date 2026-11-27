import { expect, test } from 'claude-code/testing'

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
