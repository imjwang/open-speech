/**
 * Two themes (09 § Visual design): **lavender**, a pastel light theme, and **ember**, a dark theme lit in
 * orange. "system" follows the Mac's light/dark setting. The choice is `appearance.theme` in settings, so it
 * can be changed by voice ("dark theme", "tema claro") and applies to every window at once.
 *
 * The theme is `data-theme` on <html>; `.dark` is kept in step for Tailwind's `dark:` variant.
 */
export type ThemeName = 'lavender' | 'ember'
export type ThemePreference = 'system' | ThemeName

export function resolveTheme(pref: ThemePreference, prefersDark: boolean): ThemeName {
  if (pref === 'lavender' || pref === 'ember') return pref
  return prefersDark ? 'ember' : 'lavender'
}

let stop: (() => void) | null = null

/** Apply a theme preference to this document and keep following the OS while it is "system". */
export function applyTheme(pref: ThemePreference = 'system', root: HTMLElement = document.documentElement): ThemeName {
  stop?.()
  stop = null
  const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
  const update = (): ThemeName => {
    const theme = resolveTheme(pref, !!mq?.matches)
    root.dataset.theme = theme
    root.classList.toggle('dark', theme === 'ember')
    root.style.colorScheme = theme === 'ember' ? 'dark' : 'light'
    return theme
  }
  const theme = update()
  if (pref === 'system' && mq) {
    mq.addEventListener('change', update)
    stop = () => mq.removeEventListener('change', update)
  }
  return theme
}

export function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
}
