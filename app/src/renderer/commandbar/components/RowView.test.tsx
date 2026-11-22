import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { Command, CommandGroup, CommandList } from '@/components/ui/command'
import { TooltipProvider } from '@/components/ui/tooltip'
import { buildRows, type Row } from '../rows'
import { initialBarState, type BarState, type CardModel, type TurnModel } from '../store'
import { RowView, type RowHandlers } from './RowView'

function renderBar(s: BarState) {
  const handlers: RowHandlers = {
    onSelect: vi.fn(),
    onSecondary: vi.fn(),
    onTertiary: vi.fn(),
    onCancelAction: vi.fn(),
    onTogglePreset: vi.fn(),
    onSetSetting: vi.fn(),
  }
  const groups = buildRows(s, { recents: [], search: null })
  render(
    <TooltipProvider>
      <Command label="bar" shouldFilter={false}>
        <CommandList>
          {groups.map((g) => (
            <CommandGroup key={g.id}>
              {g.rows.map((row) => (
                <RowView key={row.value} row={row} handlers={handlers} />
              ))}
            </CommandGroup>
          ))}
        </CommandList>
      </Command>
    </TooltipProvider>,
  )
  return handlers
}

const turn = (patch: Partial<TurnModel> = {}): TurnModel => ({
  turnId: 't1',
  sessionId: null,
  source: 'typed',
  text: 'show voices',
  attachments: [],
  recordingId: null,
  reply: 'Here you go.',
  replyRecordingId: null,
  done: true,
  error: null,
  cards: [],
  actionIds: [],
  ...patch,
})

const card = (kind: CardModel['kind'], rows: CardModel['rows'], data: Record<string, unknown> = {}): CardModel => ({
  id: `t1:${kind}`,
  turnId: 't1',
  kind,
  title: kind,
  rows,
  data,
})

const state = (patch: Partial<BarState>): BarState => ({ ...initialBarState, open: true, ...patch })
const selectedRows = (fn: RowHandlers['onSelect']) => vi.mocked(fn).mock.calls.map(([row]: [Row]) => row.value)

describe('RowView', () => {
  it('a confirmation card: named buttons to confirm and cancel, and a click on the row confirms', () => {
    const s = state({
      turns: [turn({ actionIds: ['a1'] })],
      actions: {
        a1: { actionId: 'a1', turnId: 't1', kind: 'add_pronunciation', summary: 'Say “SQL” as “sequel”', spoken: null, preview: null, details: {}, status: 'pending', message: null },
      },
    })
    const handlers = renderBar(s)
    expect(screen.getByText('Say “SQL” as “sequel”')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(handlers.onCancelAction).toHaveBeenCalledWith(expect.objectContaining({ actionId: 'a1' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(selectedRows(handlers.onSelect)).toEqual(['action:a1'])
  })

  it('what was sent shows its badges beside the words', () => {
    renderBar(state({ turns: [turn({ text: 'read them', attachments: [{ kind: 'file', title: 'report.pdf' }, { kind: 'url', title: 'example.org' }] })] }))
    expect(screen.getByText('read them')).toBeInTheDocument()
    expect(screen.getByText('report.pdf').closest('.badge-chip')).toHaveAttribute('data-kind', 'file')
    expect(screen.getByText('example.org').closest('.badge-chip')).toHaveAttribute('data-kind', 'url')
  })

  it('"not part of this build" reads as information, not as a failure', () => {
    renderBar(state({ turns: [turn({ reply: '', error: "Open-ended requests aren't part of this build.", errorCode: 'unavailable' })] }))
    expect(screen.getByText(/aren't part of this build/)).not.toHaveClass('text-destructive')
  })

  it('voices: a named preview button per voice, and the current one is marked', () => {
    const voices = card('voices', [
      { id: 'v1', title: 'Heart', selected: true, data: { local: true, languages: ['en-US'] } },
      { id: 'v2', title: 'Dora', data: { local: true, languages: ['es'] } },
    ])
    const handlers = renderBar(state({ turns: [turn({ source: 'button', cards: [voices] })] }))
    fireEvent.click(screen.getByRole('button', { name: 'Hear Dora' }))
    expect(vi.mocked(handlers.onTertiary).mock.calls[0]?.[0]).toMatchObject({ value: 'card:t1:voices:v2' })
    expect(screen.getByText('Current voice')).toBeInTheDocument()
    expect(screen.getByText('ES')).toBeInTheDocument()
  })

  it('auto-skip presets are chips that say what they do and toggle on press', () => {
    const presets = card('presets', [
      { id: 'p1', title: 'Headers', selected: true, data: { name: 'Headers' } },
      { id: 'p2', title: 'Tables', selected: false, data: { name: 'Tables' } },
    ])
    const handlers = renderBar(state({ turns: [turn({ source: 'button', cards: [presets] })] }))
    expect(screen.getByRole('button', { name: 'Skipping Headers' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'Reading Tables' }))
    expect(handlers.onTogglePreset).toHaveBeenCalledWith(expect.objectContaining({ kind: 'presets' }), expect.objectContaining({ id: 'p2' }))
  })

  it('settings: theme and language are radio groups of named options', () => {
    const settings = card('settings', [
      { id: 'appearance.theme', title: 'Theme', data: { key: 'appearance.theme', value: 'system', options: ['system', 'lavender', 'ember'] } },
      { id: 'language', title: 'Language', data: { key: 'language', value: 'en', options: ['auto', 'en', 'es'] } },
    ])
    const handlers = renderBar(state({ turns: [turn({ source: 'button', cards: [settings] })] }))
    expect(screen.getByRole('radio', { name: 'Match the Mac', checked: true })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Ember' }))
    expect(handlers.onSetSetting).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ id: 'appearance.theme' }), 'ember')
    fireEvent.click(screen.getByRole('radio', { name: 'Español' }))
    expect(handlers.onSetSetting).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({ id: 'language' }), 'es')
  })

  it('spending is a figure with its budget, always marked as an estimate', () => {
    const usage = card('usage', [{ id: 'u0', title: 'Claude', value: '$1.20' }], { total_usd: '1.20', budget_usd: '10', budget_pct: 0.12, cache_savings_usd: '0.30' })
    renderBar(state({ turns: [turn({ cards: [usage] })] }))
    const el = screen.getByTestId('usage-card')
    expect(el).toHaveTextContent('$1.20')
    expect(el).toHaveTextContent('of $10.00')
    expect(el).toHaveTextContent('estimated')
    expect(el).toHaveTextContent('12%')
  })

  it('renders the pending placeholder disabled, with its badges', () => {
    renderBar(state({ pending: { text: 'read them', attachments: [{ kind: 'file', title: 'a.pdf' }] } }))
    const row = screen.getByText('read them').closest('[cmdk-item]')
    expect(row).toHaveAttribute('data-disabled', 'true')
    expect(screen.getByText('a.pdf')).toBeInTheDocument()
  })
})
