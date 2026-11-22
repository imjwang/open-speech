import { Loader2, Mic, MicOff, Volume2 } from 'lucide-react'

import { useT } from '@/i18n'
import type { BarMode } from '../store'

export type OrbState = 'idle' | 'listening' | 'thinking' | 'speaking' | 'off'

export function orbState(mode: BarMode, opts: { prompting: boolean; canListen: boolean | null; error: boolean }): OrbState {
  if (opts.prompting) return 'speaking'
  if (mode === 'listening') return 'listening'
  if (mode === 'finalizing' || mode === 'waiting') return 'thinking'
  return opts.canListen === false || opts.error ? 'off' : 'idle'
}

/**
 * The voice orb: the one control for talking. It shows whether the bar is listening (and how loud it hears
 * you), working, or speaking, and pressing it starts or stops listening (the same as Tab).
 */
export function Orb({ state, level, onPress }: { state: OrbState; level: number; onPress(): void }) {
  const t = useT()
  const label =
    state === 'listening' ? t('orb.listening') : state === 'thinking' ? t('orb.thinking') : state === 'speaking' ? t('orb.speaking') : state === 'off' ? t('orb.unavailable') : t('orb.listen')
  return (
    <button
      type="button"
      tabIndex={-1}
      aria-label={label}
      title={label}
      data-state={state}
      data-testid="orb"
      disabled={state === 'off'}
      className="orb size-10 shrink-0 outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
      style={{ '--level': state === 'listening' ? Math.min(1, level * 1.15).toFixed(3) : 0 } as React.CSSProperties}
      onMouseDown={(e) => e.preventDefault()} // keep the caret in the composer
      onClick={onPress}
    >
      {state === 'thinking' ? (
        <Loader2 aria-hidden="true" className="size-5 animate-spin motion-reduce:animate-none" />
      ) : state === 'speaking' ? (
        <Volume2 aria-hidden="true" className="size-5" />
      ) : state === 'off' ? (
        <MicOff aria-hidden="true" className="size-5" />
      ) : (
        <Mic aria-hidden="true" className="size-5" />
      )}
    </button>
  )
}
