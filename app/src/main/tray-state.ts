/**
 * Menu-bar icon state (09 § Menu bar, FR-BAR-9). Pure so it can be tested without Electron: what the item
 * should show (`trayView`), and the few calls needed to get there from what it shows now (`trayUpdates`), so
 * the image and tooltip are set only when they change (reading sends a state update every second).
 */
import { t } from '../shared/i18n'

export type TrayVisual = 'idle' | 'listening' | 'speaking' | 'playing' | 'attention'

export interface TrayInputs {
  listening: boolean
  /** The engine is saying something (a reply, a notice, a preview): `assistant.speech` started → ended. */
  speaking: boolean
  playing: boolean
  /** Budget warning, async error, or the engine is down. */
  attention: boolean
}

export function trayVisual(s: TrayInputs): TrayVisual {
  if (s.attention) return 'attention'
  if (s.listening) return 'listening'
  if (s.speaking) return 'speaking'
  if (s.playing) return 'playing'
  return 'idle'
}

export function trayTooltip(visual: TrayVisual, detail: string | null, spendLine: string | null = null): string {
  const base = t(`tray.tooltip.${visual}`)
  const head = detail ? `${base} · ${detail}` : base
  return spendLine ? `${head}\n${spendLine}` : head
}

/** Everything the menu-bar item shows. `image` names a template image in resources/tray. */
export interface TrayView {
  image: string
  tooltip: string
  /** Text beside the icon (macOS): the month's spending when that option is on, else ''. */
  title: string
}

export interface TrayViewOptions {
  detail: string | null
  /** The listening pulse is in its "on" frame. */
  pulseOn: boolean
  /** "This month: $0.12 · estimated", once the engine has said. */
  spendLine: string | null
  /** The amount for beside the icon ('' to show none). */
  title: string
}

export function trayView(inputs: TrayInputs, opts: TrayViewOptions): TrayView {
  const visual = trayVisual(inputs)
  return {
    image: visual === 'listening' && opts.pulseOn ? 'listening-pulse' : visual,
    tooltip: trayTooltip(visual, opts.detail, opts.spendLine),
    title: opts.title,
  }
}

/** Only the fields of `next` that differ from what is shown (`prev`, null before the first render). */
export function trayUpdates(prev: TrayView | null, next: TrayView): Partial<TrayView> {
  const out: Partial<TrayView> = {}
  if (prev?.image !== next.image) out.image = next.image
  if (prev?.tooltip !== next.tooltip) out.tooltip = next.tooltip
  if (prev?.title !== next.title) out.title = next.title
  return out
}
