/**
 * What the bar shows, as cmdk rows (09 § Command bar: "Rows, not screens"). Pure, so it is unit-tested;
 * the components render it and the key handlers look rows up by their cmdk `value`.
 */
import type { CardRow } from '@shared/api/ws.gen'
import type { DocumentOut, PlaybackState } from '@shared/api/client'

import type { ActionModel, BarState, CardModel, DroppedItem, LocalConfirm, Notice, TurnModel } from './store'

export type DroppedAction = 'read' | 'describe' | 'summarize' | 'open'
export type SuggestionId = 'read-clipboard' | 'open-file' | 'show-voices' | 'spend'

export type Row =
  | { kind: 'local-confirm'; value: 'local-confirm'; confirm: LocalConfirm }
  | { kind: 'dropped'; value: string; action: DroppedAction; item: DroppedItem; label: string; isDefault: boolean }
  | { kind: 'submit'; value: 'submit'; text: string }
  | { kind: 'pending'; value: 'pending'; text: string }
  | { kind: 'user'; value: string; turn: TurnModel }
  | { kind: 'reply'; value: string; turn: TurnModel }
  | { kind: 'action'; value: string; action: ActionModel }
  | { kind: 'card'; value: string; card: CardModel }
  | { kind: 'card-row'; value: string; card: CardModel; row: CardRow }
  | { kind: 'notice'; value: string; notice: Notice }
  | { kind: 'job'; value: string; jobId: string; pct: number; label: string }
  | { kind: 'now-playing'; value: 'now-playing'; playback: PlaybackState }
  | { kind: 'recent'; value: string; doc: DocumentOut }
  | { kind: 'suggestion'; value: string; id: SuggestionId; label: string }

export interface RowGroup {
  id: string
  heading?: string
  rows: Row[]
}

export interface RowInputs {
  playback: PlaybackState
  recents: DocumentOut[]
  /** Results of `GET /documents?q=` for the typed text (null while not searching). */
  search: DocumentOut[] | null
}

export const SUGGESTIONS: { id: SuggestionId; label: string }[] = [
  { id: 'read-clipboard', label: 'Read clipboard' },
  { id: 'open-file', label: 'Open file…' },
  { id: 'show-voices', label: 'Show voices' },
  { id: 'spend', label: 'How much have I spent?' },
]

const MAX_TURNS = 20

export function droppedRows(item: DroppedItem): Row[] {
  const actions: { action: DroppedAction; label: string }[] = [{ action: 'read', label: 'Read it' }]
  if (item.kind === 'file' && item.isImage) actions.push({ action: 'describe', label: 'Describe' })
  actions.push({ action: 'summarize', label: 'Summarize' }, { action: 'open', label: 'Open in reader' })
  return actions.map(({ action, label }, i) => ({
    kind: 'dropped',
    value: `dropped:${action}`,
    action,
    item,
    label,
    isDefault: i === 0,
  }))
}

export function turnRows(turn: TurnModel, actions: BarState['actions']): Row[] {
  const rows: Row[] = []
  if (turn.text) rows.push({ kind: 'user', value: `user:${turn.turnId}`, turn })
  if (turn.reply || !turn.done || turn.error) rows.push({ kind: 'reply', value: `reply:${turn.turnId}`, turn })
  for (const id of turn.actionIds) {
    const action = actions[id]
    if (action) rows.push({ kind: 'action', value: `action:${id}`, action })
  }
  for (const card of turn.cards) {
    rows.push({ kind: 'card', value: `card:${card.id}`, card })
    if (card.kind !== 'usage') {
      for (const row of card.rows) rows.push({ kind: 'card-row', value: `card:${card.id}:${row.id}`, card, row })
    }
  }
  return rows
}

export function buildRows(s: BarState, x: RowInputs): RowGroup[] {
  const groups: RowGroup[] = []
  const typingText = s.mode === 'typing' ? s.text.trim() : ''

  if (s.localConfirm) groups.push({ id: 'confirm', rows: [{ kind: 'local-confirm', value: 'local-confirm', confirm: s.localConfirm }] })

  if (s.dropped) groups.push({ id: 'dropped', heading: 'Dropped', rows: droppedRows(s.dropped) })

  const input: Row[] = []
  if (typingText) input.push({ kind: 'submit', value: 'submit', text: typingText })
  if (s.pendingText) input.push({ kind: 'pending', value: 'pending', text: s.pendingText })
  if (input.length) groups.push({ id: 'input', rows: input })

  const conversation = s.turns.slice(-MAX_TURNS).flatMap((t) => turnRows(t, s.actions))
  if (conversation.length) groups.push({ id: 'conversation', heading: 'This session', rows: conversation })

  const status: Row[] = [
    ...s.notices.map((notice): Row => ({ kind: 'notice', value: `notice:${notice.id}`, notice })),
    ...Object.entries(s.jobs)
      .filter(([, j]) => j.pct < 100)
      .map(([jobId, j]): Row => ({ kind: 'job', value: `job:${jobId}`, jobId, pct: j.pct, label: j.label })),
  ]
  if (status.length) groups.push({ id: 'status', rows: status })

  const ambient = s.idle || s.mode === 'typing' || s.turns.length > 0
  if (ambient && x.playback.document_id && x.playback.state !== 'idle') {
    groups.push({ id: 'now', rows: [{ kind: 'now-playing', value: 'now-playing', playback: x.playback }] })
  }

  if (typingText && x.search) {
    if (x.search.length)
      groups.push({
        id: 'search',
        heading: 'Documents',
        rows: x.search.map((doc): Row => ({ kind: 'recent', value: `recent:${doc.id}`, doc })),
      })
  } else if (ambient && !typingText && x.recents.length) {
    groups.push({
      id: 'recents',
      heading: 'Recent',
      rows: x.recents.slice(0, 10).map((doc): Row => ({ kind: 'recent', value: `recent:${doc.id}`, doc })),
    })
  }

  if (s.idle && !typingText && !s.dropped && s.turns.length === 0) {
    groups.push({
      id: 'suggestions',
      heading: 'Try',
      rows: SUGGESTIONS.map((sug): Row => ({ kind: 'suggestion', value: `suggest:${sug.id}`, ...sug })),
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
  if (has('local-confirm')) return 'local-confirm'
  const pending = [...rows].reverse().find((r) => r.kind === 'action' && r.action.status === 'pending')
  if (pending) return pending.value
  if (has('dropped:read')) return 'dropped:read'
  if (has('submit')) return 'submit'
  const lastReply = [...rows].reverse().find((r) => r.kind === 'reply')
  if (lastReply) return lastReply.value
  return rows.find((r) => r.kind !== 'pending')?.value ?? ''
}

export function cardRowEnabled(row: CardRow): boolean {
  const enabled = row.data?.enabled
  return typeof enabled === 'boolean' ? enabled : !!row.selected
}
