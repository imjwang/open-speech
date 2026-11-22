/**
 * Interface text in the app's languages, shared by the main process (tray menu) and the renderers.
 * The language comes from the engine (`capabilities.language`, `settings.changed`), which resolves "auto".
 */
import { en, type MessageKey } from './en'
import { es } from './es'

export type { MessageKey } from './en'
export type Lang = 'en' | 'es'
export const LANGS: readonly Lang[] = ['en', 'es']

const CATALOGS: Record<Lang, Record<MessageKey, string>> = { en, es }

let current: Lang = 'en'
const listeners = new Set<() => void>()

export function isLang(value: unknown): value is Lang {
  return value === 'en' || value === 'es'
}

export function getLanguage(): Lang {
  return current
}

export function setLanguage(lang: string | null | undefined): void {
  const next: Lang = isLang(lang) ? lang : 'en'
  if (next === current) return
  current = next
  for (const cb of listeners) cb()
}

export function onLanguageChange(cb: () => void): () => void {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

export function translate(lang: Lang, key: MessageKey, params?: Record<string, string | number>): string {
  const template = CATALOGS[lang][key] ?? en[key]
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (whole, name: string) => (name in params ? String(params[name]) : whole))
}

/** Text in the current language. In React, call `useT()` instead so the component follows language changes. */
export function t(key: MessageKey, params?: Record<string, string | number>): string {
  return translate(current, key, params)
}

/** `t` for keys built at run time (e.g. `preset.${name}`): returns `fallback` when there is no such message. */
export function tMaybe(key: string, fallback: string, params?: Record<string, string | number>): string {
  return key in en ? translate(current, key as MessageKey, params) : fallback
}
