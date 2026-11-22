/** UI-only state for the command bar (03 § Shell: Zustand for UI state; server state is the engine's). */
import { createStore, type StoreApi } from 'zustand/vanilla'

import type { CardRow, SpeechDiff } from '@shared/api/ws.gen'

import type { AttachmentItem } from './composer/attachments'

/**
 * listening  – mic live, the transcript streams into the composer
 * finalizing – the utterance ended; waiting for its final transcript and turn
 * typing     – not listening (the composer is always editable, in every mode)
 * waiting    – a command was sent; waiting for the engine to start the turn
 */
export type BarMode = 'listening' | 'finalizing' | 'typing' | 'waiting'

export type ActionStatus = 'pending' | 'confirming' | 'cancelling' | 'confirmed' | 'cancelled' | 'failed'

export interface ActionModel {
  actionId: string
  turnId: string
  kind: string
  summary: string
  spoken: string | null
  preview: SpeechDiff | null
  details: Record<string, unknown>
  status: ActionStatus
  message: string | null
}

export type CardKind = 'voices' | 'rules' | 'usage' | 'recents' | 'models' | 'where' | 'presets' | 'settings' | 'queue' | 'info'

export interface CardModel {
  id: string
  turnId: string
  kind: CardKind
  title: string
  rows: CardRow[]
  data: Record<string, unknown>
}

export interface TurnAttachment {
  kind: string
  title: string
}

export interface TurnModel {
  turnId: string
  sessionId: string | null
  /** `button`: a toolbar press. It shows its result (a card) without a "you said" line. */
  source: 'typed' | 'spoken' | 'button'
  text: string
  attachments: TurnAttachment[]
  recordingId: string | null
  reply: string
  replyRecordingId: string | null
  done: boolean
  error: string | null
  /** Problem code of `error` ("unavailable": not part of this build; shown as information, not a failure). */
  errorCode?: string | null
  cards: CardModel[]
  actionIds: string[]
}

export interface Notice {
  id: string
  level: 'info' | 'warn' | 'error'
  text: string
}

/** A confirmation the bar asks for itself (no engine turn): downloading a local model. */
export interface LocalConfirm {
  kind: 'install_model'
  modelId: string
  title: string
  detail: string
}

export interface BarState {
  open: boolean
  mode: BarMode
  sessionId: string | null
  /** The live transcript while listening (it is also shown in the composer). */
  transcript: string
  /** What is typed in the composer ('' when it holds only badges). Drives document search. */
  draft: string
  /** The badges in the composer. */
  attachments: AttachmentItem[]
  /** The composer has several lines: Up/Down belong to its caret. */
  multiline: boolean
  /** Listening to add to what is already typed (the transcript is appended, not sent). */
  dictating: boolean
  /** A sent command (or utterance) shown until its turn arrives. */
  pending: { text: string; attachments: TurnAttachment[] } | null
  level: number
  speaking: boolean
  micError: string | null
  hint: string | null
  /** No speech for 5 s: show what is playing and recent documents. */
  idle: boolean
  context: { documentId?: string; conversationId?: string }
  turns: TurnModel[]
  /** Show earlier turns of this conversation, not only the latest. */
  showHistory: boolean
  actions: Record<string, ActionModel>
  notices: Notice[]
  jobs: Record<string, { pct: number; label: string }>
  localConfirm: LocalConfirm | null
  selected: string
  /** Whether the bar can listen (from the engine's capabilities). Null until known: the microphone is never
   *  opened (and the OS permission prompt never shown) before that. */
  canListen: boolean | null
  /** A spoken prompt is playing (the mic is paused meanwhile). */
  prompting: boolean
  busy: string | null
  /** Text for the polite live region (transcript, replies, action cards with key hints). */
  announcement: { id: number; text: string } | null
}

export const initialBarState: BarState = {
  open: false,
  mode: 'typing',
  sessionId: null,
  transcript: '',
  draft: '',
  attachments: [],
  multiline: false,
  dictating: false,
  pending: null,
  level: 0,
  speaking: false,
  micError: null,
  hint: null,
  idle: false,
  context: {},
  turns: [],
  showHistory: false,
  actions: {},
  notices: [],
  jobs: {},
  localConfirm: null,
  selected: '',
  canListen: null,
  prompting: false,
  busy: null,
  announcement: null,
}

export type BarStore = StoreApi<BarState>

export function createBarStore(overrides: Partial<BarState> = {}): BarStore {
  return createStore<BarState>(() => ({ ...initialBarState, ...overrides }))
}

export function pendingActions(state: BarState): ActionModel[] {
  return Object.values(state.actions).filter((a) => a.status === 'pending' || a.status === 'confirming')
}

export function latestPendingAction(state: BarState): ActionModel | null {
  for (let i = state.turns.length - 1; i >= 0; i--) {
    const turn = state.turns[i]
    if (!turn) continue
    for (let j = turn.actionIds.length - 1; j >= 0; j--) {
      const action = state.actions[turn.actionIds[j] ?? '']
      if (action && action.status === 'pending') return action
    }
  }
  return null
}

/** Nothing typed and nothing attached. */
export function composerEmpty(state: BarState): boolean {
  return !state.draft.trim() && state.attachments.length === 0
}
