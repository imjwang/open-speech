import type { SpendTone } from '@shared/spend'

import { cn } from '@/lib/utils'

const TONE: Record<SpendTone, string> = {
  primary: 'stroke-primary',
  warning: 'stroke-warning',
  destructive: 'stroke-destructive',
  muted: 'stroke-muted-foreground',
}

/** The usual colour for a share of the budget: warning from `warnAt`, red at 100 %. */
export function budgetTone(share: number, warnAt = 0.8): SpendTone {
  return share >= 1 ? 'destructive' : warnAt > 0 && share >= warnAt ? 'warning' : 'primary'
}

/**
 * A ring that fills with a share (of the budget, or of a free allowance). Decorative: whoever draws it says
 * the figure in words too.
 */
export function BudgetRing({
  share,
  tone = budgetTone(share),
  className = 'size-14',
  strokeWidth = 5,
  trackClassName = 'stroke-muted',
}: {
  share: number
  tone?: SpendTone
  className?: string
  strokeWidth?: number
  trackClassName?: string
}) {
  const r = 24 - strokeWidth / 2 - 1.5
  const c = 2 * Math.PI * r
  const used = Math.max(0, Math.min(1, share))
  return (
    <svg viewBox="0 0 48 48" className={cn('shrink-0 -rotate-90', className)} aria-hidden="true" data-testid="budget-ring" data-tone={tone}>
      <circle cx="24" cy="24" r={r} fill="none" strokeWidth={strokeWidth} className={trackClassName} />
      {used > 0.004 && (
        <circle
          cx="24"
          cy="24"
          r={r}
          fill="none"
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={`${c * used} ${c}`}
          className={TONE[tone]}
        />
      )}
    </svg>
  )
}
