/** UI-only state for the command bar (03 § Shell: Zustand for UI state; server state is the engine's). */
import { createStore, type StoreApi } from 'zustand/vanilla'

import type { CardRow, SpeechDiff } from '@shared/api/ws.gen'

import type { AttachmentItem } from './composer/attachments'

/**
 * One way at a time (07 § Voice loop details: turn-taking):
 * listening  – the microphone is live and sent to the engine; the transcript streams into the composer
 * finalizing – the utterance ended; waiting for its final transcript and turn
 * waiting    – a command was sent; waiting for the engine to answer
 * speaking   – the engine is talking (a reply, a prompt, a preview): nothing from the microphone is sent
 * typing     – the user is typing (the microphone is off; the conversation goes on)
 * recording  – a voice to clone is being recorded with its own capture path; the conversation microphone is closed
 * idle       – the conversation is over: the microphone is released and reading resumes
 * (The composer is editable in every mode.)
 */
export type BarMode = 'listening' | 'finalizing' | 'waiting' | 'speaking' | 'typing' | 'recording' | 'idle'

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

export type CardKind = 'voices' | 'rules' | 'usage' | 'recents' | 'models' | 'where' | 'settings' | 'queue' | 'info' | 'design'

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

/**
 * Recording a voice to clone, for the passage card (`clone_record` action) it belongs to: armed (waiting for the
 * engine to stop talking), recording, uploading, processing (the engine is making the voice), or failed (the card
 * offers to record again).
 */
export interface CaptureState {
  actionId: string
  phase: 'armed' | 'recording' | 'uploading' | 'processing' | 'failed'
  passageId: string
  passage: string
  language: string
  name: string
  error: string | null
  jobId: string | null
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
  /** The VAD hears the user speaking in the current utterance. (The mic *level* is not state:
   *  `controller.subscribeLevel`.) */
  userSpeaking: boolean
  /** The engine is saying something right now (`assistant.speech` started … ended), whatever the mode. */
  engineSpeaking: boolean
  micError: string | null
  hint: string | null
  context: { documentId?: string; conversationId?: string }
  turns: TurnModel[]
  /** Show earlier turns of this conversation, not only the latest. */
  showHistory: boolean
  actions: Record<string, ActionModel>
  notices: Notice[]
  jobs: Record<string, { pct: number; label: string; kind?: string | null }>
  localConfirm: LocalConfirm | null
  selected: string
  /** Whether the bar can listen: the engine's capabilities once known, before that what the shell remembered
   *  (`BarOpenedEvent.canListen`). The microphone opens on every open unless this is `false`. */
  canListen: boolean | null
  busy: string | null
  /** Text for the polite live region (transcript, replies, action cards with key hints). */
  announcement: { id: number; text: string } | null
  /** A voice being recorded to clone (null otherwise). */
  capture: CaptureState | null
}

export const initialBarState: BarState = {
  open: false,
  mode: 'idle',
  sessionId: null,
  transcript: '',
  draft: '',
  attachments: [],
  multiline: false,
  dictating: false,
  pending: null,
  userSpeaking: false,
  engineSpeaking: false,
  micError: null,
  hint: null,
  context: {},
  turns: [],
  showHistory: false,
  actions: {},
  notices: [],
  jobs: {},
  localConfirm: null,
  selected: '',
  canListen: null,
  busy: null,
  announcement: null,
  capture: null,
}

export type BarStore = StoreApi<BarState>

export function createBarStore(overrides: Partial<BarState> = {}): BarStore {
  return createStore<BarState>(() => ({ ...initialBarState, ...overrides }))
}

export function pendingActions(state: BarState): ActionModel[] {
  return Object.values(state.actions).filter((a) => a.status === 'pending' || a.status === 'confirming')
}

export function latestPendingAction(state: Pick<BarState, 'turns' | 'actions'>): ActionModel | null {
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
export function composerEmpty(state: Pick<BarState, 'draft' | 'attachments'>): boolean {
  return !state.draft.trim() && state.attachments.length === 0
}
