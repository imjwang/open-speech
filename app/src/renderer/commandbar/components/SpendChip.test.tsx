import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setLanguage } from '@shared/i18n'
import type { BudgetState } from '@shared/spend'

import { TooltipProvider } from '@/components/ui/tooltip'
import { SpendChip } from './SpendChip'

const budget = (patch: Partial<BudgetState> = {}): BudgetState => ({
  monthly_usd: '10.00',
  warn_at: 0.8,
  hard_stop: true,
  spent_usd: '0',
  pct: 0,
  free_tiers: [],
  ...patch,
})
const chirp = (used: number) => ({ provider: 'google', label: 'google', unit: 'chars' as const, used, limit: 1_000_000, resets_on: '2026-12-01' })

function chip(usage: BudgetState | null) {
  const onPress = vi.fn()
  render(
    <TooltipProvider>
      <SpendChip usage={usage} onPress={onPress} />
    </TooltipProvider>,
  )
  return { onPress, button: screen.getByTestId('spend-chip') }
}

afterEach(() => setLanguage('en'))

describe('SpendChip', () => {
  it('shows the amount with a budget ring and says it all in words', () => {
    const { button } = chip(budget({ spent_usd: '0.12', free_tiers: [chirp(120_000)] }))
    expect(screen.getByTestId('spend-amount')).toHaveTextContent('$0.12')
    expect(screen.getByTestId('budget-ring')).toHaveAttribute('data-tone', 'primary')
    expect(button).toHaveAccessibleName(
      'Spent this month: about 12 cents of your 10 dollar budget, estimated. Free voice: 120,000 of 1,000,000 characters.',
    )
  })

  it('warns near the budget and turns red past it', () => {
    chip(budget({ spent_usd: '8.50' }))
    expect(screen.getByTestId('budget-ring')).toHaveAttribute('data-tone', 'warning')
  })

  it('says Free with a muted ring for the free voice allowance while nothing has been spent', () => {
    chip(budget({ free_tiers: [chirp(250_000)] }))
    expect(screen.getByTestId('spend-amount')).toHaveTextContent('Free')
    expect(screen.getByTestId('budget-ring')).toHaveAttribute('data-tone', 'muted')
  })

  it('has no ring when nothing is used and there is no budget', () => {
    chip(budget({ monthly_usd: '0' }))
    expect(screen.queryByTestId('budget-ring')).toBeNull()
    expect(screen.getByTestId('spend-amount')).toHaveTextContent('Free')
  })

  it('speaks Spanish when the interface does', () => {
    setLanguage('es')
    const { button } = chip(budget({ spent_usd: '0.12' }))
    expect(screen.getByTestId('spend-amount')).toHaveTextContent('0,12 US$')
    expect(button).toHaveAccessibleName('Gastado este mes: unos 12 centavos de tu presupuesto de 10 dólares, estimado.')
  })

  it('before the engine has said, it is the spending button; pressing it shows the spending card', () => {
    const { button, onPress } = chip(null)
    expect(button).toHaveAccessibleName('Spending')
    expect(screen.queryByTestId('spend-amount')).toBeNull()
    fireEvent.click(button)
    expect(onPress).toHaveBeenCalledTimes(1)
  })
})
