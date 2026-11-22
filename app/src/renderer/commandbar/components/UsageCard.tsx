import { PiggyBank } from 'lucide-react'

import type { CardRow } from '@shared/api/ws.gen'

import { formatUsd } from '@/format'
import { useT } from '@/i18n'

function str(v: unknown): string | null {
  return typeof v === 'string' ? v : typeof v === 'number' ? String(v) : null
}

/** A ring that fills with the share of the budget used. */
function BudgetRing({ pct }: { pct: number }) {
  const r = 20
  const c = 2 * Math.PI * r
  const used = Math.max(0, Math.min(1, pct))
  return (
    <svg viewBox="0 0 48 48" className="size-14 -rotate-90" aria-hidden="true">
      <circle cx="24" cy="24" r={r} fill="none" strokeWidth="5" className="stroke-muted" />
      {used > 0.004 && (
        <circle
          cx="24"
          cy="24"
          r={r}
          fill="none"
          strokeWidth="5"
          strokeLinecap="round"
          strokeDasharray={`${c * used} ${c}`}
          className={used >= 1 ? 'stroke-destructive' : used >= 0.8 ? 'stroke-warning' : 'stroke-primary'}
        />
      )}
    </svg>
  )
}

/**
 * Spending at a glance (09 § Rows): a budget ring, the estimate, who it went to, and what the cache saved.
 * Reads `UsageSummary`-shaped `data`. Every figure is an estimate and says so.
 */
export function UsageCard({ title, data, rows }: { title: string; data: Record<string, unknown>; rows: CardRow[] }) {
  const t = useT()
  const total = str(data.total_usd)
  const budget = str(data.budget_usd)
  const pct = typeof data.budget_pct === 'number' ? (data.budget_pct > 1.5 ? data.budget_pct / 100 : data.budget_pct) : null
  const savings = str(data.cache_savings_usd)
  // The engine's rows are labelled ("Claude", "OpenAI") and leave out free local work.
  const breakdown = rows.slice(0, 3)
  return (
    <div className="flex w-full items-center gap-4 py-1" data-testid="usage-card" role="group" aria-label={title}>
      {pct !== null && (
        <div className="relative grid shrink-0 place-items-center">
          <BudgetRing pct={pct} />
          <span className="absolute text-[11px] font-medium tabular-nums">{Math.round(pct * 100)}%</span>
        </div>
      )}
      <div className="min-w-0 flex-1">
        <p className="flex items-baseline gap-1.5">
          <span className="text-2xl font-semibold tabular-nums">{total !== null ? formatUsd(total) : '—'}</span>
          {budget !== null && <span className="text-sm text-muted-foreground">{t('row.budget', { amount: formatUsd(budget) })}</span>}
          <span className="ml-auto text-xs text-muted-foreground">{t('row.estimated')}</span>
        </p>
        {breakdown.length > 0 ? (
          <ul className="mt-1 flex flex-wrap gap-1.5 text-xs">
            {breakdown.map((b) => (
              <li key={b.id} className="rounded-full bg-muted px-2 py-0.5">
                <span className="text-muted-foreground">{b.title}</span>{' '}
                <span className="tabular-nums">{b.value && /^[\d.]+$/.test(b.value) ? formatUsd(b.value) : b.value}</span>
              </li>
            ))}
          </ul>
        ) : null}
        {savings !== null && Number(savings) > 0 && (
          <p className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
            <PiggyBank className="size-3.5" aria-hidden="true" />
            {t('row.saved', { amount: formatUsd(savings) })}
          </p>
        )}
      </div>
    </div>
  )
}
