import { Circle, Loader2, Mic, MicOff, Volume2 } from 'lucide-react'
import { useLayoutEffect, useRef } from 'react'

import { useRenderCount } from '@/debug'
import { useT } from '@/i18n'
import type { BarMode } from '../store'

export type OrbState = 'idle' | 'listening' | 'recording' | 'thinking' | 'speaking' | 'off'

/**
 * What the orb shows. Listening wins (a barge-in listens while the engine's voice fades out); then the engine's
 * voice, whatever started it (a reply, a notice, a preview the user pressed); then working; idle is quiet.
 */
export function orbState(mode: BarMode, opts: { engineSpeaking: boolean; canListen: boolean | null; error: boolean }): OrbState {
  if (mode === 'listening') return 'listening'
  if (mode === 'recording') return 'recording'
  if (mode === 'speaking' || opts.engineSpeaking) return 'speaking'
  if (mode === 'finalizing' || mode === 'waiting') return 'thinking'
  return opts.canListen === false || opts.error ? 'off' : 'idle'
}

export interface OrbShortcut {
  /** As shown: "⌥Space". */
  display: string
  /** For `aria-keyshortcuts`: "Alt+Space". */
  aria: string
}

/**
 * The voice orb: the one control for talking. It shows whether the bar is listening (and how loud it hears
 * you), working, speaking, or idle, and pressing it does what Tab does: starts or stops listening, and while the
 * assistant speaks, stops it and listens. Its tooltip also gives the shortcut that opens the bar from anywhere.
 *
 * The level comes from `subscribeLevel` (once per frame at most) and goes straight into the `--level` CSS
 * variable, which scales the glow with a transform: a breathing orb re-renders nothing and repaints only itself.
 */
export function Orb({
  state,
  subscribeLevel,
  shortcut,
  detail,
  onPress,
}: {
  state: OrbState
  subscribeLevel?: (fn: (level: number) => void) => () => void
  shortcut?: OrbShortcut
  /** Why it is off (a microphone error), for the tooltip. */
  detail?: string | null
  onPress(): void
}) {
  useRenderCount('Orb')
  const t = useT()
  const ref = useRef<HTMLButtonElement>(null)
  const label =
    state === 'listening' ? t('orb.listening') : state === 'recording' ? t('orb.recording') : state === 'thinking' ? t('orb.thinking') : state === 'speaking' ? t('orb.speaking') : state === 'off' ? t('orb.unavailable') : t('orb.listen')
  // Why it can't listen matters only while it can't (not while it speaks a notice about it).
  const why = state === 'off' ? detail : null
  const tip = [label, why, shortcut?.display].filter(Boolean).join(' · ')
  const named = why ? `${label.replace(/[.\s]+$/, '')}. ${why}` : label

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    if ((state !== 'listening' && state !== 'recording') || !subscribeLevel) {
      el.style.setProperty('--level', '0')
      return
    }
    return subscribeLevel((level) => el.style.setProperty('--level', Math.min(1, level * 1.15).toFixed(3)))
  }, [state, subscribeLevel])

  return (
    <button
      ref={ref}
      type="button"
      tabIndex={-1}
      aria-label={named}
      aria-keyshortcuts={shortcut?.aria}
      title={tip}
      data-state={state}
      data-testid="orb"
      disabled={state === 'off'}
      className="orb size-10 shrink-0 outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
      onMouseDown={(e) => e.preventDefault()} // keep the caret in the composer
      onClick={onPress}
    >
      {state === 'recording' ? (
        <Circle aria-hidden="true" className="size-4 fill-current" />
      ) : state === 'thinking' ? (
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
