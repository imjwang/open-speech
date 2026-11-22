import type { CardRow } from '@shared/api/ws.gen'

import { Progress } from '@/components/ui/progress'
import { formatUsd } from '@/format'

function str(v: unknown): string | null {
  return typeof v === 'string' ? v : typeof v === 'number' ? String(v) : null
}

/**
 * Usage card (09 § Rows: month-to-date estimate, budget bar, top three providers, cache savings).
 * Reads `UsageSummary`-shaped `data` when the engine sends it, else the rows. Always "estimated".
 */
export function UsageCard({ title, data, rows }: { title: string; data: Record<string, unknown>; rows: CardRow[] }) {
  const total = str(data.total_usd)
  const budget = str(data.budget_usd)
  const pct = typeof data.budget_pct === 'number' ? data.budget_pct : null
  const savings = str(data.cache_savings_usd)
  const breakdown = Array.isArray(data.breakdown)
    ? (data.breakdown as Array<Record<string, unknown>>)
        .map((b) => ({ key: str(b.key) ?? '?', usd: str(b.usd_estimate) ?? '0' }))
        .slice(0, 3)
    : rows.slice(0, 3).map((r) => ({ key: r.title, usd: r.value ?? '' }))
  const pctValue = pct === null ? null : Math.round((pct > 1 ? pct / 100 : pct) * 100)
  return (
    <div className="w-full space-y-2 py-1" data-testid="usage-card">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm font-medium">{title}</span>
        <span className="text-xs text-muted-foreground">estimated</span>
      </div>
      {total !== null && (
        <p className="text-2xl font-semibold tabular-nums">
          {formatUsd(total)}
          {budget !== null && <span className="text-sm font-normal text-muted-foreground"> of {formatUsd(budget)} budget</span>}
        </p>
      )}
      {pctValue !== null && <Progress value={pctValue} aria-label={`Budget used: ${pctValue}% (estimated)`} />}
      {breakdown.length > 0 && (
        <ul className="grid grid-cols-3 gap-2 text-xs">
          {breakdown.map((b) => (
            <li key={b.key} className="rounded-md bg-muted px-2 py-1">
              <span className="block truncate text-muted-foreground">{b.key}</span>
              <span className="tabular-nums">{b.usd && /^[\d.]+$/.test(b.usd) ? formatUsd(b.usd) : b.usd}</span>
            </li>
          ))}
        </ul>
      )}
      {savings !== null && <p className="text-xs text-muted-foreground">Cache saved {formatUsd(savings)} (estimated).</p>}
    </div>
  )
}
