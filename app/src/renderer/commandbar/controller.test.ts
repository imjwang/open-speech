import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  AssistantAction,
  AssistantDone,
  AssistantSpeech,
  AssistantTurn,
  ClientMessage,
  ServerMessage,
  SttFinal,
  SttPartial,
} from '@shared/api/ws.gen'
import type { BarOpenedEvent } from '@shared/ipc-channels'

import { FakeMic, FakeSocket } from '../../../test/fakes'
import {
  BUFFER_MAX_SAMPLES,
  BarController,
  FIRST_RUN_NUDGE_MS,
  FOLLOW_UP_CONFIRM_MS,
  FOLLOW_UP_MS,
  MAX_UTTERANCE_MS,
  MIC_IDLE_OFF_MS,
  MIC_OFF_RECHECK_MS,
  PTT_MIN_HOLD_MS,
  SPEECH_START_GUARD_MS,
  STOP_GUARD_MS,
  TURN_SILENT_TIMEOUT_MS,
  TURN_START_TIMEOUT_MS,
  wireBarEvents,
  type BarDeps,
} from './controller'
import type { AttachmentItem } from './composer/attachments'
import { createBarStore, type BarState } from './store'

type Msg<T extends ClientMessage['type']> = Extract<ClientMessage, { type: T }>

const LOUD = 0.2 // ≈ -14 dBFS
const QUIET = 0.0001 // -80 dBFS

/** Let promise callbacks run (the spoken notices settle through promises). */
const flush = async () => {
  for (let i = 0; i < 4; i++) await Promise.resolve()
}

function harness(initial: Partial<BarState> = {}) {
  // Most tests run with speech input known to be set up; the other cases are tested explicitly.
  const store = createBarStore({ canListen: true, ...initial })
  const mic = new FakeMic()
  const sent: ClientMessage[] = []
  const audio: Int16Array[] = []
  const listening: boolean[] = []
  const said: string[] = []
  const sayDone: Array<(ok: boolean) => void> = []
  const log: string[] = []
  let onboarded = 0
  let now = 10_000
  let seq = 0
  let connected = true
  const deps: BarDeps = {
    send: (m) => {
      if (!connected) return false
      sent.push(m)
      return true
    },
    sendAudio: (pcm) => {
      if (!connected) return false
      audio.push(pcm)
      return true
    },
    mic,
    setListening: (on) => {
      listening.push(on)
    },
    endOfSpeechMs: () => 800,
    newSessionId: () => `s${++seq}`,
    newConversationId: () => 'conv',
    now: () => now,
    say: (text) => {
      said.push(text)
      return new Promise<void>((resolve, reject) => sayDone.push((ok) => (ok ? resolve() : reject(new Error('say failed')))))
    },
    completeOnboarding: () => {
      onboarded++
    },
    spokenShortcut: () => 'Option-Space',
    debug: (line) => log.push(line),
  }
  const controller = new BarController(store, deps)
  /** Stands in for the Lexical composer: records what the controller asks of it. */
  const composer = {
    calls: [] as string[],
    dictation: null as string | null,
    inserted: [] as AttachmentItem[],
    clear() {
      this.calls.push('clear')
      this.dictation = null
    },
    insertAttachments(items: AttachmentItem[]) {
      this.calls.push('insert')
      this.inserted.push(...items)
    },
    setDictation(text: string | null) {
      this.calls.push(text === null ? 'keep' : 'dictate')
      if (text !== null) this.dictation = text
    },
    dropDictation() {
      this.calls.push('drop')
      this.dictation = null
    },
  }
  controller.attachComposer(composer)
  const h = {
    store,
    composer,
    mic,
    sent,
    audio,
    listening,
    said,
    log,
    controller,
    get onboarded() {
      return onboarded
    },
    get state(): BarState {
      return store.getState()
    },
    /** Advance both the injected clock and the fake timers. */
    advance(ms: number) {
      now += ms
      vi.advanceTimersByTime(ms)
    },
    /** Advance only the injected clock (for hold-duration checks). */
    tick(ms: number) {
      now += ms
    },
    setConnected(on: boolean) {
      connected = on
    },
    ofType<T extends ClientMessage['type']>(type: T): Msg<T>[] {
      return sent.filter((m): m is Msg<T> => m.type === type)
    },
    barStates(): boolean[] {
      return h.ofType('bar.state').map((m) => m.open)
    },
    open(listen = true, viaHotkey = false, documentId?: string, extra: Partial<BarOpenedEvent> = {}) {
      controller.open({ listen, viaHotkey, ...(documentId ? { documentId } : {}), ...extra })
    },
    /** Speech long enough for the energy VAD to fire speech-start (but not speech-end). */
    speak() {
      mic.feed(QUIET, 300) // the VAD first measures the room (calibration)
      mic.feed(LOUD, 320)
    },
    /** End-of-speech silence (800 ms). */
    silence() {
      mic.feed(QUIET, 800)
    },
    speech(state: 'started' | 'ended', kind: AssistantSpeech['kind'] = 'reply', turn_id: string | null = null, extra: Partial<AssistantSpeech> = {}) {
      controller.onSpeech({ type: 'assistant.speech', state, kind, turn_id, ...extra })
    },
    /** The engine finishes saying what the bar asked it to say (`/assistant/say` resolves). */
    async sayFinished(ok = true) {
      sayDone.shift()?.(ok)
      await flush()
    },
    /**
     * A whole spoken turn: an utterance, its transcript, the turn, and its end. Returns the turn id.
     * The engine's reply speech (if any) is up to the test.
     */
    spokenTurn(turnId = 't1', doneExtra: Partial<AssistantDone> = {}, text = 'read this') {
      h.speak()
      h.silence()
      const session = store.getState().sessionId!
      controller.onSttFinal(final(session, text))
      controller.onTurn(turn(turnId, session, 'spoken', text))
      controller.onDone(done(turnId, { text: 'OK', ...doneExtra }))
      return turnId
    },
  }
  return h
}

const partial = (session_id: string, text: string): SttPartial => ({ type: 'stt.partial', session_id, text })
const final = (session_id: string, text: string): SttFinal => ({ type: 'stt.final', session_id, text })
const turn = (turn_id: string, session_id: string | null, source: 'typed' | 'spoken' | 'button', text = 'hi'): AssistantTurn => ({
  type: 'assistant.turn',
  turn_id,
  session_id,
  source,
  text,
})
const action = (turn_id: string, action_id: string, summary = 'Skip footers'): AssistantAction => ({
  type: 'assistant.action',
  turn_id,
  action_id,
  kind: 'rule.create',
  summary,
})
const done = (turn_id: string, extra: Partial<AssistantDone> = {}): AssistantDone => ({ type: 'assistant.done', turn_id, ...extra })

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('opening: the microphone opens at once unless listening is known to be unavailable', () => {
  it('opens listening: bar.state open (reading pauses), then mic.start with the document context', () => {
    const h = harness()
    h.open(true, false, 'doc-1')

    expect(h.sent[0]).toEqual({ type: 'bar.state', open: true })
    const starts = h.ofType('mic.start')
    expect(starts).toHaveLength(1)
    expect(starts[0]).toMatchObject({ session_id: 's1', mode: 'auto', context: { document_id: 'doc-1', conversation_id: 'conv' } })
    expect(h.state).toMatchObject({ open: true, mode: 'listening', sessionId: 's1', context: { documentId: 'doc-1', conversationId: 'conv' } })
    expect(h.mic.live).toBe(true)
    expect(h.listening.at(-1)).toBe(true)
    expect(h.controller.gate).toBe('send')
  })

  it('opens the mic at once when nothing is known yet (the shell assumes it can listen)', () => {
    const h = harness({ canListen: null })
    h.open(true, true, undefined, { canListen: true })
    expect(h.state.mode).toBe('listening')
    expect(h.mic.opens).toBe(1)
    // Unknown on both sides is also "try".
    const u = harness({ canListen: null })
    u.open(true, true, undefined, { canListen: null })
    expect(u.state.mode).toBe('listening')
    expect(u.mic.opens).toBe(1)
  })

  it('canListen:false never touches the microphone (no OS prompt, no indicator)', () => {
    const h = harness({ canListen: null })
    h.open(true, true, undefined, { canListen: false })
    expect(h.state.mode).toBe('idle')
    expect(h.mic.opens).toBe(0)
    expect(h.mic.prepared).toBe(0)
    expect(h.ofType('mic.start')).toHaveLength(0)
    // Nothing is going on, so reading isn't paused either.
    expect(h.barStates()).toEqual([])
    h.controller.toggle()
    h.open(true, true)
    expect(h.mic.opens).toBe(0)
  })

  it('what the engine says replaces what the shell remembered', () => {
    const h = harness({ canListen: null })
    h.controller.setCanListen(false, 'Listening needs the speech model.')
    h.open(true, true, undefined, { canListen: true })
    expect(h.mic.opens).toBe(0)
    expect(h.state.mode).toBe('idle')
  })

  it('listens as soon as the engine says it can, if the user has not started typing', () => {
    const h = harness({ canListen: null })
    h.open(true, true, undefined, { canListen: false })
    h.controller.setCanListen(true)
    expect(h.state.mode).toBe('listening')

    const typed = harness({ canListen: null })
    typed.open(true, true, undefined, { canListen: false })
    typed.controller.setDraft('read', [])
    typed.controller.setCanListen(true)
    expect(typed.state.mode).toBe('idle')
    expect(typed.mic.opens).toBe(0)
  })

  it('a conversation that went idle is not reopened when the engine reports listening again later', () => {
    const h = harness({ canListen: null })
    h.open(true, true, undefined, { canListen: true })
    h.controller.setCanListen(true)
    h.advance(MIC_IDLE_OFF_MS)
    expect(h.state.mode).toBe('idle')
    h.controller.setCanListen(false)
    h.controller.setCanListen(true) // e.g. a capabilities refresh
    expect(h.state.mode).toBe('idle')
    expect(h.ofType('mic.start')).toHaveLength(1)
  })

  it('opens idle when listen is false', () => {
    const h = harness()
    h.open(false)
    expect(h.state.mode).toBe('idle')
    expect(h.ofType('mic.start')).toHaveLength(0)
    expect(h.mic.opens).toBe(0)
  })

  it('a hotkey press while the bar is open and idle listens again', () => {
    const h = harness()
    h.open(false)
    h.open(true, true)
    expect(h.state.mode).toBe('listening')
    expect(h.ofType('mic.start')).toHaveLength(1)
  })

  it('a tray re-open while idle with an empty composer listens; with text it does not', () => {
    const h = harness()
    h.open(false)
    h.open(true)
    expect(h.state.mode).toBe('listening')
    const typed = harness()
    typed.open(false)
    typed.controller.setDraft('read', [])
    typed.controller.keystroke()
    typed.open(true)
    expect(typed.ofType('mic.start')).toHaveLength(0)
  })

  it('close ends the session without submitting, releases the microphone and resumes reading', () => {
    const h = harness()
    h.open(true)
    h.controller.close()

    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false, attachments: [] }])
    expect(h.sent.at(-1)).toEqual({ type: 'bar.state', open: false })
    expect(h.mic.live).toBe(false)
    expect(h.listening.at(-1)).toBe(false)
    expect(h.state).toMatchObject({ open: false, mode: 'idle', transcript: '', draft: '', attachments: [] })
    expect(h.composer.calls.at(-1)).toBe('clear')
  })

  it('a row chosen in an earlier open is forgotten: Enter acts on the default row again', () => {
    const h = harness()
    h.open(true)
    h.store.setState({ selected: 'history' })
    h.controller.close()
    h.open(true)
    expect(h.state.selected).toBe('')
  })

  it('close is a no-op when already closed', () => {
    const h = harness()
    h.controller.close()
    expect(h.sent).toHaveLength(0)
  })

  it('logs when the first audio arrived after the open (content-free)', () => {
    const h = harness()
    h.open(true)
    h.tick(42)
    h.mic.feed(QUIET, 32)
    expect(h.log.find((l) => l.startsWith('first-pcm'))).toBe('first-pcm ms=42 mic=42')
  })
})

describe('no lost first words: audio before the engine is there is kept', () => {
  it('buffers while disconnected and sends everything in order once mic.start is accepted', () => {
    const h = harness()
    h.setConnected(false)
    h.open(true)
    expect(h.state.mode).toBe('listening') // the orb listens: what is said is kept
    expect(h.controller.gate).toBe('buffer')
    for (let i = 1; i <= 5; i++) h.mic.handlers!.onPcm(new Int16Array(4).fill(i))
    expect(h.audio).toHaveLength(0)

    h.setConnected(true)
    h.controller.connected()
    expect(h.sent.map((m) => m.type)).toEqual(['bar.state', 'mic.start'])
    expect(h.ofType('mic.start')[0]).toMatchObject({ session_id: 's1' })
    expect(h.audio.map((a) => a[0])).toEqual([1, 2, 3, 4, 5])
    expect(h.controller.gate).toBe('send')
    h.mic.handlers!.onPcm(new Int16Array(4).fill(6))
    expect(h.audio.map((a) => a[0])).toEqual([1, 2, 3, 4, 5, 6])
  })

  it('keeps at most 30 s, dropping the oldest', () => {
    const h = harness()
    h.setConnected(false)
    h.open(true)
    const chunk = 16_000 // one second each
    for (let i = 0; i < 35; i++) h.mic.handlers!.onPcm(new Int16Array(chunk).fill(i))
    expect(h.controller.bufferedAudio).toBe(BUFFER_MAX_SAMPLES)
    h.setConnected(true)
    h.controller.connected()
    expect(h.audio).toHaveLength(30)
    expect(h.audio[0]![0]).toBe(5)
    expect(h.audio.at(-1)![0]).toBe(34)
  })

  it('an utterance that ended before the engine connected is sent, with mic.stop {submit}, on connect', () => {
    const h = harness()
    h.setConnected(false)
    h.open(true)
    h.speak()
    h.silence() // the local VAD ends the utterance while nothing can be sent yet
    expect(h.state.mode).toBe('finalizing')
    expect(h.log).toContain('vad-end')
    h.setConnected(true)
    h.controller.connected()
    const types = h.sent.map((m) => m.type)
    expect(types).toEqual(['bar.state', 'mic.start', 'mic.stop'])
    expect(h.ofType('mic.stop')[0]).toEqual({ type: 'mic.stop', session_id: 's1', submit: true, attachments: [] })
    expect(h.audio.length).toBeGreaterThan(30) // the whole utterance, from its first chunk
  })

  it('speech input unavailable after opening: microphone closed, buffer dropped, typing only', () => {
    const h = harness({ canListen: null })
    h.setConnected(false)
    h.open(true)
    h.mic.feed(QUIET, 320)
    expect(h.controller.bufferedAudio).toBeGreaterThan(0)
    h.setConnected(true)
    h.controller.connected()
    h.controller.onError({ type: 'error', problem: { code: 'unavailable', feature: 'speech_input', detail: 'Listening needs the speech model.' } })

    expect(h.mic.live).toBe(false)
    expect(h.controller.bufferedAudio).toBe(0)
    expect(h.state).toMatchObject({ canListen: false, hint: 'Listening needs the speech model.' })
    expect(h.state.notices).toHaveLength(0) // not an error the user caused
    // During a conversation it is said aloud, then the bar stays for typing.
    expect(h.said).toEqual(['Listening needs the speech model.'])
    expect(h.state.announcement?.text).toBe('Listening needs the speech model.')
  })

  it('another feature being unavailable (a blocked voice, a missing model) never switches listening off', () => {
    const h = harness()
    h.open(true)
    h.controller.onError({ type: 'error', problem: { code: 'unavailable', feature: 'voice', detail: "Google's voices need Cloud Text-to-Speech enabled." } })
    expect(h.mic.live).toBe(true)
    expect(h.state.canListen).toBe(true)
    expect(h.state.notices.map((n) => n.text)).toEqual(["Google's voices need Cloud Text-to-Speech enabled."])
  })

  it('unavailable before connecting (capabilities) drops what was buffered without sending it', () => {
    const h = harness({ canListen: null })
    h.setConnected(false)
    h.open(true)
    h.mic.feed(QUIET, 320)
    h.controller.setCanListen(false, 'Listening needs the speech model.')
    expect(h.mic.live).toBe(false)
    expect(h.controller.bufferedAudio).toBe(0)
    h.setConnected(true)
    h.controller.connected()
    expect(h.ofType('mic.start')).toHaveLength(0)
    expect(h.audio).toHaveLength(0)
  })

  it('a microphone that cannot open (permission off) says why and goes idle', async () => {
    const h = harness()
    h.mic.deny = new Error('Microphone access is off. Allow it for Open Speech, or type instead.')
    h.open(true)
    expect(h.state.micError).toMatch(/Microphone access is off/)
    expect(h.said).toEqual(['Microphone access is off. Allow it for Open Speech, or type instead.'])
    await h.sayFinished()
    expect(h.state.mode).toBe('idle')
    expect(h.mic.live).toBe(false)
    expect(h.barStates().at(-1)).toBe(false)
  })
})

describe('listening → typing', () => {
  it('any keystroke while listening sends mic.stop {submit:false} and switches to typing, keeping the text', () => {
    const h = harness()
    h.open(true)
    h.controller.onSttPartial(partial('s1', 'read the'))
    h.controller.keystroke()

    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false, attachments: [] }])
    expect(h.state.mode).toBe('typing')
    expect(h.composer.dictation).toBe('read the')
    expect(h.composer.calls.slice(-2)).toEqual(['dictate', 'keep'])
    // Still a conversation: reading stays paused while the user types the rest.
    expect(h.barStates()).toEqual([true])
  })

  it('stops forwarding audio once the bar leaves listening', () => {
    const h = harness()
    h.open(true)
    const handlers = h.mic.handlers!
    handlers.onPcm(new Int16Array(4))
    expect(h.audio).toHaveLength(1)
    h.controller.keystroke()
    handlers.onPcm(new Int16Array(4))
    expect(h.audio).toHaveLength(1)
  })

  it('Tab toggles listening ↔ idle with nothing heard, and listening ↔ typing with something kept', () => {
    const h = harness()
    h.open(false)
    h.controller.toggle()
    expect(h.state.mode).toBe('listening')
    h.controller.toggle()
    expect(h.state.mode).toBe('idle')
    expect(h.mic.live).toBe(false)
    h.controller.toggle()
    h.controller.onSttPartial(partial('s2', 'read'))
    h.controller.toggle()
    expect(h.state.mode).toBe('typing')
  })

  it('partials replace the input in place; stale-session partials are ignored', () => {
    const h = harness()
    h.open(true)
    h.controller.onSttPartial(partial('s1', 'read'))
    h.controller.onSttPartial(partial('s1', 'read this'))
    h.controller.onSttPartial(partial('other', 'nope'))
    expect(h.state.transcript).toBe('read this')
    expect(h.composer.dictation).toBe('read this')
  })

  it('without any speech after opening, the conversation goes idle quietly (no "Microphone off" note)', () => {
    const h = harness()
    h.open(true)
    h.advance(MIC_IDLE_OFF_MS - 1)
    expect(h.state.mode).toBe('listening')
    h.advance(1)
    expect(h.state).toMatchObject({ mode: 'idle', hint: null })
    expect(h.state.notices).toHaveLength(0)
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false, attachments: [] }])
    expect(h.mic.live).toBe(false)
    expect(h.barStates()).toEqual([true, false])
  })

  it('does not go idle on the first instant of speech', () => {
    const h = harness()
    h.open(true)
    h.mic.feed(QUIET, 300)
    h.advance(MIC_IDLE_OFF_MS - 100)
    h.mic.feed(LOUD, 96) // a word begins just as the timeout lands: not yet long enough to count as speech
    h.advance(100)
    expect(h.state.mode).toBe('listening')
    h.mic.feed(LOUD, 320)
    h.advance(MIC_OFF_RECHECK_MS)
    expect(h.state.mode).toBe('listening')
    expect(h.ofType('mic.stop')).toHaveLength(0)
  })

  it('a stray noise at the timeout only postpones it', () => {
    const h = harness()
    h.open(true)
    h.mic.feed(QUIET, 300)
    h.advance(MIC_IDLE_OFF_MS - 100)
    h.mic.feed(LOUD, 64)
    h.advance(100)
    expect(h.state.mode).toBe('listening')
    h.mic.feed(QUIET, 320)
    h.advance(MIC_OFF_RECHECK_MS)
    expect(h.state).toMatchObject({ mode: 'idle', hint: null })
  })

  it('keeps listening past the timeout once speech started or a partial arrived', () => {
    const h = harness()
    h.open(true)
    h.speak()
    expect(h.state.userSpeaking).toBe(true)
    h.advance(MIC_IDLE_OFF_MS * 2)
    expect(h.state.mode).toBe('listening')
    const p = harness()
    p.open(true)
    p.controller.onSttPartial(partial('s1', 'hello'))
    p.advance(MIC_IDLE_OFF_MS * 2)
    expect(p.state.mode).toBe('listening')
  })
})

describe('submit: each utterance submits at most once', () => {
  it('enter after speech sends mic.stop {submit:true} once; vad and ptt then do nothing', () => {
    const h = harness()
    h.open(true)
    h.speak()

    expect(h.controller.submit('enter')).toBe(true)
    expect(h.controller.submit('vad')).toBe(false)
    expect(h.controller.submit('ptt')).toBe(false)
    expect(h.controller.submit('enter')).toBe(false)

    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: true, attachments: [] }])
    expect(h.state.mode).toBe('finalizing')
    // The stream stays open for the follow-up; nothing more is sent.
    expect(h.mic.live).toBe(true)
    expect(h.controller.gate).toBe('drop')
  })

  it('end-of-speech silence (energy VAD) submits once by itself', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.silence()
    expect(h.state.mode).toBe('finalizing')
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: true, attachments: [] }])
    expect(h.controller.submit('enter')).toBe(false)
  })

  it('sound that never pauses (a TV started after the room was measured) is cut off: the mic does not stay open forever', () => {
    const h = harness()
    h.open(true)
    h.speak()
    expect(h.state.userSpeaking).toBe(true)
    h.mic.feed(LOUD, MAX_UTTERANCE_MS - 400)
    expect(h.ofType('mic.stop')).toEqual([])
    h.mic.feed(LOUD, 600)
    expect(h.log).toContain('utterance-cap')
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: true, attachments: [] }])
    expect(h.state.mode).toBe('finalizing')
  })

  it('a partial transcript counts as heard even if the VAD saw no speech', () => {
    const h = harness()
    h.open(true)
    h.controller.onSttPartial(partial('s1', 'read this'))
    expect(h.controller.submit('enter')).toBe(true)
  })

  it('enter with nothing heard keeps listening and sends nothing', () => {
    const h = harness()
    h.open(true)
    expect(h.controller.submit('enter')).toBe(false)
    expect(h.state.mode).toBe('listening')
    expect(h.ofType('mic.stop')).toHaveLength(0)
  })

  it('push-to-talk: releasing a held hotkey after speech submits once', () => {
    const h = harness()
    h.open(true, true)
    expect(h.controller.openedViaHotkey).toBe(true)
    h.tick(PTT_MIN_HOLD_MS + 50)
    h.speak()
    h.controller.hotkeyReleased()
    h.controller.hotkeyReleased()
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: true, attachments: [] }])
    expect(h.state.mode).toBe('finalizing')
    expect(h.controller.openedViaHotkey).toBe(false)
  })

  it('a quick hotkey tap (shorter than PTT_MIN_HOLD_MS) keeps listening', () => {
    const h = harness()
    h.open(true, true)
    h.tick(PTT_MIN_HOLD_MS - 100)
    h.speak()
    h.controller.hotkeyReleased()
    expect(h.state.mode).toBe('listening')
  })

  it('push-to-talk released without speech cancels quietly', () => {
    const h = harness()
    h.open(true, true)
    h.tick(PTT_MIN_HOLD_MS + 50)
    h.controller.hotkeyReleased()
    expect(h.state.mode).toBe('idle')
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false, attachments: [] }])
  })

  it('stt.final while listening means the engine submitted: no mic.stop is sent', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.controller.onSttFinal(final('s1', 'read this'))
    expect(h.state).toMatchObject({ mode: 'finalizing', transcript: '', pending: { text: 'read this', attachments: [] } })
    expect(h.composer.calls).toContain('clear')
    expect(h.ofType('mic.stop')).toHaveLength(0)
    expect(h.controller.submit('enter')).toBe(false)
    expect(h.state.announcement?.text).toBe('You said: read this')
  })

  it('mic.start is a normal assistant turn (not transcribe-only) with nothing typed', () => {
    const h = harness()
    h.open(true)
    expect(h.ofType('mic.start')[0]).toMatchObject({ transcribe_only: false })
  })
})

describe('speech as a command, or as dictation', () => {
  const report: AttachmentItem = { kind: 'file', path: '/a/report.pdf', name: 'report.pdf', fileKind: 'pdf' }

  it('with nothing typed, speech is a command: it is sent with the attached badges and the composer is freed', () => {
    const h = harness()
    h.open(false)
    h.controller.setDraft('', [report])
    h.controller.toggle()
    expect(h.ofType('mic.start')[0]).toMatchObject({ transcribe_only: false })
    h.speak()
    h.controller.onSttPartial(partial('s1', 'read these'))
    expect(h.controller.submit('vad')).toBe(true)
    expect(h.ofType('mic.stop').at(-1)).toEqual({
      type: 'mic.stop',
      session_id: 's1',
      submit: true,
      attachments: [{ kind: 'file', path: '/a/report.pdf', name: 'report.pdf' }],
    })
    expect(h.state).toMatchObject({ mode: 'finalizing', draft: '', attachments: [] })
    expect(h.state.pending).toEqual({ text: 'read these', attachments: [{ kind: 'file', title: 'report.pdf' }] })
  })

  it('if nothing was heard after all, the badges come back', () => {
    const h = harness()
    h.open(false)
    h.controller.setDraft('', [report])
    h.controller.toggle()
    h.speak()
    h.controller.submit('enter')
    h.controller.onSttFinal(final('s1', ''))
    expect(h.composer.inserted).toEqual([report])
    expect(h.state.pending).toBeNull()
  })

  it('with text already typed, speech is dictation: transcribe-only, added to the text, nothing sent', () => {
    const h = harness()
    h.open(false)
    h.composer.calls.length = 0
    h.controller.setDraft('summarize', [])
    h.controller.toggle()
    expect(h.ofType('mic.start')[0]).toMatchObject({ transcribe_only: true })
    expect(h.state.dictating).toBe(true)
    h.speak()
    h.controller.onSttPartial(partial('s1', 'the second chapter'))
    h.controller.submit('vad')
    expect(h.ofType('mic.stop').at(-1)).toMatchObject({ submit: true, attachments: [] })
    h.controller.onSttFinal(final('s1', 'the second chapter.'))
    expect(h.composer.dictation).toBe('the second chapter.')
    expect(h.composer.calls.slice(-2)).toEqual(['dictate', 'keep'])
    expect(h.composer.calls).not.toContain('clear')
    expect(h.state).toMatchObject({ mode: 'typing', dictating: false, pending: null })
    expect(h.ofType('command')).toHaveLength(0)
  })

  it('what is typed while an utterance is still being processed is kept, and the bar does not listen over it', () => {
    const h = harness()
    h.open(true)
    h.composer.calls.length = 0
    h.speak()
    h.controller.onSttPartial(partial('s1', 'pause'))
    h.controller.submit('vad')
    h.controller.setDraft('faster', [])
    h.controller.onSttFinal(final('s1', 'pause'))
    h.controller.onTurn(turn('t1', 's1', 'spoken', 'pause'))
    h.controller.onDone(done('t1'))
    expect(h.state.draft).toBe('faster')
    expect(h.composer.calls.filter((c) => c === 'clear')).toHaveLength(1)
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.start')).toHaveLength(1)
  })

  it('everything said while the bar stays open shares one conversation id', () => {
    const h = harness()
    h.open(true)
    h.controller.keystroke()
    h.controller.submitCommand('faster')
    expect(h.ofType('mic.start')[0]?.context).toMatchObject({ conversation_id: 'conv' })
    expect(h.ofType('command')[0]?.context).toMatchObject({ conversation_id: 'conv' })
  })
})

describe('the loop: speaking → follow-up → idle', () => {
  it('nothing is sent while the engine speaks, whatever it is saying', () => {
    for (const kind of ['reply', 'say', 'preview', 'recording'] as const) {
      const h = harness()
      h.open(true)
      h.speech('started', kind, kind === 'reply' ? 't9' : null)
      expect(h.controller.gate).toBe('drop')
      const before = h.audio.length
      h.mic.feed(LOUD, 640)
      expect(h.audio.length, kind).toBe(before)
      // It spoke over a listening session: that session is dropped (it may hold the reply's own words).
      expect(h.ofType('mic.stop').at(-1), kind).toMatchObject({ submit: false })
      expect(h.state.mode, kind).toBe('speaking')
      // …and the VAD never heard it as the user.
      expect(h.state.userSpeaking, kind).toBe(false)
    }
  })

  it('a spoken reply: speaking while it plays, then listening for a follow-up (the room already measured)', () => {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: true })
    expect(h.state.mode).toBe('speaking') // the reply's audio is coming: the microphone stays closed
    expect(h.controller.gate).toBe('drop')
    h.speech('started', 'reply', 't1')
    h.mic.feed(LOUD, 640)
    expect(h.state.userSpeaking).toBe(false)
    h.speech('ended', 'reply', 't1', { reason: 'finished', recording_id: 'rec_reply' })

    expect(h.state.mode).toBe('listening')
    expect(h.ofType('mic.start').map((m) => m.session_id)).toEqual(['s1', 's2'])
    expect(h.controller.gate).toBe('send')
    expect(h.state.turns.at(-1)?.replyRecordingId).toBe('rec_reply') // the reply recording arrives with `ended`
    // The same stream: no new permission prompt, no setup.
    expect(h.mic.opens).toBe(1)
    // A follow-up is heard from its first chunk: no second room measurement.
    h.mic.feed(LOUD, 160)
    expect(h.state.userSpeaking).toBe(true)
  })

  it('follow-up timeout → idle with no note, the microphone released and reading resumed', () => {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: true })
    h.speech('started', 'reply', 't1')
    h.speech('ended', 'reply', 't1')
    expect(h.state.mode).toBe('listening')
    h.advance(FOLLOW_UP_MS - 1)
    expect(h.state.mode).toBe('listening')
    h.advance(1)
    expect(h.state).toMatchObject({ mode: 'idle', hint: null })
    expect(h.state.notices).toHaveLength(0)
    expect(h.mic.live).toBe(false)
    expect(h.barStates()).toEqual([true, false])
    expect(h.listening.at(-1)).toBe(false)
  })

  it('a turn that will not speak (`speaks: false`) listens for the follow-up at once', () => {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: false })
    expect(h.state.mode).toBe('listening')
    expect(h.ofType('mic.start')).toHaveLength(2)
  })

  it('reply speech can start before the turn is done (sentence by sentence): listen only after both', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.silence()
    h.controller.onSttFinal(final('s1', 'tell me about this'))
    h.controller.onTurn(turn('t1', 's1', 'spoken'))
    h.speech('started', 'reply', 't1')
    expect(h.state.mode).toBe('speaking')
    h.controller.onDone(done('t1', { text: 'It is about…', speaks: true }))
    expect(h.state.mode).toBe('speaking')
    h.speech('ended', 'reply', 't1')
    expect(h.state.mode).toBe('listening')
  })

  it('reply speech that ended before `done` arrived does not leave the bar waiting', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.silence()
    h.controller.onSttFinal(final('s1', 'hi'))
    h.controller.onTurn(turn('t1', 's1', 'spoken'))
    h.speech('started', 'reply', 't1')
    h.speech('ended', 'reply', 't1')
    expect(h.state.mode).toBe('waiting') // still answering
    h.controller.onDone(done('t1', { speaks: true }))
    expect(h.state.mode).toBe('listening')
  })

  it('a reply that never starts: after the guard the bar listens anyway', () => {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: true })
    h.advance(SPEECH_START_GUARD_MS - 1)
    expect(h.state.mode).toBe('speaking')
    h.advance(1)
    expect(h.state.mode).toBe('listening')
  })

  it('a turn whose effect is reading audio ends the conversation instead of listening again', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.silence()
    h.controller.onSttFinal(final('s1', 'resume'))
    h.controller.onTurn(turn('t1', 's1', 'spoken', 'resume'))
    // The command resumed reading (the engine releases the bar's hold for it).
    h.controller.onPlayback({ state: 'loading', document_id: 'd1' })
    h.controller.onDone(done('t1', { text: '', speaks: false }))
    expect(h.state.mode).toBe('idle')
    expect(h.ofType('mic.start')).toHaveLength(1)
    expect(h.mic.live).toBe(false)
    expect(h.barStates()).toEqual([true, false])
  })

  it('…also when the engine says so outright (`audible_effect`)', () => {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: false, audible_effect: true } as Partial<AssistantDone>)
    expect(h.state.mode).toBe('idle')
  })

  it('reading starting while the bar listens for a follow-up ends the conversation (the mic would hear it)', () => {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: false })
    expect(h.state.mode).toBe('listening')
    h.controller.onPlayback({ state: 'playing', document_id: 'd1' })
    expect(h.state.mode).toBe('idle')
  })

  it('reading that was paused by the conversation is not an effect of the turn', () => {
    const h = harness()
    h.controller.onPlayback({ state: 'playing', document_id: 'd1' })
    h.open(true)
    h.controller.onPlayback({ state: 'paused', document_id: 'd1' }) // the bar's hold
    h.spokenTurn('t1', { speaks: false })
    expect(h.state.mode).toBe('listening')
  })

  it('a confirmation is asked aloud, then the bar listens longer for "yes" or "no"', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.silence()
    h.controller.onSttFinal(final('s1', 'skip the footers'))
    h.controller.onTurn(turn('t1', 's1', 'spoken', 'skip the footers'))
    h.controller.onAction(action('t1', 'a1'))
    h.controller.onDone(done('t1', { text: 'Skip footers?', speaks: true }))
    // The question is still being spoken: the microphone must not open yet.
    expect(h.state.mode).toBe('speaking')
    expect(h.ofType('mic.start')).toHaveLength(1)
    h.speech('started', 'reply', 't1')
    h.speech('ended', 'reply', 't1')
    expect(h.state.mode).toBe('listening')
    h.advance(FOLLOW_UP_MS)
    expect(h.state.mode).toBe('listening')
    h.advance(FOLLOW_UP_CONFIRM_MS - FOLLOW_UP_MS)
    expect(h.state.mode).toBe('idle')
  })

  it('typed turns are answered aloud and get follow-up listening too', () => {
    const h = harness()
    h.open(false)
    h.controller.submitCommand('what is this about')
    expect(h.barStates()).toEqual([true]) // reading pauses for the answer
    h.controller.onTurn(turn('t1', 's1', 'typed'))
    h.controller.onDone(done('t1', { text: 'A paper.', speaks: true }))
    expect(h.state.mode).toBe('speaking')
    h.speech('started', 'reply', 't1')
    h.speech('ended', 'reply', 't1')
    expect(h.state.mode).toBe('listening')
    expect(h.ofType('mic.start')).toHaveLength(1)
  })

  it('a toolbar press is answered and listened after, like any turn', () => {
    const h = harness()
    h.open(false)
    h.controller.submitCommand('show voices', [], { source: 'button' })
    h.controller.onTurn(turn('t1', 's1', 'button', 'show voices'))
    h.controller.onDone(done('t1', { speaks: false }))
    expect(h.state.mode).toBe('listening')
  })

  it('a typed turn without the means to listen goes idle after the answer', () => {
    const h = harness({ canListen: false })
    h.open(true)
    h.controller.submitCommand('hello')
    h.controller.onTurn(turn('t1', 's1', 'typed'))
    h.controller.onDone(done('t1', { speaks: false }))
    expect(h.state.mode).toBe('idle')
    expect(h.mic.opens).toBe(0)
    expect(h.barStates()).toEqual([true, false])
  })

  it('the assistant speaking after the bar closed does not open anything', () => {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: true })
    h.controller.close()
    h.speech('started', 'reply', 't1')
    h.speech('ended', 'reply', 't1')
    expect(h.state).toMatchObject({ open: false, mode: 'idle' })
    expect(h.mic.live).toBe(false)
  })

  it('a reconnected engine is told again that a conversation is on', () => {
    const h = harness()
    h.open(true)
    h.controller.connected()
    expect(h.barStates()).toEqual([true, true])
    h.controller.toggle() // → idle
    h.controller.connected()
    expect(h.barStates()).toEqual([true, true, false])
  })

  it('a dropped connection mid-reply stops waiting for its end', () => {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: true })
    h.speech('started', 'reply', 't1')
    h.controller.disconnected()
    expect(h.state.engineSpeaking).toBe(false)
    expect(h.state.mode).toBe('listening')
  })
})

describe('interrupting by key', () => {
  function speaking() {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: true })
    h.speech('started', 'reply', 't1')
    return h
  }

  it('Tab or the orb while the assistant speaks: speech.stop for that turn, then listen (sending after `ended`)', () => {
    const h = speaking()
    h.controller.toggle()
    expect(h.ofType('speech.stop')).toEqual([{ type: 'speech.stop', turn_id: 't1' }])
    expect(h.state.mode).toBe('listening') // the orb says "listening" at once
    expect(h.controller.gate).toBe('drop') // …but the fade and the echo tail are not the user's words
    h.mic.feed(LOUD, 320)
    expect(h.audio.slice(-1)[0]).toBeDefined()
    const sentBefore = h.audio.length
    h.speech('ended', 'reply', 't1', { reason: 'stopped' })
    expect(h.controller.gate).toBe('send')
    h.mic.feed(LOUD, 64)
    expect(h.audio.length).toBeGreaterThan(sentBefore)
    expect(h.ofType('mic.start')).toHaveLength(2)
    // The cut reply does not come back to start another follow-up.
    h.controller.onDone(done('t1', { speaks: true }))
    expect(h.state.mode).toBe('listening')
    expect(h.ofType('mic.start')).toHaveLength(2)
  })

  it('the hotkey while it speaks (bargeIn from the shell) does the same', () => {
    const h = speaking()
    h.open(true, true, undefined, { bargeIn: true })
    expect(h.ofType('speech.stop')).toEqual([{ type: 'speech.stop', turn_id: 't1' }])
    expect(h.state.mode).toBe('listening')
  })

  it('opening the bar to talk while the engine is still speaking cuts it off', () => {
    const h = speaking()
    h.controller.close()
    h.open(true, true)
    expect(h.ofType('speech.stop')).toHaveLength(1)
    expect(h.state.mode).toBe('listening')
  })

  it('Esc while it speaks stops it without listening: the conversation is over', () => {
    const h = speaking()
    h.controller.interrupt('idle')
    expect(h.ofType('speech.stop')).toEqual([{ type: 'speech.stop', turn_id: 't1' }])
    expect(h.state.mode).toBe('idle')
    expect(h.mic.live).toBe(false)
    h.speech('ended', 'reply', 't1', { reason: 'stopped' })
    expect(h.state.mode).toBe('idle')
    expect(h.ofType('mic.start')).toHaveLength(1)
  })

  it('a stopped reply whose `ended` never comes: listening starts sending after a short guard', () => {
    const h = speaking()
    h.controller.toggle()
    h.advance(STOP_GUARD_MS)
    expect(h.state.engineSpeaking).toBe(false)
    expect(h.controller.gate).toBe('send')
  })

  it('a reply that is coming but has not started yet can be cut off too', () => {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: true })
    h.controller.toggle()
    expect(h.ofType('speech.stop')).toEqual([{ type: 'speech.stop', turn_id: 't1' }])
    expect(h.controller.gate).toBe('send')
  })

  it('a prompt without a turn is stopped as "everything"', () => {
    const h = harness()
    h.open(true)
    h.speech('started', 'preview', null)
    h.controller.toggle()
    expect(h.ofType('speech.stop')).toEqual([{ type: 'speech.stop', turn_id: null }])
  })

  it('typing while the assistant speaks keeps the text and does not listen afterwards', () => {
    const h = speaking()
    h.controller.setDraft('next chapter', [])
    h.speech('ended', 'reply', 't1')
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.start')).toHaveLength(1)
  })

  it('a transport press ends the conversation first (stops the voice, releases the microphone, resumes reading)', () => {
    const h = speaking()
    h.controller.endConversation()
    expect(h.ofType('speech.stop')).toEqual([{ type: 'speech.stop', turn_id: null }])
    expect(h.state.mode).toBe('idle')
    expect(h.mic.live).toBe(false)
    expect(h.barStates()).toEqual([true, false])
    const listening = harness()
    listening.open(true)
    listening.controller.endConversation()
    expect(listening.ofType('speech.stop')).toHaveLength(0)
    expect(listening.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false, attachments: [] }])
    expect(listening.state.mode).toBe('idle')
  })
})

describe('spoken notices', () => {
  it("not heard: \"Sorry, I didn't catch that\", one more listen, then idle", async () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.controller.submit('enter')
    h.controller.onSttFinal(final('s1', '   '))
    expect(h.said).toEqual(["Sorry, I didn't catch that."])
    expect(h.state.mode).toBe('speaking')
    expect(h.state.announcement?.text).toBe("Sorry, I didn't catch that.")
    h.speech('started', 'say')
    h.speech('ended', 'say')
    await h.sayFinished()
    expect(h.state.mode).toBe('listening')
    expect(h.ofType('mic.start')).toHaveLength(2)
    h.speak()
    h.controller.submit('enter')
    h.controller.onSttFinal(final('s2', ''))
    expect(h.said).toHaveLength(1) // said once
    expect(h.state.mode).toBe('idle')
    h.advance(TURN_START_TIMEOUT_MS)
    expect(h.state.notices).toHaveLength(0)
  })

  it('waits for the spoken notice to be heard to its end, whichever arrives first', async () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.controller.submit('enter')
    h.controller.onSttFinal(final('s1', ''))
    h.speech('started', 'say')
    await h.sayFinished() // the request returned before `ended` came over the socket
    expect(h.state.mode).toBe('speaking')
    h.speech('ended', 'say')
    expect(h.state.mode).toBe('listening')
  })

  it('a notice the engine cannot say still moves on', async () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.controller.submit('enter')
    h.controller.onSttFinal(final('s1', ''))
    await h.sayFinished(false)
    expect(h.state.mode).toBe('listening')
  })

  it('no answer: a note, said aloud, then listening again', async () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.controller.submit('enter')
    h.advance(TURN_START_TIMEOUT_MS)
    expect(h.state.notices.at(-1)).toMatchObject({ level: 'warn', text: expect.stringMatching(/no answer/i) })
    expect(h.said).toEqual(['Sorry, no answer came back. Try again.'])
    await h.sayFinished()
    expect(h.state.mode).toBe('listening')
  })

  it('a started turn that never ends (the engine failed) is given up on after a silence, not left "working"', async () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.silence()
    h.controller.onSttFinal(final('s1', 'read this'))
    h.controller.onTurn(turn('t1', 's1', 'spoken'))
    h.advance(TURN_SILENT_TIMEOUT_MS - 1000)
    h.controller.onDelta({ type: 'assistant.delta', turn_id: 't1', text: 'Working on it' }) // still alive
    h.advance(TURN_SILENT_TIMEOUT_MS - 1000)
    expect(h.state.mode).toBe('waiting')
    h.advance(1000)
    expect(h.said).toEqual(['Sorry, no answer came back. Try again.'])
    await h.sayFinished()
    expect(h.state.mode).toBe('listening')
  })

  it('a budget alert while the bar is open is said aloud; during a reply it waits for the reply to end', async () => {
    const h = harness()
    h.open(true)
    h.spokenTurn('t1', { speaks: true })
    h.speech('started', 'reply', 't1')
    h.controller.onUsageAlert({ type: 'usage.alert', level: 'warn', spent: '4', budget: '5', message: 'You have used 80% of your budget.' })
    expect(h.said).toEqual([])
    h.speech('ended', 'reply', 't1')
    expect(h.said).toEqual(['You have used 80% of your budget.'])
    expect(h.state.mode).toBe('speaking')
    await h.sayFinished()
    expect(h.state.mode).toBe('listening')
    expect(h.state.notices.map((n) => n.level)).toEqual(['warn'])
  })

  it('usage alerts map stop → error and warn → warn (and are only said while the bar is open)', () => {
    const h = harness()
    h.controller.onUsageAlert({ type: 'usage.alert', level: 'warn', spent: '4', budget: '5', message: 'Close to budget' })
    h.controller.onUsageAlert({ type: 'usage.alert', level: 'stop', spent: '5', budget: '5', message: 'Budget reached' })
    expect(h.state.notices.map((n) => n.level)).toEqual(['warn', 'error'])
    expect(h.said).toEqual([])
  })
})

describe('first run: no questions', () => {
  it('listens at once; after 4 s with no speech it says once how to talk to it, then listens for a follow-up', async () => {
    const h = harness()
    h.open(true, false, undefined, { firstRun: true })
    expect(h.state.mode).toBe('listening')
    h.advance(FIRST_RUN_NUDGE_MS - 1)
    expect(h.said).toEqual([])
    h.advance(1)
    expect(h.said).toEqual(["Hi, I'm listening. Tell me what to read, like 'read my clipboard'. Press Option-Space any time to call me."])
    expect(h.state.mode).toBe('speaking')
    // What is said is shown too, as a line from the assistant.
    expect(h.state.turns.at(-1)).toMatchObject({ source: 'button', reply: `${h.said[0]} Voices are AI-generated.`, done: true })
    expect(h.state.selected).toBe(`reply:${h.state.turns.at(-1)?.turnId}`)
    h.speech('started', 'say')
    h.speech('ended', 'say')
    await h.sayFinished()
    expect(h.state.mode).toBe('listening')
    h.advance(FOLLOW_UP_MS)
    expect(h.state.mode).toBe('idle')
    expect(h.said).toHaveLength(1)
  })

  it('says on screen from the first frame that voices are AI-generated', () => {
    const h = harness()
    h.open(true, false, undefined, { firstRun: true })
    expect(h.state.turns).toEqual([expect.objectContaining({ source: 'button', reply: 'Voices are AI-generated.' })])
    const later = harness()
    later.open(true)
    expect(later.state.turns).toEqual([])
  })

  it('no nudge when the user speaks or types first', () => {
    const spoke = harness()
    spoke.open(true, false, undefined, { firstRun: true })
    spoke.speak()
    spoke.advance(FIRST_RUN_NUDGE_MS)
    expect(spoke.said).toEqual([])
    const typed = harness()
    typed.open(true, false, undefined, { firstRun: true })
    typed.controller.keystroke()
    typed.advance(FIRST_RUN_NUDGE_MS)
    expect(typed.said).toEqual([])
  })

  it('onboarding is completed after the first finished turn', () => {
    const h = harness()
    h.open(true, false, undefined, { firstRun: true })
    h.spokenTurn('t1', { speaks: false })
    expect(h.onboarded).toBe(1)
    h.spokenTurn('t2', { speaks: false })
    expect(h.onboarded).toBe(1)
  })

  it('…or when the bar closes', () => {
    const h = harness()
    h.open(true, false, undefined, { firstRun: true })
    h.controller.close()
    expect(h.onboarded).toBe(1)
    const later = harness()
    later.open(true)
    later.controller.close()
    expect(later.onboarded).toBe(0)
  })
})

describe('submitCommand', () => {
  it('sends the trimmed text as a typed command and waits for the turn', () => {
    const h = harness()
    h.open(false, false, 'doc-9')
    expect(h.controller.submitCommand('  read this page  ')).toBe(true)
    expect(h.ofType('command')).toEqual([
      {
        type: 'command',
        text: 'read this page',
        source: 'typed',
        session_id: 's1',
        context: { document_id: 'doc-9', conversation_id: 'conv' },
        attachments: [],
      },
    ])
    // Reading pauses before the command arrives.
    expect(h.sent.map((m) => m.type)).toEqual(['bar.state', 'command'])
    expect(h.state).toMatchObject({ mode: 'waiting', sessionId: 's1', pending: { text: 'read this page', attachments: [] } })
  })

  it('sends attachments with the command, or on their own ("read these")', () => {
    const h = harness()
    h.open(false)
    const files = [{ kind: 'file' as const, path: '/a/report.pdf', name: 'report.pdf' }]
    expect(h.controller.submitCommand('', files, { shown: [{ kind: 'file', title: 'report.pdf' }] })).toBe(true)
    expect(h.ofType('command')[0]).toMatchObject({ text: '', attachments: files })
    expect(h.state.pending).toEqual({ text: '', attachments: [{ kind: 'file', title: 'report.pdf' }] })
  })

  it('a toolbar press is sent as a button command and shows no "you said" line', () => {
    const h = harness()
    h.open(false)
    expect(h.controller.submitCommand('show voices', [], { source: 'button' })).toBe(true)
    expect(h.ofType('command')[0]).toMatchObject({ text: 'show voices', source: 'button' })
    expect(h.state.pending).toBeNull()
  })

  it('refuses a command with nothing in it; a second command while one is in flight is allowed', () => {
    const h = harness()
    h.open(false)
    expect(h.controller.submitCommand('   ')).toBe(false)
    expect(h.controller.submitCommand('one')).toBe(true)
    expect(h.controller.submitCommand('two')).toBe(true)
    expect(h.ofType('command').map((c) => c.text)).toEqual(['one', 'two'])
  })

  it('stops the mic (without submitting audio) when a suggestion is chosen while listening', () => {
    const h = harness()
    h.open(true)
    h.controller.submitCommand('How much have I spent?')
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false, attachments: [] }])
    expect(h.ofType('command')[0]).toMatchObject({ session_id: 's2' })
  })

  it('shows an error notice when not connected, and does not pause reading for nothing', () => {
    const h = harness()
    h.open(false)
    h.setConnected(false)
    expect(h.controller.submitCommand('hello')).toBe(false)
    expect(h.state.notices.at(-1)).toMatchObject({ level: 'error' })
    h.setConnected(true)
    h.controller.connected()
    expect(h.barStates()).toEqual([])
  })
})

describe('engine events', () => {
  it('other errors add an error notice and end a waiting turn', () => {
    const h = harness()
    h.open(false)
    h.controller.submitCommand('hello')
    h.controller.onError({ type: 'error', problem: { code: 'provider_error', title: 'Upstream failed' } })
    expect(h.state.notices).toEqual([expect.objectContaining({ level: 'error', text: 'Upstream failed' })])
    expect(h.state.pending).toBeNull()
    h.advance(TURN_START_TIMEOUT_MS)
    expect(h.state.notices).toHaveLength(1)
  })

  it('keeps at most five notices', () => {
    const h = harness()
    for (let i = 0; i < 8; i++) h.controller.onError({ type: 'error', problem: { detail: `e${i}` } })
    expect(h.state.notices.map((n) => n.text)).toEqual(['e3', 'e4', 'e5', 'e6', 'e7'])
  })

  it('a typed turn: assistant.turn → waiting, deltas build the reply, done announces it', () => {
    const h = harness()
    h.open(false)
    h.controller.submitCommand('hello')
    h.controller.onTurn(turn('t1', 's1', 'typed', 'hello'))
    expect(h.state).toMatchObject({ mode: 'waiting', pending: null, selected: 'reply:t1' })
    h.controller.onDelta({ type: 'assistant.delta', turn_id: 't1', text: 'Hi ' })
    h.controller.onDelta({ type: 'assistant.delta', turn_id: 't1', text: 'there' })
    h.controller.onDone(done('t1'))
    expect(h.state.turns[0]).toMatchObject({ turnId: 't1', reply: 'Hi there', done: true })
    expect(h.state.announcement?.text).toBe('Hi there')
  })

  it('assistant.action selects the action and announces how to confirm', () => {
    const h = harness()
    h.controller.onAction(action('t1', 'a1', 'Skip page footers'))
    expect(h.state.selected).toBe('action:a1')
    expect(h.state.actions.a1).toMatchObject({ actionId: 'a1', turnId: 't1', status: 'pending', summary: 'Skip page footers' })
    expect(h.state.turns).toEqual([expect.objectContaining({ turnId: 't1', actionIds: ['a1'] })])
    expect(h.state.announcement?.text).toMatch(/Enter confirms, Escape cancels/)
    expect(h.controller.latestPendingAction()?.actionId).toBe('a1')
  })

  it('action_resolved updates the status and message', () => {
    const h = harness()
    h.controller.onAction(action('t1', 'a1'))
    h.controller.onActionResolved({ type: 'assistant.action_resolved', action_id: 'a1', status: 'confirmed', message: 'Done.' })
    expect(h.state.actions.a1).toMatchObject({ status: 'confirmed', message: 'Done.' })
    expect(h.controller.latestPendingAction()).toBeNull()
  })

  it('done carries an error into the turn', () => {
    const h = harness()
    h.controller.onTurn(turn('t1', null, 'typed'))
    h.controller.onDone(done('t1', { error: { detail: 'Budget reached.' } }))
    expect(h.state.turns[0]).toMatchObject({ done: true, error: 'Budget reached.' })
  })

  it('a card joins its turn; the same list shown again replaces itself', () => {
    const h = harness()
    h.controller.onCard({ type: 'assistant.card', turn_id: 't1', kind: 'voices', title: 'Voices', rows: [{ id: 'a', title: 'A' }] })
    h.controller.onCard({ type: 'assistant.card', turn_id: 't1', kind: 'voices', title: 'Voices', rows: [{ id: 'b', title: 'B' }] })
    expect(h.state.turns[0]?.cards).toHaveLength(1)
    expect(h.state.turns[0]?.cards[0]).toMatchObject({ id: 't1:voices', kind: 'voices', rows: [{ id: 'b', title: 'B' }], data: {} })
  })

  it('a preview pressed while idle shows the voice on the orb, and listens for nothing afterwards', () => {
    const h = harness()
    h.open(false)
    h.speech('started', 'preview', null)
    expect(h.state).toMatchObject({ mode: 'idle', engineSpeaking: true })
    h.speech('ended', 'preview', null)
    expect(h.state).toMatchObject({ mode: 'idle', engineSpeaking: false })
    expect(h.ofType('mic.start')).toHaveLength(0)
  })
})

describe('wireBarEvents', () => {
  it('routes WS events to the controller until unsubscribed', () => {
    const h = harness()
    const socket = new FakeSocket()
    const off = wireBarEvents(socket, h.controller)
    h.open(true)
    socket.emit({ type: 'stt.partial', session_id: 's1', text: 'hello' } as ServerMessage)
    expect(h.state.transcript).toBe('hello')
    socket.emit({ type: 'assistant.speech', state: 'started', kind: 'say' } as ServerMessage)
    expect(h.state.mode).toBe('speaking')
    off()
    socket.emit({ type: 'stt.partial', session_id: 's1', text: 'ignored' } as ServerMessage)
    expect(h.state.transcript).toBe('')
  })
})

describe('the mic level stays out of the store', () => {
  it('a breathing mic changes nothing in the store (nothing re-renders for it)', () => {
    const h = harness()
    h.open()
    let notifications = 0
    const off = h.store.subscribe(() => notifications++)
    for (let i = 0; i < 50; i++) h.mic.feed(i % 2 ? 0.0004 : 0.0008, 96)
    off()
    expect(notifications).toBe(0)
    expect(h.state.mode).toBe('listening')
  })

  it('delivers the level to subscribers at most once per frame, latest value only', () => {
    const frames: Array<() => void> = []
    const store = createBarStore({ canListen: true })
    const mic = new FakeMic()
    const controller = new BarController(store, {
      send: () => true,
      sendAudio: () => true,
      mic,
      setListening: () => undefined,
      frame: (cb) => frames.push(cb),
      endOfSpeechMs: () => 800,
      newSessionId: () => 's1',
      newConversationId: () => 'c1',
      now: () => 0,
    })
    const levels: number[] = []
    const off = controller.subscribeLevel((l) => levels.push(l))
    expect(levels).toEqual([0])
    controller.open({ listen: true, viaHotkey: false })
    frames.splice(0).forEach((f) => f())
    levels.length = 0
    mic.feed(0.0004, 32)
    mic.feed(0.002, 32)
    mic.feed(0.01, 32)
    expect(frames).toHaveLength(1)
    frames.splice(0).forEach((f) => f())
    expect(levels).toHaveLength(1)
    expect(levels[0]).toBeGreaterThan(0.3)
    controller.toggle()
    frames.splice(0).forEach((f) => f())
    expect(levels.at(-1)).toBe(0)
    off()
  })

  it('a turn for what was just sent takes over from the pending row in one store update', () => {
    const h = harness()
    h.open(false)
    h.controller.submitCommand('pause')
    const sessionId = h.state.sessionId
    let notifications = 0
    const off = h.store.subscribe(() => notifications++)
    h.controller.onTurn(turn('t1', sessionId, 'typed', 'pause'))
    off()
    expect(notifications).toBe(1)
    expect(h.state).toMatchObject({ mode: 'waiting', pending: null, selected: 'reply:t1' })
  })
})
