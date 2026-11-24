/**
 * How the bar presents its state: pure, so it is tested without rendering (see BarApp.tsx).
 */
import type { BarMode } from './store'

type PlaceholderKey =
  | 'connecting'
  | 'composer.placeholder.listening'
  | 'composer.placeholder.recording'
  | 'composer.placeholder.thinking'
  | 'composer.placeholder.speaking'
  | 'composer.placeholder.idle'
  | 'composer.placeholder.typing_only'

/**
 * What the composer says when it is empty. Notes that used to be rows of their own (connecting, a microphone
 * error, why listening isn't possible) live here, so they never make the bar grow and shrink. While it listens
 * it says so even before the engine has connected: what is said is kept and sent once it is.
 */
export function composerPlaceholder(
  s: { mode: BarMode; canListen: boolean | null; micError: string | null; hint: string | null },
  connecting: boolean,
  t: (key: PlaceholderKey, params?: Record<string, string>) => string,
  shortcut = '⌥Space',
): string {
  if (s.micError) return s.micError
  if (s.hint) return s.hint
  if (s.mode === 'listening') return t('composer.placeholder.listening')
  if (s.mode === 'recording') return t('composer.placeholder.recording')
  // While it works or talks, say so, and how to cut in: the idle hint here read as if it weren't doing anything.
  if (s.mode === 'finalizing' || s.mode === 'waiting') return t('composer.placeholder.thinking')
  if (s.mode === 'speaking') return t('composer.placeholder.speaking')
  if (connecting) return t('connecting')
  return s.canListen === false ? t('composer.placeholder.typing_only') : t('composer.placeholder.idle', { shortcut })
}

/** The panel's border (1 px, top and bottom): the window is the content plus this. */
export const PANEL_BORDER_PX = 2

/**
 * While the bar is closed it is drawn in the pose the next open starts in (listening, unless it can't): the
 * hidden window's last frame is what macOS shows for the instant it takes Chromium to draw again after a show,
 * so that frame should already be the steady one. Nobody sees the closed bar otherwise.
 */
export function openingPose<T extends { mode: BarMode; micError: string | null; hint: string | null }>(
  s: T,
  open: boolean,
  canListen: boolean | null,
): T {
  if (open) return s
  return { ...s, mode: canListen === false ? 'idle' : 'listening', micError: null, hint: null }
}
