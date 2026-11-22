import { describe, expect, it } from 'vitest'

import { DEFAULT_SHORTCUTS, toAccelerator } from './accelerator'

describe('toAccelerator', () => {
  it.each([
    ['Alt+Space', 'Alt+Space'],
    ['Option+Shift+R', 'Alt+Shift+R'],
    ['option shift r', 'Alt+Shift+R'],
    ['⌥⇧Space', 'Alt+Shift+Space'],
    ['⌘K', 'Command+K'],
    ['Shift+Cmd+k', 'Command+Shift+K'],
    ['Ctrl+Alt+Delete', 'Control+Alt+Delete'],
    ['CmdOrCtrl+Shift+P', 'CommandOrControl+Shift+P'],
    ['Alt+f12', 'Alt+F12'],
    ['Alt+,', 'Alt+,'],
    ['Alt+Esc', 'Alt+Escape'],
    ['MediaPlayPause', 'MediaPlayPause'],
  ])('%s → %s', (raw, expected) => {
    expect(toAccelerator(raw)).toBe(expected)
  })

  it.each([
    ['', 'empty'],
    ['Alt+Shift', 'modifiers only'],
    ['Alt+A+B', 'two keys'],
    ['Alt+F25', 'no such function key'],
    ['Alt+Foo', 'unknown key'],
    [`Alt+${'x'.repeat(70)}`, 'too long'],
  ])('rejects %j (%s)', (raw) => {
    expect(toAccelerator(raw)).toBeNull()
  })

  it('rejects null and undefined', () => {
    expect(toAccelerator(null)).toBeNull()
    expect(toAccelerator(undefined)).toBeNull()
  })

  it('accepts the default shortcuts unchanged', () => {
    for (const value of Object.values(DEFAULT_SHORTCUTS)) expect(toAccelerator(value)).toBe(value)
  })
})
