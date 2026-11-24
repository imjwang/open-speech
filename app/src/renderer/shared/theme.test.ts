import { afterEach, describe, expect, it, vi } from 'vitest'

import { applyTheme, resolveTheme } from './theme'

function mockSystem(dark: boolean) {
  const listeners = new Set<() => void>()
  const mq = {
    matches: dark,
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
  }
  vi.spyOn(window, 'matchMedia').mockReturnValue(mq as unknown as MediaQueryList)
  return {
    set(next: boolean) {
      mq.matches = next
      for (const cb of listeners) cb()
    },
    listeners,
  }
}

afterEach(() => vi.restoreAllMocks())

describe('themes', () => {
  it('"system" is lavender by day and ember in dark mode; a chosen theme ignores the system', () => {
    expect(resolveTheme('system', false)).toBe('lavender')
    expect(resolveTheme('system', true)).toBe('ember')
    expect(resolveTheme('lavender', true)).toBe('lavender')
    expect(resolveTheme('ember', false)).toBe('ember')
  })

  it('applies the theme to the document and keeps Tailwind\'s dark variant in step', () => {
    mockSystem(false)
    const root = document.createElement('html')
    expect(applyTheme('ember', root)).toBe('ember')
    expect(root.dataset.theme).toBe('ember')
    expect(root.classList.contains('dark')).toBe(true)
    expect(root.style.colorScheme).toBe('dark')
    expect(applyTheme('lavender', root)).toBe('lavender')
    expect(root.classList.contains('dark')).toBe(false)
    expect(root.style.colorScheme).toBe('light')
  })

  it('follows the Mac while set to "system", and stops when a theme is chosen', () => {
    const system = mockSystem(false)
    const root = document.createElement('html')
    applyTheme('system', root)
    expect(root.dataset.theme).toBe('lavender')
    system.set(true)
    expect(root.dataset.theme).toBe('ember')
    applyTheme('lavender', root)
    expect(system.listeners.size).toBe(0)
    system.set(false)
    expect(root.dataset.theme).toBe('lavender')
  })
})

describe('the first paint uses the theme main remembered', () => {
  it('reads ?theme= from the page URL, else follows the Mac', async () => {
    const { initialThemePreference } = await import('./prefs')
    expect(initialThemePreference('?theme=ember&debug=1')).toBe('ember')
    expect(initialThemePreference('?theme=lavender')).toBe('lavender')
    expect(initialThemePreference('?theme=system')).toBe('system')
    expect(initialThemePreference('?theme=neon')).toBe('system')
    expect(initialThemePreference('')).toBe('system')
  })
})
