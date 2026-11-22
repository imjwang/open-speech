/**
 * Global shortcuts (09 § Shortcuts, FR-SETUP-6) from the engine's `settings.toml` `[shortcuts]`, plus
 * media keys (FR-READ-17). Media keys need the Accessibility permission on macOS; they are only
 * registered when it is already granted (no prompt).
 */
import { globalShortcut, systemPreferences } from 'electron'

import type { Shortcuts } from '../shared/api/client'
import { DEFAULT_SHORTCUTS, toAccelerator } from './accelerator'

export interface ShortcutHandlers {
  openBar(): void
  playPause(): void
  readClipboard(): void
  skip(count: number): void
}

export interface ShortcutResult {
  registered: Record<string, string>
  failed: string[]
}

function mediaKeysAllowed(): boolean {
  if (process.platform !== 'darwin') return true
  try {
    return systemPreferences.isTrustedAccessibilityClient(false)
  } catch {
    return false
  }
}

export class ShortcutManager {
  private applied = ''

  apply(shortcuts: Partial<Shortcuts> | null, handlers: ShortcutHandlers): ShortcutResult | null {
    const wanted = {
      open_bar: toAccelerator(shortcuts?.open_bar) ?? DEFAULT_SHORTCUTS.open_bar,
      play_pause: toAccelerator(shortcuts?.play_pause) ?? DEFAULT_SHORTCUTS.play_pause,
      read_clipboard: toAccelerator(shortcuts?.read_clipboard) ?? DEFAULT_SHORTCUTS.read_clipboard,
    }
    const key = JSON.stringify(wanted)
    if (key === this.applied) return null
    this.applied = key
    globalShortcut.unregisterAll()
    const result: ShortcutResult = { registered: {}, failed: [] }
    const register = (name: string, accelerator: string, fn: () => void) => {
      let ok = false
      try {
        ok = globalShortcut.register(accelerator, fn)
      } catch {
        ok = false
      }
      if (ok) result.registered[name] = accelerator
      else result.failed.push(`${name} (${accelerator})`)
    }
    register('open_bar', wanted.open_bar, handlers.openBar)
    register('play_pause', wanted.play_pause, handlers.playPause)
    register('read_clipboard', wanted.read_clipboard, handlers.readClipboard)
    if (mediaKeysAllowed()) {
      register('media_play_pause', 'MediaPlayPause', handlers.playPause)
      register('media_next', 'MediaNextTrack', () => handlers.skip(1))
      register('media_previous', 'MediaPreviousTrack', () => handlers.skip(-1))
    }
    return result
  }

  dispose(): void {
    globalShortcut.unregisterAll()
    this.applied = ''
  }
}
