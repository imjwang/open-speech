/**
 * The command bar's voice/typing state machine (09 § Command bar, 07 § Voice loop, FR-BAR-1..4).
 *
 * - Opens listening: `mic.start`, then 16 kHz PCM16 binary frames until `mic.stop`.
 * - The transcript streams into the composer as it is heard; `stt.final` settles it.
 * - Any keystroke stops the mic (`mic.stop {submit: false}`) and leaves the transcript as editable text.
 * - Tab toggles typing ↔ listening.
 * - End of utterance: 800 ms of silence (energy VAD, `voice_input.end_of_speech_ms`), releasing a held
 *   hotkey (push-to-talk), or Enter. Each path submits exactly once per utterance.
 * - Two ways to listen. With nothing typed, speech is a **command**: it is sent when the utterance ends,
 *   together with whatever is attached. With text already typed, speech is **dictation**: it is added to the
 *   text and nothing is sent until Enter.
 * - The composer is editable in every mode: what is typed while an utterance is still being processed is
 *   kept for the next command, never dropped.
 */
import type { Attachment } from '@shared/api/client'
import { t } from '@shared/i18n'
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
import { labelOf, toWire, type AttachmentItem } from './composer/attachments'
import { composerEmpty, latestPendingAction, type BarState, type BarStore, type TurnAttachment, type TurnModel } from './store'

export const IDLE_SUGGESTIONS_MS = 5000
/** Without any speech the mic turns itself off (privacy); Tab or the orb turns it back on. */
export const MIC_IDLE_OFF_MS = 15_000
/** When the idle timeout lands on the first instant of speech, how long until the next look. */
export const MIC_OFF_RECHECK_MS = 1_000
/** Hotkey held at least this long before release = push-to-talk. */
export const PTT_MIN_HOLD_MS = 350
/** Give up waiting for the engine to start a turn. */
export const TURN_START_TIMEOUT_MS = 20_000

export type SubmitPath = 'enter' | 'vad' | 'ptt'

/** What the controller may do to the composer (the Lexical editor); absent until it has mounted. */
export interface ComposerPort {
  clear(): void
  insertAttachments(items: AttachmentItem[]): void
  setDictation(text: string | null): void
  dropDictation(): void
}

export interface BarDeps {
  send(message: ClientMessage): boolean
  sendAudio(pcm: Int16Array): boolean
  mic: MicSource
  /** Mirrors listening into the menu-bar icon. */
  setListening(listening: boolean): void
  endOfSpeechMs(): number
  newSessionId(): string
  /** One id per time the bar is opened: everything said until it closes is one conversation. */
  newConversationId(): string
  now(): number
}

/** Onboarding answers by voice locally instead of submitting to the assistant. */
export type VoiceInterceptor = (transcript: string) => 'listen-again' | 'stop'

function meterLevel(level: number): number {
  // -60 dBFS → 0, -12 dBFS → 1
  return Math.max(0, Math.min(1, (toDb(level) + 60) / 48))
}

function describe(items: AttachmentItem[]): TurnAttachment[] {
  return items.map((item) => ({ kind: item.kind === 'upload' ? 'file' : item.kind, title: labelOf(item) }))
}

let noticeSeq = 0
let announceSeq = 0

export class BarController {
  private readonly vad = new EnergyVad()
  private idleTimer: ReturnType<typeof setTimeout> | null = null
  private micOffTimer: ReturnType<typeof setTimeout> | null = null
  private turnTimer: ReturnType<typeof setTimeout> | null = null
  private submittedSession: string | null = null
  private openedAt = 0
  private viaHotkey = false
  private hotkeyHandled = false
  private interceptor: VoiceInterceptor | null = null
  private composer: ComposerPort | null = null
  /** Badges sent with the utterance being processed: restored if nothing was heard after all. */
  private sentWithSpeech: AttachmentItem[] = []
  /** The bar asked to listen before the engine socket was up: listen as soon as it connects. */
  private listenWhenConnected = false

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

  attachComposer(composer: ComposerPort | null): void {
    this.composer = composer
  }

  private turnContext() {
    return { document_id: this.s.context.documentId ?? null, conversation_id: this.s.context.conversationId ?? null }
  }

  // ---------------------------------------------------------------- open / close

  open(event: BarOpenedEvent): void {
    this.viaHotkey = event.viaHotkey
    this.hotkeyHandled = false
    this.openedAt = this.deps.now()
    if (this.s.open) {
      this.set((s) => ({ context: { ...s.context, ...(event.documentId ? { documentId: event.documentId } : {}) } }))
      if (event.listen && this.s.mode === 'typing' && composerEmpty(this.s) && this.s.canListen === true) this.startListening()
      return
    }
    // One conversation for as long as the bar stays open, so follow-ups ("a bit more") have context.
    const context = { conversationId: this.deps.newConversationId(), ...(event.documentId ? { documentId: event.documentId } : {}) }
    this.composer?.clear()
    this.set({
      open: true,
      context,
      idle: false,
      micError: null,
      hint: null,
      localConfirm: null,
      transcript: '',
      draft: '',
      attachments: [],
      multiline: false,
      dictating: false,
      pending: null,
      showHistory: false,
    })
    this.deps.send({ type: 'bar.state', open: true })
    if (event.listen && this.s.canListen === true) this.startListening()
    else {
      // Capability not known yet: listen as soon as it is (if speech input turns out to be available).
      if (event.listen && this.s.canListen === null) this.listenWhenConnected = true
      this.set({ mode: 'typing' })
    }
    this.armIdle()
  }

  close(): void {
    if (!this.s.open) return
    this.listenWhenConnected = false
    if (this.s.mode === 'listening') this.stopMic(false)
    this.deps.mic.stop()
    this.clearTimers()
    this.deps.send({ type: 'bar.state', open: false })
    this.deps.setListening(false)
    this.composer?.clear()
    this.set({
      open: false,
      mode: 'typing',
      transcript: '',
      draft: '',
      attachments: [],
      multiline: false,
      dictating: false,
      pending: null,
      level: 0,
      speaking: false,
      idle: false,
      localConfirm: null,
      hint: null,
    })
  }

  /** Speak something without hearing ourselves: the mic is off while `speak` runs, then listening resumes. */
  async withMicPaused(speak: () => Promise<unknown>, opts: { listenAfter?: boolean } = {}): Promise<void> {
    const wasListening = this.s.mode === 'listening' || opts.listenAfter === true
    if (this.s.mode === 'listening') {
      // Whatever was half-heard before the prompt is noise, not an answer: drop it.
      this.stopMic(false)
      this.composer?.dropDictation()
      this.set({ mode: 'typing', transcript: '', level: 0, speaking: false })
    }
    this.set({ prompting: true })
    try {
      await speak()
    } catch {
      // Speaking is a courtesy; the prompt is also on screen.
    } finally {
      this.set({ prompting: false })
    }
    const s = this.s
    if ((wasListening || this.listenWhenConnected) && s.open && s.mode === 'typing' && composerEmpty(s) && s.canListen === true) {
      this.listenWhenConnected = false
      this.set({ hint: null })
      this.startListening()
    }
  }

  /** The engine said whether the bar can listen (capabilities, or the speech model just finished installing). */
  setCanListen(canListen: boolean): void {
    this.set({ canListen })
    this.connected()
  }

  /** The engine socket (re)connected. */
  connected(): void {
    const s = this.s
    if (!this.listenWhenConnected || s.prompting || s.canListen === null) return // picked up later
    this.listenWhenConnected = false
    if (s.open && s.mode === 'typing' && composerEmpty(s) && s.canListen) {
      this.set({ hint: null })
      this.startListening()
    }
  }

  setInterceptor(fn: VoiceInterceptor | null): void {
    this.interceptor = fn
  }

  // ---------------------------------------------------------------- the composer reports what it holds

  /** The user changed the composer (typing or badges). Dictation updates don't come through here. */
  setDraft(draft: string, attachments: AttachmentItem[], multiline = false): void {
    const filled = draft.trim().length > 0 || attachments.length > 0
    // With something to send, Enter sends it: no row stays selected unless the user moves to one.
    this.set((s) => ({
      draft,
      attachments,
      multiline,
      idle: filled ? false : s.idle,
      selected: filled ? '' : s.selected,
      hint: filled ? null : s.hint,
    }))
  }

  // ---------------------------------------------------------------- listening

  startListening(): void {
    if (this.s.prompting) {
      // Never listen over our own spoken prompt: listen once it has finished (withMicPaused).
      this.listenWhenConnected = true
      return
    }
    const sessionId = this.deps.newSessionId()
    // Text already typed: speech adds to it. Otherwise speech is the command (with any badges).
    const dictating = this.interceptor === null && this.s.draft.trim().length > 0
    this.vad.reset()
    this.vad.endOfSpeechMs = this.deps.endOfSpeechMs()
    const ok = this.deps.send({
      type: 'mic.start',
      session_id: sessionId,
      mode: 'auto',
      context: this.turnContext(),
      // Onboarding answers are matched locally and dictation stays in the composer: transcribe, don't send.
      transcribe_only: this.interceptor !== null || dictating,
    })
    if (!ok) {
      this.listenWhenConnected = true
      this.set({ mode: 'typing', hint: t('connecting') })
      return
    }
    this.listenWhenConnected = false
    // Listening again with the bar still open pauses reading again (07 § Ducking); idempotent on open.
    this.deps.send({ type: 'bar.state', open: true })
    this.set({ mode: 'listening', sessionId, transcript: '', dictating, level: 0, speaking: false, micError: null, hint: null })
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

  private stopMic(submit: boolean, attachments: Attachment[] = []): void {
    const sessionId = this.s.sessionId
    this.deps.mic.stop()
    this.deps.setListening(false)
    if (this.micOffTimer) clearTimeout(this.micOffTimer)
    this.micOffTimer = null
    if (sessionId) this.deps.send({ type: 'mic.stop', session_id: sessionId, submit, attachments })
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
    this.composer?.setDictation(null)
    this.set({ mode: 'typing', micError: err.message, level: 0, speaking: false })
  }

  /** Any keystroke while listening turns the transcript into editable text (FR-BAR-3). */
  keystroke(): void {
    if (this.s.mode !== 'listening') return
    this.stopMic(false)
    this.composer?.setDictation(null) // what was heard stays, as ordinary text
    this.set({ mode: 'typing', speaking: false, level: 0, idle: false })
  }

  /** Tab, or the orb (09 § Shortcuts). */
  toggle(): void {
    const { mode } = this.s
    if (mode === 'typing' || mode === 'waiting') this.startListening()
    else if (mode === 'listening') {
      this.stopMic(false)
      this.composer?.setDictation(null)
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

  /** End the utterance. Returns true when this call ended it; every utterance ends at most once (FR-BAR-4). */
  submit(path: SubmitPath): boolean {
    const s = this.s
    if (s.mode !== 'listening') return false
    if (!s.sessionId || this.submittedSession === s.sessionId) return false
    const heard = this.vad.hasSpeech || s.transcript.trim().length > 0
    if (!heard) {
      if (path === 'ptt') {
        // Held and released without saying anything: cancel quietly.
        this.stopMic(false)
        this.set({ mode: 'typing', level: 0, speaking: false })
      }
      return false
    }
    this.submittedSession = s.sessionId
    if (s.dictating || this.interceptor) {
      // The final transcript comes back and settles in the composer (or goes to the interceptor).
      this.stopMic(true)
      this.set({ mode: 'finalizing', level: 0, speaking: false, idle: false })
      this.armTurnTimeout()
      return true
    }
    // A spoken command: it goes with whatever is attached, and the composer is free for the next thing.
    const attachments = s.attachments.filter((a) => a.kind !== 'upload')
    this.sentWithSpeech = attachments
    this.stopMic(true, toWire(attachments, new Map()))
    this.composer?.clear()
    this.set({
      mode: 'finalizing',
      level: 0,
      speaking: false,
      idle: false,
      draft: '',
      attachments: [],
      pending: { text: s.transcript, attachments: describe(attachments) },
    })
    this.armTurnTimeout()
    return true
  }

  /**
   * Send a command: typed text and/or attachments (already in wire form), or a toolbar press
   * (`source: 'button'`, which shows its result without a "you said" line).
   */
  submitCommand(
    raw: string,
    attachments: Attachment[] = [],
    opts: { source?: 'typed' | 'button'; shown?: TurnAttachment[]; label?: string } = {},
  ): boolean {
    const text = raw.trim()
    if (!text && attachments.length === 0) return false
    if (this.s.mode === 'listening') {
      this.stopMic(false)
      this.composer?.dropDictation()
    }
    const sessionId = this.deps.newSessionId()
    const ok = this.deps.send({
      type: 'command',
      text,
      source: opts.source ?? 'typed',
      session_id: sessionId,
      context: this.turnContext(),
      attachments,
    })
    if (!ok) {
      this.notice('error', t('no_engine'))
      return false
    }
    this.submittedSession = sessionId
    this.set({
      mode: 'waiting',
      sessionId,
      transcript: '',
      dictating: false,
      pending: opts.source === 'button' ? null : { text: opts.label ?? text, attachments: opts.shown ?? [] },
      hint: null,
      idle: false,
      level: 0,
      speaking: false,
    })
    this.armTurnTimeout()
    return true
  }

  // ---------------------------------------------------------------- engine events

  onSttPartial(msg: SttPartial): void {
    const s = this.s
    if (msg.session_id !== s.sessionId || s.mode !== 'listening') return
    this.set({ transcript: msg.text, idle: false })
    this.composer?.setDictation(msg.text)
    if (this.idleTimer) clearTimeout(this.idleTimer)
    if (this.micOffTimer) clearTimeout(this.micOffTimer)
    this.idleTimer = this.micOffTimer = null
  }

  onSttFinal(msg: SttFinal): void {
    const s = this.s
    if (msg.session_id !== s.sessionId) return
    const active = s.mode === 'listening' || s.mode === 'finalizing'
    if (this.interceptor && active) {
      // An onboarding answer: the final transcript (not a lagging partial) goes to the interceptor.
      this.clearTurnTimeout()
      this.submittedSession = msg.session_id
      this.deps.mic.stop()
      this.deps.setListening(false)
      this.composer?.dropDictation()
      this.set({ mode: 'typing', transcript: '', level: 0, speaking: false })
      const next = this.interceptor(msg.text)
      if (next === 'listen-again' && this.s.open && this.s.canListen === true) this.startListening()
      return
    }
    if (!active) return
    if (s.mode === 'listening') {
      // The engine ended the utterance itself: we must not send mic.stop {submit: true} as well.
      this.submittedSession = msg.session_id
      this.deps.mic.stop()
      this.deps.setListening(false)
    }
    if (s.dictating) {
      // Dictation: the final wording settles in the composer as ordinary text; nothing is sent.
      this.clearTurnTimeout()
      this.composer?.setDictation(msg.text)
      this.composer?.setDictation(null)
      this.set({ mode: 'typing', transcript: '', dictating: false, level: 0, speaking: false })
      return
    }
    if (!msg.text.trim()) {
      // Nothing was said after all: give back what was attached and say so.
      this.clearTurnTimeout()
      this.composer?.dropDictation()
      if (this.sentWithSpeech.length) this.composer?.insertAttachments(this.sentWithSpeech)
      this.sentWithSpeech = []
      this.set({ mode: 'typing', transcript: '', pending: null, level: 0, speaking: false, hint: t('not_heard') })
      this.armIdle()
      return
    }
    if (s.mode === 'listening') {
      this.composer?.clear()
      this.set({ draft: '', attachments: [] })
    }
    this.set((prev) => ({
      mode: 'finalizing',
      transcript: '',
      level: 0,
      speaking: false,
      pending: { text: msg.text, attachments: prev.pending?.attachments ?? [] },
    }))
    this.armTurnTimeout()
    this.announce(t('heard', { text: msg.text }))
  }

  onTurn(msg: AssistantTurn): void {
    const turn: TurnModel = {
      turnId: msg.turn_id,
      sessionId: msg.session_id ?? null,
      source: msg.source,
      text: msg.text,
      attachments: (msg.attachments ?? []).map((a) => ({ kind: a.kind, title: a.title })),
      recordingId: msg.recording_id ?? null,
      reply: '',
      replyRecordingId: null,
      done: false,
      error: null,
      cards: [],
      actionIds: [],
    }
    this.set((s) => {
      const exists = s.turns.some((x) => x.turnId === msg.turn_id)
      const turns = exists
        ? s.turns.map((x) => (x.turnId === msg.turn_id ? { ...x, ...turn, cards: x.cards, actionIds: x.actionIds } : x))
        : [...s.turns, turn]
      return { turns, selected: `reply:${msg.turn_id}` }
    })
    const s = this.s
    if (msg.session_id && msg.session_id === s.sessionId && (s.mode === 'finalizing' || s.mode === 'waiting')) {
      this.clearTurnTimeout()
      this.sentWithSpeech = []
      this.set({ mode: 'waiting', transcript: '', pending: null })
    }
  }

  onDelta(msg: AssistantDelta): void {
    this.updateTurn(msg.turn_id, (x) => ({ reply: x.reply + msg.text }))
  }

  onCard(msg: AssistantCard): void {
    this.updateTurn(msg.turn_id, (x) => ({
      cards: [
        // A list shown again in the same turn replaces itself instead of piling up.
        ...x.cards.filter((c) => c.kind !== msg.kind),
        {
          id: `${msg.turn_id}:${msg.kind}`,
          turnId: msg.turn_id,
          kind: msg.kind,
          title: msg.title,
          rows: msg.rows ?? [],
          data: msg.data ?? {},
        },
      ],
    }))
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
      turns: s.turns.map((x) =>
        x.turnId === msg.turn_id && !x.actionIds.includes(msg.action_id) ? { ...x, actionIds: [...x.actionIds, msg.action_id] } : x,
      ),
      selected: `action:${msg.action_id}`,
    }))
    this.announce(`${msg.summary}. ${t('confirm_hint')}`)
  }

  onActionResolved(msg: AssistantActionResolved): void {
    this.setActionStatus(msg.action_id, msg.status, msg.message)
  }

  onDone(msg: AssistantDone): void {
    const problem = msg.error && typeof msg.error === 'object' ? (msg.error as Record<string, unknown>) : null
    const errorText = problem ? String(problem.detail ?? problem.title ?? msg.text ?? '') : null
    const errorCode = problem ? String(problem.code ?? '') || null : null
    this.updateTurn(msg.turn_id, (x) => ({
      done: true,
      reply: msg.text ? msg.text : x.reply,
      replyRecordingId: msg.reply_recording_id ?? null,
      error: errorText,
      errorCode,
    }))
    const s = this.s
    const turn = s.turns.find((x) => x.turnId === msg.turn_id)
    if (turn) this.announce(turn.error ?? turn.reply)
    const ours = !!turn?.sessionId && turn.sessionId === s.sessionId
    if (!ours || s.mode !== 'waiting') return
    const hasPending = !!turn && turn.actionIds.some((id) => s.actions[id]?.status === 'pending')
    if (hasPending && turn?.source === 'spoken' && s.open && composerEmpty(s)) {
      // Wait for a spoken "yes" / "no" (the engine resolves it); Enter and Esc still work.
      this.startListening()
    } else {
      // The turn is over: reading resumes after the reply even if the bar stays open (07 § Ducking).
      // The engine still holds reading while the spoken reply itself plays.
      this.deps.send({ type: 'bar.state', open: false })
      this.set({ mode: 'typing' })
      this.armIdle()
    }
  }

  onUsageAlert(msg: UsageAlert): void {
    this.notice(msg.level === 'stop' ? 'error' : 'warn', msg.message)
  }

  onError(msg: ErrorMsg): void {
    const p = msg.problem
    const text = typeof p.detail === 'string' && p.detail ? p.detail : typeof p.title === 'string' ? p.title : 'Engine error.'
    if (p.code === 'unavailable' && this.s.mode === 'listening') {
      // Listening isn't set up here: not an error the user caused. They can type; the orb says why.
      this.stopMic(false)
      this.composer?.dropDictation()
      this.set({ mode: 'typing', canListen: false, level: 0, speaking: false, hint: text })
      this.announce(text)
      return
    }
    this.notice('error', text)
    if (this.s.mode === 'finalizing' || this.s.mode === 'waiting') {
      this.clearTurnTimeout()
      this.set({ mode: 'typing', pending: null })
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
    if (this.s.turns.some((x) => x.turnId === turnId)) return
    this.set((s) => ({
      turns: [
        ...s.turns,
        {
          turnId,
          sessionId: null,
          source: 'typed',
          text: '',
          attachments: [],
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

  private updateTurn(turnId: string, fn: (x: TurnModel) => Partial<TurnModel>): void {
    this.ensureTurn(turnId)
    this.set((s) => ({ turns: s.turns.map((x) => (x.turnId === turnId ? { ...x, ...fn(x) } : x)) }))
  }

  private armIdle(): void {
    if (this.idleTimer) clearTimeout(this.idleTimer)
    this.idleTimer = setTimeout(() => {
      this.idleTimer = null
      const s = this.s
      if (s.open && !s.speaking && !s.transcript && composerEmpty(s)) this.set({ idle: true })
    }, IDLE_SUGGESTIONS_MS)
  }

  private armMicOff(delayMs = MIC_IDLE_OFF_MS): void {
    if (this.micOffTimer) clearTimeout(this.micOffTimer)
    this.micOffTimer = setTimeout(() => {
      this.micOffTimer = null
      const s = this.s
      if (s.mode !== 'listening' || this.vad.hasSpeech || s.transcript) return
      // Someone starting to speak at this very moment is not idle: look again shortly.
      if (this.vad.maybeSpeech) return this.armMicOff(MIC_OFF_RECHECK_MS)
      this.stopMic(false)
      this.set({ mode: 'typing', level: 0, hint: t('mic_off') })
    }, delayMs)
  }

  private armTurnTimeout(): void {
    this.clearTurnTimeout()
    this.turnTimer = setTimeout(() => {
      this.turnTimer = null
      const s = this.s
      if (s.mode === 'finalizing' || s.mode === 'waiting') {
        this.set({ mode: 'typing', pending: null, dictating: false })
        this.notice('warn', t('no_answer'))
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
