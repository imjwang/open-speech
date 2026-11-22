import { Keyboard, Loader2, Mic, MicOff } from 'lucide-react'

import { cn } from '@/lib/utils'
import type { BarMode } from '../store'

/** Live mic level (▁▃▅▂) while listening; the mode is also given as text for screen readers. */
export function MicIndicator({ mode, level, error }: { mode: BarMode; level: number; error: boolean }) {
  if (mode === 'listening') {
    const weights = [0.55, 0.85, 1, 0.7]
    return (
      <span role="img" aria-label="Listening" className="flex h-6 shrink-0 items-center gap-2 text-listen">
        <Mic className="size-5 text-listen" aria-hidden="true" />
        <span className="flex h-5 items-end gap-[3px]" aria-hidden="true" data-testid="level-meter">
          {weights.map((w, i) => (
            <span
              key={i}
              className="w-[3px] rounded-full bg-listen"
              style={{ height: `${Math.max(3, Math.round(20 * Math.min(1, level * w * 1.15)))}px` }}
            />
          ))}
        </span>
      </span>
    )
  }
  if (mode === 'finalizing' || mode === 'waiting') {
    return <Loader2 role="img" aria-label="Working" className="size-5 shrink-0 animate-spin text-muted-foreground motion-reduce:animate-none" />
  }
  return error ? (
    <MicOff role="img" aria-label="Microphone unavailable" className="size-5 shrink-0 text-muted-foreground" />
  ) : (
    <Keyboard role="img" aria-label="Typing" className={cn('size-5 shrink-0 text-muted-foreground')} />
  )
}
