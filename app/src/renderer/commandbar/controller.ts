/**
 * The command bar's voice/typing state machine (09 § Command bar, 07 § Voice loop, FR-BAR-1..4).
 *
 * - Opens listening: `mic.start`, then 16 kHz PCM16 binary frames until `mic.stop`.
 * - Partials replace the input in place; `stt.final` replaces them.
 * - Any keystroke stops the mic (`mic.stop {submit: false}`) and leaves the transcript editable.
 * - Tab toggles typing ↔ listening.
 * - End of utterance: 800 ms of silence (energy VAD, `voice_input.end_of_speech_ms`), releasing a held
 *   hotkey (push-to-talk), or Enter. Each path submits exactly once per utterance.
 * - Typed text is submitted as a WS `command`.
 */
import type { BarOpenedEvent } from '@shared/ipc-channels'
import type {
  AssistantAction,
  AssistantActionResolved,
  AssistantCard,
  AssistantDelta,
  AssistantDone,
  AssistantTurn,
  ClientMessage,
  ErrorMsg,
  JobProgress,
  SttFinal,
  SttPartial,
  UsageAlert,
} from '@shared/api/ws.gen'

import type { MicSource } from '@/audio/mic'
import type { SocketLike } from '@/engine'
import { EnergyVad, toDb } from '@/audio/vad'
import { latestPendingAction, type BarState, type BarStore, type TurnModel } from './store'

export const IDLE_SUGGESTIONS_MS = 5000
/** Without any speech the mic turns itself off (privacy and STT cost); Tab turns it back on. */
export const MIC_IDLE_OFF_MS = 15_000
/** Hotkey held at least this long before release = push-to-talk. */
export const PTT_MIN_HOLD_MS = 350
/** Give up waiting for the engine to start a turn. */
export const TURN_START_TIMEOUT_MS = 20_000

export type SubmitPath = 'enter' | 'vad' | 'ptt'

export interface BarDeps {
  send(message: ClientMessage): boolean
  sendAudio(pcm: Int16Array): boolean
  mic: MicSource
  /** Mirrors listening into the menu-bar icon. */
  setListening(listening: boolean): void
  endOfSpeechMs(): number
  newSessionId(): string
  now(): number
}

/** Onboarding answers by voice locally instead of submitting to the assistant. */
export type VoiceInterceptor = (transcript: string) => 'listen-again' | 'stop'

function meterLevel(level: number): number {
  // -60 dBFS → 0, -12 dBFS → 1
  return Math.max(0, Math.min(1, (toDb(level) + 60) / 48))
}

let noticeSeq = 0
let announceSeq = 0

export class BarController {
  private readonly vad = new EnergyVad()
  private idleTimer: ReturnType<typeof setTimeout> | null = null
  private micOffTimer: ReturnType<typeof setTimeout> | null = null
  private turnTimer: ReturnType<typeof setTimeout> | null = null
  private submittedSession: string | null = null
  private ownSessions = new Set<string>()
  private openedAt = 0
  private viaHotkey = false
  private hotkeyHandled = false
  private interceptor: VoiceInterceptor | null = null

  constructor(
    readonly store: BarStore,
    private readonly deps: BarDeps,
  ) {}

  private get s(): BarState {
    return this.store.getState()
  }

  private set(patch: Partial<BarState> | ((s: BarState) => Partial<BarState>)): void {
    this.store.setState(patch)
  }

  // ---------------------------------------------------------------- open / close

  open(event: BarOpenedEvent): void {
    const context = event.documentId ? { documentId: event.documentId } : {}
    this.viaHotkey = event.viaHotkey
    this.hotkeyHandled = false
    this.openedAt = this.deps.now()
    if (this.s.open) {
      this.set({ context })
      if (event.listen && this.s.mode === 'typing' && !this.s.text) this.startListening()
      return
    }
    this.set({
      open: true,
      context,
      idle: false,
      micError: null,
      hint: null,
      dropped: null,
      localConfirm: null,
      keyEntry: false,
      text: '',
      pendingText: null,
    })
    this.deps.send({ type: 'bar.state', open: true })
    if (event.listen && this.s.canListen) this.startListening()
    else this.set({ mode: 'typing' })
    this.armIdle()
  }

  close(): void {
    if (!this.s.open) return
    if (this.s.mode === 'listening') this.stopMic(false)
    this.deps.mic.stop()
    this.clearTimers()
    this.deps.send({ type: 'bar.state', open: false })
    this.deps.setListening(false)
    this.set({
      open: false,
      mode: 'typing',
      text: '',
      pendingText: null,
      level: 0,
      speaking: false,
      idle: false,
      dropped: null,
      localConfirm: null,
      keyEntry: false,
      hint: null,
    })
  }

  /** Speak something without hearing ourselves: the mic is off while `speak` runs, then listening resumes. */
  async withMicPaused(speak: () => Promise<unknown>): Promise<void> {
    const wasListening = this.s.mode === 'listening'
    if (wasListening) {
      this.stopMic(false)
      this.set({ mode: 'typing', level: 0, speaking: false })
    }
    try {
      await speak()
    } catch {
      // Speaking is a courtesy; the prompt is also on screen.
    }
    if (wasListening && this.s.open && this.s.mode === 'typing' && !this.s.text && this.s.canListen) this.startListening()
  }

  setInterceptor(fn: VoiceInterceptor | null): void {
    this.interceptor = fn
  }

  // ---------------------------------------------------------------- listening

  startListening(): void {
    const sessionId = this.deps.newSessionId()
    this.vad.reset()
    this.vad.endOfSpeechMs = this.deps.endOfSpeechMs()
    const ok = this.deps.send({
      type: 'mic.start',
      session_id: sessionId,
      mode: 'auto',
      context: { document_id: this.s.context.documentId ?? null },
    })
    if (!ok) {
      this.set({ mode: 'typing', micError: 'Not connected to the engine yet. Type instead, or try again.' })
      return
    }
    this.ownSessions.add(sessionId)
    // Listening again with the bar still open pauses reading again (07 § Ducking); idempotent on open.
    this.deps.send({ type: 'bar.state', open: true })
    this.set({ mode: 'listening', sessionId, text: '', level: 0, speaking: false, micError: null, hint: null })
    this.deps.setListening(true)
    void this.deps.mic.start({
      onPcm: (pcm) => {
        const s = this.s
        if (s.sessionId === sessionId && s.mode === 'listening') this.deps.sendAudio(pcm)
      },
      onLevel: (level, ms) => this.onLevel(sessionId, level, ms),
      onError: (err) => this.onMicError(sessionId, err),
    })
    this.armMicOff()
  }

  private stopMic(submit: boolean): void {
    const sessionId = this.s.sessionId
    this.deps.mic.stop()
    this.deps.setListening(false)
    if (this.micOffTimer) clearTimeout(this.micOffTimer)
    this.micOffTimer = null
    if (sessionId) this.deps.send({ type: 'mic.stop', session_id: sessionId, submit })
  }

  private onLevel(sessionId: string, level: number, durationMs: number): void {
    const s = this.s
    if (s.sessionId !== sessionId || s.mode !== 'listening') return
    const event = this.vad.push(level, durationMs)
    this.set({ level: meterLevel(level) })
    if (event === 'speech-start') {
      this.set({ speaking: true, idle: false })
      if (this.idleTimer) clearTimeout(this.idleTimer)
      if (this.micOffTimer) clearTimeout(this.micOffTimer)
      this.idleTimer = this.micOffTimer = null
    } else if (event === 'speech-end') {
      this.submit('vad')
    }
  }

  private onMicError(sessionId: string, err: Error): void {
    if (this.s.sessionId !== sessionId || this.s.mode !== 'listening') return
    this.stopMic(false)
    this.set({ mode: 'typing', micError: err.message, level: 0, speaking: false })
  }

  /** Any keystroke while listening turns the transcript into editable text (FR-BAR-3). */
  keystroke(): void {
    if (this.s.mode !== 'listening') return
    this.stopMic(false)
    this.set({ mode: 'typing', speaking: false, level: 0, idle: false })
  }

  /** Tab (09 § Shortcuts). */
  toggle(): void {
    const { mode } = this.s
    if (mode === 'typing') this.startListening()
    else if (mode === 'listening') {
      this.stopMic(false)
      this.set({ mode: 'typing', speaking: false, level: 0 })
    }
  }

  /** The global hotkey was released (keyup of Space/Option in the focused bar). */
  hotkeyReleased(): void {
    if (!this.viaHotkey || this.hotkeyHandled) return
    this.hotkeyHandled = true
    if (this.deps.now() - this.openedAt >= PTT_MIN_HOLD_MS && this.s.mode === 'listening') this.submit('ptt')
  }

  get openedViaHotkey(): boolean {
    return this.viaHotkey && !this.hotkeyHandled
  }

  // ---------------------------------------------------------------- submit

  /** Returns true when this call submitted; every utterance submits at most once (FR-BAR-4). */
  submit(path: SubmitPath): boolean {
    const s = this.s
    if (s.mode === 'listening') {
      if (!s.sessionId || this.submittedSession === s.sessionId) return false
      const heard = this.vad.hasSpeech || s.text.trim().length > 0
      if (!heard) {
        if (path === 'ptt') {
          // Held and released without saying anything: cancel quietly.
          this.stopMic(false)
          this.set({ mode: 'typing', level: 0, speaking: false })
        }
        return false
      }
      if (this.interceptor) {
        const transcript = s.text
        this.submittedSession = s.sessionId
        this.stopMic(false)
        this.set({ mode: 'typing', level: 0, speaking: false })
        if (this.interceptor(transcript) === 'listen-again' && this.s.open) this.startListening()
        return true
      }
      this.submittedSession = s.sessionId
      this.stopMic(true)
      this.set({ mode: 'finalizing', level: 0, speaking: false, idle: false })
      this.armTurnTimeout()
      return true
    }
    if (s.mode === 'typing' && path === 'enter') return this.submitText(s.text)
    return false
  }

  /** Submit typed text (or a suggestion) as a `command`. */
  submitText(raw: string): boolean {
    const text = raw.trim()
    if (!text || this.s.mode === 'waiting' || this.s.mode === 'finalizing') return false
    if (this.s.mode === 'listening') this.stopMic(false)
    const sessionId = this.deps.newSessionId()
    const ok = this.deps.send({
      type: 'command',
      text,
      source: 'typed',
      session_id: sessionId,
      context: { document_id: this.s.context.documentId ?? null },
    })
    if (!ok) {
      this.notice('error', 'Not connected to the engine. Your command was not sent.')
      return false
    }
    this.ownSessions.add(sessionId)
    this.submittedSession = sessionId
    this.set({ mode: 'waiting', sessionId, text: '', pendingText: text, idle: false, dropped: null })
    this.armTurnTimeout()
    return true
  }

  setText(text: string): void {
    if (this.s.mode === 'typing') this.set({ text, idle: text ? false : this.s.idle })
  }

  // ---------------------------------------------------------------- engine events

  onSttPartial(msg: SttPartial): void {
    const s = this.s
    if (msg.session_id !== s.sessionId || s.mode !== 'listening') return
    this.set({ text: msg.text, idle: false })
    if (this.idleTimer) clearTimeout(this.idleTimer)
    if (this.micOffTimer) clearTimeout(this.micOffTimer)
    this.idleTimer = this.micOffTimer = null
  }

  onSttFinal(msg: SttFinal): void {
    const s = this.s
    if (msg.session_id !== s.sessionId) return
    if (s.mode === 'listening') {
      // The engine ended the utterance itself: it submits, so we must not send mic.stop {submit: true}.
      this.submittedSession = msg.session_id
      this.deps.mic.stop()
      this.deps.setListening(false)
      this.set({ mode: 'finalizing', text: msg.text, level: 0, speaking: false })
      this.armTurnTimeout()
    } else if (s.mode === 'finalizing') {
      this.set({ text: msg.text })
    } else {
      return
    }
    if (msg.text.trim()) this.announce(`You said: ${msg.text}`)
    if (!msg.text.trim()) {
      this.clearTurnTimeout()
      this.set({ mode: 'typing', text: '', hint: "Didn't catch that. Press Tab to try again, or type." })
      this.armIdle()
    }
  }

  onTurn(msg: AssistantTurn): void {
    const turn: TurnModel = {
      turnId: msg.turn_id,
      sessionId: msg.session_id ?? null,
      source: msg.source,
      text: msg.text,
      recordingId: msg.recording_id ?? null,
      reply: '',
      replyRecordingId: null,
      done: false,
      error: null,
      cards: [],
      actionIds: [],
    }
    this.set((s) => {
      const exists = s.turns.some((t) => t.turnId === msg.turn_id)
      const turns = exists
        ? s.turns.map((t) => (t.turnId === msg.turn_id ? { ...t, ...turn, cards: t.cards, actionIds: t.actionIds } : t))
        : [...s.turns, turn]
      return { turns, selected: `reply:${msg.turn_id}` }
    })
    const s = this.s
    if (msg.session_id && msg.session_id === s.sessionId && (s.mode === 'finalizing' || s.mode === 'waiting')) {
      this.clearTurnTimeout()
      this.set({ mode: 'waiting', text: '', pendingText: null })
    }
  }

  onDelta(msg: AssistantDelta): void {
    this.updateTurn(msg.turn_id, (t) => ({ reply: t.reply + msg.text }))
  }

  onCard(msg: AssistantCard): void {
    this.updateTurn(msg.turn_id, (t) => ({
      cards: [
        ...t.cards,
        {
          id: `${msg.turn_id}:${t.cards.length}`,
          turnId: msg.turn_id,
          kind: msg.kind,
          title: msg.title,
          rows: msg.rows ?? [],
          data: msg.data ?? {},
        },
      ],
    }))
    if (msg.kind === 'keys') this.set({ keyEntry: true })
  }

  onAction(msg: AssistantAction): void {
    this.ensureTurn(msg.turn_id)
    this.set((s) => ({
      actions: {
        ...s.actions,
        [msg.action_id]: {
          actionId: msg.action_id,
          turnId: msg.turn_id,
          kind: msg.kind,
          summary: msg.summary,
          spoken: msg.spoken ?? null,
          preview: msg.preview ?? null,
          details: msg.details ?? {},
          status: 'pending',
          message: null,
        },
      },
      turns: s.turns.map((t) =>
        t.turnId === msg.turn_id && !t.actionIds.includes(msg.action_id)
          ? { ...t, actionIds: [...t.actionIds, msg.action_id] }
          : t,
      ),
      selected: `action:${msg.action_id}`,
    }))
    this.announce(`${msg.summary}. Press Enter to confirm, or Escape to cancel.`)
  }

  onActionResolved(msg: AssistantActionResolved): void {
    this.setActionStatus(msg.action_id, msg.status, msg.message)
  }

  onDone(msg: AssistantDone): void {
    const errorText =
      msg.error && typeof msg.error === 'object'
        ? String((msg.error as Record<string, unknown>).detail ?? (msg.error as Record<string, unknown>).title ?? 'Something went wrong.')
        : null
    this.updateTurn(msg.turn_id, (t) => ({
      done: true,
      reply: msg.text ? msg.text : t.reply,
      replyRecordingId: msg.reply_recording_id ?? null,
      error: errorText,
    }))
    const s = this.s
    const turn = s.turns.find((t) => t.turnId === msg.turn_id)
    if (turn) this.announce(turn.error ?? turn.reply)
    const ours = !!turn?.sessionId && turn.sessionId === s.sessionId
    if (!ours || s.mode !== 'waiting') return
    const hasPending = !!turn && turn.actionIds.some((id) => s.actions[id]?.status === 'pending')
    if (hasPending && turn?.source === 'spoken' && s.open) {
      // Wait for a spoken "yes" / "no" (the engine resolves it); Enter and Esc still work.
      this.startListening()
    } else {
      this.set({ mode: 'typing', text: '' })
      this.armIdle()
    }
  }

  onUsageAlert(msg: UsageAlert): void {
    this.notice(msg.level === 'stop' ? 'error' : 'warn', msg.message)
  }

  onError(msg: ErrorMsg): void {
    const p = msg.problem
    const text = typeof p.detail === 'string' && p.detail ? p.detail : typeof p.title === 'string' ? p.title : 'Engine error.'
    if (p.code === 'missing_key' && this.s.mode === 'listening') {
      // No speech-to-text set up: not an error the user caused. Type instead; the placeholder says why.
      this.stopMic(false)
      this.set({ mode: 'typing', canListen: false, level: 0, speaking: false })
      this.announce(text)
      return
    }
    this.notice('error', text)
    if (this.s.mode === 'finalizing' || this.s.mode === 'waiting') {
      this.clearTurnTimeout()
      this.set({ mode: 'typing', pendingText: null })
    }
  }

  onJobProgress(msg: JobProgress): void {
    this.set((s) => ({ jobs: { ...s.jobs, [msg.job_id]: { pct: msg.pct, label: msg.label } } }))
  }

  // ---------------------------------------------------------------- helpers for the UI

  setActionStatus(actionId: string, status: BarState['actions'][string]['status'], message?: string | null): void {
    this.set((s) => {
      const action = s.actions[actionId]
      if (!action) return {}
      return { actions: { ...s.actions, [actionId]: { ...action, status, message: message ?? action.message } } }
    })
  }

  notice(level: 'info' | 'warn' | 'error', text: string): void {
    const id = `n${++noticeSeq}`
    this.set((s) => ({ notices: [...s.notices.slice(-4), { id, level, text }] }))
    if (level !== 'info') this.announce(text)
  }

  announce(text: string): void {
    if (text.trim()) this.set({ announcement: { id: ++announceSeq, text } })
  }

  dismissNotice(id: string): void {
    this.set((s) => ({ notices: s.notices.filter((n) => n.id !== id) }))
  }

  latestPendingAction() {
    return latestPendingAction(this.s)
  }

  // ---------------------------------------------------------------- internals

  private ensureTurn(turnId: string): void {
    if (this.s.turns.some((t) => t.turnId === turnId)) return
    this.set((s) => ({
      turns: [
        ...s.turns,
        {
          turnId,
          sessionId: null,
          source: 'typed',
          text: '',
          recordingId: null,
          reply: '',
          replyRecordingId: null,
          done: false,
          error: null,
          cards: [],
          actionIds: [],
        },
      ],
    }))
  }

  private updateTurn(turnId: string, fn: (t: TurnModel) => Partial<TurnModel>): void {
    this.ensureTurn(turnId)
    this.set((s) => ({ turns: s.turns.map((t) => (t.turnId === turnId ? { ...t, ...fn(t) } : t)) }))
  }

  private armIdle(): void {
    if (this.idleTimer) clearTimeout(this.idleTimer)
    this.idleTimer = setTimeout(() => {
      this.idleTimer = null
      if (this.s.open && !this.s.speaking && !this.s.text) this.set({ idle: true })
    }, IDLE_SUGGESTIONS_MS)
  }

  private armMicOff(): void {
    if (this.micOffTimer) clearTimeout(this.micOffTimer)
    this.micOffTimer = setTimeout(() => {
      this.micOffTimer = null
      const s = this.s
      if (s.mode === 'listening' && !this.vad.hasSpeech && !s.text) {
        this.stopMic(false)
        this.set({ mode: 'typing', level: 0, hint: 'Microphone off. Press Tab to talk.' })
      }
    }, MIC_IDLE_OFF_MS)
  }

  private armTurnTimeout(): void {
    this.clearTurnTimeout()
    this.turnTimer = setTimeout(() => {
      this.turnTimer = null
      const s = this.s
      if (s.mode === 'finalizing' || s.mode === 'waiting') {
        this.set({ mode: 'typing', pendingText: null })
        this.notice('warn', 'The engine did not answer. Try again.')
      }
    }, TURN_START_TIMEOUT_MS)
  }

  private clearTurnTimeout(): void {
    if (this.turnTimer) clearTimeout(this.turnTimer)
    this.turnTimer = null
  }

  private clearTimers(): void {
    if (this.idleTimer) clearTimeout(this.idleTimer)
    if (this.micOffTimer) clearTimeout(this.micOffTimer)
    this.clearTurnTimeout()
    this.idleTimer = this.micOffTimer = null
  }
}

/** Connect engine WS events to the controller. Returns an unsubscribe function. */
export function wireBarEvents(socket: Pick<SocketLike, 'on'>, controller: BarController): () => void {
  const offs = [
    socket.on('stt.partial', (m) => controller.onSttPartial(m)),
    socket.on('stt.final', (m) => controller.onSttFinal(m)),
    socket.on('assistant.turn', (m) => controller.onTurn(m)),
    socket.on('assistant.delta', (m) => controller.onDelta(m)),
    socket.on('assistant.card', (m) => controller.onCard(m)),
    socket.on('assistant.action', (m) => controller.onAction(m)),
    socket.on('assistant.action_resolved', (m) => controller.onActionResolved(m)),
    socket.on('assistant.done', (m) => controller.onDone(m)),
    socket.on('usage.alert', (m) => controller.onUsageAlert(m)),
    socket.on('job.progress', (m) => controller.onJobProgress(m)),
    socket.on('error', (m) => controller.onError(m)),
  ]
  return () => offs.forEach((off) => off())
}
