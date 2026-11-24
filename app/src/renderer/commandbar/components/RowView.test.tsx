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
    onSetSetting: vi.fn(),
    onRecord: vi.fn(),
    onStopRecording: vi.fn(),
    subscribeLevel: vi.fn(() => () => undefined),
    onReplay: vi.fn(),
    onDeleteVoice: vi.fn(),
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

  it('the passage card: the passage, the consent line, and a record button that is the confirmation', () => {
    const details = {
      passage: 'I like to start my mornings slowly.',
      passage_id: 'en-1',
      language: 'en',
      name: 'My voice',
      consent: "This is my voice, or I have the speaker's permission to clone it. Clones stay on this Mac.",
    }
    const pending = state({
      turns: [turn({ actionIds: ['c1'] })],
      actions: { c1: { actionId: 'c1', turnId: 't1', kind: 'clone_record', summary: 'Clone your voice', spoken: null, preview: null, details, status: 'pending', message: null } },
    })
    const handlers = renderBar(pending)
    expect(screen.getByTestId('clone-passage')).toHaveTextContent('I like to start my mornings slowly.')
    expect(screen.getByTestId('clone-consent')).toHaveTextContent("I have the speaker's permission")
    fireEvent.click(screen.getByRole('button', { name: 'Record. I confirm the line below.' }))
    expect(handlers.onRecord).toHaveBeenCalledWith(expect.objectContaining({ actionId: 'c1' }))
    expect(screen.getByTestId('clone-card')).toHaveAttribute('data-phase', 'pending')
  })

  it('the passage card while recording: a stop button and the level meter; after a miss, record again', () => {
    const details = { passage: 'Every summer we drive up the coast.', passage_id: 'en-2', language: 'en', name: 'My voice' }
    const base = {
      turns: [turn({ actionIds: ['c1'] })],
      actions: { c1: { actionId: 'c1', turnId: 't1', kind: 'clone_record', summary: 'Clone your voice', spoken: null, preview: null, details, status: 'confirmed' as const, message: 'Go ahead.' } },
    }
    const capture = { actionId: 'c1', passageId: 'en-2', passage: details.passage, language: 'en', name: 'My voice', error: null, jobId: null }
    const handlers = renderBar(state({ ...base, mode: 'recording', capture: { ...capture, phase: 'recording' } }))
    expect(screen.getByTestId('clone-card')).toHaveAttribute('data-phase', 'recording')
    expect(screen.getByTestId('clone-meter')).toBeInTheDocument()
    expect(handlers.subscribeLevel).toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Stop and use this recording' }))
    expect(handlers.onStopRecording).toHaveBeenCalled()
  })

  it('a failed take says why and offers to record again', () => {
    const details = { passage: 'Every summer we drive up the coast.', passage_id: 'en-2', language: 'en', name: 'My voice' }
    const handlers = renderBar(
      state({
        turns: [turn({ actionIds: ['c1'] })],
        actions: { c1: { actionId: 'c1', turnId: 't1', kind: 'clone_record', summary: 'Clone your voice', spoken: null, preview: null, details, status: 'confirmed', message: null } },
        capture: { actionId: 'c1', passageId: 'en-2', passage: details.passage, language: 'en', name: 'My voice', error: "I didn't hear the passage. Press record to try again.", jobId: null, phase: 'failed' },
      }),
    )
    expect(screen.getByRole('alert')).toHaveTextContent("I didn't hear the passage")
    fireEvent.click(screen.getByRole('button', { name: 'Record. I confirm the line below.' }))
    expect(handlers.onRecord).toHaveBeenCalled()
  })

  it('"Keep it?" has a button to hear the sample again', () => {
    const handlers = renderBar(
      state({
        turns: [turn({ actionIds: ['k1'] })],
        actions: { k1: { actionId: 'k1', turnId: 't1', kind: 'keep_clone', summary: 'Keep the voice My voice', spoken: 'Keep it?', preview: null, details: { slug: 'my-voice', recording_id: 'rec_1', via_turn: true }, status: 'pending', message: null } },
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Hear it again' }))
    expect(handlers.onReplay).toHaveBeenCalledWith('rec_1')
  })

  it('voices: ＋ "Clone a voice" is a command row; your own voices have a person icon and a delete button', () => {
    const voices = card('voices', [
      { id: 'qwen3:clone:my-voice', title: 'My voice', data: { local: true, languages: ['en'], clone: true, source: 'recording', delete_command: 'delete the voice My voice' } },
      { id: 'qwen3:clone:teller', title: 'Teller', data: { local: true, languages: ['en'], clone: true, source: 'design', delete_command: 'delete the voice Teller' } },
      { id: 'voices:clone', title: 'Clone a voice', data: { command: 'clone my voice', icon: 'clone' } },
    ])
    const handlers = renderBar(state({ turns: [turn({ source: 'button', cards: [voices] })] }))
    expect(screen.getByLabelText('Your voice')).toBeInTheDocument()
    expect(screen.getByLabelText('Designed voice')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Delete My voice' }))
    expect(handlers.onDeleteVoice).toHaveBeenCalledWith(expect.objectContaining({ id: 'qwen3:clone:my-voice' }))
    fireEvent.click(screen.getByText('Clone a voice'))
    expect(selectedRows(handlers.onSelect)).toEqual(['card:t1:voices:voices:clone'])
    expect(screen.queryByRole('button', { name: 'Hear Clone a voice' })).toBeNull()
  })

  it('designed candidates: play each, Enter keeps one', () => {
    const design = card('design', [
      { id: 'cand_1', title: 'Voice 1', data: { recording_id: 'rec_a', candidate_id: 'cand_1', command: 'keep voice 1' } },
      { id: 'cand_2', title: 'Voice 2', data: { recording_id: 'rec_b', candidate_id: 'cand_2', command: 'keep voice 2' } },
    ])
    const handlers = renderBar(state({ turns: [turn({ cards: [design] })] }))
    fireEvent.click(screen.getByRole('button', { name: 'Hear Voice 2' }))
    expect(handlers.onReplay).toHaveBeenCalledWith('rec_b')
    fireEvent.click(screen.getByText('Voice 1'))
    expect(selectedRows(handlers.onSelect)).toEqual(['card:t1:design:cand_1'])
  })

  it('local voice size and precision: named options, and the ones that need a download say how big it is', () => {
    const settings = card('settings', [
      { id: 'qwen3.size', title: 'Local voice size', data: { key: 'qwen3.size', value: '0.6b', options: ['0.6b', '1.7b'], auto: true, downloads: { '1.7b': { model_id: 'qwen3-tts-1.7b-customvoice-8bit', size: '2.4 GB' } } } },
      { id: 'qwen3.precision', title: 'Local voice precision', data: { key: 'qwen3.precision', value: '8bit', options: ['4bit', '8bit', 'bf16'], downloads: {} } },
    ])
    const handlers = renderBar(state({ turns: [turn({ source: 'button', cards: [settings] })] }))
    expect(screen.getByRole('radio', { name: 'Small', checked: true })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Large · 2.4 GB download' }))
    expect(handlers.onSetSetting).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ id: 'qwen3.size' }), '1.7b')
    expect(screen.getByRole('radio', { name: 'Full' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: '8-bit', checked: true })).toBeInTheDocument()
  })

  it('models: Qwen variants read as the family with size and precision chips', () => {
    const models = card('models', [
      { id: 'qwen3-tts-0.6b-base-8bit', title: 'Qwen voice cloning', subtitle: '1.9 GB · 3 GB memory', data: { installed: true, family: 'base', size: '0.6b', precision: '8bit', chosen: true, status: 'Installed' } },
    ])
    renderBar(state({ turns: [turn({ source: 'button', cards: [models] })] }))
    expect(screen.getByText('Qwen voice cloning')).toBeInTheDocument()
    expect(screen.getAllByTestId('model-chip').map((c) => c.textContent)).toEqual(['0.6B', '8-bit'])
    expect(screen.getByLabelText('Chosen in settings')).toBeInTheDocument()
  })

  it('renders the pending placeholder disabled, with its badges', () => {
    renderBar(state({ pending: { text: 'read them', attachments: [{ kind: 'file', title: 'a.pdf' }] } }))
    const row = screen.getByText('read them').closest('[cmdk-item]')
    expect(row).toHaveAttribute('data-disabled', 'true')
    expect(screen.getByText('a.pdf')).toBeInTheDocument()
  })
})
