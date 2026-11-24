/**
 * What the bar shows under the composer, as cmdk rows (09 § Command bar: "Rows, not screens"). Pure, so it
 * is unit-tested; the components render it and the key handlers look rows up by their cmdk `value`.
 *
 * The bar shows the present, not a log: the latest exchange, with earlier ones one press away.
 */
import type { CardRow } from '@shared/api/ws.gen'
import type { DocumentOut } from '@shared/api/client'

import type { ActionModel, BarState, CaptureState, CardModel, LocalConfirm, Notice, TurnAttachment, TurnModel } from './store'
import { composerEmpty } from './store'

export type Row =
  | { kind: 'local-confirm'; value: 'local-confirm'; confirm: LocalConfirm }
  | { kind: 'pending'; value: 'pending'; text: string; attachments: TurnAttachment[] }
  | { kind: 'history'; value: 'history'; count: number }
  | { kind: 'user'; value: string; turn: TurnModel }
  | { kind: 'reply'; value: string; turn: TurnModel }
  /** `capture`: the recording that belongs to this card (the passage card of a voice being cloned). */
  | { kind: 'action'; value: string; action: ActionModel; capture: CaptureState | null }
  | { kind: 'card'; value: string; card: CardModel }
  | { kind: 'card-row'; value: string; card: CardModel; row: CardRow }
  | { kind: 'notice'; value: string; notice: Notice }
  /** `job`: what it is ("install", "clone", "design"), for its icon. */
  | { kind: 'job'; value: string; jobId: string; pct: number; label: string; job?: string | null }
  | { kind: 'recent'; value: string; doc: DocumentOut }

export interface RowGroup {
  id: string
  rows: Row[]
}

/** The part of the bar's state the rows are built from (a component subscribes to just these). */
export type RowState = Pick<
  BarState,
  | 'localConfirm'
  | 'pending'
  | 'turns'
  | 'showHistory'
  | 'actions'
  | 'notices'
  | 'jobs'
  | 'mode'
  | 'draft'
  | 'attachments'
  | 'selected'
  | 'capture'
>

export function rowState(s: BarState): RowState {
  return {
    localConfirm: s.localConfirm,
    pending: s.pending,
    turns: s.turns,
    showHistory: s.showHistory,
    actions: s.actions,
    notices: s.notices,
    jobs: s.jobs,
    mode: s.mode,
    draft: s.draft,
    attachments: s.attachments,
    selected: s.selected,
    capture: s.capture,
  }
}

export interface RowInputs {
  recents: DocumentOut[]
  /** Results of `GET /documents?q=` for the typed text (null while not searching). */
  search: DocumentOut[] | null
}

const MAX_TURNS = 20
const RECENTS_SHOWN = 5
/** Cards drawn as one block (a figure) rather than one row per item. */
const BLOCK_CARDS = new Set(['usage', 'where'])

export function turnRows(turn: TurnModel, actions: BarState['actions'], capture: CaptureState | null = null): Row[] {
  const rows: Row[] = []
  const pressed = turn.source === 'button'
  if (!pressed && (turn.text || turn.attachments.length)) rows.push({ kind: 'user', value: `user:${turn.turnId}`, turn })
  // A toolbar press answers with its card; its spoken-style summary would only repeat what the card shows.
  const cardOnly = pressed && turn.cards.length > 0 && !turn.error
  if (!cardOnly && (turn.reply || !turn.done || turn.error)) rows.push({ kind: 'reply', value: `reply:${turn.turnId}`, turn })
  for (const id of turn.actionIds) {
    const action = actions[id]
    if (action) rows.push({ kind: 'action', value: `action:${id}`, action, capture: capture?.actionId === id ? capture : null })
  }
  for (const card of turn.cards) {
    if (BLOCK_CARDS.has(card.kind)) rows.push({ kind: 'card', value: `card:${card.id}`, card })
    else for (const row of card.rows) rows.push({ kind: 'card-row', value: `card:${card.id}:${row.id}`, card, row })
  }
  return rows
}

export function buildRows(s: RowState, x: RowInputs): RowGroup[] {
  const groups: RowGroup[] = []
  const typed = s.mode !== 'listening' ? s.draft.trim() : ''

  if (s.localConfirm) groups.push({ id: 'confirm', rows: [{ kind: 'local-confirm', value: 'local-confirm', confirm: s.localConfirm }] })

  if (s.pending) groups.push({ id: 'pending', rows: [{ kind: 'pending', value: 'pending', ...s.pending }] })

  const turns = s.turns.slice(-MAX_TURNS)
  const shown = s.showHistory ? turns : turns.slice(-1)
  const conversation: Row[] = []
  if (turns.length > 1 && !s.showHistory) conversation.push({ kind: 'history', value: 'history', count: turns.length - 1 })
  conversation.push(...shown.flatMap((turn) => turnRows(turn, s.actions, s.capture)))
  if (conversation.length) groups.push({ id: 'conversation', rows: conversation })

  const status: Row[] = [
    ...s.notices.map((notice): Row => ({ kind: 'notice', value: `notice:${notice.id}`, notice })),
    ...Object.entries(s.jobs)
      .filter(([, j]) => j.pct < 100)
      .map(([jobId, j]): Row => ({ kind: 'job', value: `job:${jobId}`, jobId, pct: j.pct, label: j.label, job: j.kind ?? null })),
  ]
  if (status.length) groups.push({ id: 'status', rows: status })

  if (typed && s.attachments.length === 0 && x.search) {
    // Typing finds documents by title and text (FR-BAR-8).
    if (x.search.length)
      groups.push({ id: 'search', rows: x.search.map((doc): Row => ({ kind: 'recent', value: `recent:${doc.id}`, doc })) })
  } else if (composerEmpty(s) && s.mode !== 'listening' && !s.pending && s.turns.length === 0 && x.recents.length) {
    groups.push({
      id: 'recents',
      rows: x.recents.slice(0, RECENTS_SHOWN).map((doc): Row => ({ kind: 'recent', value: `recent:${doc.id}`, doc })),
    })
  }
  return groups
}

export function allRows(groups: RowGroup[]): Row[] {
  return groups.flatMap((g) => g.rows)
}

export function findRow(groups: RowGroup[], value: string): Row | undefined {
  return allRows(groups).find((r) => r.value === value)
}

/** The row Enter acts on when the user has not moved the selection (09: "default row"). */
export function defaultSelection(groups: RowGroup[], s: Pick<RowState, 'selected' | 'mode' | 'draft' | 'attachments'>): string {
  const rows = allRows(groups)
  const has = (v: string) => rows.some((r) => r.value === v)
  if (s.selected && has(s.selected)) return s.selected
  // Something is typed or attached: Enter sends it, so nothing is selected until the user picks a row.
  if (!composerEmpty(s) && s.mode !== 'listening') return ''
  if (has('local-confirm')) return 'local-confirm'
  const pending = [...rows].reverse().find((r) => r.kind === 'action' && r.action.status === 'pending')
  if (pending) return pending.value
  const lastReply = [...rows].reverse().find((r) => r.kind === 'reply')
  if (lastReply) return lastReply.value
  return rows.find((r) => r.kind !== 'pending' && r.kind !== 'card' && r.kind !== 'history')?.value ?? ''
}

/**
 * Two rows draw the same thing: rebuilding the rows makes new row objects every time, but what a row shows comes
 * from the models it points at, and those keep their identity until they change. A row whose models are the
 * same doesn't re-render (`RowView` is memoized on this).
 */
export function sameRow(a: Row, b: Row): boolean {
  if (a === b) return true
  if (a.kind !== b.kind || a.value !== b.value) return false
  switch (a.kind) {
    case 'local-confirm':
      return a.confirm === (b as typeof a).confirm
    case 'pending': {
      const o = b as typeof a
      return a.text === o.text && a.attachments === o.attachments
    }
    case 'history':
      return a.count === (b as typeof a).count
    case 'user': {
      // A reply streaming in replaces the turn object; the "you said" line only shows what was said.
      const x = a.turn
      const y = (b as typeof a).turn
      return x === y || (x.text === y.text && x.attachments === y.attachments && x.recordingId === y.recordingId && x.source === y.source)
    }
    case 'reply':
      return a.turn === (b as typeof a).turn
    case 'action': {
      const o = b as typeof a
      return a.action === o.action && a.capture === o.capture
    }
    case 'card':
      return a.card === (b as typeof a).card
    case 'card-row': {
      const o = b as typeof a
      return a.card === o.card && a.row === o.row
    }
    case 'notice':
      return a.notice === (b as typeof a).notice
    case 'job': {
      const o = b as typeof a
      return a.pct === o.pct && a.label === o.label && a.job === o.job
    }
    case 'recent':
      return a.doc === (b as typeof a).doc
  }
}

export function cardRowEnabled(row: CardRow): boolean {
  const enabled = row.data?.enabled
  return typeof enabled === 'boolean' ? enabled : !!row.selected
}
