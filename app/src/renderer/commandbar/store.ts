/** UI-only state for the command bar (03 § Shell: Zustand for UI state; server state is the engine's). */
import { createStore, type StoreApi } from 'zustand/vanilla'

import type { CardRow, SpeechDiff } from '@shared/api/ws.gen'
import type { UserSettings } from '@shared/api/client'

/**
 * listening  – mic live, partials replace the input in place
 * finalizing – `mic.stop {submit: true}` sent; waiting for `stt.final` / `assistant.turn`
 * typing     – the input is editable text; Enter submits it as a `command`
 * waiting    – a command was submitted; waiting for the engine to start the turn
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

export type CardKind = 'voices' | 'rules' | 'usage' | 'recents' | 'models' | 'where' | 'keys' | 'presets' | 'info'

export interface CardModel {
  id: string
  turnId: string
  kind: CardKind
  title: string
  rows: CardRow[]
  data: Record<string, unknown>
}

export interface TurnModel {
  turnId: string
  sessionId: string | null
  source: 'typed' | 'spoken'
  text: string
  recordingId: string | null
  reply: string
  replyRecordingId: string | null
  done: boolean
  error: string | null
  /** Problem code of `error` (e.g. "missing_key": shown as guidance with a key action, not as a failure). */
  errorCode?: string | null
  cards: CardModel[]
  actionIds: string[]
}

export interface Notice {
  id: string
  level: 'info' | 'warn' | 'error'
  text: string
}

export type DroppedItem =
  | { kind: 'file'; file: File; name: string; isImage: boolean }
  | { kind: 'url'; url: string }
  | { kind: 'text'; text: string }

export type LocalConfirm =
  | { kind: 'install_model'; modelId: string; title: string; detail: string }
  | { kind: 'estimate'; title: string; detail: string; retryId: string }

export interface BarState {
  open: boolean
  mode: BarMode
  sessionId: string | null
  /** Input value: the live transcript while listening, editable text while typing. */
  text: string
  /** Typed command shown until its turn arrives. */
  pendingText: string | null
  level: number
  speaking: boolean
  micError: string | null
  hint: string | null
  /** No speech for 5 s: show Now playing, Recents and suggestions. */
  idle: boolean
  context: { documentId?: string }
  turns: TurnModel[]
  actions: Record<string, ActionModel>
  notices: Notice[]
  jobs: Record<string, { pct: number; label: string }>
  dropped: DroppedItem | null
  localConfirm: LocalConfirm | null
  keyEntry: boolean
  selected: string
  settings: UserSettings | null
  /** False when no speech-to-text provider is set up (no OpenAI/Gemini key): the bar opens typing. */
  canListen: boolean
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
  text: '',
  pendingText: null,
  level: 0,
  speaking: false,
  micError: null,
  hint: null,
  idle: false,
  context: {},
  turns: [],
  actions: {},
  notices: [],
  jobs: {},
  dropped: null,
  localConfirm: null,
  keyEntry: false,
  selected: '',
  settings: null,
  canListen: true,
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
