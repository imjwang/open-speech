import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { canListenFallback, parseShellState, ShellStateStore, shellStatePath, type ShellStateFs } from './shell-state'

function memoryFs(initial: Record<string, string> = {}): ShellStateFs & { files: Record<string, string>; writes: number } {
  const files = { ...initial }
  return {
    files,
    writes: 0,
    read(file) {
      return files[file] ?? null
    },
    write(file, text) {
      this.writes++
      files[file] = text
    },
  }
}

describe('shell state', () => {
  it('lives with the profile: OPEN_SPEECH_HOME when set, else userData', () => {
    expect(shellStatePath({ OPEN_SPEECH_HOME: '/tmp/profile' }, '/Users/x/Library/Application Support/Open Speech')).toBe(
      path.join('/tmp/profile', 'shell-state.json'),
    )
    expect(shellStatePath({}, '/data')).toBe(path.join('/data', 'shell-state.json'))
  })

  const defaults = { themePref: 'system', traySpend: false, canListen: null, onboardingCompleted: null }

  it('turns anything into a complete, valid state', () => {
    expect(parseShellState(undefined)).toEqual(defaults)
    expect(parseShellState('nonsense')).toEqual(defaults)
    expect(parseShellState({ themePref: 'neon', traySpend: 'yes', canListen: 'maybe', onboardingCompleted: 3 })).toEqual(defaults)
    expect(parseShellState({ themePref: 'ember', traySpend: true, later: 1 })).toEqual({ ...defaults, themePref: 'ember', traySpend: true })
    expect(parseShellState({ canListen: false, onboardingCompleted: true })).toEqual({ ...defaults, canListen: false, onboardingCompleted: true })
  })

  it('can the bar listen before the engine answers? what it said last, else yes when packaged', () => {
    expect(canListenFallback({ canListen: null }, true)).toBe(true)
    expect(canListenFallback({ canListen: null }, false)).toBe(false)
    expect(canListenFallback({ canListen: false }, true)).toBe(false)
    expect(canListenFallback({ canListen: true }, false)).toBe(true)
  })

  it('reads the saved theme at startup, and survives a corrupt file', () => {
    const fs = memoryFs({ '/p/shell-state.json': JSON.stringify({ themePref: 'lavender' }) })
    expect(new ShellStateStore('/p/shell-state.json', fs).get().themePref).toBe('lavender')
    const warnings: string[] = []
    const broken = new ShellStateStore('/q/shell-state.json', memoryFs({ '/q/shell-state.json': '{not json' }), (m) => warnings.push(m))
    expect(broken.get().themePref).toBe('system')
    expect(warnings).toHaveLength(1)
  })

  it('writes only when something changed', () => {
    const fs = memoryFs()
    const store = new ShellStateStore('/p/shell-state.json', fs)
    store.update({ themePref: 'system' })
    expect(fs.writes).toBe(0)
    store.update({ themePref: 'ember' })
    store.update({ themePref: 'ember' })
    expect(fs.writes).toBe(1)
    expect(JSON.parse(fs.files['/p/shell-state.json'] ?? '{}')).toEqual({ ...defaults, themePref: 'ember' })
    expect(store.update({ traySpend: true }).traySpend).toBe(true)
    expect(fs.writes).toBe(2)
  })
})
