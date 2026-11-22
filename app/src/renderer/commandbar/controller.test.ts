import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  AssistantAction,
  AssistantDone,
  AssistantTurn,
  ClientMessage,
  ServerMessage,
  SttFinal,
  SttPartial,
} from '@shared/api/ws.gen'

import { FakeMic, FakeSocket } from '../../../test/fakes'
import {
  BarController,
  IDLE_SUGGESTIONS_MS,
  MIC_IDLE_OFF_MS,
  PTT_MIN_HOLD_MS,
  TURN_START_TIMEOUT_MS,
  wireBarEvents,
  type BarDeps,
} from './controller'
import { createBarStore, type BarState } from './store'

type Msg<T extends ClientMessage['type']> = Extract<ClientMessage, { type: T }>

const LOUD = 0.2 // ≈ -14 dBFS
const QUIET = 0.0001 // -80 dBFS

function harness(initial: Partial<BarState> = {}) {
  // Most tests run with speech input known to be set up; capability-unknown is tested explicitly.
  const store = createBarStore({ canListen: true, ...initial })
  const mic = new FakeMic()
  const sent: ClientMessage[] = []
  const audio: Int16Array[] = []
  const listening: boolean[] = []
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
      audio.push(pcm)
      return true
    },
    mic,
    setListening: (on) => {
      listening.push(on)
    },
    endOfSpeechMs: () => 800,
    newSessionId: () => `s${++seq}`,
    now: () => now,
  }
  const controller = new BarController(store, deps)
  return {
    store,
    mic,
    sent,
    audio,
    listening,
    controller,
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
    open(listen = true, viaHotkey = false, documentId?: string) {
      controller.open({ listen, viaHotkey, ...(documentId ? { documentId } : {}) })
    },
    /** Speech long enough for the energy VAD to fire speech-start (but not speech-end). */
    speak() {
      mic.feed(LOUD, 320)
    },
    /** End-of-speech silence (800 ms). */
    silence() {
      mic.feed(QUIET, 800)
    },
  }
}

const partial = (session_id: string, text: string): SttPartial => ({ type: 'stt.partial', session_id, text })
const final = (session_id: string, text: string): SttFinal => ({ type: 'stt.final', session_id, text })
const turn = (turn_id: string, session_id: string | null, source: 'typed' | 'spoken', text = 'hi'): AssistantTurn => ({
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

describe('BarController.open / close', () => {
  it('never opens the mic before it knows speech input is set up, then listens once it is', () => {
    const h = harness({ canListen: null })
    h.controller.open({ listen: true, viaHotkey: true })
    expect(h.store.getState().mode).toBe('typing')
    expect(h.sent.some((m) => m.type === 'mic.start')).toBe(false)
    h.controller.setCanListen(true)
    expect(h.store.getState().mode).toBe('listening')
    expect(h.sent.some((m) => m.type === 'mic.start')).toBe(true)
  })

  it('stays typing when the engine says speech input is not set up', () => {
    const h = harness({ canListen: null })
    h.controller.open({ listen: true, viaHotkey: true })
    h.controller.setCanListen(false)
    expect(h.store.getState().mode).toBe('typing')
    expect(h.sent.some((m) => m.type === 'mic.start')).toBe(false)
  })

  it('opens listening: bar.state open, then mic.start with the document context', () => {
    const h = harness()
    h.open(true, false, 'doc-1')

    expect(h.sent[0]).toEqual({ type: 'bar.state', open: true })
    const starts = h.ofType('mic.start')
    expect(starts).toHaveLength(1)
    expect(starts[0]).toMatchObject({ session_id: 's1', mode: 'auto', context: { document_id: 'doc-1' } })
    expect(h.state).toMatchObject({ open: true, mode: 'listening', sessionId: 's1', context: { documentId: 'doc-1' } })
    expect(h.mic.active).toBe(true)
    expect(h.listening.at(-1)).toBe(true)
  })

  it('opens typing without mic.start when no speech-to-text is set up (canListen: false)', () => {
    const h = harness({ canListen: false })
    h.open(true)

    expect(h.ofType('bar.state')).toEqual([{ type: 'bar.state', open: true }])
    expect(h.ofType('mic.start')).toHaveLength(0)
    expect(h.mic.starts).toBe(0)
    expect(h.state.mode).toBe('typing')
  })

  it('opens typing when listen is false', () => {
    const h = harness()
    h.open(false)
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.start')).toHaveLength(0)
  })

  it('waits in typing mode while the engine is not connected, then listens once it connects', () => {
    const h = harness()
    h.setConnected(false)
    h.open(true)
    expect(h.state.mode).toBe('typing')
    expect(h.state.hint).toMatch(/connecting/i)
    expect(h.mic.starts).toBe(0)

    h.setConnected(true)
    h.controller.connected()
    expect(h.state).toMatchObject({ mode: 'listening', hint: null })
    expect(h.ofType('mic.start')).toHaveLength(1)
  })

  it('does not listen on connect if the user started typing meanwhile', () => {
    const h = harness()
    h.setConnected(false)
    h.open(true)
    h.controller.setText('read')
    h.setConnected(true)
    h.controller.connected()
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.start')).toHaveLength(0)
  })

  // Suspected bug (controller.ts close()): close() does not clear listenWhenConnected, so a bar re-opened
  // for typing starts the mic when the engine connects later.
  it('a bar re-opened for typing does not start listening when the engine connects', () => {
    const h = harness()
    h.setConnected(false)
    h.open(true)
    h.controller.close()
    h.open(false)
    h.setConnected(true)
    h.controller.connected()
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.start')).toHaveLength(0)
  })

  it('re-opening while open in typing mode with an empty input starts listening', () => {
    const h = harness()
    h.open(false)
    h.open(true)
    expect(h.state.mode).toBe('listening')
    expect(h.ofType('mic.start')).toHaveLength(1)
  })

  // Suspected bug (controller.ts open(), already-open branch): it ignores canListen, so a tray/reader re-open
  // with listen:true sends mic.start even though no speech-to-text provider is configured.
  it('re-opening while open does not start the mic when canListen is false', () => {
    const h = harness({ canListen: false })
    h.open(true)
    h.open(true)
    expect(h.ofType('mic.start')).toHaveLength(0)
  })

  it('close stops the mic without submitting and resets the bar', () => {
    const h = harness()
    h.open(true)
    h.controller.close()

    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false }])
    expect(h.sent.at(-1)).toEqual({ type: 'bar.state', open: false })
    expect(h.mic.active).toBe(false)
    expect(h.listening.at(-1)).toBe(false)
    expect(h.state).toMatchObject({ open: false, mode: 'typing', text: '' })
  })

  it('close is a no-op when already closed', () => {
    const h = harness()
    h.controller.close()
    expect(h.sent).toHaveLength(0)
  })

  it('shows idle suggestions after IDLE_SUGGESTIONS_MS without speech or text', () => {
    const h = harness()
    h.open(true)
    h.advance(IDLE_SUGGESTIONS_MS - 1)
    expect(h.state.idle).toBe(false)
    h.advance(1)
    expect(h.state.idle).toBe(true)
  })
})

describe('BarController listening → typing', () => {
  it('any keystroke while listening sends mic.stop {submit:false} and switches to typing', () => {
    const h = harness()
    h.open(true)
    h.controller.onSttPartial(partial('s1', 'read the'))
    h.controller.keystroke()

    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false }])
    expect(h.state.mode).toBe('typing')
    // The transcript stays editable.
    expect(h.state.text).toBe('read the')
    expect(h.mic.active).toBe(false)
  })

  it('keystroke while typing does nothing', () => {
    const h = harness()
    h.open(false)
    const before = h.sent.length
    h.controller.keystroke()
    expect(h.sent).toHaveLength(before)
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

  it('Tab toggles typing ↔ listening', () => {
    const h = harness()
    h.open(false)
    h.controller.toggle()
    expect(h.state.mode).toBe('listening')
    h.controller.toggle()
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false }])
  })

  it('partials replace the input in place; stale-session partials are ignored', () => {
    const h = harness()
    h.open(true)
    h.controller.onSttPartial(partial('s1', 'read'))
    h.controller.onSttPartial(partial('s1', 'read this'))
    h.controller.onSttPartial(partial('other', 'nope'))
    expect(h.state.text).toBe('read this')
  })

  it('a mic error drops to typing with the error message', () => {
    const h = harness()
    h.open(true)
    h.mic.handlers!.onError(new Error('Microphone permission denied'))
    expect(h.state.mode).toBe('typing')
    expect(h.state.micError).toBe('Microphone permission denied')
    expect(h.ofType('mic.stop')[0]).toMatchObject({ submit: false })
  })

  it('turns the mic off after MIC_IDLE_OFF_MS without speech', () => {
    const h = harness()
    h.open(true)
    h.advance(MIC_IDLE_OFF_MS - 1)
    expect(h.state.mode).toBe('listening')
    h.advance(1)
    expect(h.state.mode).toBe('typing')
    expect(h.state.hint).toMatch(/microphone off/i)
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false }])
    expect(h.mic.active).toBe(false)
  })

  it('keeps the mic on past MIC_IDLE_OFF_MS once speech started', () => {
    const h = harness()
    h.open(true)
    h.speak()
    expect(h.state.speaking).toBe(true)
    h.advance(MIC_IDLE_OFF_MS * 2)
    expect(h.state.mode).toBe('listening')
  })

  it('keeps the mic on past MIC_IDLE_OFF_MS once a partial arrived', () => {
    const h = harness()
    h.open(true)
    h.controller.onSttPartial(partial('s1', 'hello'))
    h.advance(MIC_IDLE_OFF_MS * 2)
    expect(h.state.mode).toBe('listening')
  })
})

describe('BarController.submit: each utterance submits at most once', () => {
  it('enter after speech sends mic.stop {submit:true} once; vad and ptt then do nothing', () => {
    const h = harness()
    h.open(true)
    h.speak()

    expect(h.controller.submit('enter')).toBe(true)
    expect(h.controller.submit('vad')).toBe(false)
    expect(h.controller.submit('ptt')).toBe(false)
    expect(h.controller.submit('enter')).toBe(false)

    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: true }])
    expect(h.state.mode).toBe('finalizing')
  })

  it('end-of-speech silence (energy VAD) submits once by itself', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.silence()

    expect(h.state.mode).toBe('finalizing')
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: true }])
    expect(h.controller.submit('enter')).toBe(false)
    expect(h.ofType('mic.stop')).toHaveLength(1)
  })

  it('a partial transcript counts as heard even if the VAD saw no speech', () => {
    const h = harness()
    h.open(true)
    h.controller.onSttPartial(partial('s1', 'read this'))
    expect(h.controller.submit('enter')).toBe(true)
    expect(h.ofType('mic.stop')[0]).toMatchObject({ submit: true })
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

    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: true }])
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
    expect(h.ofType('mic.stop')).toHaveLength(0)
  })

  it('push-to-talk released without speech cancels quietly', () => {
    const h = harness()
    h.open(true, true)
    h.tick(PTT_MIN_HOLD_MS + 50)
    h.controller.hotkeyReleased()
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false }])
  })

  it('hotkey release is ignored when the bar was not opened by the hotkey', () => {
    const h = harness()
    h.open(true, false)
    h.tick(PTT_MIN_HOLD_MS + 50)
    h.speak()
    h.controller.hotkeyReleased()
    expect(h.state.mode).toBe('listening')
  })

  it('stt.final while listening means the engine submitted: no mic.stop is sent', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.controller.onSttFinal(final('s1', 'read this'))

    expect(h.state).toMatchObject({ mode: 'finalizing', text: 'read this' })
    expect(h.ofType('mic.stop')).toHaveLength(0)
    expect(h.mic.active).toBe(false)
    expect(h.controller.submit('enter')).toBe(false)
    expect(h.state.announcement?.text).toBe('You said: read this')
  })

  it('an empty stt.final returns to typing with a hint and no timeout warning', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.controller.submit('enter')
    h.controller.onSttFinal(final('s1', '   '))

    expect(h.state.mode).toBe('typing')
    expect(h.state.text).toBe('')
    expect(h.state.hint).toMatch(/didn't catch that/i)
    h.advance(TURN_START_TIMEOUT_MS)
    expect(h.state.notices).toHaveLength(0)
  })

  it('warns and returns to typing when the engine never starts the turn', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.controller.submit('enter')
    h.advance(TURN_START_TIMEOUT_MS)
    expect(h.state.mode).toBe('typing')
    expect(h.state.notices.at(-1)).toMatchObject({ level: 'warn', text: expect.stringMatching(/did not answer/) })
  })

  it('an interceptor gets the final transcript (not a partial), sends no command, then listens again', () => {
    const h = harness()
    const seen: string[] = []
    h.controller.setInterceptor((t) => {
      seen.push(t)
      return 'listen-again'
    })
    h.open(true)
    expect(h.ofType('mic.start')[0]).toMatchObject({ transcribe_only: true })
    h.speak()
    h.controller.onSttPartial(partial('s1', 'tw'))
    expect(h.controller.submit('enter')).toBe(true)
    expect(seen).toEqual([])

    h.controller.onSttFinal(final('s1', 'number two'))
    expect(seen).toEqual(['number two'])
    expect(h.ofType('command')).toHaveLength(0)
    expect(h.state).toMatchObject({ mode: 'listening', sessionId: 's2' })
    expect(h.ofType('mic.start').map((m) => m.transcribe_only)).toEqual([true, true])
  })

  it("an interceptor returning 'stop' leaves the bar typing", () => {
    const h = harness()
    h.controller.setInterceptor(() => 'stop')
    h.open(true)
    h.speak()
    h.silence()
    h.controller.onSttFinal(final('s1', 'skip setup'))
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.start')).toHaveLength(1)
    // No stray turn-timeout warning after a locally handled answer.
    h.advance(TURN_START_TIMEOUT_MS)
    expect(h.state.notices).toHaveLength(0)
  })

  it('without an interceptor the mic session is a normal assistant turn', () => {
    const h = harness()
    h.open(true)
    expect(h.ofType('mic.start')[0]).toMatchObject({ transcribe_only: false })
  })
})

describe('BarController.submitText', () => {
  it('sends the trimmed text as a typed command and waits for the turn', () => {
    const h = harness()
    h.open(false, false, 'doc-9')
    expect(h.controller.submitText('  read this page  ')).toBe(true)

    expect(h.ofType('command')).toEqual([
      { type: 'command', text: 'read this page', source: 'typed', session_id: 's1', context: { document_id: 'doc-9' } },
    ])
    expect(h.state).toMatchObject({ mode: 'waiting', sessionId: 's1', text: '', pendingText: 'read this page' })
  })

  it('Enter in typing mode submits the input text', () => {
    const h = harness()
    h.open(false)
    h.controller.setText('show voices')
    expect(h.controller.submit('enter')).toBe(true)
    expect(h.ofType('command')[0]).toMatchObject({ text: 'show voices' })
  })

  it('refuses empty text and a second submit while waiting', () => {
    const h = harness()
    h.open(false)
    expect(h.controller.submitText('   ')).toBe(false)
    expect(h.controller.submitText('one')).toBe(true)
    expect(h.controller.submitText('two')).toBe(false)
    expect(h.ofType('command')).toHaveLength(1)
  })

  it('stops the mic (without submitting audio) when a suggestion is chosen while listening', () => {
    const h = harness()
    h.open(true)
    h.controller.submitText('How much have I spent?')
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false }])
    expect(h.ofType('command')[0]).toMatchObject({ session_id: 's2' })
  })

  it('shows an error notice when not connected', () => {
    const h = harness()
    h.open(false)
    h.setConnected(false)
    expect(h.controller.submitText('hello')).toBe(false)
    expect(h.state.notices.at(-1)).toMatchObject({ level: 'error' })
    expect(h.state.mode).toBe('typing')
  })
})

describe('BarController engine events', () => {
  it('missing_key while listening switches to typing, sets canListen:false, and adds no notice', () => {
    const h = harness()
    h.open(true)
    h.controller.onError({ type: 'error', problem: { code: 'missing_key', detail: 'Add an OpenAI or Gemini key to talk.' } })

    expect(h.state).toMatchObject({ mode: 'typing', canListen: false })
    expect(h.state.notices).toHaveLength(0)
    expect(h.state.announcement?.text).toBe('Add an OpenAI or Gemini key to talk.')
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false }])
  })

  it('other errors add an error notice and end a waiting turn', () => {
    const h = harness()
    h.open(false)
    h.controller.submitText('hello')
    h.controller.onError({ type: 'error', problem: { code: 'provider_error', title: 'Upstream failed' } })

    expect(h.state.notices).toEqual([expect.objectContaining({ level: 'error', text: 'Upstream failed' })])
    expect(h.state).toMatchObject({ mode: 'typing', pendingText: null })
    // The turn timeout was cleared.
    h.advance(TURN_START_TIMEOUT_MS)
    expect(h.state.notices).toHaveLength(1)
  })

  it('keeps at most five notices', () => {
    const h = harness()
    for (let i = 0; i < 8; i++) h.controller.onError({ type: 'error', problem: { detail: `e${i}` } })
    expect(h.state.notices.map((n) => n.text)).toEqual(['e3', 'e4', 'e5', 'e6', 'e7'])
  })

  it('a typed turn: assistant.turn → waiting, done → typing', () => {
    const h = harness()
    h.open(false)
    h.controller.submitText('hello')
    h.controller.onTurn(turn('t1', 's1', 'typed', 'hello'))
    expect(h.state).toMatchObject({ mode: 'waiting', pendingText: null, selected: 'reply:t1' })
    h.controller.onDelta({ type: 'assistant.delta', turn_id: 't1', text: 'Hi ' })
    h.controller.onDelta({ type: 'assistant.delta', turn_id: 't1', text: 'there' })
    h.controller.onDone(done('t1'))

    expect(h.state.turns[0]).toMatchObject({ turnId: 't1', reply: 'Hi there', done: true })
    expect(h.state.mode).toBe('typing')
    expect(h.state.announcement?.text).toBe('Hi there')
  })

  it('assistant.action selects the action and announces how to confirm', () => {
    const h = harness()
    h.controller.onAction(action('t1', 'a1', 'Skip page footers'))

    expect(h.state.selected).toBe('action:a1')
    expect(h.state.actions.a1).toMatchObject({ actionId: 'a1', turnId: 't1', status: 'pending', summary: 'Skip page footers' })
    expect(h.state.turns).toEqual([expect.objectContaining({ turnId: 't1', actionIds: ['a1'] })])
    expect(h.state.announcement?.text).toMatch(/Press Enter to confirm/)
    expect(h.controller.latestPendingAction()?.actionId).toBe('a1')
  })

  it('action_resolved updates the status and message', () => {
    const h = harness()
    h.controller.onAction(action('t1', 'a1'))
    h.controller.onActionResolved({ type: 'assistant.action_resolved', action_id: 'a1', status: 'confirmed', message: 'Done.' })
    expect(h.state.actions.a1).toMatchObject({ status: 'confirmed', message: 'Done.' })
    expect(h.controller.latestPendingAction()).toBeNull()
  })

  it('done for a spoken turn with a pending action listens again for yes/no', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.silence()
    h.controller.onTurn(turn('t1', 's1', 'spoken', 'skip the footers'))
    h.controller.onAction(action('t1', 'a1'))
    h.controller.onDone(done('t1', { text: 'Skip footers?' }))

    expect(h.state.mode).toBe('listening')
    expect(h.ofType('mic.start').map((m) => m.session_id)).toEqual(['s1', 's2'])
    expect(h.mic.active).toBe(true)
  })

  it('done for a spoken turn without a pending action returns to typing', () => {
    const h = harness()
    h.open(true)
    h.speak()
    h.silence()
    h.controller.onTurn(turn('t1', 's1', 'spoken'))
    h.controller.onDone(done('t1', { text: 'OK' }))
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.start')).toHaveLength(1)
  })

  it('done for a typed turn with a pending action returns to typing (Enter confirms)', () => {
    const h = harness()
    h.open(false)
    h.controller.submitText('skip footers')
    h.controller.onTurn(turn('t1', 's1', 'typed'))
    h.controller.onAction(action('t1', 'a1'))
    h.controller.onDone(done('t1'))
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.start')).toHaveLength(0)
  })

  it('done carries an error into the turn', () => {
    const h = harness()
    h.controller.onTurn(turn('t1', null, 'typed'))
    h.controller.onDone(done('t1', { error: { detail: 'Budget reached.' } }))
    expect(h.state.turns[0]).toMatchObject({ done: true, error: 'Budget reached.' })
  })

  it('a keys card opens key entry', () => {
    const h = harness()
    h.controller.onCard({ type: 'assistant.card', turn_id: 't1', kind: 'keys', title: 'Keys' })
    expect(h.state.keyEntry).toBe(true)
    expect(h.state.turns[0]?.cards[0]).toMatchObject({ id: 't1:0', kind: 'keys', rows: [], data: {} })
  })

  it('usage alerts map stop → error and warn → warn', () => {
    const h = harness()
    h.controller.onUsageAlert({ type: 'usage.alert', level: 'warn', spent: '4', budget: '5', message: 'Close to budget' })
    h.controller.onUsageAlert({ type: 'usage.alert', level: 'stop', spent: '5', budget: '5', message: 'Budget reached' })
    expect(h.state.notices.map((n) => n.level)).toEqual(['warn', 'error'])
  })
})

describe('BarController.withMicPaused', () => {
  function deferred() {
    let resolve!: () => void
    let reject!: (e: Error) => void
    const promise = new Promise<void>((res, rej) => {
      resolve = res
      reject = rej
    })
    return { promise, resolve, reject }
  }

  it('stops the mic while speaking and resumes listening afterwards', async () => {
    const h = harness()
    h.open(true)
    const d = deferred()
    const p = h.controller.withMicPaused(() => d.promise)

    expect(h.state.mode).toBe('typing')
    expect(h.state.prompting).toBe(true)
    expect(h.mic.active).toBe(false)
    expect(h.ofType('mic.stop')).toEqual([{ type: 'mic.stop', session_id: 's1', submit: false }])

    d.resolve()
    await p
    expect(h.state.prompting).toBe(false)
    expect(h.state).toMatchObject({ mode: 'listening', sessionId: 's2' })
    expect(h.mic.active).toBe(true)
  })

  it('does not start listening afterwards if it was typing', async () => {
    const h = harness()
    h.open(false)
    await h.controller.withMicPaused(async () => {})
    expect(h.state.mode).toBe('typing')
    expect(h.ofType('mic.start')).toHaveLength(0)
  })

  it('resumes listening even if speaking fails', async () => {
    const h = harness()
    h.open(true)
    const d = deferred()
    const p = h.controller.withMicPaused(() => d.promise)
    d.reject(new Error('say failed'))
    await p
    expect(h.state.mode).toBe('listening')
    expect(h.state.prompting).toBe(false)
  })

  it('does not resume if the user started typing or closed the bar meanwhile', async () => {
    const typed = harness()
    typed.open(true)
    await typed.controller.withMicPaused(async () => {
      typed.controller.setText('read')
    })
    expect(typed.state.mode).toBe('typing')

    const closed = harness()
    closed.open(true)
    await closed.controller.withMicPaused(async () => {
      closed.controller.close()
    })
    expect(closed.state.open).toBe(false)
    expect(closed.ofType('mic.start')).toHaveLength(1)
  })
})

describe('wireBarEvents', () => {
  it('routes WS events to the controller until unsubscribed', () => {
    const h = harness()
    const socket = new FakeSocket()
    const off = wireBarEvents(socket, h.controller)
    h.open(true)
    socket.emit({ type: 'stt.partial', session_id: 's1', text: 'hello' } as ServerMessage)
    expect(h.state.text).toBe('hello')
    off()
    socket.emit({ type: 'stt.partial', session_id: 's1', text: 'ignored' } as ServerMessage)
    expect(h.state.text).toBe('hello')
  })
})
