import { describe, expect, it, vi } from 'vitest'

import { IPC } from '../shared/ipc'
import { registerIpc, type IpcDeps, type IpcEventLike, type IpcMainLike } from './ipc'

const BAR = { id: 1 }
const READER = { id: 2 }

function harness() {
  const on = new Map<string, (event: IpcEventLike, ...args: unknown[]) => void>()
  const ipc: IpcMainLike = {
    on: (channel, listener) => on.set(channel, listener),
    handle: () => undefined,
  }
  const deps = {
    isShell: (s: { id: number }) => s.id === BAR.id || s.id === READER.id,
    isBar: (s: { id: number }) => s.id === BAR.id,
    isWebPage: () => false,
    resizeBar: vi.fn((h: number) => Math.max(72, Math.min(600, h))),
    barShown: vi.fn(),
    warn: vi.fn(),
  } as unknown as IpcDeps & { resizeBar: ReturnType<typeof vi.fn>; barShown: ReturnType<typeof vi.fn> }
  registerIpc(ipc, deps)
  const send = (channel: string, sender: { id: number }, payload?: unknown) => {
    const event: IpcEventLike = { sender }
    on.get(channel)?.(event, payload)
    return event
  }
  return { send, deps }
}

describe('bar:resize is synchronous', () => {
  it('answers with the height the window now has', () => {
    const h = harness()
    expect(h.send(IPC.barResize, BAR, { height: 240 }).returnValue).toBe(240)
    expect(h.send(IPC.barResize, BAR, { height: 1900 }).returnValue).toBe(600) // clamped by the window manager
    expect(h.deps.resizeBar).toHaveBeenCalledTimes(2)
  })

  it('answers every request, even one it refuses (the page is blocked until it does)', () => {
    const h = harness()
    const fromReader = h.send(IPC.barResize, READER, { height: 240 })
    expect('returnValue' in fromReader).toBe(true)
    expect(fromReader.returnValue).toBeNull()
    expect(h.send(IPC.barResize, BAR, { height: 'tall' }).returnValue).toBeNull()
    expect(h.send(IPC.barResize, BAR).returnValue).toBeNull()
    expect(h.deps.resizeBar).not.toHaveBeenCalled()
  })
})

describe('bar:shown', () => {
  it('passes a valid open from the bar to the window manager, nothing else', () => {
    const h = harness()
    h.send(IPC.barShown, BAR, { openId: 3, height: 160 })
    h.send(IPC.barShown, READER, { openId: 4, height: 160 })
    h.send(IPC.barShown, BAR, { openId: -1, height: 160 })
    expect(h.deps.barShown).toHaveBeenCalledTimes(1)
    expect(h.deps.barShown).toHaveBeenCalledWith({ openId: 3, height: 160 })
  })
})
