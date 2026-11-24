import { describe, expect, it } from 'vitest'

import { composerPlaceholder, openingPose } from './bar-view'

const t = (key: string, params?: Record<string, string>) => `<${key}${params?.shortcut ? ` ${params.shortcut}` : ''}>`
const base = { mode: 'idle' as const, canListen: true, micError: null, hint: null }

describe('the composer placeholder carries the notes that used to be rows', () => {
  it('says what is going on, most important first', () => {
    expect(composerPlaceholder({ ...base, micError: 'Microphone access is off.', hint: 'x' }, true, t)).toBe('Microphone access is off.')
    expect(composerPlaceholder({ ...base, hint: 'Listening needs the speech model.' }, true, t)).toBe('Listening needs the speech model.')
    expect(composerPlaceholder(base, true, t)).toBe('<connecting>')
    expect(composerPlaceholder({ ...base, canListen: false }, false, t)).toBe('<composer.placeholder.typing_only>')
  })

  it('listening says so even before the engine has connected (what is said is kept)', () => {
    expect(composerPlaceholder({ ...base, mode: 'listening' }, true, t)).toBe('<composer.placeholder.listening>')
  })

  it('idle says how to talk again, with the shortcut', () => {
    expect(composerPlaceholder(base, false, t, '⌥Space')).toBe('<composer.placeholder.idle ⌥Space>')
    expect(composerPlaceholder(base, false, t, '⌃Space')).toBe('<composer.placeholder.idle ⌃Space>')
  })

  it('while it works or speaks it says so, not how to start talking', () => {
    expect(composerPlaceholder({ ...base, mode: 'waiting' }, false, t)).toBe('<composer.placeholder.thinking>')
    expect(composerPlaceholder({ ...base, mode: 'finalizing' }, false, t)).toBe('<composer.placeholder.thinking>')
    expect(composerPlaceholder({ ...base, mode: 'speaking' }, false, t)).toBe('<composer.placeholder.speaking>')
  })
})

describe('the closed bar is drawn in the pose the next open starts in', () => {
  it('listening unless it is known that it cannot, with no leftover notes', () => {
    const closed = { mode: 'idle' as const, micError: 'old error', hint: 'old note', other: 1 }
    expect(openingPose(closed, false, true)).toEqual({ mode: 'listening', micError: null, hint: null, other: 1 })
    // Unknown means "try": the microphone opens on every open unless listening is known to be unavailable.
    expect(openingPose(closed, false, null)).toMatchObject({ mode: 'listening' })
    expect(openingPose(closed, false, false)).toMatchObject({ mode: 'idle' })
  })

  it('an open bar is drawn as it is', () => {
    const open = { mode: 'typing' as const, micError: 'e', hint: 'h' }
    expect(openingPose(open, true, true)).toBe(open)
  })
})
