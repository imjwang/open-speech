/**
 * The command bar's conversation loop (09 § Command bar, 07 § Voice loop details, FR-BAR-1..4).
 *
 * One way at a time. The bar listens from the instant it opens, sends what it heard when the user stops, shows
 * that it is working, then the engine speaks the reply. The engine plays through CoreAudio, where Chromium's
 * echo cancellation can't hear it, so nothing from the microphone is sent while the engine speaks
 * (`assistant.speech` started … ended, every kind). When the reply has been heard the bar listens again for a
 * follow-up (`FOLLOW_UP_MS`); if nothing comes, the conversation goes idle: the microphone is released and
 * reading resumes.
 *
 *   listening → finalizing → waiting → speaking → listening (follow-up) → … → idle
 *
 * - The microphone opens on every open unless speech input is *known* to be unavailable (`canListen === false`).
 *   Audio captured before the engine has accepted `mic.start` (it isn't connected yet) is kept, up to
 *   `BUFFER_MAX_SAMPLES`, and sent in order once it is; the local VAD runs meanwhile, and an utterance that ended
 *   before the engine connected is submitted the moment it does. The stream stays open for the whole
 *   conversation and is released at idle or close.
 * - End of an utterance: `voice_input.end_of_speech_ms` of silence (energy VAD), releasing a held hotkey
 *   (push-to-talk) or Enter; each submits exactly once.
 * - With nothing typed, speech is a **command** (sent with the badges); with text typed, it is **dictation**.
 * - Any keystroke stops listening and keeps the transcript as text. Tab, the orb or the hotkey while the engine
 *   speaks stop it (`speech.stop`) and listen; Esc stops it and goes idle.
 * - A turn whose effect is reading audio (reading resumes) ends the conversation instead of listening again.
 * - The bar speaks its own notices (nothing heard, no answer, the budget, speech input unavailable) through
 *   `/assistant/say`, in the interface language; they are announced to screen readers too.
 * - `bar.state {open}` follows the conversation: true while listening, thinking or speaking, false at idle.
 * - Cloning a voice (08 § Voice cloning): once the passage card is confirmed (its record button, or "ready"), the bar
 *   records the passage with its own capture path (audio/reference.ts: echo cancellation, noise suppression and
 *   gain control off, 48 kHz float) as soon as the engine is quiet. The conversation microphone is closed for it;
 *   reading stays paused. The take ends on silence after the reading (or the orb, Tab or Enter), and is uploaded
 *   with this conversation's session: the engine answers in it with the voice's sample and "Keep it?".
 */
import type { Attachment, CloneUpload } from '@shared/api/client'
import { t } from '@shared/i18n'
import type { BarOpenedEvent } from '@shared/ipc-channels'
import type {
  AssistantAction,
  AssistantActionResolved,
  AssistantCard,
  AssistantDelta,
  AssistantDone,
  AssistantSpeech,
  AssistantTurn,
  ClientMessage,
  ErrorMsg,
  JobProgress,
  PlaybackState,
  SttFinal,
  SttPartial,
  UsageAlert,
} from '@shared/api/ws.gen'

import type { MicSource } from '@/audio/mic'
import type { ReferenceRecorder } from '@/audio/reference'
import type { SocketLike } from '@/engine'
import { EnergyVad, toDb } from '@/audio/vad'
import { labelOf, toWire, type AttachmentItem } from './composer/attachments'
import { composerEmpty, latestPendingAction, type BarMode, type BarState, type BarStore, type TurnAttachment, type TurnModel } from './store'

/** Listening after the bar opens: without any speech the conversation goes idle (the microphone is released). */
export const MIC_IDLE_OFF_MS = 15_000
/** When the idle timeout lands on the first instant of speech, how long until the next look. */
export const MIC_OFF_RECHECK_MS = 1_000
/** Listening for a follow-up after a reply has been heard. */
export const FOLLOW_UP_MS = 7_000
/** … when the reply asked a question (a confirmation card is waiting for "yes" or "no"). */
export const FOLLOW_UP_CONFIRM_MS = 10_000
/** Hotkey held at least this long before release = push-to-talk. */
export const PTT_MIN_HOLD_MS = 350
/** Give up waiting for the engine to start a turn. */
export const TURN_START_TIMEOUT_MS = 20_000
/** A started turn that has said nothing for this long (no delta, card, action or end) is given up on. */
export const TURN_SILENT_TIMEOUT_MS = 60_000
/** The engine said a reply is coming (`speaks`) or we asked it to say something, but it never started. */
export const SPEECH_START_GUARD_MS = 15_000
/** Something started speaking and its `ended` never came (a lost event): stop waiting after this. */
export const SPEECH_END_GUARD_MS = 120_000
/** After `speech.stop`, the fade and the echo tail are short: don't wait longer than this for `ended`. */
export const STOP_GUARD_MS = 2_000
/** First run: with no speech this long after the bar opened by itself, it says once how to talk to it. */
export const FIRST_RUN_NUDGE_MS = 4_000
/**
 * No utterance runs longer than this. The energy VAD only ends speech after a pause, so sound that never pauses
 * (a TV or music that started after the room was measured) would otherwise keep the microphone open forever with
 * nothing ever submitted. At the cap what was heard is submitted: the engine drops noise, the bar says it didn't
 * catch that, listens once more, then goes idle. Spoken commands are short; dictation gets longer.
 */
export const MAX_UTTERANCE_MS = 20_000
export const MAX_DICTATION_MS = 60_000
/** Audio kept while the engine is not connected yet (30 s at 16 kHz); the oldest goes first beyond that. */
export const BUFFER_MAX_SAMPLES = 16_000 * 30
/** Recording a voice to clone: this much silence after the reading ends the take (people pause between sentences). */
export const CAPTURE_END_OF_SPEECH_MS = 1_600
/** … nothing heard this long after the take starts gives up. */
export const CAPTURE_NO_SPEECH_MS = 8_000
/** … and no take runs longer than this. */
export const CAPTURE_MAX_MS = 30_000
/** Making a voice can take a while (a download, the first load of the model): each progress event of its job keeps
 *  the turn alive for this long. */
export const CLONE_SILENT_TIMEOUT_MS = 60_000

export type SubmitPath = 'enter' | 'vad' | 'ptt'
/** What happens to the microphone's audio right now. */
export type AudioGate = 'drop' | 'buffer' | 'send'

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
  setListening(listening: boolean, recording?: boolean): void
  /** Run `cb` before the next paint (requestAnimationFrame); level updates are coalesced to one per frame. */
  frame?(cb: () => void): void
  endOfSpeechMs(): number
  newSessionId(): string
  /** One id per time the bar is opened: everything said until it closes is one conversation. */
  newConversationId(): string
  now(): number
  /** Say something in the assistant voice (`POST /assistant/say`); settles once it has been said or failed. */
  say?(text: string): Promise<unknown>
  /** The first run is over: `PATCH /settings {onboarding_completed: true}`. */
  completeOnboarding?(): void
  /** The open-bar shortcut as it is said aloud ("Option-Space"), for the first-run nudge. */
  spokenShortcut?(): string
  /** Content-free debug log: event names and timings, never what was said. */
  debug?(line: string): void
  /** The capture path for a voice to clone (its own microphone settings; never the conversation microphone). */
  recorder?: ReferenceRecorder
  /** `POST /voices/clones` with the take. */
  uploadClone?(wav: Blob, fields: CloneUpload): Promise<{ job_id: string; slug: string }>
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function meterLevel(level: number): number {
  // -60 dBFS → 0, -12 dBFS → 1
  return Math.max(0, Math.min(1, (toDb(level) + 60) / 48))
}

function describe(items: AttachmentItem[]): TurnAttachment[] {
  return items.map((item) => ({ kind: item.kind === 'upload' ? 'file' : item.kind, title: labelOf(item) }))
}

function playing(p: PlaybackState | null | undefined): boolean {
  return p?.state === 'playing' || p?.state === 'loading'
}

/** The turn the loop is following: the last thing the user sent in this conversation. */
interface LiveTurn {
  turnId: string | null
  sessionId: string
  done: boolean
  /** `assistant.done.speaks`: its reply audio is playing or coming. */
  speaks: boolean
  /** Its reply speech has been heard to the end (or will never come). */
  speechOver: boolean
  /** Its effect is reading audio (reading resumed): the conversation ends instead of listening again. */
  audible: boolean
}

/** `open`: the bar opened or Tab was pressed; `follow-up`: after a reply; `barge-in`: the user cut the engine off. */
type ListenKind = 'open' | 'follow-up' | 'barge-in'

let noticeSeq = 0
let announceSeq = 0
let captionSeq = 0

export class BarController {
  private readonly vad = new EnergyVad()
  private listenTimer: ReturnType<typeof setTimeout> | null = null
  private turnTimer: ReturnType<typeof setTimeout> | null = null
  private speechGuard: ReturnType<typeof setTimeout> | null = null
  private nudgeTimer: ReturnType<typeof setTimeout> | null = null
  private submittedSession: string | null = null
  private openedAt = 0
  private viaHotkey = false
  private hotkeyHandled = false
  private composer: ComposerPort | null = null
  /** Badges sent with the utterance being processed: restored if nothing was heard after all. */
  private sentWithSpeech: AttachmentItem[] = []

  // ---- the microphone and its audio
  /** `mic.start` for this session hasn't been accepted (the engine isn't connected): audio is buffered. */
  private pendingStart: { sessionId: string; transcribeOnly: boolean } | null = null
  /** The utterance ended while buffering: `mic.stop {submit}` with these attachments once connected. */
  private submitOnConnect: Attachment[] | null = null
  private buffer: Int16Array[] = []
  private bufferedSamples = 0
  private firstPcmPending = false
  private micRequestedAt = 0

  // ---- the conversation
  /** A conversation is active (listening, thinking, speaking or typing a reply): reading is paused. */
  private active = false
  /** The capability as the engine said it (null until it has). */
  private engineCanListen: boolean | null = null
  /** The open asked to listen but listening wasn't possible yet: listen when the engine says it is. */
  private wantListen = false
  /** Sent, waiting for its turn (`assistant.turn`) or its end (`assistant.done`). */
  private inFlight: string | null = null
  private turn: LiveTurn | null = null
  /** Engine speech being heard right now, by `${kind}:${turn_id}`. */
  private readonly speech = new Map<string, number>()
  /** Reading started playing while a command was in flight: its answer is reading audio. */
  private readingStartedInTurn = false
  /** Our own `/assistant/say` hasn't settled yet. */
  private sayPending = false
  /** When the current speech is over: listen again, or end the conversation. */
  private afterSpeech: 'listen' | 'idle' | null = null
  /** Notices to say at the next quiet moment (the budget alert while a reply is still playing). */
  private readonly noticeQueue: string[] = []
  /** "Sorry, I didn't catch that" was said: a second miss ends the conversation. */
  private retried = false
  /** How long the current utterance has been going (see MAX_UTTERANCE_MS). */
  private utteranceMs = 0
  private firstRun = false
  private nudged = false
  /** First run: the line that says voices are AI-generated (provider policy), later with the nudge's words. */
  private firstRunCaption: string | null = null
  private readingPlaying = false

  // ---- recording a voice to clone
  private readonly captureVad = new EnergyVad({ endOfSpeechMs: CAPTURE_END_OF_SPEECH_MS })
  private captureTimers: ReturnType<typeof setTimeout>[] = []
  /** Bumped whenever a take starts or ends: late callbacks of an older one are ignored. */
  private captureGen = 0
  /** The take's chunks and loudest level so far (content-free, for the debug log). */
  private captureChunks = 0
  private captureMax = 0

  /** The microphone level for the orb (0..1). Kept out of the store: it changes ~30 times a second. */
  private level = 0
  private levelQueued = false
  private readonly levelListeners = new Set<(level: number) => void>()

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

  private debug(line: string): void {
    this.deps.debug?.(line)
  }

  attachComposer(composer: ComposerPort | null): void {
    this.composer = composer
  }

  /**
   * The microphone level (0..1) for the orb's meter, at most once per animation frame. It never goes through
   * the store, so a breathing orb re-renders nothing: the orb writes it straight into a CSS variable.
   */
  subscribeLevel(fn: (level: number) => void): () => void {
    this.levelListeners.add(fn)
    fn(this.level)
    return () => {
      this.levelListeners.delete(fn)
    }
  }

  private setLevel(level: number): void {
    this.level = level
    if (this.levelQueued) return
    this.levelQueued = true
    const flush = () => {
      this.levelQueued = false
      for (const fn of this.levelListeners) fn(this.level)
    }
    if (this.deps.frame) this.deps.frame(flush)
    else if (typeof requestAnimationFrame === 'function') requestAnimationFrame(flush)
    else setTimeout(flush, 16)
  }

  private turnContext() {
    return { document_id: this.s.context.documentId ?? null, conversation_id: this.s.context.conversationId ?? null }
  }

  /** The engine is saying something right now. */
  get engineSpeaking(): boolean {
    return this.speech.size > 0
  }

  /** What happens to captured audio right now (07: nothing is sent while the engine speaks). */
  get gate(): AudioGate {
    if (this.s.mode !== 'listening' || this.engineSpeaking) return 'drop'
    return this.pendingStart ? 'buffer' : 'send'
  }

  /** Samples of audio held for an engine that isn't connected yet. */
  get bufferedAudio(): number {
    return this.bufferedSamples
  }

  /** The bar is speaking or about to (a reply that is coming, our own notice). */
  get speakingNow(): boolean {
    return this.engineSpeaking || this.s.mode === 'speaking'
  }

  // ---------------------------------------------------------------- open / close

  open(event: Omit<BarOpenedEvent, 'openId'>): void {
    this.viaHotkey = event.viaHotkey
    this.hotkeyHandled = false
    this.openedAt = this.deps.now()
    if (this.engineCanListen === null && event.canListen !== undefined) this.set({ canListen: event.canListen })
    if (this.s.open) {
      this.set((s) => ({ context: { ...s.context, ...(event.documentId ? { documentId: event.documentId } : {}) } }))
      const { mode } = this.s
      if (event.bargeIn || this.speakingNow) this.interrupt(this.s.canListen === false ? 'idle' : 'listen')
      else if (event.viaHotkey && mode !== 'listening') this.toggle()
      else if (event.listen && composerEmpty(this.s) && (mode === 'idle' || mode === 'typing')) this.listen('open')
      return
    }
    // One conversation for as long as the bar stays open, so follow-ups ("a bit more") have context.
    const context = { conversationId: this.deps.newConversationId(), ...(event.documentId ? { documentId: event.documentId } : {}) }
    this.composer?.clear()
    this.set({
      open: true,
      mode: 'idle',
      context,
      micError: null,
      hint: null,
      localConfirm: null,
      transcript: '',
      draft: '',
      attachments: [],
      multiline: false,
      dictating: false,
      pending: null,
      userSpeaking: false,
      showHistory: false,
      // A row chosen in an earlier open is not a choice for this one: Enter acts on the default row again.
      selected: '',
    })
    this.turn = null
    this.inFlight = null
    this.retried = false
    this.afterSpeech = null
    this.firstRun = event.firstRun === true
    this.nudged = false
    this.wantListen = event.listen
    if (this.firstRun) this.firstRunCaption = this.caption(t('voices.ai'))
    if (event.listen && this.s.canListen !== false) {
      // Opened to talk while the engine is still saying something (a reply heard after the bar closed): cut it.
      if (event.bargeIn || this.engineSpeaking) this.interrupt('listen')
      else this.listen('open')
    }
    if (this.firstRun) this.armNudge()
  }

  close(): void {
    if (!this.s.open) return
    this.cancelCapture()
    if (this.s.mode === 'listening') this.endSession(false)
    this.releaseStream()
    this.clearTimers()
    if (this.firstRun) this.finishFirstRun()
    this.active = false
    this.deps.send({ type: 'bar.state', open: false })
    this.deps.setListening(false)
    this.composer?.clear()
    this.turn = null
    this.inFlight = null
    this.afterSpeech = null
    this.wantListen = false
    this.noticeQueue.length = 0
    this.set({
      open: false,
      mode: 'idle',
      transcript: '',
      draft: '',
      attachments: [],
      multiline: false,
      dictating: false,
      pending: null,
      userSpeaking: false,
      localConfirm: null,
      hint: null,
      capture: null,
    })
  }

  /** The engine said whether the bar can listen (capabilities, or the speech model just finished installing). */
  setCanListen(canListen: boolean, reason?: string | null): void {
    const was = this.engineCanListen
    this.engineCanListen = canListen
    this.set({ canListen })
    const s = this.s
    if (!canListen) {
      if (s.open && (s.mode === 'listening' || this.deps.mic.live)) this.speechUnavailable(reason ?? null)
      return
    }
    if (was !== true && s.open && this.wantListen && s.mode === 'idle' && composerEmpty(s) && !this.speakingNow) this.listen('open')
  }

  /** The engine socket (re)connected. */
  connected(): void {
    if (this.active) this.deps.send({ type: 'bar.state', open: true }) // a restarted engine forgot it
    const pending = this.pendingStart
    if (!pending || !this.s.open) return
    const ok = this.deps.send({
      type: 'mic.start',
      session_id: pending.sessionId,
      mode: 'auto',
      context: this.turnContext(),
      transcribe_only: pending.transcribeOnly,
    })
    if (!ok) return
    this.pendingStart = null
    const held = this.buffer
    this.buffer = []
    this.bufferedSamples = 0
    for (const pcm of held) this.deps.sendAudio(pcm)
    this.debug(`flush chunks=${held.length}`)
    if (this.submitOnConnect) {
      // The user finished speaking before the engine was there: send it now.
      this.deps.send({ type: 'mic.stop', session_id: pending.sessionId, submit: true, attachments: this.submitOnConnect })
      this.submitOnConnect = null
      this.armTurnTimeout()
    }
  }

  /** The engine socket dropped: nothing it was saying will report `ended`. */
  disconnected(): void {
    if (!this.speech.size) return
    this.speech.clear()
    if (this.turn) this.turn.speechOver = true
    this.set({ engineSpeaking: false })
    if (this.s.open && this.s.mode === 'speaking') this.settle()
  }

  // ---------------------------------------------------------------- the composer reports what it holds

  /** The user changed the composer (typing or badges). Dictation updates don't come through here. */
  setDraft(draft: string, attachments: AttachmentItem[], multiline = false): void {
    const filled = draft.trim().length > 0 || attachments.length > 0
    if (filled) this.cancelNudge()
    // With something to send, Enter sends it: no row stays selected unless the user moves to one.
    this.set((s) => ({ draft, attachments, multiline, selected: filled ? '' : s.selected, hint: filled ? null : s.hint }))
  }

  // ---------------------------------------------------------------- listening

  /** Listen now (Tab, the orb, a re-open). Kept public for the bar's keys and the tests. */
  startListening(): void {
    this.listen('open')
  }

  private listen(kind: ListenKind): void {
    const s0 = this.s
    if (!s0.open) return
    if (s0.canListen === false) {
      this.goIdle()
      return
    }
    if (s0.mode === 'listening') this.endSession(false)
    const sessionId = this.deps.newSessionId()
    // Text already typed: speech adds to it. Otherwise speech is the command (with any badges).
    const dictating = s0.draft.trim().length > 0
    this.vad.reset({ keepFloor: kind !== 'open' })
    this.vad.endOfSpeechMs = this.deps.endOfSpeechMs()
    this.setLevel(0)
    this.setActive(true)
    this.wantListen = false
    this.buffer = []
    this.bufferedSamples = 0
    this.submitOnConnect = null
    this.set({ mode: 'listening', sessionId, transcript: '', dictating, userSpeaking: false, micError: null, hint: null })
    this.deps.setListening(true)
    this.openStream()
    const ok = this.deps.send({ type: 'mic.start', session_id: sessionId, mode: 'auto', context: this.turnContext(), transcribe_only: dictating })
    this.pendingStart = ok ? null : { sessionId, transcribeOnly: dictating }
    this.debug(`listen kind=${kind}${ok ? '' : ' buffering'}`)
    const confirm = latestPendingAction(this.s) !== null
    this.armListenTimeout(kind !== 'follow-up' ? MIC_IDLE_OFF_MS : confirm ? FOLLOW_UP_CONFIRM_MS : FOLLOW_UP_MS)
  }

  private openStream(): void {
    if (this.deps.mic.live) return
    this.firstPcmPending = true
    this.micRequestedAt = this.deps.now()
    void this.deps.mic.open({
      onPcm: (pcm) => this.onPcm(pcm),
      onLevel: (level, ms) => this.onLevel(level, ms),
      onError: (err) => this.onMicError(err),
    })
  }

  /** Release the microphone (no indicator) and forget any audio held for the engine. */
  private releaseStream(): void {
    this.deps.mic.close()
    this.buffer = []
    this.bufferedSamples = 0
    this.pendingStart = null
    this.submitOnConnect = null
    this.setLevel(0)
  }

  /** End the current listening session with the engine (`mic.stop`); the stream itself stays open. */
  private endSession(submit: boolean, attachments: Attachment[] = []): void {
    const sessionId = this.s.sessionId
    this.setLevel(0)
    this.deps.setListening(false)
    this.clearListenTimer()
    if (this.pendingStart?.sessionId === sessionId) {
      // The engine never heard of this session: nothing to stop there.
      this.pendingStart = null
      this.buffer = []
      this.bufferedSamples = 0
      return
    }
    if (sessionId) this.deps.send({ type: 'mic.stop', session_id: sessionId, submit, attachments })
  }

  private onPcm(pcm: Int16Array): void {
    if (this.firstPcmPending) {
      this.firstPcmPending = false
      const now = this.deps.now()
      this.debug(`first-pcm ms=${Math.round(now - this.openedAt)} mic=${Math.round(now - this.micRequestedAt)}`)
    }
    const gate = this.gate
    if (gate === 'send') {
      if (this.deps.sendAudio(pcm)) return
      // The connection dropped mid-utterance: keep the audio for when it is back.
      const s = this.s
      if (s.sessionId) this.pendingStart = { sessionId: s.sessionId, transcribeOnly: s.dictating }
    } else if (gate === 'drop') return
    this.buffer.push(pcm)
    this.bufferedSamples += pcm.length
    while (this.bufferedSamples > BUFFER_MAX_SAMPLES && this.buffer.length > 1) this.bufferedSamples -= this.buffer.shift()!.length
  }

  private onLevel(level: number, durationMs: number): void {
    const s = this.s
    if (s.mode !== 'listening' || this.engineSpeaking) return
    const event = this.vad.push(level, durationMs)
    this.setLevel(meterLevel(level))
    if (event === 'speech-start') {
      this.utteranceMs = 0
      this.set({ userSpeaking: true })
      this.clearListenTimer()
      this.cancelNudge()
    } else if (event === 'speech-end') {
      this.debug('vad-end')
      this.submit('vad')
    } else if (this.vad.isSpeaking) {
      this.utteranceMs += durationMs
      if (this.utteranceMs >= (s.dictating ? MAX_DICTATION_MS : MAX_UTTERANCE_MS)) {
        this.debug('utterance-cap')
        this.submit('vad')
      }
    }
  }

  private onMicError(err: Error): void {
    const s = this.s
    this.debug('mic-error')
    if (s.mode === 'listening') {
      this.endSession(false)
      this.composer?.setDictation(null)
    }
    this.releaseStream()
    this.set({ micError: err.message, userSpeaking: false })
    if (!s.open) return
    if (this.active) this.sayNotice(err.message, 'idle')
    else this.goIdle()
  }

  /** Speech input turned out not to be available: stop listening, keep the bar for typing, and say why. */
  private speechUnavailable(reason: string | null): void {
    const wasListening = this.s.mode === 'listening'
    if (wasListening) {
      this.endSession(false)
      this.composer?.dropDictation()
    }
    this.releaseStream()
    this.wantListen = false
    this.set({ canListen: false, userSpeaking: false, hint: reason })
    if (reason) this.announce(reason)
    if (wasListening && reason && this.active) this.sayNotice(reason, 'idle')
    else this.goIdle()
  }

  /** Any keystroke while listening turns the transcript into editable text (FR-BAR-3). */
  keystroke(): void {
    this.cancelNudge()
    if (this.s.mode !== 'listening') return
    this.endSession(false)
    this.composer?.setDictation(null) // what was heard stays, as ordinary text
    this.set({ mode: 'typing', userSpeaking: false })
  }

  /** Tab, or the orb (09 § Shortcuts): stop the engine and talk; stop listening; or start listening. */
  toggle(): void {
    const { mode } = this.s
    if (mode === 'recording') return void this.finishCapture()
    if (this.speakingNow) return this.interrupt('listen')
    if (mode === 'listening') {
      const kept = this.s.transcript.trim().length > 0 || !composerEmpty(this.s)
      this.endSession(false)
      this.composer?.setDictation(null)
      if (kept) this.set({ mode: 'typing', userSpeaking: false })
      else this.goIdle()
      return
    }
    if (this.s.canListen !== false) this.listen('open')
  }

  /**
   * Barge-in by key: stop what the engine is saying (a 15 ms fade), then listen (Tab, the orb, the hotkey) or
   * end the conversation (Esc). Listening starts at once, but nothing is sent until the engine reports the
   * speech `ended` (fade + echo tail), so the reply's last syllable never becomes the user's words.
   */
  interrupt(next: 'listen' | 'idle'): void {
    this.cancelNudge()
    if (this.s.mode === 'recording') {
      this.cancelCapture()
      if (next === 'idle') return this.goIdle()
    }
    // One turn's speech is named; anything else (a prompt, several things at once) stops everything.
    const ids = [...this.speech.keys()].map((k) => k.slice(k.indexOf(':') + 1))
    const turnId = ids.length ? (ids.every((id) => id && id === ids[0]) ? ids[0]! : null) : (this.turn?.turnId ?? null)
    this.deps.send({ type: 'speech.stop', turn_id: turnId })
    this.debug(`barge-in next=${next}`)
    // We no longer wait for that turn: its reply was cut short, and the user has moved on.
    if (this.turn) this.turn.speechOver = true
    this.turn = null
    this.inFlight = null
    this.sayPending = false
    this.noticeQueue.length = 0
    this.clearSpeechGuard()
    if (this.engineSpeaking) this.armSpeechGuard(STOP_GUARD_MS)
    this.afterSpeech = null
    if (next === 'idle') this.goIdle()
    else {
      this.set({ pending: null })
      this.listen('barge-in')
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

  /** A transport button (play, skip, speed, next) was pressed: the user wants reading, not the conversation. */
  endConversation(): void {
    if (!this.s.open) return
    if (this.speakingNow) this.deps.send({ type: 'speech.stop', turn_id: null })
    this.turn = null
    this.inFlight = null
    this.sayPending = false
    this.noticeQueue.length = 0
    this.clearTurnTimeout()
    this.set({ pending: null })
    this.goIdle()
  }

  // ---------------------------------------------------------------- submit

  /** End the utterance. Returns true when this call ended it; every utterance ends at most once (FR-BAR-4). */
  submit(path: SubmitPath): boolean {
    const s = this.s
    if (s.mode !== 'listening') return false
    if (!s.sessionId || this.submittedSession === s.sessionId) return false
    const heard = this.vad.hasSpeech || s.transcript.trim().length > 0
    if (!heard) {
      // Held and released without saying anything: cancel quietly.
      if (path === 'ptt') this.goIdle()
      return false
    }
    this.submittedSession = s.sessionId
    this.cancelNudge()
    this.inFlight = s.sessionId
    this.turn = null
    this.readingStartedInTurn = false
    if (s.dictating) {
      // The final transcript comes back and settles in the composer.
      this.finishUtterance([])
      this.set({ mode: 'finalizing', userSpeaking: false })
      return true
    }
    // A spoken command: it goes with whatever is attached, and the composer is free for the next thing.
    const attachments = s.attachments.filter((a) => a.kind !== 'upload')
    this.sentWithSpeech = attachments
    this.finishUtterance(toWire(attachments, new Map()))
    this.composer?.clear()
    this.set({
      mode: 'finalizing',
      userSpeaking: false,
      draft: '',
      attachments: [],
      pending: { text: s.transcript, attachments: describe(attachments) },
    })
    return true
  }

  /** `mic.stop {submit}` now, or as soon as the engine is connected if it hasn't accepted the session yet. */
  private finishUtterance(attachments: Attachment[]): void {
    this.setLevel(0)
    this.deps.setListening(false)
    this.clearListenTimer()
    if (this.pendingStart) {
      this.submitOnConnect = attachments
      this.debug('submit-on-connect')
      return
    }
    const sessionId = this.s.sessionId
    if (sessionId) this.deps.send({ type: 'mic.stop', session_id: sessionId, submit: true, attachments })
    this.armTurnTimeout()
  }

  /**
   * Send a command: typed text and/or attachments (already in wire form), or a toolbar press
   * (`source: 'button'`, which shows its result without a "you said" line). It is answered aloud and the bar
   * then listens for a follow-up, like a spoken one.
   */
  submitCommand(
    raw: string,
    attachments: Attachment[] = [],
    opts: { source?: 'typed' | 'button'; shown?: TurnAttachment[]; label?: string } = {},
  ): boolean {
    const text = raw.trim()
    if (!text && attachments.length === 0) return false
    this.cancelNudge()
    if (this.s.mode === 'listening') {
      this.endSession(false)
      this.composer?.dropDictation()
    }
    const sessionId = this.deps.newSessionId()
    // Reading pauses before the command arrives (a "resume" must not be paused again right after).
    const wasActive = this.active
    this.setActive(true)
    const ok = this.deps.send({
      type: 'command',
      text,
      source: opts.source ?? 'typed',
      session_id: sessionId,
      context: this.turnContext(),
      attachments,
    })
    if (!ok) {
      this.active = wasActive // nothing reached the engine
      this.notice('error', t('no_engine'))
      return false
    }
    this.submittedSession = sessionId
    this.inFlight = sessionId
    this.turn = null
    this.retried = false
    this.readingStartedInTurn = false
    this.set({
      mode: 'waiting',
      sessionId,
      transcript: '',
      dictating: false,
      pending: opts.source === 'button' ? null : { text: opts.label ?? text, attachments: opts.shown ?? [] },
      hint: null,
      userSpeaking: false,
    })
    this.armTurnTimeout()
    return true
  }

  // ---------------------------------------------------------------- engine events

  onSttPartial(msg: SttPartial): void {
    const s = this.s
    if (msg.session_id !== s.sessionId || s.mode !== 'listening') return
    this.set({ transcript: msg.text })
    this.composer?.setDictation(msg.text)
    this.clearListenTimer()
    this.cancelNudge()
  }

  onSttFinal(msg: SttFinal): void {
    const s = this.s
    if (msg.session_id !== s.sessionId) return
    const active = s.mode === 'listening' || s.mode === 'finalizing'
    if (!active) return
    if (s.mode === 'listening') {
      // The engine ended the utterance itself: we must not send mic.stop {submit: true} as well.
      this.submittedSession = msg.session_id
      this.turn = null
      this.readingStartedInTurn = false
      this.cancelNudge()
      this.setLevel(0)
      this.deps.setListening(false)
      this.clearListenTimer()
    }
    if (s.dictating) {
      // Dictation: the final wording settles in the composer as ordinary text; nothing is sent.
      this.clearTurnTimeout()
      this.inFlight = null
      this.composer?.setDictation(msg.text)
      this.composer?.setDictation(null)
      this.set({ mode: 'typing', transcript: '', dictating: false, userSpeaking: false })
      return
    }
    if (!msg.text.trim()) {
      // Nothing was said after all: give back what was attached, say so, and listen once more.
      this.clearTurnTimeout()
      this.inFlight = null
      this.composer?.dropDictation()
      if (this.sentWithSpeech.length) this.composer?.insertAttachments(this.sentWithSpeech)
      this.sentWithSpeech = []
      this.set({ transcript: '', pending: null, userSpeaking: false })
      if (this.retried) {
        this.retried = false
        this.goIdle()
        return
      }
      this.retried = true
      this.sayNotice(t('say.not_heard'), 'listen', { caption: true })
      return
    }
    if (s.mode === 'listening') {
      this.inFlight = msg.session_id
      this.composer?.clear()
      this.set({ draft: '', attachments: [] })
    }
    this.set((prev) => ({
      mode: 'finalizing',
      transcript: '',
      userSpeaking: false,
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
    const s = this.s
    // The turn for what we just sent: it takes over from the pending row. One store update for both.
    const ours = !!msg.session_id && msg.session_id === this.inFlight
    const takeOver = ours && (s.mode === 'finalizing' || s.mode === 'waiting')
    this.set((prev) => {
      const exists = prev.turns.some((x) => x.turnId === msg.turn_id)
      const turns = exists
        ? prev.turns.map((x) => (x.turnId === msg.turn_id ? { ...x, ...turn, cards: x.cards, actionIds: x.actionIds } : x))
        : [...prev.turns, turn]
      return { turns, selected: `reply:${msg.turn_id}`, ...(takeOver ? { mode: 'waiting' as const, transcript: '', pending: null } : {}) }
    })
    if (ours) {
      // A take being made into a voice: the engine's turn takes over from here (its sample, "Keep it?").
      const cap = this.s.capture
      if (cap && (cap.phase === 'uploading' || cap.phase === 'processing')) this.set({ capture: null })
      this.armTurnTimeout(TURN_SILENT_TIMEOUT_MS)
      this.sentWithSpeech = []
      this.retried = false
      this.turn = { turnId: msg.turn_id, sessionId: msg.session_id!, done: false, speaks: false, speechOver: false, audible: this.readingStartedInTurn }
    }
  }

  onDelta(msg: AssistantDelta): void {
    this.updateTurn(msg.turn_id, (x) => ({ reply: x.reply + msg.text }))
    this.turnProgressed(msg.turn_id)
  }

  /** The turn being answered said something: it is alive, keep waiting for its end. */
  private turnProgressed(turnId: string): void {
    if (this.turn?.turnId === turnId && !this.turn.done && this.turnTimer) this.armTurnTimeout(TURN_SILENT_TIMEOUT_MS)
  }

  onCard(msg: AssistantCard): void {
    this.turnProgressed(msg.turn_id)
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
    this.turnProgressed(msg.turn_id)
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
    // The passage card was confirmed ("ready" or its record button): record once the engine is quiet.
    if (msg.status === 'confirmed' && this.s.actions[msg.action_id]?.kind === 'clone_record') this.armCapture(msg.action_id)
  }

  onDone(msg: AssistantDone): void {
    const problem = msg.error && typeof msg.error === 'object' ? (msg.error as Record<string, unknown>) : null
    const errorText = problem ? String(problem.detail ?? problem.title ?? msg.text ?? '') : null
    const errorCode = problem ? String(problem.code ?? '') || null : null
    this.updateTurn(msg.turn_id, (x) => ({
      done: true,
      reply: msg.text ? msg.text : x.reply,
      replyRecordingId: msg.reply_recording_id ?? x.replyRecordingId,
      error: errorText,
      errorCode,
    }))
    const turn = this.s.turns.find((x) => x.turnId === msg.turn_id)
    if (turn) this.announce(turn.error ?? turn.reply)
    const live = this.turn
    if (!live || live.turnId !== msg.turn_id) return
    live.done = true
    live.speaks = msg.speaks === true
    // Reading audio is the answer ("resume", "read my clipboard", "faster" while reading): the conversation
    // ends so it can be heard. The engine may say so outright; otherwise reading is already playing again.
    live.audible ||= (msg as { audible_effect?: boolean }).audible_effect === true || this.readingPlaying
    this.inFlight = null
    this.clearTurnTimeout()
    if (this.firstRun) this.finishFirstRun()
    const { mode } = this.s
    if (mode === 'waiting' || mode === 'finalizing' || mode === 'speaking') this.settle()
  }

  /** `assistant.speech`: the engine started or finished saying something (07 § Turn-taking). */
  onSpeech(msg: AssistantSpeech): void {
    const key = `${msg.kind}:${msg.turn_id ?? ''}`
    this.debug(`speech-${msg.state} kind=${msg.kind}${msg.reason ? ` reason=${msg.reason}` : ''}`)
    if (msg.state === 'started') {
      if (msg.turn_id) this.turnProgressed(msg.turn_id)
      this.speech.set(key, (this.speech.get(key) ?? 0) + 1)
      this.set({ engineSpeaking: true })
      this.armSpeechGuard(SPEECH_END_GUARD_MS)
      // The take would record the speakers: stop it (the card offers to record again).
      if (this.s.mode === 'recording') this.cancelCapture()
      const s = this.s
      if (!s.open || !this.active) return
      if (s.mode === 'listening') {
        // Speaking wins: the microphone would hear it. What was half-heard is dropped (it may hold the reply).
        this.endSession(false)
        if (!s.dictating) this.composer?.dropDictation()
        else this.composer?.setDictation(null)
        this.set({ transcript: '', userSpeaking: false })
      }
      if (s.mode !== 'typing') this.set({ mode: 'speaking' })
      return
    }
    // ended
    const n = (this.speech.get(key) ?? 0) - 1
    if (n > 0) this.speech.set(key, n)
    else this.speech.delete(key)
    if (msg.kind === 'reply' && msg.turn_id && msg.recording_id) {
      const recordingId = msg.recording_id
      this.updateTurn(msg.turn_id, () => ({ replyRecordingId: recordingId }))
    }
    if (msg.kind === 'reply' && this.turn && msg.turn_id === this.turn.turnId) this.turn.speechOver = true
    if (this.speech.size) return
    this.clearSpeechGuard()
    this.set({ engineSpeaking: false })
    if (this.s.open && this.s.mode === 'speaking') this.settle()
  }

  /** `playback.state`: reading starting again during a turn means the turn's answer is reading audio. */
  onPlayback(p: PlaybackState): void {
    const was = this.readingPlaying
    this.readingPlaying = playing(p)
    if (was || !this.readingPlaying || !this.s.open || !this.active) return
    if (this.inFlight || (this.turn && !this.turn.done)) {
      this.readingStartedInTurn = true
      if (this.turn) this.turn.audible = true
      return
    }
    const { mode, userSpeaking } = this.s
    if (mode === 'listening' && !userSpeaking) {
      // Reading is playing out loud: the microphone would hear it. The user chose to listen to it.
      this.debug('reading-resumed')
      this.goIdle()
    } else if (this.turn) this.turn.audible = true
  }

  onUsageAlert(msg: UsageAlert): void {
    this.notice(msg.level === 'stop' ? 'error' : 'warn', msg.message)
    const s = this.s
    if (!s.open || !this.deps.say) return
    // Said aloud while the bar is open; never over a reply or the user's own words.
    if (s.mode === 'idle' || (s.mode === 'typing' && !this.active) || (s.mode === 'listening' && !s.userSpeaking && !this.engineSpeaking)) {
      this.sayNotice(msg.message, this.active ? 'listen' : 'idle')
    } else this.noticeQueue.push(msg.message)
  }

  onError(msg: ErrorMsg): void {
    const p = msg.problem
    const text = typeof p.detail === 'string' && p.detail ? p.detail : typeof p.title === 'string' ? p.title : 'Engine error.'
    // Only speech input being unavailable switches listening off. Other "unavailable" errors (a voice whose key is
    // blocked, a model not installed, a failed install) arrive while the conversation's mic is open too, and must
    // not turn the bar into typing only: they are said like any other error.
    if (p.code === 'unavailable' && p.feature === 'speech_input' && (this.s.mode === 'listening' || this.deps.mic.live)) {
      // Listening isn't set up here: not an error the user caused. They can type; the bar says why.
      this.engineCanListen = false
      this.speechUnavailable(text)
      return
    }
    this.notice('error', text)
    if (this.s.mode === 'finalizing' || this.s.mode === 'waiting') {
      this.clearTurnTimeout()
      this.inFlight = null
      this.turn = null
      this.set({ pending: null })
      this.settle()
    }
  }

  onJobProgress(msg: JobProgress): void {
    this.set((s) => ({ jobs: { ...s.jobs, [msg.job_id]: { pct: msg.pct, label: msg.label, kind: msg.kind ?? s.jobs[msg.job_id]?.kind ?? null } } }))
    // A voice being made (or designed) reports as it goes: the turn waiting for it is alive.
    const cap = this.s.capture
    if (cap && cap.phase === 'processing' && this.inFlight && (cap.jobId === null || cap.jobId === msg.job_id)) this.armTurnTimeout(CLONE_SILENT_TIMEOUT_MS)
    else if (this.turn && !this.turn.done && this.turnTimer) this.armTurnTimeout(Math.max(TURN_SILENT_TIMEOUT_MS, CLONE_SILENT_TIMEOUT_MS))
  }

  // ---------------------------------------------------------------- recording a voice to clone

  /**
   * The passage card was confirmed (its record button, or "ready"): record as soon as the engine is quiet. Pressing
   * record while the engine is still talking stops it first (the take must not hear the speakers).
   */
  armCapture(actionId: string): void {
    const action = this.s.actions[actionId]
    if (!action || action.kind !== 'clone_record' || !this.s.open || !this.deps.recorder) return
    const cur = this.s.capture
    if (cur && cur.actionId === actionId && cur.phase !== 'failed' && cur.phase !== 'armed') return
    const d = action.details
    this.cancelNudge()
    this.set({
      capture: {
        actionId,
        phase: 'armed',
        passageId: str(d.passage_id),
        passage: str(d.passage),
        language: str(d.language) || 'en',
        name: str(d.name),
        error: null,
        jobId: null,
      },
    })
    if (this.engineSpeaking && !this.inFlight) {
      // The user pressed record over the instructions: cut them, record once the fade has been heard.
      this.deps.send({ type: 'speech.stop', turn_id: null })
      if (this.turn) this.turn.speechOver = true
      this.armSpeechGuard(STOP_GUARD_MS)
      this.set({ mode: 'speaking' })
      return
    }
    if (!this.speakingNow && !this.inFlight && !(this.turn && !this.turn.done)) this.startCapture()
  }

  private startCapture(): void {
    const cap = this.s.capture
    const recorder = this.deps.recorder
    if (!cap || !recorder || !this.s.open) return
    if (this.s.mode === 'listening') {
      this.endSession(false)
      this.composer?.dropDictation()
    }
    this.releaseStream() // one microphone at a time: the conversation's closes
    this.clearListenTimer()
    this.clearTurnTimeout()
    this.clearSpeechGuard()
    this.setActive(true) // reading stays paused
    this.captureVad.reset()
    this.captureVad.endOfSpeechMs = CAPTURE_END_OF_SPEECH_MS
    this.setLevel(0)
    this.set({ mode: 'recording', userSpeaking: false, transcript: '', capture: { ...cap, phase: 'recording', error: null } })
    this.deps.setListening(true, true)
    this.debug('capture-start')
    this.captureChunks = 0
    this.captureMax = 0
    const gen = ++this.captureGen
    void recorder.start({
      onLevel: (level, ms) => this.onCaptureLevel(gen, level, ms),
      onError: (err) => {
        if (gen === this.captureGen) this.failCapture(err.message)
      },
    })
    this.clearCaptureTimers()
    this.captureTimers.push(
      setTimeout(() => {
        if (gen === this.captureGen && this.s.mode === 'recording' && !this.captureVad.hasSpeech) this.failCapture(t('clone.not_heard'))
      }, CAPTURE_NO_SPEECH_MS),
      setTimeout(() => {
        if (gen === this.captureGen && this.s.mode === 'recording') void this.finishCapture()
      }, CAPTURE_MAX_MS),
    )
  }

  private onCaptureLevel(gen: number, level: number, durationMs: number): void {
    if (gen !== this.captureGen || this.s.mode !== 'recording') return
    if (this.captureChunks++ === 0) this.debug('capture-first-chunk')
    this.captureMax = Math.max(this.captureMax, level)
    const event = this.captureVad.push(level, durationMs)
    this.setLevel(meterLevel(level))
    if (event === 'speech-start') this.set({ userSpeaking: true })
    else if (event === 'speech-end') {
      this.debug('capture-end')
      void this.finishCapture()
    }
  }

  /** End the take (silence after the reading, the orb, Tab or Enter) and send it to become a voice. */
  async finishCapture(): Promise<void> {
    const cap = this.s.capture
    const recorder = this.deps.recorder
    if (!cap || cap.phase !== 'recording' || !recorder) return
    this.debug(`capture-levels chunks=${this.captureChunks} max=${Math.round(toDb(this.captureMax))}dB`)
    const gen = ++this.captureGen
    this.clearCaptureTimers()
    this.setLevel(0)
    this.deps.setListening(false)
    const heard = this.captureVad.hasSpeech
    const sessionId = this.deps.newSessionId()
    this.inFlight = sessionId
    this.submittedSession = sessionId
    this.turn = null
    this.retried = false
    this.readingStartedInTurn = false
    this.set({
      mode: 'waiting',
      sessionId,
      userSpeaking: false,
      capture: { ...cap, phase: 'uploading' },
      pending: { text: t('clone.processing'), attachments: [] },
    })
    const take = await recorder.stop()
    if (gen !== this.captureGen) return
    const upload = this.deps.uploadClone
    if (!take || !heard || !upload) {
      this.inFlight = null
      this.set({ pending: null })
      return this.failCapture(t('clone.not_heard'))
    }
    this.armTurnTimeout(CLONE_SILENT_TIMEOUT_MS)
    this.debug(`capture-upload s=${take.seconds.toFixed(1)}`)
    try {
      const res = await upload(take.wav, {
        name: cap.name || null,
        language: cap.language,
        passageId: cap.passageId || null,
        consent: true, // the passage card, with its consent line, was confirmed to start this take
        source: 'recording',
        keep: false,
        sessionId,
        conversationId: this.s.context.conversationId ?? null,
      })
      const now = this.s.capture
      if (gen === this.captureGen && now && now.actionId === cap.actionId && now.phase === 'uploading') {
        this.set({ capture: { ...now, phase: 'processing', jobId: res.job_id } })
      }
    } catch (err) {
      if (gen !== this.captureGen) return
      this.inFlight = null
      this.clearTurnTimeout()
      this.set({ pending: null })
      this.failCapture((err as Error).message || t('clone.failed'))
    }
  }

  /** The take is over without a voice (nothing heard, the microphone failed): say why; the card can record again. */
  private failCapture(message: string): void {
    this.debug(`capture-levels chunks=${this.captureChunks} max=${Math.round(toDb(this.captureMax))}dB`)
    this.captureGen++
    this.clearCaptureTimers()
    this.deps.recorder?.cancel()
    this.setLevel(0)
    this.deps.setListening(false)
    const cap = this.s.capture
    if (cap) this.set({ capture: { ...cap, phase: 'failed', error: message }, userSpeaking: false })
    this.debug('capture-failed')
    if (!this.s.open) return
    this.sayNotice(message, 'idle')
  }

  /** Throw the take away (Esc, the bar closing, the engine starting to talk). */
  cancelCapture(): void {
    const cap = this.s.capture
    if (!cap) return
    this.captureGen++
    this.clearCaptureTimers()
    this.deps.recorder?.cancel()
    this.setLevel(0)
    if (cap.phase === 'recording' || cap.phase === 'armed') {
      this.deps.setListening(false)
      this.set({ capture: { ...cap, phase: 'failed', error: null }, userSpeaking: false })
      if (this.s.mode === 'recording') this.set({ mode: 'idle' })
    }
  }

  private clearCaptureTimers(): void {
    for (const timer of this.captureTimers) clearTimeout(timer)
    this.captureTimers = []
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

  // ---------------------------------------------------------------- the loop

  /**
   * The engine has gone quiet (or a turn ended): what now? Still speaking or a reply still coming → speaking;
   * a turn still being answered → thinking; otherwise the next step: a queued notice, the end of the
   * conversation (reading resumes), or a follow-up.
   */
  private settle(): void {
    const s = this.s
    if (!s.open) return
    if (this.engineSpeaking || this.sayPending) return this.setMode('speaking')
    const turn = this.turn
    if (this.inFlight && !turn?.done) {
      // Still being answered (it spoke a sentence and went on working): keep an eye on it going quiet.
      if (turn && !this.turnTimer) this.armTurnTimeout(TURN_SILENT_TIMEOUT_MS)
      return this.setMode(s.mode === 'finalizing' && !turn ? 'finalizing' : 'waiting')
    }
    if (turn?.done && turn.speaks && !turn.speechOver) {
      // The reply's audio is coming: keep the microphone closed until it has been heard.
      this.setMode('speaking')
      if (!this.speechGuard) this.armSpeechGuard(SPEECH_START_GUARD_MS)
      return
    }
    this.clearSpeechGuard()
    const notice = this.noticeQueue.shift()
    if (notice !== undefined) return this.sayNotice(notice, this.afterSpeech ?? 'listen')
    if (this.s.capture?.phase === 'armed') {
      // "Go ahead." has been said (or the instructions were cut): record the passage now.
      this.afterSpeech = null
      return this.startCapture()
    }
    const after = this.afterSpeech
    this.afterSpeech = null
    if (turn?.audible || after === 'idle') return this.goIdle()
    if (!composerEmpty(this.s)) return this.setMode('typing')
    if (this.s.canListen === false) return this.goIdle()
    this.listen('follow-up')
  }

  private setMode(mode: BarMode): void {
    if (this.s.mode !== mode) this.set({ mode })
  }

  /**
   * Say a notice aloud (and to screen readers), with the microphone closed; then listen or go idle. With
   * `caption` it is also shown, as a line from the assistant (what is spoken is always on screen too, 09 § 7);
   * notices that already have a row or a placeholder of their own don't need one.
   */
  private sayNotice(text: string, then: 'listen' | 'idle', opts: { caption?: boolean } = {}): void {
    this.announce(text)
    if (opts.caption) this.caption(text)
    const say = this.deps.say
    if (this.s.mode === 'listening') {
      this.endSession(false)
      this.composer?.dropDictation()
    }
    this.afterSpeech = then
    if (!say) return this.settle()
    this.sayPending = true
    this.set({ mode: 'speaking', transcript: '', userSpeaking: false })
    if (!this.engineSpeaking) this.armSpeechGuard(SPEECH_START_GUARD_MS)
    this.debug('say')
    const done = () => {
      if (!this.sayPending) return // interrupted meanwhile
      this.sayPending = false
      if (this.s.open && this.s.mode === 'speaking') this.settle()
    }
    say(text).then(done, done)
  }

  /** Show a line from the assistant that is not a turn of its own (what the bar says itself). Returns its id. */
  private caption(text: string): string {
    const turnId = `said:${++captionSeq}`
    this.set((s) => ({
      turns: [
        ...s.turns,
        { turnId, sessionId: null, source: 'button', text: '', attachments: [], recordingId: null, reply: text, replyRecordingId: null, done: true, error: null, cards: [], actionIds: [] },
      ],
      selected: `reply:${turnId}`,
    }))
    return turnId
  }

  /** The conversation is over: release the microphone, resume reading, quiet orb. */
  private goIdle(): void {
    if (this.s.mode === 'listening') {
      this.endSession(false)
      this.composer?.setDictation(null)
    }
    this.clearListenTimer()
    this.clearTurnTimeout()
    this.cancelNudge()
    this.releaseStream()
    this.setActive(false)
    this.deps.setListening(false)
    this.afterSpeech = null
    this.retried = false
    this.sayPending = false
    this.wantListen = false // the speech model finishing later must not reopen a conversation that ended
    this.set({ mode: 'idle', userSpeaking: false, transcript: '', dictating: false })
    this.debug('idle')
  }

  private setActive(active: boolean): void {
    if (this.active === active) return
    this.active = active
    this.deps.send({ type: 'bar.state', open: active })
  }

  /**
   * The one safety timer while the bar waits on the engine's voice: for speech to start (a reply that is
   * coming, our own notice), for it to end, or for a stopped one to fade. When it fires the bar stops waiting.
   */
  private armSpeechGuard(ms: number): void {
    this.clearSpeechGuard()
    this.speechGuard = setTimeout(() => {
      this.speechGuard = null
      this.debug('speech-guard')
      this.speech.clear()
      this.sayPending = false
      if (this.turn?.speaks) this.turn.speechOver = true
      this.set({ engineSpeaking: false })
      if (this.s.open && this.s.mode === 'speaking') this.settle()
    }, ms)
  }

  private armNudge(): void {
    this.cancelNudge()
    this.nudgeTimer = setTimeout(() => {
      this.nudgeTimer = null
      const s = this.s
      const quiet = !s.userSpeaking && !this.vad.hasSpeech && !s.transcript && composerEmpty(s)
      if (!s.open || this.nudged || !quiet || this.inFlight || this.turn || this.speakingNow) return
      if (s.mode !== 'listening' && s.mode !== 'idle') return
      this.nudged = true
      const shortcut = this.deps.spokenShortcut?.() ?? 'Option-Space'
      const text = t(s.canListen === false ? 'say.first_run.typing' : 'say.first_run', { shortcut })
      this.setActive(true)
      this.sayNotice(text, s.canListen === false ? 'idle' : 'listen')
      // Shown with the AI-voice disclosure that has been on screen since the bar opened.
      const shown = `${text} ${t('voices.ai')}`
      const id = this.firstRunCaption
      if (id && this.s.turns.at(-1)?.turnId === id) this.updateTurn(id, () => ({ reply: shown }))
      else this.caption(shown)
    }, FIRST_RUN_NUDGE_MS)
  }

  private cancelNudge(): void {
    if (this.nudgeTimer) clearTimeout(this.nudgeTimer)
    this.nudgeTimer = null
  }

  private finishFirstRun(): void {
    this.firstRun = false
    this.deps.completeOnboarding?.()
  }

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

  private armListenTimeout(delayMs: number): void {
    this.clearListenTimer()
    this.listenTimer = setTimeout(() => {
      this.listenTimer = null
      const s = this.s
      if (s.mode !== 'listening' || this.vad.hasSpeech || s.transcript) return
      // Someone starting to speak at this very moment is not idle: look again shortly.
      if (this.vad.maybeSpeech) return this.armListenTimeout(MIC_OFF_RECHECK_MS)
      // Nothing was said: the conversation is over (no note, no row: the orb goes quiet and reading resumes).
      this.goIdle()
    }, delayMs)
  }

  private clearListenTimer(): void {
    if (this.listenTimer) clearTimeout(this.listenTimer)
    this.listenTimer = null
  }

  /** No turn (or no word from a started one) in time: say so, and listen again. */
  private armTurnTimeout(ms = TURN_START_TIMEOUT_MS): void {
    this.clearTurnTimeout()
    this.turnTimer = setTimeout(() => {
      this.turnTimer = null
      const s = this.s
      if (s.mode === 'finalizing' || s.mode === 'waiting') {
        this.inFlight = null
        this.turn = null
        this.set({ pending: null, dictating: false })
        this.notice('warn', t('no_answer'))
        this.sayNotice(t('say.no_answer'), 'listen')
      }
    }, ms)
  }

  private clearTurnTimeout(): void {
    if (this.turnTimer) clearTimeout(this.turnTimer)
    this.turnTimer = null
  }

  private clearSpeechGuard(): void {
    if (this.speechGuard) clearTimeout(this.speechGuard)
    this.speechGuard = null
  }

  private clearTimers(): void {
    this.clearListenTimer()
    this.clearTurnTimeout()
    this.clearSpeechGuard()
    this.cancelNudge()
    this.clearCaptureTimers()
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
    socket.on('assistant.speech', (m) => controller.onSpeech(m)),
    socket.on('playback.state', (m) => controller.onPlayback(m.playback)),
    socket.on('usage.alert', (m) => controller.onUsageAlert(m)),
    socket.on('job.progress', (m) => controller.onJobProgress(m)),
    socket.on('error', (m) => controller.onError(m)),
  ]
  return () => offs.forEach((off) => off())
}
