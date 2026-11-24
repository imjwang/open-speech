import { describe, expect, it } from 'vitest'

import type { DocumentOut } from '@shared/api/client'

import { buildRows, cardRowEnabled, defaultSelection, findRow, sameRow, turnRows, type RowGroup, type RowInputs } from './rows'
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

const inputs = (patch: Partial<RowInputs> = {}): RowInputs => ({ recents: [], search: null, ...patch })

const turnModel = (turnId: string, patch: Partial<TurnModel> = {}): TurnModel => ({
  turnId,
  sessionId: null,
  source: 'typed',
  text: `ask ${turnId}`,
  attachments: [],
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

const card = (kind: CardModel['kind'], rows = 2): CardModel => ({
  id: `t1:${kind}`,
  turnId: 't1',
  kind,
  title: kind,
  rows: Array.from({ length: rows }, (_, i) => ({ id: `r${i}`, title: `Row ${i}` })),
  data: {},
})

const kinds = (groups: RowGroup[]) => groups.map((g) => `${g.id}:${g.rows.map((r) => r.kind).join(',')}`)

describe('buildRows', () => {
  it('is empty while listening with nothing to show', () => {
    expect(buildRows(state({ mode: 'listening' }), inputs({ recents: [doc('a')] }))).toEqual([])
  })

  it('shows recent documents in an empty, quiet bar, and only then', () => {
    const recents = [doc('a'), doc('b')]
    expect(kinds(buildRows(state(), inputs({ recents })))).toEqual(['recents:recent,recent'])
    expect(buildRows(state({ draft: 'x' }), inputs({ recents }))).toEqual([])
    expect(buildRows(state({ turns: [turnModel('t1')] }), inputs({ recents })).some((g) => g.id === 'recents')).toBe(false)
  })

  it('orders groups: confirm, pending, conversation, status, search', () => {
    const s = state({
      draft: 'doc',
      localConfirm: { kind: 'install_model', modelId: 'm', title: 'Model', detail: '92 MB' },
      pending: { text: 'read this', attachments: [] },
      turns: [turnModel('t1')],
      notices: [{ id: 'n1', level: 'warn', text: 'careful' }],
      jobs: { j1: { pct: 40, label: 'Downloading' }, j2: { pct: 100, label: 'Done' } },
    })
    expect(kinds(buildRows(s, inputs({ search: [doc('a')] })))).toEqual([
      'confirm:local-confirm',
      'pending:pending',
      'conversation:user,reply',
      'status:notice,job',
      'search:recent',
    ])
  })

  it('searches documents by what is typed, but not while badges are attached or while listening', () => {
    const search = [doc('a')]
    expect(buildRows(state({ draft: 'doc' }), inputs({ search })).some((g) => g.id === 'search')).toBe(true)
    const attached = state({ draft: 'doc', attachments: [{ kind: 'url', url: 'https://example.org' }] })
    expect(buildRows(attached, inputs({ search })).some((g) => g.id === 'search')).toBe(false)
    expect(buildRows(state({ draft: 'doc', mode: 'listening' }), inputs({ search })).some((g) => g.id === 'search')).toBe(false)
  })

  it('shows the latest exchange, with earlier ones behind a "history" row', () => {
    const turns = [turnModel('t1'), turnModel('t2'), turnModel('t3')]
    const latest = buildRows(state({ turns }), inputs())
    expect(kinds(latest)).toEqual(['conversation:history,user,reply'])
    expect(findRow(latest, 'history')).toMatchObject({ count: 2 })
    expect(findRow(latest, 'reply:t3')).toBeDefined()
    expect(findRow(latest, 'reply:t1')).toBeUndefined()

    const all = buildRows(state({ turns, showHistory: true }), inputs())
    expect(kinds(all)).toEqual(['conversation:user,reply,user,reply,user,reply'])
  })

  it('keeps only the last 20 turns even with history shown', () => {
    const turns = Array.from({ length: 25 }, (_, i) => turnModel(`t${i}`))
    const groups = buildRows(state({ turns, showHistory: true }), inputs())
    expect(findRow(groups, 'reply:t4')).toBeUndefined()
    expect(findRow(groups, 'reply:t5')).toBeDefined()
  })
})

describe('turnRows', () => {
  it('lists user, reply, actions, then cards: lists as one row per item, figures as one block', () => {
    const turn = turnModel('t1', { actionIds: ['a1'], cards: [card('voices'), card('usage', 13), card('where')] })
    const rows = turnRows(turn, { a1: actionModel('a1', 't1') })
    expect(rows.map((r) => r.kind)).toEqual(['user', 'reply', 'action', 'card-row', 'card-row', 'card', 'card'])
  })

  it('a toolbar press shows its card without a "you said" line or a repeated summary', () => {
    const pressed = turnModel('t1', { source: 'button', text: 'show voices', reply: '3 voices are ready.', cards: [card('voices')] })
    expect(turnRows(pressed, {}).map((r) => r.kind)).toEqual(['card-row', 'card-row'])
    // …but an error from a press is still shown.
    const failed = turnModel('t2', { source: 'button', text: 'read my clipboard', reply: '', error: 'Your clipboard is empty.' })
    expect(turnRows(failed, {}).map((r) => r.kind)).toEqual(['reply'])
  })

  it('shows what was attached even when nothing was typed', () => {
    const attached = turnModel('t1', { text: '', attachments: [{ kind: 'file', title: 'report.pdf' }] })
    expect(turnRows(attached, {}).map((r) => r.kind)).toEqual(['user', 'reply'])
  })

  it('shows a reply row while streaming or on error, but not for a finished empty reply', () => {
    expect(turnRows(turnModel('t1', { reply: '', done: false }), {}).some((r) => r.kind === 'reply')).toBe(true)
    expect(turnRows(turnModel('t1', { reply: '', error: 'boom' }), {}).some((r) => r.kind === 'reply')).toBe(true)
    expect(turnRows(turnModel('t1', { reply: '' }), {}).some((r) => r.kind === 'reply')).toBe(false)
  })
})

describe('defaultSelection', () => {
  const sel = (s: BarState, x = inputs()) => defaultSelection(buildRows(s, x), s)

  it('keeps an explicit selection that still exists, and drops a stale one', () => {
    const turns = [turnModel('t1')]
    expect(sel(state({ turns, selected: 'user:t1' }))).toBe('user:t1')
    expect(sel(state({ turns, selected: 'reply:gone' }))).toBe('reply:t1')
  })

  it('prefers the local confirm, then the latest pending action, then the last reply', () => {
    const turns = [turnModel('t1', { actionIds: ['a1', 'a2'] })]
    const actions = { a1: actionModel('a1', 't1', 'confirmed'), a2: actionModel('a2', 't1') }
    const confirm = { kind: 'install_model' as const, modelId: 'm', title: 'Model', detail: '' }
    expect(sel(state({ turns, actions, localConfirm: confirm }))).toBe('local-confirm')
    expect(sel(state({ turns, actions }))).toBe('action:a2')
    expect(sel(state({ turns }))).toBe('reply:t1')
  })

  it('with something typed or attached, nothing is selected: Enter sends the composer', () => {
    const turns = [turnModel('t1')]
    expect(sel(state({ turns, draft: 'faster' }))).toBe('')
    expect(sel(state({ turns, attachments: [{ kind: 'url', url: 'https://example.org' }] }))).toBe('')
    // …unless the user moved to a row themselves.
    expect(sel(state({ draft: 'doc', selected: 'recent:a' }), inputs({ search: [doc('a')] }))).toBe('recent:a')
  })

  it('never defaults to the pending placeholder, a block card or the history row', () => {
    expect(sel(state({ pending: { text: 'x', attachments: [] } }))).toBe('')
    const pressed = turnModel('t2', { source: 'button', reply: '', cards: [card('usage')] })
    expect(sel(state({ turns: [turnModel('t1'), pressed] }))).toBe('')
    const listed = turnModel('t3', { source: 'button', reply: '', cards: [card('voices')] })
    expect(sel(state({ turns: [turnModel('t1'), listed] }))).toBe('card:t1:voices:r0')
  })
})

describe('cardRowEnabled', () => {
  it('prefers data.enabled, else selected', () => {
    expect(cardRowEnabled({ id: 'a', title: 'A', selected: true })).toBe(true)
    expect(cardRowEnabled({ id: 'a', title: 'A', selected: true, data: { enabled: false } })).toBe(false)
    expect(cardRowEnabled({ id: 'a', title: 'A' })).toBe(false)
  })
})

describe('sameRow (RowView is memoized on it)', () => {
  const t1 = { turnId: 't1', sessionId: null, source: 'typed' as const, text: 'show voices', attachments: [], recordingId: null, reply: '', replyRecordingId: null, done: false, error: null, cards: [], actionIds: [] }

  it('rebuilt rows that point at the same models are the same row', () => {
    const s = { ...initialBarState, turns: [t1] }
    const a = buildRows(s, { recents: [], search: null })
    const b = buildRows(s, { recents: [], search: null })
    const rowsA = a.flatMap((g) => g.rows)
    const rowsB = b.flatMap((g) => g.rows)
    expect(rowsA.length).toBeGreaterThan(0)
    rowsA.forEach((row, i) => expect(sameRow(row, rowsB[i] as typeof row)).toBe(true))
  })

  it('a reply streaming in re-renders the reply, not the "you said" line', () => {
    const before = buildRows({ ...initialBarState, turns: [t1] }, { recents: [], search: null }).flatMap((g) => g.rows)
    const t1b = { ...t1, reply: 'Here are' }
    const after = buildRows({ ...initialBarState, turns: [t1b] }, { recents: [], search: null }).flatMap((g) => g.rows)
    const user = (rows: typeof before) => rows.find((r) => r.kind === 'user')
    const reply = (rows: typeof before) => rows.find((r) => r.kind === 'reply')
    expect(sameRow(user(before)!, user(after)!)).toBe(true)
    expect(sameRow(reply(before)!, reply(after)!)).toBe(false)
  })

  it('different rows are never the same', () => {
    const rows = buildRows({ ...initialBarState, turns: [t1] }, { recents: [], search: null }).flatMap((g) => g.rows)
    expect(sameRow(rows[0]!, rows[1]!)).toBe(false)
  })
})
