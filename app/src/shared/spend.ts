/**
 * This month's spending, said briefly (09 § Command bar: the spend chip; § Menu bar: the tray line). Shared by
 * the bar (renderer) and the tray (main process). Money comes from the engine as decimal strings and is only
 * ever turned into numbers for display; every figure is an estimate (FR-ASST-6) and says so where it is read.
 */
import type { BudgetState, FreeTierState } from './api/ws.gen'
import { translate, type Lang, type MessageKey } from './i18n'
import { en } from './i18n/en'

export type { BudgetState, FreeTierState } from './api/ws.gen'

function toNumber(value: string | number | null | undefined): number {
  const n = typeof value === 'number' ? value : Number(value ?? 0)
  return Number.isFinite(n) ? n : 0
}

function usd(lang: Lang, digits: 0 | 2): Intl.NumberFormat {
  return new Intl.NumberFormat(lang, { style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** Dollars and cents in the interface language ("$3.20", "3,20 US$"); a real cost under a cent is "<$0.01". */
export function formatMoney(value: string | number | null | undefined, lang: Lang): string {
  const n = toNumber(value)
  if (n > 0 && n < 0.01) return `<${usd(lang, 2).format(0.01)}`
  return usd(lang, 2).format(n)
}

/**
 * The amount on the chip: exactly nothing → "Free" / "Gratis"; under a cent → "<$0.01"; else dollars and
 * cents ("$0.12"), whole dollars from $100 ("$123").
 */
export function formatSpend(value: string | number | null | undefined, lang: Lang): string {
  const n = toNumber(value)
  if (n <= 0) return translate(lang, 'spend.free')
  if (n < 0.01) return `<${usd(lang, 2).format(0.01)}`
  return n >= 100 ? usd(lang, 0).format(n) : usd(lang, 2).format(n)
}

export type SpendTone = 'primary' | 'warning' | 'destructive' | 'muted'

export interface SpendRing {
  /** 0..1 (and above 1 past the budget). */
  share: number
  tone: SpendTone
}

/** The share of a free allowance used (the largest, if there are several), 0 when none is in use. */
export function freeTierShare(tiers: readonly FreeTierState[] | undefined): number {
  let share = 0
  for (const tier of tiers ?? []) if (tier.limit > 0 && tier.used > 0) share = Math.max(share, tier.used / tier.limit)
  return share
}

/**
 * What the chip's ring shows: the share of the budget spent (warning from `warn_at`, red at 100 %); while
 * nothing has been spent, the share of a free allowance used, in a muted colour; an empty ring when there is a
 * budget and nothing is used; no ring at all when nothing is used and there is no budget.
 */
export function spendRing(b: BudgetState): SpendRing | null {
  const spent = toNumber(b.spent_usd)
  const budget = toNumber(b.monthly_usd)
  if (spent > 0) {
    if (budget <= 0) return null
    const share = spent / budget
    const tone: SpendTone = share >= 1 ? 'destructive' : b.warn_at > 0 && share >= b.warn_at ? 'warning' : 'primary'
    return { share, tone }
  }
  const free = freeTierShare(b.free_tiers)
  if (free > 0) return { share: free, tone: 'muted' }
  return budget > 0 ? { share: 0, tone: 'primary' } : null
}

/** What a free allowance is called, in the interface language ("Free voice"), else the engine's own label. */
export function tierLabel(tier: Pick<FreeTierState, 'provider' | 'label'>, lang: Lang): string {
  const key = `spend.tier.${tier.provider}`
  return key in en ? translate(lang, key as MessageKey) : tier.label
}

/** "12 cents", "3 dollars 20 cents", "10 dollars": money in words, for screen readers. */
export function spokenUsd(value: number, lang: Lang): string {
  const cents = Math.round(value * 100)
  const dollars = Math.floor(cents / 100)
  const rest = cents % 100
  const centWords = rest === 1 ? translate(lang, 'spend.cent') : translate(lang, 'spend.cents', { n: rest })
  if (dollars === 0) return centWords
  const dollarWords = dollars === 1 ? translate(lang, 'spend.dollar') : translate(lang, 'spend.dollars', { n: dollars })
  return rest === 0 ? dollarWords : translate(lang, 'spend.dollars_cents', { dollars: dollarWords, cents: centWords })
}

/**
 * The chip's accessible name, e.g. "Spent this month: about 12 cents of your 10 dollar budget, estimated. Free
 * voice: 120,000 of 1,000,000 characters."
 */
export function spendLabel(b: BudgetState, lang: Lang): string {
  const spent = toNumber(b.spent_usd)
  const budget = toNumber(b.monthly_usd)
  const budgetWords = budget > 0 ? translate(lang, 'spend.budget_amount', { n: new Intl.NumberFormat(lang, { maximumFractionDigits: 2 }).format(budget) }) : ''
  let main: string
  if (spent <= 0) main = budget > 0 ? translate(lang, 'spend.label.nothing_budget', { budget: budgetWords }) : translate(lang, 'spend.label.nothing')
  else if (spent < 0.01) main = budget > 0 ? translate(lang, 'spend.label.under_cent_budget', { budget: budgetWords }) : translate(lang, 'spend.label.under_cent')
  else {
    const amount = spokenUsd(spent, lang)
    main = budget > 0 ? translate(lang, 'spend.label.spent_budget', { amount, budget: budgetWords }) : translate(lang, 'spend.label.spent', { amount })
  }
  const count = new Intl.NumberFormat(lang)
  const tiers = (b.free_tiers ?? []).map((tier) =>
    translate(lang, 'spend.label.free_tier', { label: tierLabel(tier, lang), used: count.format(tier.used), limit: count.format(tier.limit) }),
  )
  return [main, ...tiers].join(' ')
}

/** The tray menu's first line and tooltip: "This month: $0.12 · estimated". */
export function traySpendLine(b: BudgetState | null, lang: Lang): string | null {
  if (!b) return null
  const spent = toNumber(b.spent_usd)
  return spent > 0 ? translate(lang, 'tray.spend', { amount: formatSpend(spent, lang) }) : translate(lang, 'tray.spend_none')
}
