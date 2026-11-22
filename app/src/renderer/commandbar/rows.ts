/**
 * What the bar shows under the composer, as cmdk rows (09 § Command bar: "Rows, not screens"). Pure, so it
 * is unit-tested; the components render it and the key handlers look rows up by their cmdk `value`.
 *
 * The bar shows the present, not a log: the latest exchange, with earlier ones one press away.
 */
import type { CardRow } from '@shared/api/ws.gen'
import type { DocumentOut } from '@shared/api/client'

import type { ActionModel, BarState, CardModel, LocalConfirm, Notice, TurnAttachment, TurnModel } from './store'
import { composerEmpty } from './store'

export type Row =
  | { kind: 'local-confirm'; value: 'local-confirm'; confirm: LocalConfirm }
  | { kind: 'pending'; value: 'pending'; text: string; attachments: TurnAttachment[] }
  | { kind: 'history'; value: 'history'; count: number }
  | { kind: 'user'; value: string; turn: TurnModel }
  | { kind: 'reply'; value: string; turn: TurnModel }
  | { kind: 'action'; value: string; action: ActionModel }
  | { kind: 'card'; value: string; card: CardModel }
  | { kind: 'card-row'; value: string; card: CardModel; row: CardRow }
  | { kind: 'notice'; value: string; notice: Notice }
  | { kind: 'job'; value: string; jobId: string; pct: number; label: string }
  | { kind: 'recent'; value: string; doc: DocumentOut }

export interface RowGroup {
  id: string
  rows: Row[]
}

export interface RowInputs {
  recents: DocumentOut[]
  /** Results of `GET /documents?q=` for the typed text (null while not searching). */
  search: DocumentOut[] | null
}

const MAX_TURNS = 20
const RECENTS_SHOWN = 5
/** Cards drawn as one block (a figure, chips, a grid) rather than one row per item. */
const BLOCK_CARDS = new Set(['usage', 'where', 'presets'])

export function turnRows(turn: TurnModel, actions: BarState['actions']): Row[] {
  const rows: Row[] = []
  const pressed = turn.source === 'button'
  if (!pressed && (turn.text || turn.attachments.length)) rows.push({ kind: 'user', value: `user:${turn.turnId}`, turn })
  // A toolbar press answers with its card; its spoken-style summary would only repeat what the card shows.
  const cardOnly = pressed && turn.cards.length > 0 && !turn.error
  if (!cardOnly && (turn.reply || !turn.done || turn.error)) rows.push({ kind: 'reply', value: `reply:${turn.turnId}`, turn })
  for (const id of turn.actionIds) {
    const action = actions[id]
    if (action) rows.push({ kind: 'action', value: `action:${id}`, action })
  }
  for (const card of turn.cards) {
    if (BLOCK_CARDS.has(card.kind)) rows.push({ kind: 'card', value: `card:${card.id}`, card })
    else for (const row of card.rows) rows.push({ kind: 'card-row', value: `card:${card.id}:${row.id}`, card, row })
  }
  return rows
}

export function buildRows(s: BarState, x: RowInputs): RowGroup[] {
  const groups: RowGroup[] = []
  const typed = s.mode !== 'listening' ? s.draft.trim() : ''

  if (s.localConfirm) groups.push({ id: 'confirm', rows: [{ kind: 'local-confirm', value: 'local-confirm', confirm: s.localConfirm }] })

  if (s.pending) groups.push({ id: 'pending', rows: [{ kind: 'pending', value: 'pending', ...s.pending }] })

  const turns = s.turns.slice(-MAX_TURNS)
  const shown = s.showHistory ? turns : turns.slice(-1)
  const conversation: Row[] = []
  if (turns.length > 1 && !s.showHistory) conversation.push({ kind: 'history', value: 'history', count: turns.length - 1 })
  conversation.push(...shown.flatMap((turn) => turnRows(turn, s.actions)))
  if (conversation.length) groups.push({ id: 'conversation', rows: conversation })

  const status: Row[] = [
    ...s.notices.map((notice): Row => ({ kind: 'notice', value: `notice:${notice.id}`, notice })),
    ...Object.entries(s.jobs)
      .filter(([, j]) => j.pct < 100)
      .map(([jobId, j]): Row => ({ kind: 'job', value: `job:${jobId}`, jobId, pct: j.pct, label: j.label })),
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
export function defaultSelection(groups: RowGroup[], s: BarState): string {
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

export function cardRowEnabled(row: CardRow): boolean {
  const enabled = row.data?.enabled
  return typeof enabled === 'boolean' ? enabled : !!row.selected
}
