import { describe, expect, it } from 'vitest'

import { formatMoney, formatSpend, freeTierShare, spendLabel, spendRing, spokenUsd, traySpendLine, type BudgetState } from './spend'

const budget = (patch: Partial<BudgetState> = {}): BudgetState => ({
  monthly_usd: '10.00',
  warn_at: 0.8,
  hard_stop: true,
  spent_usd: '0',
  pct: 0,
  free_tiers: [],
  ...patch,
})

// The engine labels it "google"; the interface names it in its own language.
const chirp = (used: number) => ({ provider: 'google', label: 'google', unit: 'chars' as const, used, limit: 1_000_000, resets_on: '2026-12-01' })

describe('formatSpend', () => {
  it('says Free / Gratis for exactly nothing', () => {
    expect(formatSpend('0', 'en')).toBe('Free')
    expect(formatSpend('0.0000', 'en')).toBe('Free')
    expect(formatSpend(0, 'es')).toBe('Gratis')
    expect(formatSpend(null, 'en')).toBe('Free')
  })

  it('never rounds a real cost down to nothing', () => {
    expect(formatSpend('0.0001', 'en')).toBe('<$0.01')
    expect(formatSpend('0.0099', 'en')).toBe('<$0.01')
    expect(formatSpend('0.004', 'es')).toBe('<0,01 US$')
  })

  it('shows dollars and cents, and whole dollars from $100', () => {
    expect(formatSpend('0.12', 'en')).toBe('$0.12')
    expect(formatSpend('0.01', 'en')).toBe('$0.01')
    expect(formatSpend('12.5', 'en')).toBe('$12.50')
    expect(formatSpend('99.994', 'en')).toBe('$99.99')
    expect(formatSpend('123.40', 'en')).toBe('$123')
    expect(formatSpend('1234.5', 'en')).toBe('$1,235')
    expect(formatSpend('0.12', 'es')).toBe('0,12 US$')
  })

  it('the spending card shows plain money in the interface language', () => {
    expect(formatMoney('0', 'en')).toBe('$0.00')
    expect(formatMoney('10', 'en')).toBe('$10.00')
    expect(formatMoney('3.2', 'es')).toBe('3,20\u00a0US$')
    expect(formatMoney('0.002', 'en')).toBe('<$0.01')
  })

  it('treats garbage as nothing rather than NaN', () => {
    expect(formatSpend('abc', 'en')).toBe('Free')
  })
})

describe('spendRing', () => {
  it('fills with the share of the budget, warning from warn_at, red at 100 %', () => {
    expect(spendRing(budget({ spent_usd: '1.20' }))).toEqual({ share: 0.12, tone: 'primary' })
    expect(spendRing(budget({ spent_usd: '8.00' }))).toEqual({ share: 0.8, tone: 'warning' })
    expect(spendRing(budget({ spent_usd: '10.00' }))).toEqual({ share: 1, tone: 'destructive' })
    expect(spendRing(budget({ spent_usd: '12.00' }))?.tone).toBe('destructive')
  })

  it('shows the free allowance in a muted colour while nothing has been spent', () => {
    expect(spendRing(budget({ free_tiers: [chirp(120_000)] }))).toEqual({ share: 0.12, tone: 'muted' })
    // Once money is spent the budget is what matters.
    expect(spendRing(budget({ spent_usd: '0.50', free_tiers: [chirp(1_000_000)] }))).toEqual({ share: 0.05, tone: 'primary' })
  })

  it('is an empty ring with a budget and nothing used, and no ring with neither', () => {
    expect(spendRing(budget())).toEqual({ share: 0, tone: 'primary' })
    expect(spendRing(budget({ monthly_usd: '0' }))).toBeNull()
    // Spending with no budget: the amount says it all.
    expect(spendRing(budget({ monthly_usd: '0', spent_usd: '0.30' }))).toBeNull()
  })

  it('takes the fullest free allowance', () => {
    expect(freeTierShare([chirp(100_000), { ...chirp(600_000), provider: 'other' }])).toBe(0.6)
    expect(freeTierShare(undefined)).toBe(0)
  })
})

describe('spoken amounts and the chip label', () => {
  it('says money in words', () => {
    expect(spokenUsd(0.12, 'en')).toBe('12 cents')
    expect(spokenUsd(0.01, 'en')).toBe('1 cent')
    expect(spokenUsd(1, 'en')).toBe('1 dollar')
    expect(spokenUsd(3.2, 'en')).toBe('3 dollars 20 cents')
    expect(spokenUsd(3.2, 'es')).toBe('3 dólares con 20 centavos')
  })

  it('reads the month like a person would', () => {
    expect(spendLabel(budget({ spent_usd: '0.12', free_tiers: [chirp(120_000)] }), 'en')).toBe(
      'Spent this month: about 12 cents of your 10 dollar budget, estimated. Free voice: 120,000 of 1,000,000 characters.',
    )
    expect(spendLabel(budget({ spent_usd: '0.12', free_tiers: [chirp(120_000)] }), 'es')).toBe(
      'Gastado este mes: unos 12 centavos de tu presupuesto de 10 dólares, estimado. Voz gratis: 120.000 de 1.000.000 caracteres.',
    )
    expect(spendLabel(budget(), 'en')).toBe('Nothing spent this month, of your 10 dollar budget.')
    expect(spendLabel(budget({ monthly_usd: '0' }), 'en')).toBe('Nothing spent this month.')
    expect(spendLabel(budget({ spent_usd: '0.003' }), 'en')).toBe('Spent this month: less than a cent of your 10 dollar budget, estimated.')
    expect(spendLabel(budget({ monthly_usd: '0', spent_usd: '2' }), 'en')).toBe('Spent this month: about 2 dollars, estimated.')
  })

  it("names a free allowance it doesn't know by the engine's label", () => {
    expect(spendLabel(budget({ monthly_usd: '0', free_tiers: [{ ...chirp(5), provider: 'acme', label: 'Acme minutes' }] }), 'en')).toBe(
      'Nothing spent this month. Acme minutes: 5 of 1,000,000 characters.',
    )
  })

  it('gives the tray its line', () => {
    expect(traySpendLine(null, 'en')).toBeNull()
    expect(traySpendLine(budget({ spent_usd: '0.12' }), 'en')).toBe('This month: $0.12 · estimated')
    expect(traySpendLine(budget(), 'en')).toBe('This month: nothing spent')
    expect(traySpendLine(budget({ spent_usd: '0.12' }), 'es')).toBe('Este mes: 0,12 US$ · estimado')
  })
})
