import { Wallet } from 'lucide-react'

import { formatSpend, spendLabel, spendRing, type BudgetState } from '@shared/spend'

import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useEngineStore } from '@/engine'
import { useLanguage, useT } from '@/i18n'
import { cn } from '@/lib/utils'
import { BudgetRing } from './BudgetRing'

/**
 * This month's spending, always in view (09 § Command bar): a ring for the share of the budget (or of the
 * free voice allowance while nothing has been spent) and the amount. Press it for the spending card. Its name
 * for screen readers says it all in words: "Spent this month: about 12 cents of your 10 dollar budget,
 * estimated. Free voice: 120,000 of 1,000,000 characters."
 */
export function SpendChip({ usage, onPress }: { usage: BudgetState | null; onPress(): void }) {
  const t = useT()
  const lang = useLanguage()
  const label = usage ? spendLabel(usage, lang) : t('tool.usage')
  const ring = usage ? spendRing(usage) : null
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          tabIndex={-1}
          aria-label={label}
          data-testid="spend-chip"
          className={cn(
            'inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg px-2 text-xs font-medium text-muted-foreground tabular-nums transition-colors outline-none',
            'hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring',
          )}
          onMouseDown={(e) => e.preventDefault()} // keep the caret in the composer
          onClick={onPress}
        >
          {usage ? (
            <>
              {ring && <BudgetRing share={ring.share} tone={ring.tone} className="size-4" strokeWidth={7} trackClassName="stroke-border" />}
              <span data-testid="spend-amount">{formatSpend(usage.spent_usd, lang)}</span>
            </>
          ) : (
            <Wallet aria-hidden="true" className="size-[18px]" />
          )}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-72 text-balance">
        {label}
      </TooltipContent>
    </Tooltip>
  )
}

/** The chip, following the engine: `GET /usage/budget` on connect and on every open, `usage.changed` between. */
export function LiveSpendChip({ onPress }: { onPress(): void }) {
  const usage = useEngineStore((s) => s.usage)
  return <SpendChip usage={usage} onPress={onPress} />
}
