/**
 * What every window needs to know about the user and the build: settings, capabilities, language, theme.
 * The engine owns all of it; this store mirrors it and applies theme and language to the document the moment
 * they change, whoever changed them (a voice command, the settings card, a hand edit of settings.toml).
 */
import { create } from 'zustand'

import { unwrap, type Capabilities, type UserSettings } from '@shared/api/client'
import { setLanguage, type Lang } from '@shared/i18n'

import type { LiveEngine } from './engine'
import { applyTheme, type ThemeName, type ThemePreference } from './theme'

export interface Prefs {
  settings: UserSettings | null
  capabilities: Capabilities | null
  language: Lang
  theme: ThemeName
}

export const usePrefs = create<Prefs>(() => ({ settings: null, capabilities: null, language: 'en', theme: 'lavender' }))

function apply(settings: UserSettings | null, language: string | null | undefined): void {
  const pref = (settings?.appearance?.theme ?? 'system') as ThemePreference
  const theme = applyTheme(pref)
  const root = document.documentElement
  root.classList.toggle('high-contrast', !!settings?.reader?.high_contrast)
  if (language) {
    setLanguage(language)
    root.lang = language
  }
  usePrefs.setState((s) => ({ theme, language: (language as Lang | undefined) ?? s.language }))
}

/** Keep the store (and this document's theme and language) in step with the engine. Returns a disposer. */
export function syncPrefs(engine: LiveEngine): () => void {
  apply(null, null)
  const refresh = async () => {
    const client = engine.client
    if (!client) return
    try {
      const [settings, capabilities] = await Promise.all([
        unwrap(client.GET('/api/v1/settings')),
        unwrap(client.GET('/api/v1/capabilities')),
      ])
      usePrefs.setState({ settings, capabilities })
      apply(settings, capabilities.language)
    } catch {
      // transient: the next connect or event refreshes it
    }
  }
  const offs = [
    engine.socket.onStatus((status) => {
      if (status === 'open') void refresh()
    }),
    engine.socket.on('settings.changed', (msg) => {
      usePrefs.setState({ settings: msg.settings as UserSettings })
      apply(msg.settings as UserSettings, msg.language)
    }),
    engine.socket.on('capabilities.changed', (msg) => {
      usePrefs.setState({ capabilities: msg.capabilities as Capabilities })
      apply(usePrefs.getState().settings, msg.capabilities.language)
    }),
  ]
  void refresh()
  return () => offs.forEach((off) => off())
}

/** Change settings from a control (the settings card, the reader). The engine echoes `settings.changed`. */
export async function patchSettings(engine: LiveEngine, patch: Record<string, unknown>): Promise<void> {
  const client = engine.client
  if (!client) return
  const settings = await unwrap(client.PATCH('/api/v1/settings', { body: patch }))
  usePrefs.setState({ settings })
  apply(settings, usePrefs.getState().capabilities?.language)
}
