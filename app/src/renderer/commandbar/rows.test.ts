import { describe, expect, it } from 'vitest'

import type { DocumentOut, PlaybackState } from '@shared/api/client'

import { IDLE_PLAYBACK } from '@/playback'
import {
  buildRows,
  cardRowEnabled,
  defaultSelection,
  droppedRows,
  findRow,
  SUGGESTIONS,
  turnRows,
  type RowGroup,
  type RowInputs,
} from './rows'
import { initialBarState, type ActionModel, type BarState, type CardModel, type TurnModel } from './store'

const state = (patch: Partial<BarState> = {}): BarState => ({ ...initialBarState, open: true, ...patch })

const doc = (id: string): DocumentOut => ({
  id,
  kind: 'pdf',
  title: `Doc ${id}`,
  block_count: 10,
  created_at: '2026-01-01T00:00:00Z',
  last_opened_at: '2026-01-01T00:00:00Z',
  progress: 0,
  has_source_file: true,
})

const PLAYING: PlaybackState = { ...IDLE_PLAYBACK, state: 'playing', document_id: 'd1', document_title: 'Doc d1' }

const inputs = (patch: Partial<RowInputs> = {}): RowInputs => ({ playback: IDLE_PLAYBACK, recents: [], search: null, ...patch })

const turnModel = (turnId: string, patch: Partial<TurnModel> = {}): TurnModel => ({
  turnId,
  sessionId: null,
  source: 'typed',
  text: `ask ${turnId}`,
  recordingId: null,
  reply: `answer ${turnId}`,
  replyRecordingId: null,
  done: true,
  error: null,
  cards: [],
  actionIds: [],
  ...patch,
})

const actionModel = (actionId: string, turnId: string, status: ActionModel['status'] = 'pending'): ActionModel => ({
  actionId,
  turnId,
  kind: 'rule.create',
  summary: `do ${actionId}`,
  spoken: null,
  preview: null,
  details: {},
  status,
  message: null,
})

const card = (id: string, kind: CardModel['kind'], rowIds: string[] = []): CardModel => ({
  id,
  turnId: 't1',
  kind,
  title: kind,
  rows: rowIds.map((r) => ({ id: r, title: r })),
  data: {},
})

const ids = (groups: RowGroup[]) => groups.map((g) => g.id)
const values = (groups: RowGroup[], id: string) => groups.find((g) => g.id === id)?.rows.map((r) => r.value) ?? []

const imageFile = { kind: 'file' as const, file: new File(['x'], 'cat.png', { type: 'image/png' }), name: 'cat.png', isImage: true }
const pdfFile = { kind: 'file' as const, file: new File(['x'], 'a.pdf', { type: 'application/pdf' }), name: 'a.pdf', isImage: false }

describe('buildRows', () => {
  it('is empty while listening with nothing to show', () => {
    expect(buildRows(state({ mode: 'listening' }), inputs({ playback: PLAYING, recents: [doc('r1')] }))).toEqual([])
  })

  it('orders groups: confirm, dropped, input, conversation, status, now playing, search', () => {
    const s = state({
      mode: 'typing',
      text: '  find taxes ',
      pendingText: 'previous',
      localConfirm: { kind: 'install_model', modelId: 'm', title: 'Install?', detail: '300 MB' },
      dropped: { kind: 'url', url: 'https://example.com/a' },
      turns: [turnModel('t1')],
      notices: [{ id: 'n1', level: 'warn', text: 'careful' }],
      jobs: { j1: { pct: 40, label: 'Importing' }, j2: { pct: 100, label: 'Finished' } },
    })
    const groups = buildRows(s, inputs({ playback: PLAYING, recents: [doc('r1')], search: [doc('s1')] }))

    expect(ids(groups)).toEqual(['confirm', 'dropped', 'input', 'conversation', 'status', 'now', 'search'])
    expect(values(groups, 'input')).toEqual(['submit', 'pending'])
    expect(findRow(groups, 'submit')).toMatchObject({ kind: 'submit', text: 'find taxes' })
    // Finished jobs are hidden.
    expect(values(groups, 'status')).toEqual(['notice:n1', 'job:j1'])
    expect(groups.find((g) => g.id === 'search')?.heading).toBe('Documents')
  })

  it('offers no submit row for the live transcript while listening', () => {
    const groups = buildRows(state({ mode: 'listening', text: 'read this' }), inputs())
    expect(findRow(groups, 'submit')).toBeUndefined()
  })

  it('keeps only the last 20 turns', () => {
    const turns = Array.from({ length: 25 }, (_, i) => turnModel(`t${i}`))
    const conv = values(buildRows(state({ turns }), inputs()), 'conversation')
    expect(conv[0]).toBe('user:t5')
    expect(conv.at(-1)).toBe('reply:t24')
    expect(conv).toHaveLength(40)
  })

  describe('now playing', () => {
    it('shows only when ambient (idle, typing, or with turns)', () => {
      const x = inputs({ playback: PLAYING })
      expect(ids(buildRows(state({ mode: 'listening' }), x))).not.toContain('now')
      expect(ids(buildRows(state({ mode: 'listening', idle: true }), x))).toContain('now')
      expect(ids(buildRows(state({ mode: 'typing' }), x))).toContain('now')
      expect(ids(buildRows(state({ mode: 'waiting', turns: [turnModel('t1')] }), x))).toContain('now')
    })

    it('hides when playback is idle or has no document', () => {
      expect(ids(buildRows(state({ mode: 'typing' }), inputs()))).not.toContain('now')
      const noDoc = { ...PLAYING, document_id: null }
      expect(ids(buildRows(state({ mode: 'typing' }), inputs({ playback: noDoc })))).not.toContain('now')
    })
  })

  describe('recents vs search', () => {
    it('shows up to 10 recents when ambient and not typing', () => {
      const recents = Array.from({ length: 12 }, (_, i) => doc(`r${i}`))
      const groups = buildRows(state({ mode: 'typing' }), inputs({ recents }))
      expect(values(groups, 'recents')).toEqual(recents.slice(0, 10).map((d) => `recent:${d.id}`))
      expect(groups.find((g) => g.id === 'recents')?.heading).toBe('Recent')
    })

    it('replaces recents with search results while typing', () => {
      const groups = buildRows(state({ mode: 'typing', text: 'tax' }), inputs({ recents: [doc('r1')], search: [doc('s1'), doc('s2')] }))
      expect(ids(groups)).not.toContain('recents')
      expect(values(groups, 'search')).toEqual(['recent:s1', 'recent:s2'])
    })

    it('shows neither recents nor an empty Documents group when the search has no hits', () => {
      const groups = buildRows(state({ mode: 'typing', text: 'tax' }), inputs({ recents: [doc('r1')], search: [] }))
      expect(ids(groups)).toEqual(['input'])
    })

    it('shows no recents while typing before search results arrive', () => {
      const groups = buildRows(state({ mode: 'typing', text: 'tax' }), inputs({ recents: [doc('r1')], search: null }))
      expect(ids(groups)).toEqual(['input'])
    })

    it('ignores search results when the input is empty', () => {
      const groups = buildRows(state({ mode: 'typing', text: '   ' }), inputs({ recents: [doc('r1')], search: [doc('s1')] }))
      expect(ids(groups)).toEqual(['recents'])
    })
  })

  describe('suggestions', () => {
    it('show only when idle with no turns, no typed text and nothing dropped', () => {
      const groups = buildRows(state({ mode: 'listening', idle: true }), inputs())
      expect(values(groups, 'suggestions')).toEqual(SUGGESTIONS.map((s) => `suggest:${s.id}`))
      expect(groups.find((g) => g.id === 'suggestions')?.heading).toBe('Try')
    })

    it('hide when not idle, with turns, with typed text, or with a dropped item', () => {
      const has = (s: BarState) => ids(buildRows(s, inputs())).includes('suggestions')
      expect(has(state({ mode: 'typing', idle: false }))).toBe(false)
      expect(has(state({ idle: true, turns: [turnModel('t1')] }))).toBe(false)
      expect(has(state({ idle: true, mode: 'typing', text: 'hi' }))).toBe(false)
      expect(has(state({ idle: true, dropped: { kind: 'text', text: 'hello' } }))).toBe(false)
    })
  })
})

describe('defaultSelection', () => {
  const sel = (s: BarState, x: RowInputs = inputs()) => defaultSelection(buildRows(s, x), s)

  it('keeps an explicit selection that still exists, and drops a stale one', () => {
    const turns = [turnModel('t1'), turnModel('t2')]
    expect(sel(state({ turns, selected: 'user:t1' }))).toBe('user:t1')
    expect(sel(state({ turns, selected: 'action:gone' }))).toBe('reply:t2')
  })

  it('prefers the local confirm over everything', () => {
    const s = state({
      localConfirm: { kind: 'estimate', title: 'Costs $0.40', detail: '', retryId: 'r' },
      dropped: { kind: 'url', url: 'https://example.com' },
      turns: [turnModel('t1', { actionIds: ['a1'] })],
      actions: { a1: actionModel('a1', 't1') },
      mode: 'typing',
      text: 'hello',
    })
    expect(sel(s)).toBe('local-confirm')
  })

  it('then the latest pending action (not a resolved one)', () => {
    const s = state({
      dropped: { kind: 'url', url: 'https://example.com' },
      turns: [turnModel('t1', { actionIds: ['a1'] }), turnModel('t2', { actionIds: ['a2', 'a3'] })],
      actions: { a1: actionModel('a1', 't1'), a2: actionModel('a2', 't2'), a3: actionModel('a3', 't2', 'confirmed') },
    })
    expect(sel(s)).toBe('action:a2')
  })

  it('then the dropped item’s Read it, then submit, then the last reply', () => {
    const turns = [turnModel('t1'), turnModel('t2')]
    expect(sel(state({ mode: 'typing', text: 'go', dropped: { kind: 'text', text: 'abc' }, turns }))).toBe('dropped:read')
    expect(sel(state({ mode: 'typing', text: 'go', turns }))).toBe('submit')
    expect(sel(state({ turns }))).toBe('reply:t2')
  })

  it('never defaults to the pending placeholder row', () => {
    expect(sel(state({ mode: 'waiting', pendingText: 'hello' }))).toBe('')
    expect(sel(state({ mode: 'waiting', pendingText: 'hello', notices: [{ id: 'n1', level: 'info', text: 'hi' }] }))).toBe('notice:n1')
  })
})

describe('droppedRows', () => {
  it('offers Describe only for images', () => {
    expect(droppedRows(imageFile).map((r) => r.value)).toEqual(['dropped:read', 'dropped:describe', 'dropped:summarize', 'dropped:open'])
    expect(droppedRows(pdfFile).map((r) => r.value)).toEqual(['dropped:read', 'dropped:summarize', 'dropped:open'])
    expect(droppedRows({ kind: 'url', url: 'https://example.com' }).map((r) => r.value)).not.toContain('dropped:describe')
  })

  it('marks Read it as the default', () => {
    const rows = droppedRows(imageFile)
    expect(rows.map((r) => (r.kind === 'dropped' ? r.isDefault : null))).toEqual([true, false, false, false])
  })
})

describe('turnRows', () => {
  it('lists user, reply, actions, then cards with their rows', () => {
    const t = turnModel('t1', { actionIds: ['a1', 'missing'], cards: [card('c1', 'voices', ['v1', 'v2'])] })
    const rows = turnRows(t, { a1: actionModel('a1', 't1') })
    expect(rows.map((r) => r.value)).toEqual(['user:t1', 'reply:t1', 'action:a1', 'card:c1', 'card:c1:v1', 'card:c1:v2'])
  })

  it('does not expand usage cards into card rows', () => {
    const t = turnModel('t1', { cards: [card('u1', 'usage', ['openai', 'gemini'])] })
    expect(turnRows(t, {}).map((r) => r.value)).toEqual(['user:t1', 'reply:t1', 'card:u1'])
  })

  it('shows a reply row while streaming or on error, but not for a finished empty reply', () => {
    expect(turnRows(turnModel('t1', { reply: '', done: false }), {}).map((r) => r.kind)).toEqual(['user', 'reply'])
    expect(turnRows(turnModel('t1', { reply: '', error: 'Oops' }), {}).map((r) => r.kind)).toEqual(['user', 'reply'])
    expect(turnRows(turnModel('t1', { reply: '', done: true }), {}).map((r) => r.kind)).toEqual(['user'])
    expect(turnRows(turnModel('t1', { text: '' }), {}).map((r) => r.kind)).toEqual(['reply'])
  })
})

describe('cardRowEnabled', () => {
  it('prefers data.enabled, else selected', () => {
    expect(cardRowEnabled({ id: 'r', title: 'r', selected: true, data: { enabled: false } })).toBe(false)
    expect(cardRowEnabled({ id: 'r', title: 'r', data: { enabled: true } })).toBe(true)
    expect(cardRowEnabled({ id: 'r', title: 'r', selected: true })).toBe(true)
    expect(cardRowEnabled({ id: 'r', title: 'r' })).toBe(false)
  })
})
