/**
 * `settings.toml` `[shortcuts]` values (e.g. "Alt+Space", "Option+Shift+R", "⌥⇧Space") → Electron
 * accelerators (FR-SETUP-6). Returns null for anything that is not exactly modifiers + one key.
 */
const MODIFIERS: Record<string, string> = {
  alt: 'Alt',
  option: 'Alt',
  opt: 'Alt',
  '⌥': 'Alt',
  shift: 'Shift',
  '⇧': 'Shift',
  ctrl: 'Control',
  control: 'Control',
  '⌃': 'Control',
  cmd: 'Command',
  command: 'Command',
  meta: 'Command',
  super: 'Super',
  '⌘': 'Command',
  cmdorctrl: 'CommandOrControl',
  commandorcontrol: 'CommandOrControl',
}

const NAMED_KEYS: Record<string, string> = {
  space: 'Space',
  '␣': 'Space',
  enter: 'Enter',
  return: 'Return',
  tab: 'Tab',
  esc: 'Escape',
  escape: 'Escape',
  backspace: 'Backspace',
  delete: 'Delete',
  up: 'Up',
  down: 'Down',
  left: 'Left',
  right: 'Right',
  home: 'Home',
  end: 'End',
  pageup: 'PageUp',
  pagedown: 'PageDown',
  plus: 'Plus',
  mediaplaypause: 'MediaPlayPause',
  medianexttrack: 'MediaNextTrack',
  mediaprevioustrack: 'MediaPreviousTrack',
  mediastop: 'MediaStop',
}

const MODIFIER_ORDER = ['CommandOrControl', 'Command', 'Control', 'Super', 'Alt', 'Shift']

function splitTokens(raw: string): string[] {
  // Accept "Alt+Shift+R", "Alt Shift R" and symbol runs like "⌥⇧Space".
  const expanded = raw.replace(/([⌥⇧⌃⌘])/g, '+$1+')
  return expanded
    .split(/[+\s]+/)
    .map((t) => t.trim())
    .filter(Boolean)
}

export function toAccelerator(raw: string | null | undefined): string | null {
  if (!raw || typeof raw !== 'string' || raw.length > 64) return null
  const mods = new Set<string>()
  let key: string | null = null
  for (const token of splitTokens(raw)) {
    const lower = token.toLowerCase()
    const mod = MODIFIERS[lower]
    if (mod) {
      mods.add(mod)
      continue
    }
    if (key !== null) return null // two non-modifier keys
    const named = NAMED_KEYS[lower]
    if (named) key = named
    else if (/^f([1-9]|1[0-9]|2[0-4])$/i.test(token)) key = token.toUpperCase()
    else if (/^[a-z0-9]$/i.test(token)) key = token.toUpperCase()
    else if (/^[`\-=[\]\;',./]$/.test(token)) key = token
    else return null
  }
  if (!key) return null
  const ordered = MODIFIER_ORDER.filter((m) => mods.has(m))
  return [...ordered, key].join('+')
}

export const DEFAULT_SHORTCUTS = {
  open_bar: 'Alt+Space',
  play_pause: 'Alt+Shift+Space',
  read_clipboard: 'Alt+Shift+R',
} as const
