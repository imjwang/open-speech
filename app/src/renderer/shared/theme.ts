/** Follows the OS light/dark setting (09 § Visual design); the reader may override via settings. */
export type ThemePreference = 'system' | 'light' | 'dark'

export function applyTheme(pref: ThemePreference = 'system', root: HTMLElement = document.documentElement): () => void {
  const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
  const update = () => {
    const dark = pref === 'dark' || (pref === 'system' && !!mq?.matches)
    root.classList.toggle('dark', dark)
    root.style.colorScheme = dark ? 'dark' : 'light'
  }
  update()
  if (pref !== 'system' || !mq) return () => undefined
  mq.addEventListener('change', update)
  return () => mq.removeEventListener('change', update)
}

export function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
}
