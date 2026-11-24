/**
 * Cloning a voice from the bar (09 § Voices, 08 § Voice cloning): the passage card is confirmed (record button or
 * "ready"), the conversation microphone closes and the reference capture records once the engine is quiet, the take
 * ends on silence and is uploaded with the conversation's session, and the engine's turn ("Keep it?") takes over.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { CloneUpload } from '@shared/api/client'
import type { AssistantAction, ClientMessage } from '@shared/api/ws.gen'

import type { RecorderHandlers, ReferenceRecorder, ReferenceTake } from '@/audio/reference'
import { FakeMic } from '../../../test/fakes'
import { BarActions } from './actions'
import { CAPTURE_NO_SPEECH_MS, BarController, CLONE_SILENT_TIMEOUT_MS, TURN_START_TIMEOUT_MS, type BarDeps } from './controller'
import { createBarStore } from './store'

const LOUD = 0.2
const QUIET = 0.0001
const flush = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve()
}

class FakeRecorder implements ReferenceRecorder {
  handlers: RecorderHandlers | null = null
  starts = 0
  cancels = 0
  stops = 0
  get recording(): boolean {
    return this.handlers !== null
  }
  async start(handlers: RecorderHandlers): Promise<void> {
    this.starts++
    this.handlers = handlers
  }
  async stop(): Promise<ReferenceTake | null> {
    this.stops++
    this.handlers = null
    return { wav: new Blob([new Uint8Array(44)], { type: 'audio/wav' }), seconds: 9, sampleRate: 48000 }
  }
  cancel(): void {
    if (this.handlers) this.cancels++
    this.handlers = null
  }
  feed(level: number, ms: number): void {
    for (let t = 0; t < ms; t += 32) this.handlers?.onLevel(level, 32)
  }
}

const passageCard = (actionId = 'c1', turnId = 't1'): AssistantAction => ({
  type: 'assistant.action',
  turn_id: turnId,
  action_id: actionId,
  kind: 'clone_record',
  summary: 'Clone your voice',
  spoken: 'Read the passage on the screen aloud.',
  details: { passage: 'I like to start my mornings slowly.', passage_id: 'en-1', language: 'en', name: 'My voice', consent: 'This is my voice…' },
})

function harness() {
  const store = createBarStore({ canListen: true })
  const mic = new FakeMic()
  const recorder = new FakeRecorder()
  const sent: ClientMessage[] = []
  const uploads: Array<{ wav: Blob; fields: CloneUpload }> = []
  const said: string[] = []
  let seq = 0
  let uploadResult: Promise<{ job_id: string; slug: string }> = Promise.resolve({ job_id: 'job_1', slug: 'my-voice' })
  const deps: BarDeps = {
    send: (m) => {
      sent.push(m)
      return true
    },
    sendAudio: () => true,
    mic,
    setListening: () => undefined,
    endOfSpeechMs: () => 800,
    newSessionId: () => `s${++seq}`,
    newConversationId: () => 'conv',
    now: () => Date.now(),
    say: (text) => {
      said.push(text)
      return Promise.resolve()
    },
    recorder,
    uploadClone: (wav, fields) => {
      uploads.push({ wav, fields })
      return uploadResult
    },
  }
  const controller = new BarController(store, deps)
  return {
    store,
    mic,
    recorder,
    sent,
    uploads,
    said,
    controller,
    get state() {
      return store.getState()
    },
    setUpload(p: Promise<{ job_id: string; slug: string }>) {
      uploadResult = p
    },
    ofType<T extends ClientMessage['type']>(type: T) {
      return sent.filter((m): m is Extract<ClientMessage, { type: T }> => m.type === type)
    },
    /** The bar is open and listening, with the passage card on screen. */
    withCard() {
      controller.open({ listen: true, viaHotkey: false })
      controller.onTurn({ type: 'assistant.turn', turn_id: 't1', session_id: 'x', source: 'spoken', text: 'clone my voice' })
      controller.onAction(passageCard())
      controller.onDone({ type: 'assistant.done', turn_id: 't1', text: 'Read the passage…', speaks: false })
    },
    read() {
      recorder.feed(QUIET, 300)
      recorder.feed(LOUD, 3000)
      recorder.feed(QUIET, 1700) // past the end-of-speech silence
    },
  }
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('recording a voice to clone', () => {
  it('the record button: the conversation microphone closes, the reference capture opens, reading stays paused', () => {
    const h = harness()
    h.withCard()
    expect(h.mic.live).toBe(true)
    h.controller.onActionResolved({ type: 'assistant.action_resolved', action_id: 'c1', status: 'confirmed', message: 'Go ahead.' })
    expect(h.mic.live).toBe(false)
    expect(h.mic.closes).toBe(1)
    expect(h.recorder.starts).toBe(1)
    expect(h.state.mode).toBe('recording')
    expect(h.state.capture).toMatchObject({ actionId: 'c1', phase: 'recording', passageId: 'en-1' })
    expect(h.controller.gate).toBe('drop') // nothing goes to speech recognition meanwhile
    expect(h.ofType('bar.state').at(-1)?.open).toBe(true)
    expect(h.ofType('mic.stop').at(-1)).toMatchObject({ submit: false })
  })

  it('"ready" said: records only after "Go ahead." has been heard', () => {
    const h = harness()
    h.withCard()
    // The user said "ready": a turn is in flight; the engine confirms the card while it answers.
    h.controller.submitCommand('ready')
    const session = h.state.sessionId!
    h.controller.onTurn({ type: 'assistant.turn', turn_id: 't2', session_id: session, source: 'typed', text: 'ready' })
    h.controller.onActionResolved({ type: 'assistant.action_resolved', action_id: 'c1', status: 'confirmed', message: 'Go ahead.' })
    expect(h.recorder.starts).toBe(0)
    expect(h.state.capture?.phase).toBe('armed')
    h.controller.onDone({ type: 'assistant.done', turn_id: 't2', text: 'Go ahead.', speaks: true })
    h.controller.onSpeech({ type: 'assistant.speech', state: 'started', kind: 'reply', turn_id: 't2' })
    expect(h.recorder.starts).toBe(0)
    h.controller.onSpeech({ type: 'assistant.speech', state: 'ended', kind: 'reply', turn_id: 't2', reason: 'finished' })
    expect(h.recorder.starts).toBe(1)
    expect(h.state.mode).toBe('recording')
  })

  it('pressing record over the instructions cuts them first', () => {
    const h = harness()
    h.withCard()
    h.controller.onSpeech({ type: 'assistant.speech', state: 'started', kind: 'reply', turn_id: 't1' })
    h.controller.armCapture('c1')
    expect(h.ofType('speech.stop')).toHaveLength(1)
    expect(h.recorder.starts).toBe(0)
    h.controller.onSpeech({ type: 'assistant.speech', state: 'ended', kind: 'reply', turn_id: 't1', reason: 'stopped' })
    expect(h.recorder.starts).toBe(1)
  })

  it('silence after the reading ends the take; it is uploaded with this conversation, unkept, with consent', async () => {
    const h = harness()
    h.withCard()
    h.controller.armCapture('c1')
    h.read()
    await flush()
    expect(h.recorder.stops).toBe(1)
    expect(h.uploads).toHaveLength(1)
    const { fields } = h.uploads[0]!
    expect(fields).toEqual({
      name: 'My voice',
      language: 'en',
      passageId: 'en-1',
      consent: true,
      source: 'recording',
      keep: false,
      sessionId: h.state.sessionId,
      conversationId: 'conv',
    })
    expect(h.state.mode).toBe('waiting')
    expect(h.state.capture).toMatchObject({ phase: 'processing', jobId: 'job_1' })
    expect(h.state.pending?.text).toBe('Making your voice…')
    // The engine's turn for that session takes over: its sample, then "Keep it?".
    h.controller.onTurn({ type: 'assistant.turn', turn_id: 't3', session_id: fields.sessionId!, source: 'button', text: '' })
    expect(h.state.capture).toBeNull()
    expect(h.state.pending).toBeNull()
    h.controller.onAction({ type: 'assistant.action', turn_id: 't3', action_id: 'k1', kind: 'keep_clone', summary: 'Keep the voice My voice', spoken: 'Keep it?', details: { via_turn: true, recording_id: 'rec_1' } })
    h.controller.onDone({ type: 'assistant.done', turn_id: 't3', text: 'This is how I sound with your voice. Keep it?', speaks: true })
    h.controller.onSpeech({ type: 'assistant.speech', state: 'started', kind: 'reply', turn_id: 't3' })
    h.controller.onSpeech({ type: 'assistant.speech', state: 'ended', kind: 'reply', turn_id: 't3', reason: 'finished' })
    // Back to listening for "yes" or "no", with the conversation microphone again.
    expect(h.state.mode).toBe('listening')
    expect(h.mic.live).toBe(true)
  })

  it('processing progress keeps the bar waiting past the usual turn timeout', async () => {
    let release: (v: { job_id: string; slug: string }) => void = () => undefined
    const h = harness()
    h.setUpload(new Promise((r) => (release = r)))
    h.withCard()
    h.controller.armCapture('c1')
    h.read()
    await flush()
    release({ job_id: 'job_9', slug: 'my-voice' })
    await flush()
    for (let i = 0; i < 4; i++) {
      vi.advanceTimersByTime(TURN_START_TIMEOUT_MS - 1000)
      h.controller.onJobProgress({ type: 'job.progress', job_id: 'job_9', pct: 20 * (i + 1), label: 'Downloading the cloning model' })
    }
    expect(h.state.mode).toBe('waiting')
    expect(h.state.notices).toHaveLength(0)
    vi.advanceTimersByTime(CLONE_SILENT_TIMEOUT_MS + 10)
    expect(h.state.notices.at(-1)?.text).toBe('No answer. Try again.')
  })

  it('nothing heard: the take is dropped, the bar says so, and the card offers to record again', async () => {
    const h = harness()
    h.withCard()
    h.controller.armCapture('c1')
    h.recorder.feed(QUIET, 500)
    vi.advanceTimersByTime(CAPTURE_NO_SPEECH_MS + 10)
    await flush()
    expect(h.uploads).toHaveLength(0)
    expect(h.recorder.cancels).toBe(1)
    expect(h.state.capture).toMatchObject({ phase: 'failed', error: "I didn't hear the passage. Press record to try again." })
    expect(h.said.at(-1)).toBe("I didn't hear the passage. Press record to try again.")
    // Record again: straight to the capture (the card was confirmed already).
    h.controller.armCapture('c1')
    expect(h.recorder.starts).toBe(2)
  })

  it('a rejected upload is said and leaves the card ready to record again', async () => {
    const h = harness()
    h.setUpload(Promise.reject(new Error('Cloning needs a confirmation that this is your voice.')))
    h.withCard()
    h.controller.armCapture('c1')
    h.read()
    await flush()
    expect(h.state.capture?.phase).toBe('failed')
    expect(h.said.at(-1)).toBe('Cloning needs a confirmation that this is your voice.')
  })

  it('Esc throws the take away; the orb sends it now; closing the bar stops it', async () => {
    const h = harness()
    h.withCard()
    h.controller.armCapture('c1')
    h.controller.interrupt('idle')
    expect(h.recorder.cancels).toBe(1)
    expect(h.state.mode).toBe('idle')
    expect(h.uploads).toHaveLength(0)
    h.controller.armCapture('c1')
    h.recorder.feed(QUIET, 300)
    h.recorder.feed(LOUD, 2000)
    h.controller.toggle() // the orb: done reading
    await flush()
    expect(h.uploads).toHaveLength(1)
    h.controller.armCapture('c1')
    h.controller.close()
    expect(h.state.capture).toBeNull()
  })

  it('the engine starting to talk stops the take (it would record the speakers)', () => {
    const h = harness()
    h.withCard()
    h.controller.armCapture('c1')
    h.controller.onSpeech({ type: 'assistant.speech', state: 'started', kind: 'say', turn_id: null })
    expect(h.recorder.cancels).toBe(1)
    expect(h.state.capture?.phase).toBe('failed')
  })
})

describe('the clone cards from the bar\'s buttons', () => {
  function actions(h: ReturnType<typeof harness>) {
    const engine = { client: null } as unknown as ConstructorParameters<typeof BarActions>[0]
    const bridge = { capabilities: {} } as unknown as ConstructorParameters<typeof BarActions>[2]
    return new BarActions(engine, h.controller, bridge)
  }

  it('"Clone a voice" on the voices card sends its command', async () => {
    const h = harness()
    h.controller.open({ listen: false, viaHotkey: false })
    const a = actions(h)
    const card = { id: 't1:voices', turnId: 't1', kind: 'voices' as const, title: 'Voices', rows: [], data: {} }
    await a.runCardRow(card, { id: 'voices:clone', title: 'Clone a voice', selected: false, data: { command: 'clone my voice', icon: 'clone' } }, 'primary')
    expect(h.ofType('command').at(-1)).toMatchObject({ text: 'clone my voice', source: 'button' })
  })

  it('"Keep it?" answered with the button is a turn, so the answer is spoken', async () => {
    const h = harness()
    h.controller.open({ listen: false, viaHotkey: false })
    h.controller.onAction({ type: 'assistant.action', turn_id: 't1', action_id: 'k1', kind: 'keep_clone', summary: 'Keep', details: { via_turn: true } })
    const a = actions(h)
    await a.confirmAction('k1')
    expect(h.ofType('command').at(-1)).toMatchObject({ text: 'yes', source: 'button' })
    expect(h.state.actions.k1?.status).toBe('confirming')
    h.controller.onAction({ type: 'assistant.action', turn_id: 't2', action_id: 'k2', kind: 'keep_clone', summary: 'Keep', details: { via_turn: true } })
    await a.cancelAction('k2')
    expect(h.ofType('command').at(-1)).toMatchObject({ text: 'no', source: 'button' })
  })

  it('the trash button on your voice asks the engine (a confirmation card)', () => {
    const h = harness()
    h.controller.open({ listen: false, viaHotkey: false })
    actions(h).deleteVoice({ id: 'qwen3:clone:my-voice', title: 'My voice', selected: false, data: { delete_command: 'delete the voice My voice' } })
    expect(h.ofType('command').at(-1)).toMatchObject({ text: 'delete the voice My voice', source: 'button' })
  })
})
