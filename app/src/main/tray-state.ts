/** Menu-bar icon state (09 § Menu bar, FR-BAR-9). Pure so it can be tested without Electron. */
export type TrayVisual = 'idle' | 'listening' | 'playing' | 'attention'

export interface TrayInputs {
  listening: boolean
  playing: boolean
  /** Budget warning, async error, or the engine is down. */
  attention: boolean
}

export function trayVisual(s: TrayInputs): TrayVisual {
  if (s.attention) return 'attention'
  if (s.listening) return 'listening'
  if (s.playing) return 'playing'
  return 'idle'
}

export function trayTooltip(visual: TrayVisual, detail: string | null): string {
  const base: Record<TrayVisual, string> = {
    idle: 'Open Speech',
    listening: 'Open Speech: listening',
    playing: 'Open Speech: reading',
    attention: 'Open Speech: needs attention',
  }
  return detail ? `${base[visual]} · ${detail}` : base[visual]
}
