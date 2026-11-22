import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { Command, CommandGroup, CommandList } from '@/components/ui/command'
import { IDLE_PLAYBACK } from '@/playback'
import { buildRows, type Row } from '../rows'
import { initialBarState, type BarState } from '../store'
import { RowView, type RowHandlers } from './RowView'

function renderBar(s: BarState) {
  const handlers: RowHandlers = { onSelect: vi.fn(), onSecondary: vi.fn(), onTertiary: vi.fn(), onCancelAction: vi.fn() }
  const groups = buildRows(s, { playback: IDLE_PLAYBACK, recents: [], search: null })
  render(
    <Command label="bar" shouldFilter={false}>
      <CommandList>
        {groups.map((g) => (
          <CommandGroup key={g.id} heading={g.heading}>
            {g.rows.map((row) => (
              <RowView key={row.value} row={row} handlers={handlers} />
            ))}
          </CommandGroup>
        ))}
      </CommandList>
    </Command>,
  )
  return handlers
}

const selectedRows = (fn: RowHandlers['onSelect']) => vi.mocked(fn).mock.calls.map(([row]: [Row]) => row.value)

describe('RowView', () => {
  it('renders submit, pending action and notice rows, and selects on click', () => {
    const s: BarState = {
      ...initialBarState,
      open: true,
      mode: 'typing',
      text: 'skip footers',
      turns: [
        {
          turnId: 't1',
          sessionId: null,
          source: 'typed',
          text: 'hello',
          recordingId: null,
          reply: 'Hi there',
          replyRecordingId: null,
          done: true,
          error: null,
          cards: [],
          actionIds: ['a1'],
        },
      ],
      actions: {
        a1: {
          actionId: 'a1',
          turnId: 't1',
          kind: 'rule.create',
          summary: 'Skip page footers',
          spoken: null,
          preview: null,
          details: {},
          status: 'pending',
          message: null,
        },
      },
      notices: [{ id: 'n1', level: 'error', text: 'Engine offline' }],
    }
    const handlers = renderBar(s)

    expect(screen.getByText('Skip page footers')).toBeInTheDocument()
    expect(screen.getByText('Engine offline')).toBeInTheDocument()
    expect(screen.getByText('Hi there')).toBeInTheDocument()

    fireEvent.click(screen.getByText('skip footers'))
    expect(selectedRows(handlers.onSelect)).toEqual(['submit'])

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(handlers.onCancelAction).toHaveBeenCalledWith(expect.objectContaining({ actionId: 'a1' }))
    // The cancel button does not also select (confirm) the action row.
    expect(selectedRows(handlers.onSelect)).toEqual(['submit'])
  })

  it('renders the pending placeholder disabled', () => {
    const handlers = renderBar({ ...initialBarState, open: true, mode: 'waiting', pendingText: 'read this' })
    const item = screen.getByText('read this').closest('[cmdk-item]')
    expect(item).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(screen.getByText('read this'))
    expect(handlers.onSelect).not.toHaveBeenCalled()
  })
})
