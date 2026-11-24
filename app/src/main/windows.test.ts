import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// A stand-in for Electron's BrowserWindow that records what the window manager does to the bar.
const calls: string[] = []
class FakeWindow {
  static all: FakeWindow[] = []
  visible = false
  opacity = 1
  bounds = { x: 0, y: 0, width: 680, height: 96 }
  sent: Array<[string, unknown]> = []
  captureResolvers: Array<() => void> = []
  /** Pages load at once unless a test holds them (`FakeWindow.holdLoads`). */
  static holdLoads = false
  handlers: Record<string, Array<() => void>> = {}
  webContents = {
    id: 1,
    on: (event: string, cb: () => void) => {
      ;(this.handlers[event] ??= []).push(cb)
    },
    send: (channel: string, payload: unknown) => {
      if (channel === 'engine:changed') return // sent on every page load; not what these tests look at
      calls.push(`send ${channel}`)
      this.sent.push([channel, payload])
    },
    capturePage: () =>
      new Promise<void>((resolve) => {
        calls.push('capturePage')
        this.captureResolvers.push(resolve)
      }),
  }
  constructor() {
    FakeWindow.all.push(this)
  }
  on() {}
  once() {}
  loadURL() {
    if (!FakeWindow.holdLoads) this.finishLoad()
    return Promise.resolve()
  }
  finishLoad() {
    for (const cb of this.handlers['did-finish-load'] ?? []) cb()
  }
  setAlwaysOnTop() {}
  setVisibleOnAllWorkspaces() {}
  isDestroyed() {
    return false
  }
  isVisible() {
    return this.visible
  }
  getSize() {
    return [this.bounds.width, this.bounds.height]
  }
  getBounds() {
    return { ...this.bounds }
  }
  setBounds(b: typeof this.bounds, animate?: boolean) {
    calls.push(`setBounds h=${b.height} animate=${String(animate)}`)
    this.bounds = { ...b }
  }
  setOpacity(o: number) {
    calls.push(`setOpacity ${o}`)
    this.opacity = o
  }
  show() {
    calls.push('show')
    this.visible = true
  }
  showInactive() {
    calls.push('showInactive')
    this.visible = true
  }
  hide() {
    calls.push('hide')
    this.visible = false
  }
  focus() {
    calls.push('focus')
  }
  invalidateShadow() {
    calls.push('invalidateShadow')
  }
  isMinimized() {
    return false
  }
  restore() {}
  setVibrancy() {}
  setBackgroundColor() {}
}

vi.mock('electron', () => ({
  BrowserWindow: FakeWindow,
  nativeTheme: { shouldUseDarkColors: false },
  screen: {
    getCursorScreenPoint: () => ({ x: 10, y: 10 }),
    getDisplayNearestPoint: () => ({ workArea: { x: 0, y: 0, width: 1440, height: 900 } }),
    getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 1440, height: 900 } }),
  },
}))
vi.mock('./web-view', () => ({
  WebPageView: class {
    owns() {
      return false
    }
  },
}))

const { WindowManager, REVEAL_FALLBACK_MS } = await import('./windows')

let barContext: { canListen: boolean | null; speaking: boolean; listening: boolean; recording?: boolean } = {
  canListen: true,
  speaking: false,
  listening: false,
}

function manager() {
  return new WindowManager({
    preloadPath: '/preload.cjs',
    pageUrl: (page) => `app://open-speech/${page}/index.html`,
    isMac: true,
    hideOnBlur: false,
    macPanel: false,
    web: {} as never,
    onBarVisibility: () => undefined,
    barContext: () => barContext,
  })
}

const flush = async () => {
  for (let i = 0; i < 3; i++) await Promise.resolve()
}

beforeEach(() => {
  calls.length = 0
  FakeWindow.all = []
  barContext = { canListen: true, speaking: false, listening: false }
  FakeWindow.holdLoads = false
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
})
afterEach(() => vi.useRealTimers())

describe('opening the bar never shows a stale frame', () => {
  it('shows the window invisible, tells the page, and makes it visible only after the page painted and a frame was drawn', async () => {
    const wm = manager()
    wm.showBar({ listen: true, viaHotkey: false })
    const bar = FakeWindow.all[0]!
    expect(calls).toEqual(['setOpacity 0', 'setBounds h=96 animate=false', 'send bar:opened', 'show', 'focus'])
    expect(bar.opacity).toBe(0)
    const opened = bar.sent[0]?.[1] as { openId: number }
    expect(opened.openId).toBeGreaterThan(0)

    calls.length = 0
    wm.barShown({ openId: opened.openId, height: 160 })
    // Sized to what the page painted, then shown once the compositor has drawn it.
    expect(calls).toEqual(['setBounds h=160 animate=false', 'invalidateShadow', 'capturePage'])
    expect(bar.opacity).toBe(0)
    bar.captureResolvers.shift()?.()
    await flush()
    expect(bar.opacity).toBe(1)
  })

  it('ignores a late answer for an earlier open', () => {
    const wm = manager()
    wm.showBar({ listen: true, viaHotkey: false })
    const bar = FakeWindow.all[0]!
    const first = (bar.sent[0]?.[1] as { openId: number }).openId
    wm.hideBar()
    wm.showBar({ listen: true, viaHotkey: false })
    calls.length = 0
    wm.barShown({ openId: first, height: 300 })
    expect(calls).toEqual([])
    expect(bar.opacity).toBe(0)
  })

  it('shows it anyway if the page never answers', () => {
    const wm = manager()
    wm.showBar({ listen: true, viaHotkey: false })
    const bar = FakeWindow.all[0]!
    vi.advanceTimersByTime(REVEAL_FALLBACK_MS - 1)
    expect(bar.opacity).toBe(0)
    vi.advanceTimersByTime(1)
    expect(bar.opacity).toBe(1)
  })
})

describe('resizing', () => {
  it('is immediate (no animation), clamped to the screen, and recomputes the shadow', () => {
    const wm = manager()
    wm.showBar({ listen: true, viaHotkey: false })
    calls.length = 0
    expect(wm.resizeBar(240)).toBe(240)
    expect(wm.resizeBar(5000)).toBe(630) // 70 % of the work area
    expect(wm.resizeBar(10)).toBe(72)
    expect(calls).toEqual([
      'setBounds h=240 animate=false',
      'invalidateShadow',
      'setBounds h=630 animate=false',
      'invalidateShadow',
      'setBounds h=72 animate=false',
      'invalidateShadow',
    ])
    calls.length = 0
    expect(wm.resizeBar(72)).toBe(72)
    expect(calls).toEqual([]) // no change, no call
  })
})

describe('the reader', () => {
  it("comes up without taking focus while the bar is visible (it would hide the bar)", () => {
    const wm = manager()
    wm.showBar({ listen: true, viaHotkey: false })
    wm.openReader({ documentId: 'doc_1' }) // creates the window; it shows when ready
    const reader = FakeWindow.all[1]!
    calls.length = 0
    wm.openReader({ documentId: 'doc_2' })
    expect(calls).toContain('showInactive')
    expect(calls).not.toContain('focus')
    wm.hideBar()
    calls.length = 0
    wm.openReader({ documentId: 'doc_3' })
    expect(calls).toEqual(['send reader:show', 'show', 'focus'])
    expect(reader.visible).toBe(true)
  })
})

describe('opening the bar to talk', () => {
  const opened = (bar: InstanceType<typeof FakeWindow>) =>
    bar.sent.filter(([channel]) => channel === 'bar:opened').map(([, payload]) => payload as Record<string, unknown>)

  it('tells the page whether it can listen, as the shell last heard', () => {
    const wm = manager()
    barContext.canListen = false
    wm.showBar({ listen: true, viaHotkey: true })
    expect(opened(FakeWindow.all[0]!)[0]).toMatchObject({ listen: true, canListen: false })
  })

  it('the hotkey while the assistant speaks interrupts it instead of closing the bar', () => {
    const wm = manager()
    wm.showBar({ listen: true, viaHotkey: true })
    vi.setSystemTime(Date.now() + 1000) // past the key-repeat guard
    barContext.speaking = true
    wm.showBar({ listen: true, viaHotkey: true })
    const bar = FakeWindow.all[0]!
    expect(bar.visible).toBe(true)
    expect(opened(bar).at(-1)).toMatchObject({ bargeIn: true })
  })

  it('the hotkey closes a listening bar, and talks again when the bar is idle', () => {
    const wm = manager()
    wm.showBar({ listen: true, viaHotkey: true })
    const bar = FakeWindow.all[0]!
    vi.setSystemTime(Date.now() + 1000)
    barContext.listening = false
    wm.showBar({ listen: true, viaHotkey: true })
    expect(bar.visible).toBe(true)
    expect(opened(bar)).toHaveLength(2)
    expect(opened(bar).at(-1)?.bargeIn).toBeUndefined()
    vi.setSystemTime(Date.now() + 1000)
    barContext.listening = true
    wm.showBar({ listen: true, viaHotkey: true })
    expect(bar.visible).toBe(false)
  })

  it('opened again before the first open was revealed: the reveal follows the newer open', async () => {
    const wm = manager()
    wm.showBar({ listen: true, viaHotkey: false })
    const bar = FakeWindow.all[0]!
    const first = opened(bar)[0]!.openId
    wm.showBar({ listen: true, viaHotkey: false }) // a second tray click while still invisible
    const second = opened(bar).at(-1)!.openId as number
    expect(second).not.toBe(first)
    wm.barShown({ openId: second, height: 120 })
    bar.captureResolvers.shift()?.()
    await flush()
    expect(bar.opacity).toBe(1)
  })

  it('the hotkey while a voice is being recorded for cloning finishes the take instead of closing the bar', () => {
    const wm = manager()
    wm.showBar({ listen: true, viaHotkey: true })
    const bar = FakeWindow.all[0]!
    vi.setSystemTime(Date.now() + 1000)
    barContext = { canListen: true, speaking: false, listening: true, recording: true } as typeof barContext
    wm.showBar({ listen: true, viaHotkey: true })
    expect(bar.visible).toBe(true)
    expect(opened(bar)).toHaveLength(2)
  })

  it('a key repeat of the held hotkey changes nothing', () => {
    const wm = manager()
    wm.showBar({ listen: true, viaHotkey: true })
    barContext.listening = true
    wm.showBar({ listen: true, viaHotkey: true })
    expect(FakeWindow.all[0]!.visible).toBe(true)
  })

  it('an open asked for before the page has loaded (the first launch) happens once it has', () => {
    const wm = manager()
    FakeWindow.holdLoads = true
    const bar = wm.createBar() as unknown as InstanceType<typeof FakeWindow>
    wm.showBar({ listen: true, viaHotkey: false, firstRun: true })
    wm.showBar({ listen: true, viaHotkey: false, firstRun: true })
    expect(bar.visible).toBe(false)
    expect(opened(bar)).toHaveLength(0)
    bar.finishLoad()
    expect(bar.visible).toBe(true)
    expect(opened(bar)).toEqual([expect.objectContaining({ firstRun: true, canListen: true })])
  })
})
