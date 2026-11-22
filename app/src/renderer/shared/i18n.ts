/** React bindings for the shared interface text. */
import { useMemo, useSyncExternalStore } from 'react'

import { getLanguage, onLanguageChange, t, tMaybe, type Lang } from '@shared/i18n'

export { t, tMaybe, type Lang, type MessageKey } from '@shared/i18n'

/** The current interface language; the component re-renders when it changes. */
export function useLanguage(): Lang {
  return useSyncExternalStore(onLanguageChange, getLanguage, getLanguage)
}

/**
 * `t`, subscribed to language changes. The function returned is a new one for each language: components are
 * compiled to reuse what they rendered while their inputs are the same, and a `t` that never changed
 * identity left labels in the previous language.
 */
export function useT(): typeof t {
  const lang = useLanguage()
  return useMemo(() => bind(lang), [lang])
}

function bind(_lang: Lang): typeof t {
  return (key, params) => t(key, params)
}

/** `tMaybe` (for keys built at run time), subscribed to language changes in the same way. */
export function useTMaybe(): typeof tMaybe {
  const lang = useLanguage()
  return useMemo(() => bindMaybe(lang), [lang])
}

function bindMaybe(_lang: Lang): typeof tMaybe {
  return (key, fallback, params) => tMaybe(key, fallback, params)
}
