import { AudioLines, PiggyBank } from 'lucide-react'

import type { CardRow } from '@shared/api/ws.gen'
import { formatMoney, tierLabel, type FreeTierState } from '@shared/spend'

import { useEngineStore } from '@/engine'
import { useLanguage, useT } from '@/i18n'
import { BudgetRing, budgetTone } from './BudgetRing'

function str(v: unknown): string | null {
  return typeof v === 'string' ? v : typeof v === 'number' ? String(v) : null
}

/** "Free voice: 120,000 of 1,000,000 characters · resets Dec 1", with a thin bar for the share used. */
export function FreeTierLine({ tier }: { tier: FreeTierState }) {
  const t = useT()
  const lang = useLanguage()
  const count = new Intl.NumberFormat(lang)
  const share = tier.limit > 0 ? Math.min(1, tier.used / tier.limit) : 0
  const resets = Number.isNaN(Date.parse(tier.resets_on))
    ? null
    : new Intl.DateTimeFormat(lang, { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(new Date(tier.resets_on))
  return (
    <li className="flex items-center gap-2 text-xs text-muted-foreground" data-testid="free-tier">
      <AudioLines aria-hidden="true" className="size-3.5 shrink-0" />
      <span className="min-w-0 truncate">
        {t('usage.free_tier', { label: tierLabel(tier, lang), used: count.format(tier.used), limit: count.format(tier.limit) })}
        {resets ? ` · ${t('usage.resets', { date: resets })}` : ''}
      </span>
      <span aria-hidden="true" className="ml-auto h-1 w-16 shrink-0 overflow-hidden rounded-full bg-muted">
        <span className={share >= 1 ? 'block h-full bg-destructive' : 'block h-full bg-muted-foreground'} style={{ width: `${Math.round(share * 100)}%` }} />
      </span>
    </li>
  )
}

/**
 * Spending at a glance (09 § Rows): a budget ring, the estimate, who it went to, free allowances in use, and
 * what the cache saved. Reads `UsageSummary`-shaped `data`; the free allowances come from the live budget
 * state (`usage.changed`). Every figure is an estimate and says so.
 */
export function UsageCard({ title, data, rows }: { title: string; data: Record<string, unknown>; rows: CardRow[] }) {
  const t = useT()
  const lang = useLanguage()
  const formatUsd = (value: string) => formatMoney(value, lang)
  const tiers = useEngineStore((s) => s.usage?.free_tiers) ?? []
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
          <BudgetRing share={pct} tone={budgetTone(pct)} />
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
        {tiers.length > 0 && (
          <ul className="mt-1.5 space-y-1">
            {tiers.map((tier) => (
              <FreeTierLine key={tier.provider} tier={tier} />
            ))}
          </ul>
        )}
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
